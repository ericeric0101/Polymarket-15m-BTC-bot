"""FR1 data download: Hyperliquid vs Binance perps, funding + 4h candles (public APIs, read-only).

    .venv/bin/python scripts/fr1_download.py

Steps (prereg reports/hl_binance_funding_20261017/prereg.md section 2):
1. Coins listed on both venues (HL perp universe x Binance USDT-M PERPETUAL, TRADING).
   HL "kXXX" maps to Binance "1000XXXUSDT" (both quoted per 1000 units).
2. 4h candles for every common coin, both venues, whole period.
3. Coverage filter (< 2% missing candles) and rank by HL dev-period mean daily notional; top 30.
4. Funding history for the top 30, both venues.
Output: data/fr1/{universe.json, candles/<coin>_{hl,bn}.json, funding/<coin>_{hl,bn}.json, selected.json}
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import requests

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "fr1"
HL = "https://api.hyperliquid.xyz/info"
BN = "https://fapi.binance.com"
START = datetime(2025, 10, 6, tzinfo=timezone.utc)      # one spare week before the 2025-10-13 data start
END = datetime(2026, 10, 12, tzinfo=timezone.utc)
DEV_START = datetime(2025, 10, 13, tzinfo=timezone.utc)
DEV_END = datetime(2026, 4, 13, tzinfo=timezone.utc)
TOP_N = 30
MAX_MISSING = 0.02
H4 = 4 * 3600 * 1000

session = requests.Session()


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def hl_post(body: dict, tries: int = 5):
    for i in range(tries):
        r = session.post(HL, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        time.sleep(0.25)
        return r.json()
    raise RuntimeError(f"HL rate limited: {body.get('type')}")


def bn_get(path: str, params: dict, tries: int = 5):
    for i in range(tries):
        r = session.get(BN + path, params=params, timeout=30)
        if r.status_code in (418, 429):
            time.sleep(5 * (i + 1))
            continue
        r.raise_for_status()
        time.sleep(0.1)
        return r.json()
    raise RuntimeError(f"Binance rate limited: {path}")


def universe() -> list[dict]:
    hl_coins = [u["name"] for u in hl_post({"type": "meta"})["universe"] if not u.get("isDelisted")]
    info = bn_get("/fapi/v1/exchangeInfo", {})
    bn = {s["symbol"] for s in info["symbols"]
          if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
    out = []
    for c in hl_coins:
        sym = f"1000{c[1:]}USDT" if c.startswith("k") and c[1:].isupper() else f"{c}USDT"
        if sym in bn:
            out.append({"coin": c, "bn_symbol": sym})
    return out


def hl_candles(coin: str) -> list:
    rows, t = [], ms(START)
    while t < ms(END):
        page = hl_post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "4h",
                                                         "startTime": t, "endTime": ms(END)}})
        if not page:
            break
        rows += page
        t = page[-1]["t"] + H4
        if len(page) < 100:
            break
    return [[r["t"], float(r["c"]), float(r["v"])] for r in rows if r["t"] < ms(END)]


def bn_candles(sym: str) -> list:
    rows, t = [], ms(START)
    while t < ms(END):
        page = bn_get("/fapi/v1/klines", {"symbol": sym, "interval": "4h", "startTime": t,
                                          "endTime": ms(END) - 1, "limit": 1500})
        if not page:
            break
        rows += page
        t = page[-1][0] + H4
        if len(page) < 1500:
            break
    return [[r[0], float(r[4]), float(r[7])] for r in rows]     # open time, close, quote volume


def hl_funding(coin: str) -> list:
    rows, t = [], ms(START)
    while t < ms(END):
        page = hl_post({"type": "fundingHistory", "coin": coin, "startTime": t, "endTime": ms(END)})
        if not page:
            break
        rows += page
        t = page[-1]["time"] + 1
        if len(page) < 500:
            break
    return [[r["time"], float(r["fundingRate"])] for r in rows if r["time"] < ms(END)]


def bn_funding(sym: str) -> list:
    rows, t = [], ms(START)
    while t < ms(END):
        page = bn_get("/fapi/v1/fundingRate", {"symbol": sym, "startTime": t, "endTime": ms(END) - 1, "limit": 1000})
        if not page:
            break
        rows += page
        t = page[-1]["fundingTime"] + 1
        if len(page) < 1000:
            break
    return [[r["fundingTime"], float(r["fundingRate"])] for r in rows]


def dump(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def main() -> int:
    uni = universe()
    dump(OUT / "universe.json", uni)
    print(f"common coins: {len(uni)}")
    expected = (ms(END) - ms(DEV_START)) // H4
    ranked = []
    for i, u in enumerate(uni):
        c, sym = u["coin"], u["bn_symbol"]
        hp, bp = OUT / "candles" / f"{c}_hl.json", OUT / "candles" / f"{c}_bn.json"
        if not (hp.exists() and bp.exists()):
            dump(hp, hl_candles(c))
            dump(bp, bn_candles(sym))
        h, b = json.loads(hp.read_text()), json.loads(bp.read_text())
        hs = {r[0] for r in h if r[0] >= ms(DEV_START)}
        bs = {r[0] for r in b if r[0] >= ms(DEV_START)}
        miss = max(1 - len(hs) / expected, 1 - len(bs) / expected)
        dev_ntl = sum(r[1] * r[2] for r in h if ms(DEV_START) <= r[0] < ms(DEV_END))
        days = (DEV_END - DEV_START).days
        ranked.append({**u, "missing": round(miss, 4), "hl_dev_daily_ntl": dev_ntl / days})
        if (i + 1) % 25 == 0:
            print(f"  candles {i + 1}/{len(uni)}")
    ok = [r for r in ranked if r["missing"] < MAX_MISSING]
    ok.sort(key=lambda r: -r["hl_dev_daily_ntl"])
    sel = ok[:TOP_N]
    dump(OUT / "ranked.json", ranked)
    dump(OUT / "selected.json", sel)
    print(f"coverage ok: {len(ok)}/{len(ranked)}; selected top {len(sel)}: {[r['coin'] for r in sel]}")
    for r in sel:
        c, sym = r["coin"], r["bn_symbol"]
        dump(OUT / "funding" / f"{c}_hl.json", hl_funding(c))
        dump(OUT / "funding" / f"{c}_bn.json", bn_funding(sym))
        print(f"  funding {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

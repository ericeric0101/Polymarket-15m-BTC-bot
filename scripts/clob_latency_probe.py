"""Measure Polymarket CLOB order round-trip latency from this machine (speed-path feasibility).

Runs by CK in their own terminal (real account, near-zero risk):

    CLOB_PROBE_CONFIRM=REAL_ORDERS .venv/bin/python -I scripts/clob_latency_probe.py --confirm --minutes 60

Every --interval seconds, on the NEXT BTC 5m market (not trading yet, so nobody sells at 0.01):
  1. FAK  BUY 5 @ 0.01  -> cannot match (asks are far above 0.01) and never rests on the book.
     Measures the taker path: sign + POST /order + matching-engine reply.
  2. post-only GTC BUY 5 @ 0.01, then cancel immediately. Measures POST and DELETE separately.
     Worst case (someone sells 5 @ 0.01 in the < 1 s it rests): 0.05 USDC.
  3. Unauthenticated GET /time and GET /book: plain network RTT baseline.
Results: data/clob_latency_probe/probe_<start>.jsonl and a summary printed at the end (Ctrl-C ends early).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bot.research.price_recorder import market_tokens_for, stream_slugs  # noqa: E402
from bot.research.twap_z_live import LiveOrderClient  # noqa: E402

CONFIRM_ENV, CONFIRM_VALUE = "CLOB_PROBE_CONFIRM", "REAL_ORDERS"
PRICE, SIZE = 0.01, 5.0


def ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def next_btc5m_token() -> str | None:
    from price_recorder import fetch_gamma_market
    slug = stream_slugs(time.time(), (("btc", 300),), ahead=1)[1]          # next 5m market
    meta = market_tokens_for([slug], fetch_gamma_market)
    up = [t for t, m in meta.items() if m["outcome"] == "Up"]
    return up[0] if up else None


def probe_once(c: LiveOrderClient, token: str) -> dict:
    from py_clob_client_v2.clob_types import OrderPayload, OrderType
    cl = c.client
    r: dict = {"ts": time.time(), "token": token}
    t = time.perf_counter(); cl.get_server_time(); r["get_time_ms"] = ms(t)
    t = time.perf_counter(); book = cl.get_order_book(token); r["get_book_ms"] = ms(t)
    asks = (book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)) or []
    r["best_ask"] = min((float(a["price"] if isinstance(a, dict) else a.price) for a in asks), default=None)
    if r["best_ask"] is not None and r["best_ask"] <= PRICE + 0.005:
        r["skipped"] = "ask_too_low"
        return r
    # 1. FAK that cannot match
    t = time.perf_counter(); signed = cl.create_order(c._args(token, PRICE, SIZE)); r["sign_ms"] = ms(t)
    t = time.perf_counter()
    try:
        res = cl.post_order(signed, order_type=OrderType.FAK)
        r["fak_post_ms"] = ms(t)
        r["fak_status"] = (res or {}).get("status") if isinstance(res, dict) else str(res)[:80]
        r["fak_error"] = (res or {}).get("errorMsg") if isinstance(res, dict) else None
        r["fak_taking"] = (res or {}).get("takingAmount") if isinstance(res, dict) else None
    except Exception as exc:
        r["fak_post_ms"] = ms(t); r["fak_exception"] = f"{type(exc).__name__}: {exc}"[:200]
    # 2. post-only resting order, cancelled immediately
    signed = cl.create_order(c._args(token, PRICE, SIZE))
    t = time.perf_counter()
    try:
        res = cl.post_order(signed, order_type=OrderType.GTC, post_only=True)
        r["gtc_post_ms"] = ms(t)
        oid = (res or {}).get("orderID") if isinstance(res, dict) else None
        r["gtc_status"] = (res or {}).get("status") if isinstance(res, dict) else None
        if oid:
            t = time.perf_counter()
            cres = cl.cancel_order(OrderPayload(orderID=oid))
            r["cancel_ms"] = ms(t)
            r["cancel_ok"] = bool(cres and oid in (cres.get("canceled") or []))
    except Exception as exc:
        r["gtc_post_ms"] = ms(t); r["gtc_exception"] = f"{type(exc).__name__}: {exc}"[:200]
    return r


def summarize(rows: list[dict]) -> dict:
    out = {"probes": len(rows), "skipped": sum(1 for r in rows if r.get("skipped"))}
    for k in ("get_time_ms", "get_book_ms", "sign_ms", "fak_post_ms", "gtc_post_ms", "cancel_ms"):
        v = sorted(r[k] for r in rows if isinstance(r.get(k), (int, float)))
        if v:
            q = lambda p: v[min(len(v) - 1, int(p * len(v)))]
            out[k] = {"n": len(v), "min": v[0], "p50": statistics.median(v), "p90": q(.9), "p99": q(.99), "max": v[-1]}
    out["fak_statuses"] = sorted({str(r.get("fak_status") or r.get("fak_error") or r.get("fak_exception")) for r in rows
                                  if not r.get("skipped")})
    out["fills"] = [r for r in rows if r.get("fak_taking") not in (None, "0", "", 0)]
    out["cancel_failures"] = sum(1 for r in rows if "cancel_ms" in r and not r.get("cancel_ok"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args(argv)
    if not a.confirm or os.environ.get(CONFIRM_ENV) != CONFIRM_VALUE:
        print(f"refused: pass --confirm and set {CONFIRM_ENV}={CONFIRM_VALUE}")
        return 2
    from dotenv import dotenv_values
    env = {**{k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}, **os.environ}
    c = LiveOrderClient.from_env(env)
    print(f"connected; collateral balance={c.collateral_balance()} USDC")
    out_dir = REPO_ROOT / "data" / "clob_latency_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_{int(time.time())}.jsonl"
    rows, end, token, token_ts = [], time.time() + a.minutes * 60, None, 0.0
    try:
        while time.time() < end:
            if token is None or time.time() - token_ts > 240:
                token, token_ts = next_btc5m_token(), time.time()
            if token:
                r = probe_once(c, token)
                rows.append(r)
                with open(path, "a") as fh:
                    fh.write(json.dumps(r, default=str) + "\n")
                print(f"{time.strftime('%H:%M:%S')} time={r.get('get_time_ms')}ms book={r.get('get_book_ms')}ms "
                      f"sign={r.get('sign_ms')}ms FAK={r.get('fak_post_ms')}ms[{r.get('fak_status') or r.get('fak_error') or r.get('fak_exception') or r.get('skipped')}] "
                      f"GTC={r.get('gtc_post_ms')}ms cancel={r.get('cancel_ms')}ms ok={r.get('cancel_ok')}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    s = summarize(rows)
    (out_dir / (path.stem + "_summary.json")).write_text(json.dumps(s, indent=1, default=str))
    print(json.dumps(s, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FR1 analysis (reports/hl_binance_funding_20261017/prereg.md).

    .venv/bin/python scripts/fr1_analyze.py dev           # theta grid on dev weeks 1-25, writes theta_choice.json
    .venv/bin/python scripts/fr1_analyze.py validation    # needs theta_choice.json; weeks 26-51

Accounting is per unit notional. Position d = +1: short HL / long Binance (HL funding higher),
d = -1 the reverse. Weekly P&L per coin = d * (HL funding sum - Binance funding sum)
- d * change in basis (HL - BN) / BN - trading costs when d changes.
Annualized return on notional = total P&L / unit-weeks deployed * 52.18.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = REPO / "data" / "fr1"
REPORT_DIR = REPO / "reports" / "hl_binance_funding_20261017"
CHOICE = REPORT_DIR / "theta_choice.json"

M0 = datetime(2025, 10, 13, tzinfo=timezone.utc)
N_WEEKS = 52                       # week k = [M0 + 7k, M0 + 7(k+1)), k = 0..51
DEV = range(1, 26)                 # trading weeks
VAL = range(26, 52)
THETAS = (0.05, 0.10, 0.20, 0.40)  # annualized
WEEKS_PER_YEAR = 365.25 / 7
FEES = {"taker": (0.00045, 0.0005), "maker": (0.00015, 0.0002)}   # (HL, Binance) per leg per side
SLIP = 0.0002
LEVERAGE = 3.0
N_BOOT = 10_000
BLOCK = 4
SEED = 20261017


def week_edges() -> list[int]:
    return [int((M0 + timedelta(weeks=k)).timestamp() * 1000) for k in range(N_WEEKS + 1)]


def load_coin(coin: str):
    """Weekly funding sums (HL, BN) and basis at each week edge; None where data is missing."""
    edges = week_edges()
    out = {}
    for venue in ("hl", "bn"):
        f = json.loads((DATA / "funding" / f"{coin}_{venue}.json").read_text())
        t = np.array([r[0] for r in f], dtype=np.int64)
        v = np.array([r[1] for r in f])
        sums, counts = [], []
        for k in range(N_WEEKS):
            m = (t >= edges[k]) & (t < edges[k + 1])
            sums.append(v[m].sum())
            counts.append(int(m.sum()))
        out[f"{venue}_sum"] = np.array(sums)
        out[f"{venue}_n"] = np.array(counts)
        c = json.loads((DATA / "candles" / f"{coin}_{venue}.json").read_text())
        closes = {r[0]: r[1] for r in c}
        # Price at a week edge = close of the 4h candle that opens at edge - 4h.
        out[f"{venue}_px"] = np.array([closes.get(e - 4 * 3600 * 1000, np.nan) for e in edges])
    out["basis"] = (out["hl_px"] - out["bn_px"]) / out["bn_px"]
    # A week is usable if both venues settled funding in it (HL hourly: expect ~168).
    out["ok"] = (out["hl_n"] >= 150) & (out["bn_n"] >= 1) & ~np.isnan(out["basis"][:-1]) & ~np.isnan(out["basis"][1:])
    return out


def simulate(coins: dict, theta: float, weeks: range, fee_mult: float = 1.0, fee_kind: str = "taker"):
    """Returns per-week arrays (pnl units, deployed units) and per-coin P&L totals."""
    fee_hl, fee_bn = FEES[fee_kind]
    leg_cost = fee_mult * (fee_hl + fee_bn + 2 * SLIP)          # one side (open or close), both legs
    pnl = np.zeros(N_WEEKS)
    units = np.zeros(N_WEEKS)
    by_coin = {}
    for coin, d in coins.items():
        prev = 0
        total = 0.0
        for k in weeks:
            sig_ok = d["ok"][k - 1] and d["ok"][k]
            s = (d["hl_sum"][k - 1] - d["bn_sum"][k - 1]) * WEEKS_PER_YEAR if sig_ok else 0.0
            pos = (1 if s > 0 else -1) if sig_ok and abs(s) >= theta else 0
            cost = 0.0
            if pos != prev:
                cost += leg_cost * ((prev != 0) + (pos != 0))
            w = cost * -1
            if pos != 0:
                fund = pos * (d["hl_sum"][k] - d["bn_sum"][k])
                basis = -pos * (d["basis"][k + 1] - d["basis"][k])
                w += fund + basis
                units[k] += 1
            pnl[k] += w
            total += w
            prev = pos
        if prev != 0:                       # close what is still open at the end of the phase
            pnl[weeks[-1]] -= leg_cost
            total -= leg_cost
        by_coin[coin] = total
    return pnl, units, by_coin


def annualized(pnl: np.ndarray, units: np.ndarray, weeks: range) -> float:
    u = units[list(weeks)].sum()
    return float(pnl[list(weeks)].sum() / u * WEEKS_PER_YEAR) if u > 0 else float("nan")


def block_bootstrap(pnl, units, weeks, n=N_BOOT, block=BLOCK, seed=SEED):
    idx = np.array(list(weeks))
    p, u = pnl[idx], units[idx]
    L = len(idx)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, L, size=(n, int(np.ceil(L / block))))
    samp = (starts[:, :, None] + np.arange(block)[None, None, :]) % L
    samp = samp.reshape(n, -1)[:, :L]
    ps, us = p[samp].sum(axis=1), u[samp].sum(axis=1)
    vals = np.where(us > 0, ps / np.maximum(us, 1e-12) * WEEKS_PER_YEAR, np.nan)
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def load_all() -> dict:
    sel = json.loads((DATA / "selected.json").read_text())
    return {r["coin"]: load_coin(r["coin"]) for r in sel}


def describe(coins, theta, weeks, label) -> list[str]:
    pnl, units, by_coin = simulate(coins, theta, weeks)
    ann = annualized(pnl, units, weeks)
    lo, hi = block_bootstrap(pnl, units, weeks)
    active = int((units[list(weeks)] > 0).sum())
    lines = [f"{label}: theta={theta:.0%}  net APR on notional {ann:+.2%} [95% CI {lo:+.2%}, {hi:+.2%}]  "
             f"on capital ({LEVERAGE:g}x) {ann * LEVERAGE / 2:+.2%}  active weeks {active}/{len(weeks)}  "
             f"unit-weeks {units[list(weeks)].sum():.0f}"]
    return lines


def main(argv=None) -> int:
    phase = (argv or sys.argv[1:] or ["dev"])[0]
    coins = load_all()
    print(f"coins: {len(coins)}  {list(coins)}")
    if phase == "dev":
        rows = []
        for th in THETAS:
            pnl, units, _ = simulate(coins, th, DEV)
            rows.append((th, annualized(pnl, units, DEV)))
            print("\n".join(describe(coins, th, DEV, "dev")))
        best = max(rows, key=lambda r: (np.nan_to_num(r[1], nan=-1e9), r[0]))
        CHOICE.write_text(json.dumps({"theta": best[0], "dev_apr_notional": best[1],
                                      "chosen_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "grid": rows}, indent=1))
        print(f"chosen theta = {best[0]:.0%} (dev APR on notional {best[1]:+.2%}); wrote {CHOICE.relative_to(REPO)}")
        return 0
    if phase != "validation":
        print("phase must be dev or validation")
        return 2
    if not CHOICE.exists():
        print("theta_choice.json missing: run the dev phase and record the choice first")
        return 2
    th = json.loads(CHOICE.read_text())["theta"]
    pnl, units, by_coin = simulate(coins, th, VAL)
    ann = annualized(pnl, units, VAL)
    lo, hi = block_bootstrap(pnl, units, VAL)
    active = int((units[list(VAL)] > 0).sum())
    cap = ann * LEVERAGE / 2
    c1, c2, c3 = ann > 0 and lo > 0, cap >= 0.10, active >= 10
    print("## Primary (validation weeks 26-51)")
    print("\n".join(describe(coins, th, VAL, "validation")))
    print(f"conditions: CI>0 {c1}  capital APR>=10% {c2}  active weeks>=10 {c3}  -> "
          f"{'PASS' if (c1 and c2 and c3) else 'FAIL: FR1 closes'}")
    print("\n## Secondary")
    for kind, mult, lab in (("maker", 1.0, "maker fees"), ("taker", 2.0, "fees+slippage x2")):
        p, u, _ = simulate(coins, th, VAL, mult, kind)
        print(f"{lab}: APR on notional {annualized(p, u, VAL):+.2%}")
    for sub in (["BTC", "ETH"],):
        cs = {c: coins[c] for c in sub if c in coins}
        p, u, _ = simulate(cs, th, VAL)
        print(f"{'+'.join(sub)} only: APR on notional {annualized(p, u, VAL):+.2%}, unit-weeks {u[list(VAL)].sum():.0f}")
    tot = sum(by_coin.values())
    top = sorted(by_coin.items(), key=lambda kv: -kv[1])[:3]
    print(f"top-3 coins share of total P&L: {sum(v for _, v in top) / tot:.0%} {top}" if tot else "total P&L 0")
    wk = pnl[list(VAL)] / np.maximum(units[list(VAL)], 1)
    print(f"worst week per unit {wk.min():+.4f}; best {wk.max():+.4f}")
    same = []
    for d in coins.values():
        sp = d["hl_sum"] - d["bn_sum"]
        for k in VAL:
            if d["ok"][k - 1] and d["ok"][k] and sp[k - 1] != 0:
                same.append(np.sign(sp[k]) == np.sign(sp[k - 1]))
    print(f"sign persistence week to week: {np.mean(same):.1%} (n={len(same)})")
    jumps = [abs(d["basis"][k + 1] - d["basis"][k]) for d in coins.values() for k in VAL
             if not np.isnan(d["basis"][k + 1] - d["basis"][k])]
    print(f"weekly |basis change|: median {np.median(jumps):.5f}  p99 {np.percentile(jumps, 99):.5f}  max {max(jumps):.5f}")
    # Single-venue carry (reference only, approximate): short HL perp, long spot, every week, no
    # signal; basis to spot is not modelled (no spot candles), costs = HL taker + 0.1% spot per side.
    carry = []
    for d in coins.values():
        fund = [d["hl_sum"][k] for k in VAL if d["ok"][k]]
        if fund:
            carry.append(np.mean(fund) * WEEKS_PER_YEAR - 2 * (FEES["taker"][0] + 0.001 + 2 * SLIP) / (len(VAL) / WEEKS_PER_YEAR))
    print(f"reference HL-perp-vs-spot carry (approx., hold whole phase): median coin APR {np.median(carry):+.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

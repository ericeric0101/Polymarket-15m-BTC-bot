"""Compare the TWAP-z DRY-RUN executor with the LIVE executor, market by market.

Both processes run the same code with independent feeds. For every market that either one
triggered, this lists: did each trigger (checkpoint, side, ask)? did the live FAK order fill,
at what price and latency? and, once settled, the live PnL vs the dry-run's hypothetical PnL
(buy shares at the dry-run ask, official outcome, fee 0.07 p (1-p)).

    .venv/bin/python -I scripts/compare_twap_z_dry_live.py [--since 2026-10-17T05:00Z] [--fetch]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

R = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(R / "scripts"))
LABELS = R / "data" / "twap_z_executor_live" / "official_labels_cache.json"


def _rows(pattern: str) -> pd.DataFrame:
    out = []
    for f in sorted(glob.glob(str(R / pattern))):
        for line in open(f):
            r = json.loads(line)
            if r.get("stream"):
                out.append(r)
    return pd.DataFrame(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO UTC, e.g. 2026-10-17T05:00Z (default: live start)")
    ap.add_argument("--fetch", action="store_true", help="fetch official outcomes for compared markets")
    ap.add_argument("--shares", type=float, default=5.5)
    a = ap.parse_args(argv)

    live_orders = _rows("data/twap_z_executor_live/orders_*.jsonl")
    live_dec = _rows("data/twap_z_executor_live/20*.jsonl")
    dry = _rows("data/twap_z_executor/20*.jsonl")
    if live_dec.empty:
        print("no live decision rows yet (data/twap_z_executor_live/)")
        return 0
    since = (datetime.fromisoformat(a.since.replace("Z", "+00:00")).timestamp() if a.since
             else float(live_dec.eval_ts.min()))
    trig = lambda df: (df[(df.eval_ts >= since) & (df.action == "WOULD_SUBMIT")]
                       .sort_values("eval_ts").drop_duplicates("market_slug").set_index("market_slug"))
    D, L = trig(dry), trig(live_dec)
    # Markets both processes were running for: the dry-run must have evaluated them too.
    seen_dry = set(dry[dry.eval_ts >= since].market_slug)
    m = pd.DataFrame(index=sorted((set(D.index) | set(L.index)) & seen_dry))
    for tag, src in (("dry", D), ("live", L)):
        m[f"{tag}_cp"], m[f"{tag}_side"], m[f"{tag}_ask"] = src.checkpoint, src.side, src.ask
    if not live_orders.empty:
        o = live_orders[live_orders.live_action == "ORDER"].drop_duplicates("market_slug").set_index("market_slug")
        m["order_status"] = o.order.map(lambda x: x.get("status"))
        m["order_error"] = o.order.map(lambda x: x.get("error"))
        m["filled_shares"] = o.order.map(lambda x: x.get("filled_shares"))
        m["filled_usdc"] = o.order.map(lambda x: x.get("filled_usdc"))
        m["decision_to_post_ms"] = o.order.map(lambda x: x.get("decision_to_post_ms"))
        b = live_orders[live_orders.live_action == "BLOCKED"].drop_duplicates("market_slug").set_index("market_slug")
        m["blocked_reason"] = b.live_reason
    m["fill_price"] = m.get("filled_usdc") / m.get("filled_shares") if "filled_usdc" in m else None

    cache = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    if a.fetch:
        import httpx
        from fetch_official_resolutions import fetch_one
        with httpx.Client(timeout=20) as c:
            for s in m.index:
                end = int(s.rsplit("-", 1)[-1]) + (300 if "-5m-" in s else 900)
                if cache.get(s) not in ("UP", "DOWN") and end + 120 < datetime.now(timezone.utc).timestamp():
                    cache[s] = fetch_one(c, "https://gamma-api.polymarket.com", s).get("official_outcome")
        LABELS.parent.mkdir(parents=True, exist_ok=True)
        LABELS.write_text(json.dumps(cache, indent=1))
    m["official"] = m.index.map(lambda s: cache.get(s))
    fee = lambda p: .07 * p * (1 - p)
    def pnl(side, ask):
        win = (m[side] == "up") == (m.official == "UP")
        return (win.astype(float) - m[ask] - fee(m[ask])) * a.shares
    known = m.official.isin(["UP", "DOWN"])
    m.loc[known & m.dry_cp.notna(), "dry_pnl"] = pnl("dry_side", "dry_ask")
    if "filled_shares" in m:
        win = (m.live_side == "up") == (m.official == "UP")
        m.loc[known & (m.filled_shares.fillna(0) > 0), "live_pnl"] = win.astype(float) * m.filled_shares - m.filled_usdc

    pd.set_option("display.width", 220)
    print(m.drop(columns=[c for c in ("order_error",) if c in m and m[c].isna().all()]).to_string())
    both = m.dry_cp.notna() & m.live_cp.notna()
    print(f"\nmarkets: {len(m)} | both triggered: {int(both.sum())} | dry only: {int((m.dry_cp.notna() & m.live_cp.isna()).sum())}"
          f" | live only: {int((m.live_cp.notna() & m.dry_cp.isna()).sum())}")
    if "filled_shares" in m:
        sub = m[m.live_cp.notna() & m.order_status.notna()]
        filled = sub.filled_shares.fillna(0) > 0
        print(f"live orders: {len(sub)} | filled: {int(filled.sum())} ({100 * filled.mean():.0f}%) | "
              f"median decision->post {sub.decision_to_post_ms.median():.0f} ms | "
              f"mean fill price - decision ask: {(m.fill_price - m.live_ask)[filled.reindex(m.index, fill_value=False)].mean():+.4f}")
    if "live_pnl" in m:
        print(f"settled: dry hypothetical PnL {m.dry_pnl.sum():+.3f} USDC (n={int(m.dry_pnl.notna().sum())}) | "
              f"live realised PnL {m.live_pnl.sum():+.3f} USDC (n={int(m.live_pnl.notna().sum())})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

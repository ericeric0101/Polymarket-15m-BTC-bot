"""Replay recorder Chainlink data through the live executor core in receipt order and compare,
at every plan_v2 checkpoint, with recorder_mechanics.ChainlinkSeries (the shadow v2 evaluator).

    .venv/bin/python reports/twap_z_late_window_executor_20261016/verify_replay.py 2026-10-15
"""
import glob
import sys
from pathlib import Path

import pandas as pd

R = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(R), str(R / "reports" / "twap_z_forward_shadow_20261015")]
from recorder_mechanics import ChainlinkSeries  # noqa: E402
from bot.research.twap_z_executor import CHECKPOINTS, LiveChainlink  # noqa: E402


def main(days):
    files = sorted(f for d in days for f in glob.glob(str(R / "data" / "price_sources_1s" / d / "*.parquet")))
    p = pd.concat([pd.read_parquet(f) for f in files])
    p = p[p.source.isin(["chainlink_spot", "chainlink_twap60"])].dropna(subset=["last_source_ms"])
    p = p.sort_values("last_received_ms")
    cs = ChainlinkSeries(p, window_offset=-62)
    live = LiveChainlink()
    lo, hi = int(p.last_received_ms.min() // 1000), int(p.last_received_ms.max() // 1000)
    queries = sorted((s + 900 - h + 0.05, s, h) for s in range((lo // 900 + 1) * 900, hi - 900, 900) for h in CHECKPOINTS)
    rows = iter(p.itertuples())
    pending = next(rows, None)
    n = bad = usable = 0
    for t, start, h in queries:
        while pending is not None and pending.last_received_ms / 1000 <= t:
            live.observe("spot" if pending.source == "chainlink_spot" else "twap", pending.close,
                         pending.last_received_ms / 1000, int(pending.last_source_ms))
            pending = next(rows, None)
        end = start + 900
        k_live, k_cs = live.strike(start, t), cs.strike(start, t)
        n += 1
        ok = (k_live is None) == (k_cs is None) and (k_cs is None or abs(k_live - k_cs) < 1e-9)
        if k_cs is not None:
            a, b = live.required_future_avg(end, k_cs, t), cs.required_future_avg(end, k_cs, t)
            ok &= a[1:] == b[1:] and (a[0] is None) == (b[0] is None) and (a[0] is None or abs(a[0] - b[0]) < 1e-6)
            usable += a[0] is not None
        sa, sb = live.sigma(t), cs.sigma(t)
        ok &= (sa is None) == (sb is None) and (sa is None or abs(sa / sb - 1) < 1e-9)
        ok &= live.spot_asof(t)[0] == cs.spot_asof(t)
        bad += not ok
    print(f"files={len(files)} checkpoints={n} usable_required_avg={usable} mismatches={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["2026-10-15"]))

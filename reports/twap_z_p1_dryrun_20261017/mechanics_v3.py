"""plan_v3 section 3: validate the TWAP window offset per asset on the first 2 hours after the
population start (2026-10-16 03:00-05:00Z). Mechanics only: no outcomes, books or PnL."""
import glob, json, sys
from pathlib import Path
import pandas as pd

R = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(R / "reports" / "twap_z_forward_shadow_20261015"))
from recorder_mechanics import validate_window  # noqa: E402

T0, T1 = 1792119600, 1792119600 + 7200
files = sorted(glob.glob(str(R / "data/price_sources_1s/2026-10-16/0[2-5]*.parquet")))
p = pd.concat([pd.read_parquet(f) for f in files])
out = {"window_utc": ["2026-10-16T03:00:00Z", "2026-10-16T05:00:00Z"], "assets": {}}
for asset, (spot, twap) in {"btc": ("chainlink_spot", "chainlink_twap60"),
                            "eth": ("chainlink_spot:eth", "chainlink_twap60:eth"),
                            "sol": ("chainlink_spot:sol", "chainlink_twap60:sol"),
                            "xrp": ("chainlink_spot:xrp", "chainlink_twap60:xrp")}.items():
    q = p[p.source.isin([spot, twap]) & (p.last_source_ms // 1000).between(T0 - 120, T1)].copy()
    q["source"] = q.source.map({spot: "chainlink_spot", twap: "chainlink_twap60"})
    v = validate_window(q)
    best = v.sort_values("median_abs_bps").iloc[0]
    out["assets"][asset] = {"best_offset": int(best.window_offset), "n_windows": int(best.n),
                            "median_abs_bps": float(best.median_abs_bps), "p99_abs_bps": float(best.p99_abs_bps),
                            "max_abs_bps": float(best.max_abs_bps),
                            "second_best_median_bps": float(v.sort_values("median_abs_bps").iloc[1].median_abs_bps),
                            "included": bool(best.window_offset == -62 and best.p99_abs_bps <= 0.5)}
print(json.dumps(out, indent=1))
(R / "reports/twap_z_forward_shadow_20261015/mechanics_v3.json").write_text(json.dumps(out, indent=1))

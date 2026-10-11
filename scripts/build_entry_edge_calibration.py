"""Build the E5B-D `edge_cal_v1` calibration table (offline, read-only inputs).

Follows docs/research/e5b_d_prereg.md exactly: E0 decision points strictly before
FORWARD_CUTOFF_TS, one observation per (market, side, cell), Beta shrinkage toward
the cell mean ask (K=20), usable cells need n >= 20 and >= 3 Taipei days.

Usage:
  python -I scripts/build_entry_edge_calibration.py --e0-dir <reports/entry_research_e0_e4_20261011> \
      --out config/research/entry_edge_calibration_v1.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.entry_shadow import (  # noqa: E402
    EDGE_CAL_ASK_EDGES,
    EDGE_CAL_MIN_DAYS,
    EDGE_CAL_MIN_MARKETS,
    EDGE_CAL_SHRINK_K,
    EDGE_CAL_TTE_SPLIT_SEC,
    FORWARD_CUTOFF_TS,
    edge_cell,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e0-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    import pyarrow.parquet as pq

    e0 = Path(args.e0_dir)
    labels = {}
    for name in ("e0_labels_dev.csv", "e0_labels_holdout_SEALED.csv"):
        for row in csv.DictReader(open(e0 / name)):
            if str(row.get("resolved")).lower() in ("true", "1", "yes") and row["official_outcome"] in ("UP", "DOWN"):
                labels[row["market_slug"]] = row["official_outcome"]
    table = pq.read_table(e0 / "e0_decision_points.parquet",
                          columns=["market_slug", "t", "tte", "side", "ask", "taipei_day"]).to_pylist()
    table.sort(key=lambda r: (r["market_slug"], r["side"], r["t"]))
    first = {}
    for r in table:
        if r["t"] is None or r["t"] >= FORWARD_CUTOFF_TS or r["ask"] is None or r["tte"] is None:
            continue
        if r["market_slug"] not in labels:
            continue
        cell = edge_cell(float(r["ask"]), float(r["tte"]))
        if cell is None:
            continue
        key = (r["market_slug"], r["side"], cell)
        if key not in first:
            first[key] = r
    cells: dict[str, dict] = {}
    for (slug, side, cell), r in first.items():
        c = cells.setdefault(cell, {"n": 0, "wins": 0, "sum_ask": 0.0, "days": set()})
        c["n"] += 1
        c["wins"] += int(labels[slug] == str(side).upper())
        c["sum_ask"] += float(r["ask"])
        c["days"].add(r["taipei_day"])
    out_cells = {}
    for cell, c in sorted(cells.items()):
        mean_ask = c["sum_ask"] / c["n"]
        usable = c["n"] >= EDGE_CAL_MIN_MARKETS and len(c["days"]) >= EDGE_CAL_MIN_DAYS
        out_cells[cell] = {
            "n_markets": c["n"], "wins": c["wins"], "mean_ask": round(mean_ask, 6),
            "observed_win_rate": round(c["wins"] / c["n"], 6), "taipei_days": len(c["days"]),
            "residual_shrunk": round((c["wins"] - c["sum_ask"]) / (c["n"] + EDGE_CAL_SHRINK_K), 6),
            "usable": usable,
        }
    prereg = (ROOT / "docs/research/e5b_d_prereg.md").read_bytes()
    doc = {
        "version": "edge_cal_v1", "estimator": "ask_plus_shrunk_cell_residual", "forward_cutoff_ts": FORWARD_CUTOFF_TS,
        "ask_edges": list(EDGE_CAL_ASK_EDGES), "tte_split_sec": EDGE_CAL_TTE_SPLIT_SEC,
        "shrink_k": EDGE_CAL_SHRINK_K, "min_markets": EDGE_CAL_MIN_MARKETS, "min_days": EDGE_CAL_MIN_DAYS,
        "unit": "first grid point per (market, side, cell)", "markets_with_labels": len(labels),
        "prereg_sha256": hashlib.sha256(prereg).hexdigest(), "cells": out_cells,
    }
    Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in doc.items() if k != "cells"}, indent=1))
    for cell, c in out_cells.items():
        print(cell, c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

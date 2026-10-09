#!/usr/bin/env python3
"""Per-market outcome provenance: official > canonical TWAP > (journal: diagnostic only).

Offline. Inputs: an official-resolution cache file (explicit path), tier A exports
(canonical TWAP settlement), the trade journal (MARKET_SETTLEMENT, read-only).
Every source value and every mismatch is preserved; nothing is overwritten.

  python3 scripts/build_outcome_provenance.py --official data/research_export/official_resolution/<file>.json
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.runtime_env import load_runtime_env  # noqa: E402

FIELDS = ("market_slug", "market_start_utc_epoch", "official_outcome", "canonical_twap_outcome",
          "journal_outcome", "journal_outcome_rows", "journal_outcome_conflicting_rows", "journal_spot",
          "journal_strike", "outcome_source_used", "outcome_used", "journal_mismatch", "twap_mismatch",
          "market_resolution_confidence", "official_cache_content_sha256")


def _start_epoch(slug: str):
    try:
        return int(slug.rsplit("-", 1)[-1])
    except ValueError:
        return ""


def build(official_path: Path, export_root: Path, journal: Path) -> tuple[list[dict], dict]:
    cache = json.loads(official_path.read_text())
    official = {m["slug"]: m["official_outcome"] for m in cache["markets"]}
    canonical = {}
    for path in sorted(glob.glob(str(export_root / "A_market_summary" / "*.csv"))):
        for row in csv.DictReader(open(path)):
            if row["settlement_side"] in ("UP", "DOWN"):
                canonical[row["market_slug"]] = row["settlement_side"]
    journal_rows = defaultdict(list)
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        for (pj,) in conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='MARKET_SETTLEMENT' ORDER BY id"):
            d = json.loads(pj or "{}")
            if d.get("slug"):
                journal_rows[d["slug"]].append(d)
    finally:
        conn.close()
    rows = []
    for slug in sorted(set(official) | set(canonical) | set(journal_rows)):
        jr = [d for d in journal_rows.get(slug, []) if d.get("outcome") in ("UP", "DOWN")]
        j_out = jr[0]["outcome"] if jr else ""  # first runtime label for the market
        conflicting = len({d["outcome"] for d in jr}) > 1
        off = official.get(slug, "")
        off = off if off in ("UP", "DOWN") else ""
        can = canonical.get(slug, "")
        if off:
            used, source = off, "POLYMARKET_OFFICIAL"
            confidence = "HIGH" if not can or can == off else "HIGH_OFFICIAL_TWAP_CONFLICT"
        elif can:
            used, source, confidence = can, "CANONICAL_TWAP", "MEDIUM"
        else:
            used, source, confidence = "UNKNOWN", "NONE", "NONE"  # journal is never primary truth
        rows.append({
            "market_slug": slug, "market_start_utc_epoch": _start_epoch(slug),
            "official_outcome": off, "canonical_twap_outcome": can, "journal_outcome": j_out,
            "journal_outcome_rows": len(jr), "journal_outcome_conflicting_rows": int(conflicting),
            "journal_spot": jr[0].get("spot") if jr else "", "journal_strike": jr[0].get("strike") if jr else "",
            "outcome_source_used": source, "outcome_used": used,
            "journal_mismatch": (int(j_out != used) if j_out and used != "UNKNOWN" else ""),
            "twap_mismatch": (int(can != off) if can and off else ""),
            "market_resolution_confidence": confidence,
            "official_cache_content_sha256": cache["content_sha256"],
        })
    def rate(key):
        vals = [r[key] for r in rows if r[key] != ""]
        return {"compared": len(vals), "mismatch": sum(vals), "rate": round(sum(vals) / len(vals), 4) if vals else None}
    summary = {"official_cache": str(official_path), "official_cache_content_sha256": cache["content_sha256"],
               "official_fetched_at_utc": cache["fetched_at_utc"], "markets": len(rows),
               "outcome_source_used": dict(Counter(r["outcome_source_used"] for r in rows)),
               "confidence": dict(Counter(r["market_resolution_confidence"] for r in rows)),
               "journal_vs_used": rate("journal_mismatch"), "twap_vs_official": rate("twap_mismatch"),
               "journal_conflicting_markets": sum(r["journal_outcome_conflicting_rows"] for r in rows)}
    return rows, summary


def main() -> int:
    load_runtime_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--official", required=True)
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--journal", default=os.getenv("TRADE_DB_PATH") or "logs/trade_journal.db")
    ap.add_argument("--out-dir", default="data/research_export/outcome_provenance")
    ap.add_argument("--no-offsite", action="store_true", help="do not mirror to RESEARCH_OFFSITE_DIR (reproduction runs)")
    a = ap.parse_args()
    rows, summary = build(Path(a.official), Path(a.export), Path(a.journal))
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"market_outcomes_{summary['official_cache_content_sha256'][:12]}"
    target = out_dir / f"{stem}.csv"
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary["file"] = str(target)
    summary["file_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (out_dir / f"{stem}.summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    offsite = None if a.no_offsite else os.getenv("RESEARCH_OFFSITE_DIR")
    if offsite:
        mirror = Path(offsite).expanduser() / "outcome_provenance"
        mirror.mkdir(parents=True, exist_ok=True)
        for f in (target, out_dir / f"{stem}.summary.json"):
            shutil.copyfile(f, mirror / f.name)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Re-run the final strategy research (Stages 2-5) offline, deterministically, with a manifest.

Inputs are fixed files only (no network): the versioned official-resolution cache (verified against
its .sha256 sidecar), the trade journal (read-only), and the research export (A summaries, P paths).
Steps, in order:
  1. build_outcome_provenance.py      -> market_outcomes_<cache content hash>.csv
  2. final_research_analysis.py       -> stage2_official/ (official labels) and stage2_journal/ (journal labels)
  3. research_entry_analysis.py       -> stage3/   (LIVE and DRY-RUN reported separately)
  4. research_flip_analysis.py        -> stage4/
  5. research_stop_analysis.py        -> stage5/
The manifest records git HEAD, script hashes, input fingerprints, the covered date range per cohort,
and a result digest per output JSON with generated_at_utc removed; --compare <prior run dir> checks
that digests match a previous run (determinism check). Bootstrap seeds are fixed (11).

Inclusion rules (applied inside the scripts): outcome = official Polymarket resolution, else canonical
TWAP, else journal (outcome_source_used per market); LIVE = filled positions in the journal; DRY-RUN =
SHADOW_SIM fills; historical pre-v2 paths only when twap_recomputation_class is FRESH*.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("build_outcome_provenance.py", "final_research_analysis.py", "research_entry_analysis.py",
           "research_flip_analysis.py", "research_stop_analysis.py", "reproduce_research_iteration.py")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def result_digest(path: Path) -> str:
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "generated_at_utc"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    data = strip(json.loads(path.read_text()))
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def journal_fingerprint(journal: Path) -> dict:
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        rows, max_id = conn.execute("SELECT COUNT(*), MAX(rowid) FROM order_events").fetchone()
    finally:
        conn.close()
    stat = journal.stat()
    # A full hash of a multi-GB journal is impractical; row count + max rowid + size pin the content
    # the analyses read (order_events is append-only).
    return {"path": str(journal), "size_bytes": stat.st_size, "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "order_events_rows": rows, "order_events_max_rowid": max_id}


def verify_official_cache(cache: Path) -> str:
    sidecar = cache.with_name(cache.name + ".sha256")
    if not sidecar.exists():
        raise SystemExit(f"missing sha256 sidecar for {cache}")
    expected = sidecar.read_text().split()[0]
    actual = sha256_file(cache)
    if actual != expected:
        raise SystemExit(f"official cache hash mismatch: {actual} != {expected}")
    return actual


def run(step: list[str]) -> None:
    print("+", " ".join(step), flush=True)
    subprocess.run([sys.executable, *step], cwd=ROOT, check=True)


def date_ranges(out: Path) -> dict:
    ranges = {}
    entry = json.loads((out / "stage3" / "entry_analysis.json").read_text())
    for cohort, cov in entry["coverage"].items():
        days = cov["days"]
        ranges[f"entry_{cohort}"] = {"first_day": days[0] if days else None, "last_day": days[-1] if days else None,
                                     "n_days": len(days), "n_markets": cov["n"]}
    return ranges


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--official", required=True, help="data/research_export/official_resolution/official_resolutions_<UTC>.json")
    ap.add_argument("--journal", default="logs/trade_journal.db")
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--out", required=True)
    ap.add_argument("--compare", help="prior run directory whose result digests must match")
    a = ap.parse_args()
    out, export, journal, official = Path(a.out).resolve(), Path(a.export), Path(a.journal), Path(a.official)
    out.mkdir(parents=True, exist_ok=True)
    cache_sha = verify_official_cache(official)
    content_hash = json.loads(official.read_text()).get("content_sha256", "")
    prov_dir = out / "outcome_provenance"
    run(["scripts/build_outcome_provenance.py", "--official", str(official), "--export", str(export),
         "--journal", str(journal), "--out-dir", str(prov_dir), "--no-offsite"])
    provenance = sorted(prov_dir.glob("market_outcomes_*.csv"))[-1]
    common = ["--journal", str(journal), "--export", str(export)]
    run(["scripts/final_research_analysis.py", *common, "--provenance", str(provenance), "--labels", "official",
         "--out", str(out / "stage2_official")])
    run(["scripts/final_research_analysis.py", *common, "--provenance", str(provenance), "--labels", "journal",
         "--out", str(out / "stage2_journal")])
    sims = out / "stage2_official" / "tables" / "shadow_sim_markets.csv"
    run(["scripts/research_entry_analysis.py", *common, "--tables", str(out / "stage2_official" / "tables"),
         "--out", str(out / "stage3")])
    run(["scripts/research_flip_analysis.py", *common, "--provenance", str(provenance), "--sims", str(sims),
         "--out", str(out / "stage4")])
    run(["scripts/research_stop_analysis.py", *common, "--provenance", str(provenance), "--sims", str(sims),
         "--out", str(out / "stage5")])

    outputs = {"stage2_official": "stage2_official/results.json", "stage2_journal": "stage2_journal/results.json",
               "stage3": "stage3/entry_analysis.json", "stage4": "stage4/flip_analysis.json",
               "stage5": "stage5/stop_analysis.json"}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "scripts", "bot"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    inputs = {"official_cache": {"path": str(official), "file_sha256": cache_sha, "content_sha256": content_hash},
              "outcome_provenance_csv": {"path": str(provenance), "sha256": sha256_file(provenance)},
              "journal": journal_fingerprint(journal),
              "export_files": {str(p.relative_to(export)): sha256_file(p) for p in sorted(export.glob("A_market_summary/*.csv"))
                               + sorted(export.glob("P_paths/*/*.parquet"))}}
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "git_head": head, "code_dirty": dirty,
                "network_used": False, "bootstrap_seed": 11,
                "scripts": {s: sha256_file(ROOT / "scripts" / s) for s in SCRIPTS},
                "inputs": inputs, "date_ranges": date_ranges(out),
                "result_digests": {k: result_digest(out / v) for k, v in outputs.items()}}
    if a.compare:
        prior = Path(a.compare)
        manifest["compare"] = {k: (result_digest(prior / v) == manifest["result_digests"][k]) if (prior / v).exists() else None
                               for k, v in outputs.items()}
    (out / "reproduction_manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps({"result_digests": manifest["result_digests"], "compare": manifest.get("compare")}, indent=1))
    return 0 if not a.compare or all(v is not False for v in manifest["compare"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

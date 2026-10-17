"""S1 collector health (blind-safe: counts only, no prices, no outcomes).

    .venv/bin/python scripts/sports_odds_health.py [--hours 24]

Snapshots by kind, match rate per league, credits remaining, and error lines in
logs/sports_odds/*.log over the last N hours.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "data" / "sports_odds"
LOG_DIR = REPO_ROOT / "logs" / "sports_odds"
EXPECTED_BASELINES_PER_DAY = 12


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)

    kinds: Counter = Counter()
    pregame: dict = defaultdict(int)
    matched: dict = defaultdict(int)
    last_credit = None
    path = OUT_ROOT / "scan_log.jsonl"
    for line in (path.read_text().splitlines() if path.exists() else []):
        rec = json.loads(line)
        if datetime.fromisoformat(rec["scan_utc"]) < since:
            continue
        kinds[rec.get("kind", "baseline")] += 1
        last_credit = (rec.get("credits") or {}).get("x-requests-remaining", last_credit)
        for lg, st in rec["stats"].items():
            pregame[lg] += st["pregame"]
            matched[lg] += st["matched"]

    print(f"S1 health {now.isoformat(timespec='seconds')} (last {args.hours:g}h)")
    exp = EXPECTED_BASELINES_PER_DAY * args.hours / 24
    flag = "" if kinds["baseline"] >= 0.9 * exp else "  <-- LOW"
    print(f"  baseline snapshots {kinds['baseline']} (expected ~{exp:.0f}){flag}; closing snapshots {kinds['closing']}")
    print(f"  credits remaining: {last_credit}")
    for lg in sorted(pregame):
        rate = matched[lg] / pregame[lg] if pregame[lg] else 0.0
        print(f"  {lg:6s} pre-game rows {pregame[lg]:5d}  matched {rate:6.1%}")
    errors = 0
    for log in sorted(LOG_DIR.glob("*.log")):
        for line in log.read_text(errors="replace").splitlines():
            if ("failed" in line or "Traceback" in line or "Error" in line or "STOP" in line) and \
                    line[:20] >= since.strftime("%Y-%m-%dT%H:%M:%S"):
                errors += 1
    tracebacks = sum(log.read_text(errors="replace").count("Traceback") for log in LOG_DIR.glob("*.log"))
    print(f"  error lines (timestamped, window): {errors}; tracebacks in logs (all time): {tracebacks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

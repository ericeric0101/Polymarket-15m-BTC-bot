"""S1 collector health (blind-safe: counts only, no prices, no outcomes).

    .venv/bin/python scripts/sports_odds_health.py [--hours 24]

Snapshots by kind, match rate per league, credits remaining, and error lines in
logs/sports_odds/*.log over the last N hours. With --notify, problems raise a macOS
notification (daily launchd job). Exit code 1 when there is a problem.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "data" / "sports_odds"
LOG_DIR = REPO_ROOT / "logs" / "sports_odds"
EXPECTED_BASELINES_PER_DAY = 12
COLLECTION_T0 = datetime(2026, 10, 17, 11, 13, 49, tzinfo=timezone.utc)   # prereg freeze 172799e
CREDIT_WARN = 3000          # collector stops spending at 2000
MATCH_WARN = 0.80           # soccer/UFC matched 100% in dev scans; NHL/NBA lag book listings


def notify(title: str, message: str) -> None:
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Basso"'
    subprocess.run(["osascript", "-e", script], check=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--notify", action="store_true", help="macOS notification when a problem is found")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    since = max(now - timedelta(hours=args.hours), COLLECTION_T0)   # dev scans before T0 never count
    window_h = (now - since).total_seconds() / 3600

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

    print(f"S1 health {now.isoformat(timespec='seconds')} (window {window_h:.1f}h since {since.isoformat(timespec='seconds')})")
    exp = EXPECTED_BASELINES_PER_DAY * window_h / 24
    flag = "  <-- LOW" if exp >= 1 and kinds["baseline"] < int(0.9 * exp) else ""
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

    problems = []
    if exp >= 1 and kinds["baseline"] < int(0.9 * exp):
        problems.append(f"baseline {kinds['baseline']}/{exp:.0f}")
    if window_h >= 6 and kinds["closing"] == 0:
        problems.append("no closing snapshots")
    if last_credit is not None and float(last_credit) < CREDIT_WARN:
        problems.append(f"credits {last_credit}")
    if errors:
        problems.append(f"{errors} error lines")
    low = [lg for lg in ("epl", "lal", "bun", "sea", "fl1", "ucl", "uel", "ufc")
           if pregame.get(lg) and matched[lg] / pregame[lg] < MATCH_WARN]
    if low:
        problems.append("low match " + ",".join(low))
    print("  status: " + ("OK" if not problems else "PROBLEM: " + "; ".join(problems)))
    if args.notify and problems:
        notify("S1 sports odds collector", "; ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

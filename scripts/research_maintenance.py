#!/usr/bin/env python3
"""One research maintenance pass: export tiers A/B/P, then split tier C partitions.

Started (niced, detached) by the launcher every time the bot starts. Only fully
completed UTC days are processed and days with verified manifests are skipped,
so a pass during a short test session is a cheap no-op. Never deletes data;
retention stays a manual command while the bot is stopped.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "data" / "research_partitions" / ".maintenance.lock"
STATE = ROOT / "data" / "research_partitions" / "maintenance_state.json"


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("research maintenance already running; skipping", flush=True)
            return 0
        started = time.time()
        steps = {}
        for name, command in (
            ("export", [sys.executable, "scripts/research_daily_export.py", "--pending"]),
            ("split", [sys.executable, "scripts/research_partition.py", "split", "--all-completed"]),
            ("eligibility", [sys.executable, "scripts/research_partition.py", "retention", "--all-completed"]),
        ):
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            steps[name] = {"exit_code": result.returncode, "stdout_tail": result.stdout[-4000:],
                           "stderr_tail": result.stderr[-2000:]}
            print(f"[{name}] exit={result.returncode}\n{result.stdout}{result.stderr}", flush=True)
            if result.returncode != 0 and name != "eligibility":
                break
        state = {"started_at": started, "finished_at": time.time(), "pid": os.getpid(), "steps": steps,
                 "ok": all(step["exit_code"] == 0 for step in steps.values())}
        temporary = STATE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=1))
        os.replace(temporary, STATE)
        return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

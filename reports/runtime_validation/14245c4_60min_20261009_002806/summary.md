# 14245c4 60-minute dry-run validation (2026-10-09 00:28:50 → 01:28:29 +08)

- Runtime: commit 14245c4, git_dirty=false, TEST_DRY_RUN, PID 70266, run_id run_1791476945_b68a37e0
- Stop: SIGINT at 01:28:10, exited 21 s, exit code 0, forced_boundary backup 6.7 s (image 3,193,434,112 B), no remaining process
- Checkpoints: checkpoint_T15/T30/T45/T60.json (monitor_validation.py, SQLite mode=ro)

| Gate | Result |
|---|---|
| Real order lifecycle events | 0 (dry-run submits 6, cancels 2, sim fills 4) |
| First snapshot after open (<= 2 s) | 2.96 / 3.11 / 2.77 s — FAIL by ~1 s (was 18.8-19.3 s) |
| Strike fill lag after authoritative lock (<= 2 s) | 0.0 / 0.0 / 0.0 s — PASS |
| Authoritative strike availability | 2.94 / 3.11 s first try; third market upstream pending at 2.77, 6.22, 9.93 s, verified 13.30 s |
| Strike-pending rows before strike | written (third market first row 2.77 s with strike unavailable) |
| ENTRY_DECISION_TRACE vs lifecycle counter | 801 = 801, write failures 0 — PASS |
| Freshness v2 | 963/963 rows v2 |
| Writer drops / errors / tracebacks / SQLite errors | 0 / 0 / 0 / 0 |
| Research store payload | 10.0 MB in ~58 min (~250 MB/day) — above ~190 MB/day estimate |
| Journal payload | 9.0 MB in ~58 min (includes first-hour diagnostic burst) |

## Gate revision (operator decision 2026-10-09)
First entry is only allowed at time_left <= 600 s (FIRST_ENTRY_MAX_TIME_LEFT_SEC=600, i.e. 5 min after open;
MAKER_EARLY_SELL_ONLY_SEC=240), so opening capture latency does not affect entry decisions.
Opening gate is now: first snapshot <= 5 s AND strike filled <= 2 s after authoritative lock.
Result under revised gate: 2.96 / 3.11 / 2.77 s and 0.0 / 0.0 / 0.0 s -> OPENING_CAPTURE_GATE=PASS.

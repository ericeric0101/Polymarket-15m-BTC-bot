# 95f0860 60-minute dry-run validation (2026-10-09 15:26:42 → 16:26:54 +08)

- Runtime: HEAD 95f0860, TEST_DRY_RUN (`.venv/bin/python -u run_bot.py`), run_id run_1791530811_a7cd0127.
- Stop: SIGINT 16:26:34, exited 16:26:54 (20 s), exit code 0; no bot process remains.
- Disk (free, GiB): start 13.752 → at shutdown backup 12.678 (free_bytes_before=13,612,953,600) → after 13.479.
  The 13.5 GiB pre-run threshold was met (13.756). The 10 GiB research guard never triggered (0 RESEARCH_STORAGE_GUARD_TRIGGERED).
- Shutdown backup: `forced_boundary` completed 16:26:44, image 3,205,922,816 B, no leftover .tmp. No periodic backup (interval 10800 s).
- Storage state in log: `STORAGE UNKNOWN -> WARNING` once (free < 20 GiB warning band); no STORAGE CRITICAL.
- Errors: 0 tracebacks. 1 ERROR line = the normal clean-return-without-rollover shutdown path after SIGINT.
- Orders: 0 real order lifecycle events; dry-run submits 6 / cancels 2; shadow sim fills 4, settled 3.
- Settlement: 3 full markets canonical_twap (UP; margins 3.38 / 11.31 / 5.76 bps); first market joined mid-way had no strike lock → UNKNOWN (inv 0, no PnL/guard effect).
- Research shadow evidence written: CANDIDATE_POLICY_SHADOW 76 rows (policy_version CANDIDATE_ENTRY_POLICY_V1, 4 markets, agreement 42 / disagreement 34);
  STOP_SHADOW_CANDIDATE 4 + STOP_SHADOW_RESOLUTION 4 (HARD_LOSS_EQUIVALENT 2, ADVERSE_CROSS 1, ADVERSE_CROSS_PERSIST_15S 1).
- Research rows written: 6,090.

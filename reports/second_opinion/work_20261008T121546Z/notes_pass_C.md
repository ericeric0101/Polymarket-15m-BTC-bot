# Pass C — research validity (data, read-only)
## Manifest (evidence/build_manifest.py -> data_validity_manifest.csv; summary evidence/12_manifest_summary.txt)
- Universe: 480 15-min slots 2026-10-03..07 (UTC; slug epoch = market start, UTC). Data timestamps are epoch (UTC).
- Phase per run from strategy_runs.notes_json.run_manifest.git_commit ancestry (3b50637=A, 33896ce=B). Runs before 2026-10-05 have NO run_manifest/git_commit -> PRE_A_BY_TIME (started before 3b50637 author time). Many runs git_dirty=true with dirty_diff_hash=null -> exact runtime code not reconstructable.
- Native v2 snapshots exist only 2026-10-06 16:30:58Z .. 2026-10-07 15:27:43Z (74 slugs, 8 runs) (evidence/11_v2_provenance.txt). Operator's "Oct 6-7 native v2" overstated: Oct 6 00:00-16:28Z is v1.
- Structural blind spot: first prediction snapshot is ~18.6-19.3 s after market open in essentially every market (native v2 p05..p75 18.6-19.3 s) -> opening 19 s never captured by the 1 Hz snapshotter.
- Snapshot cadence ~0.8 Hz (median 713 snapshots/900 s in v2).
- Tiers: T1=57 (Oct6 7, Oct7 50), T2=95 (Oct3 29, Oct4 17, Oct5 18, Oct6 31), T3=328. Phase x tier: T1 = PRE_A 17 / POST_A 39 / POST_B 1. POST_B cohort = 3 markets total (1 clean).
- Exclusions (non-exclusive): gap>10s 187, incomplete edges 182, settlement_missing 108, insufficient_span 107, TWAP-recompute unresolved 62, no snapshots 56, startup_partial 52, raw_pre_v2 15.
- Joint (all-component) recomputation is NEVER fully resolved for any pre-v2 market (344/345 markets contain MISSING_REQUIRED_FIELD rows); tier 2 uses TWAP-component resolution, which is the only freshness input to the leader/settlement comparison (row-level TWAP class FRESH_* required for each cutoff row).
## Flip stats (evidence/flip_stats.py -> 13_flip_stats.txt). flip(k)= sign(60s official TWAP - strike) at T-k != canonical settlement side.
- T1 (N_days=2, all weekday): T-300 13/57=0.228 [0.138,0.352]; T-180 7/56=0.125; T-120 6/57=0.105; T-60 5/57; T-30 4/56.
- T2 weekend (Oct3-4, N_days=2) vs weekday (Oct5-6, N_days=2): T-300 0/46 vs 10/49; T-180 1/46 vs 7/49; T-120 0/46 vs 7/48. Day-label permutation: 6 distinct assignments, min attainable one-sided p=0.167 -> no significance attainable. Day bootstrap with 2 clusters/group is degenerate (reported but not meaningful).
## Selection check (14_selection_check.txt) — KEY
- Weekend EXCLUDED markets: T-300 flip 19/104 (any fresh cutoff), twap_cross_count share>=2 = 0.33; weekend INCLUDED: 0/46, share>=2 = 0.15. Exclusion is outcome-associated (gaps/interruptions co-occur with choppy markets) -> the tiered weekend sample is biased toward calm markets.
- Labelled sensitivity (all settled markets with fresh-TWAP cutoff, any tier): T-300 Oct3 0.125, Oct4 0.129, Oct5 0.137, Oct6 0.194, Oct7 0.255; T-180 Oct3 0.027, Oct4 0.031, Oct5 0.135, Oct6 0.138, Oct7 0.130. => T-300 weekend-vs-weekday gap mostly a selection artifact; T-180/T-120 gap persists directionally but N_days=2 vs 3.
## Sigma bins (15_sigma_bins.txt)
- z>=2: 0 flips in T1 (T-300 0/17) and T2 (1/10); z<0.5 highest. Mid bins non-monotonic with fully overlapping Wilson CIs (e.g. T1 T-300 [0.5,1) 1/7 vs [1,2) 4/20). Observed rates do not track Phi(-z) (T2 T-300 [0.5,1): 0/32 vs mean Phi(-z)=0.237) — z is not a calibrated probability; consistent with the time-decayed sigma and spot-vs-TWAP reference mismatch.

# INCIDENT (auditor-caused, Pass B, ~2026-10-08 12:31-12:32Z)
- First invocation of evidence/prefix_check.sh from zsh passed "<sha> <tests...>" as ONE argument (zsh does not word-split $var). The script's
  unquoted `rm -rf $P` (P=/tmp/audit_prefix_$SHA) then word-split in bash and deleted 7 tracked files relative to repo root:
  tests/test_{fast_follow_retirement,forward_shadow,outcome_decommission,prediction_research_snapshot,scheduled_rollover_interval,trade_journal_backup,twap_forward_shadow}.py
  and `mkdir -p $P` created empty directories at those paths.
- Detected via `git status`. Those files were clean at Pass 0 (status showed only untracked reports). Restored with `rmdir` + `git show HEAD:<path> > <path>`;
  verified `git hash-object` == `git rev-parse HEAD:<path>` for all 7 and mode 644 == index 100644. No index/HEAD operation was used. git status now equals Pass 0 except reports/second_opinion/.
- Script hardened (sha regex, quoting, /tmp guard). The pre-fix results in 16_prefix_checks.txt come from the SECOND (correct) loop.

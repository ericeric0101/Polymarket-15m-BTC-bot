# Research cleanup manifest 20261009T065902Z

Status: DRY-RUN (nothing deleted). Awaiting approval.

- generated_at_utc: 2026-10-09T06:59:04.012492+00:00
- head_before_cleanup: bb9bd79964421d049581398dd40a0e1a848a2adf
- files_considered: 958
- files_kept: 866
- files_to_delete: 92
- bytes_to_delete_logical: 8026918
- by_category: {'A': 184, 'B': 56, 'C': 0, 'D': 92, 'E': 468, 'F': 139, 'G': 19}
- regenerated_reference_dir: /private/tmp/claude-501/-Users-cheng-kaihuang-Polymarket-BTC-15-Minute-Trading-Bot-main/bf13566c-6875-4b05-b4a7-8d601225f84a/scratchpad/regen_20261009T064906Z
- icloud_mirror: ~/Library/Mobile Documents/com~apple~CloudDocs/polymarket-research (not modified)

- safety_backup_archive: /Users/cheng-kaihuang/polymarket-research-cold-archive/research_cleanup_20261009T065902Z.tar.gz (sha256 587fe13a080059811c4aead45ab793a4843091c28d842a65557feef1bdd8fa33; 92 files; extract-verified)

## Files to delete

| path | bytes | sha256 | tracked | check |
|---|---:|---|---|---|
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/results.json` | 653970 | `b8f192a74bd05955…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/calibration_losing_side.csv` | 5057 | `bb0004bccd76cf2f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/chosen_side_accuracy.csv` | 3832 | `8be5ba476351bd17…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/flip_by_difficulty.csv` | 4103 | `a16b0744ba4402a4…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/gate_counterfactual.csv` | 243266 | `c02ee3858ac55b29…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/gate_counterfactual_rows.csv` | 645768 | `14435d98a013eb11…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_by_exit_kind.csv` | 375 | `4bdad7d10c5ef35f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_by_price.csv` | 448 | `421d509186d974c3…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_by_score.csv` | 441 | `2993437393d7fa38…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_by_time_left.csv` | 386 | `0db73bcc3b08d426…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_by_weekend.csv` | 237 | `bbcccb8d27064f61…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_overall.csv` | 166 | `5c4ac76c72867bff…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/live_positions.csv` | 26749 | `ba2b5a4603b1b13f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/path_cutoff_rows.csv` | 165760 | `8aca0128ed488f88…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/shadow_sim_markets.csv` | 34753 | `de7df33f85153688…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/sim_by_elapsed.csv` | 375 | `a55b3d419dac5791…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/sim_by_price.csv` | 381 | `d5e07d5b0acea8a6…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/sim_by_side.csv` | 231 | `509c2755466b1b10…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/sim_by_weekend.csv` | 239 | `9d0e39fc76639b24…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_journal/tables/sim_overall.csv` | 166 | `982fd54980ebe4fd…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/results.json` | 680651 | `d250767f41020853…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/calibration_losing_side.csv` | 5168 | `e820a2a789a4ae65…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/chosen_side_accuracy.csv` | 3879 | `bf60c64c17c302ab…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/flip_by_difficulty.csv` | 4136 | `032ed00a4876e4d5…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/gate_counterfactual.csv` | 254098 | `f5e1205b9df3f1b2…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/gate_counterfactual_rows.csv` | 690001 | `b3f5ec477bb71a45…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_by_exit_kind.csv` | 382 | `e58c5c29bf0b1742…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_by_price.csv` | 443 | `f24af124a46f1f11…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_by_score.csv` | 457 | `c766c8eaf943c422…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_by_time_left.csv` | 394 | `e63d3d111392449f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_by_weekend.csv` | 240 | `621bc278973c9a31…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_overall.csv` | 168 | `9175ecafdf36ebb4…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/live_positions.csv` | 30503 | `66b33aab0ef0acaf…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/path_cutoff_rows.csv` | 175181 | `ff0229120c1ab787…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/shadow_sim_markets.csv` | 34713 | `255d4b671e9851f9…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/sim_by_elapsed.csv` | 374 | `b6b6c4a9e97f865a…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/sim_by_price.csv` | 387 | `fcb2426103c9bd92…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/sim_by_side.csv` | 233 | `505874554294086e…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/sim_by_weekend.csv` | 240 | `f64af46afefbeb19…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage2_official/tables/sim_overall.csv` | 167 | `b36ef3b4992196e3…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage3/entry_analysis.json` | 54281 | `f01b957f90761b1a…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage3/entry_rows_DRY_RUN.csv` | 33451 | `70bf3feccd18996a…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage3/entry_rows_LIVE.csv` | 16039 | `e9730cb84683a2d8…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage4/flip_analysis.json` | 28317 | `be0027ef0a169765…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage4/flip_cutoff_rows.csv` | 196236 | `74c482076813c124…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction/stage5/stop_analysis.json` | 16675 | `18e31c7363de1e03…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/results.json` | 653912 | `83693228030db11c…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/calibration_losing_side.csv` | 5057 | `bb0004bccd76cf2f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/chosen_side_accuracy.csv` | 3832 | `8be5ba476351bd17…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/flip_by_difficulty.csv` | 4103 | `a16b0744ba4402a4…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/gate_counterfactual.csv` | 243266 | `c02ee3858ac55b29…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/gate_counterfactual_rows.csv` | 645768 | `14435d98a013eb11…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_by_exit_kind.csv` | 375 | `4bdad7d10c5ef35f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_by_price.csv` | 448 | `421d509186d974c3…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_by_score.csv` | 441 | `2993437393d7fa38…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_by_time_left.csv` | 386 | `0db73bcc3b08d426…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_by_weekend.csv` | 237 | `bbcccb8d27064f61…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_overall.csv` | 166 | `5c4ac76c72867bff…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/live_positions.csv` | 26749 | `ba2b5a4603b1b13f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/path_cutoff_rows.csv` | 165760 | `8aca0128ed488f88…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/shadow_sim_markets.csv` | 34753 | `de7df33f85153688…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/sim_by_elapsed.csv` | 375 | `a55b3d419dac5791…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/sim_by_price.csv` | 381 | `d5e07d5b0acea8a6…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/sim_by_side.csv` | 231 | `509c2755466b1b10…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/sim_by_weekend.csv` | 239 | `9d0e39fc76639b24…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_journal/tables/sim_overall.csv` | 166 | `982fd54980ebe4fd…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/results.json` | 680593 | `2952dfc3e5931338…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/calibration_losing_side.csv` | 5168 | `e820a2a789a4ae65…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/chosen_side_accuracy.csv` | 3879 | `bf60c64c17c302ab…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/flip_by_difficulty.csv` | 4136 | `032ed00a4876e4d5…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/gate_counterfactual.csv` | 254098 | `f5e1205b9df3f1b2…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/gate_counterfactual_rows.csv` | 690001 | `b3f5ec477bb71a45…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_by_exit_kind.csv` | 382 | `e58c5c29bf0b1742…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_by_price.csv` | 443 | `f24af124a46f1f11…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_by_score.csv` | 457 | `c766c8eaf943c422…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_by_time_left.csv` | 394 | `e63d3d111392449f…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_by_weekend.csv` | 240 | `621bc278973c9a31…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_overall.csv` | 168 | `9175ecafdf36ebb4…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/live_positions.csv` | 30503 | `66b33aab0ef0acaf…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/path_cutoff_rows.csv` | 175181 | `ff0229120c1ab787…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/shadow_sim_markets.csv` | 34713 | `255d4b671e9851f9…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/sim_by_elapsed.csv` | 374 | `b6b6c4a9e97f865a…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/sim_by_price.csv` | 387 | `fcb2426103c9bd92…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/sim_by_side.csv` | 233 | `505874554294086e…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/sim_by_weekend.csv` | 240 | `f64af46afefbeb19…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage2_official/tables/sim_overall.csv` | 167 | `b36ef3b4992196e3…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage3/entry_analysis.json` | 54281 | `1aca4373df49dfc7…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage3/entry_rows_DRY_RUN.csv` | 33451 | `70bf3feccd18996a…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage3/entry_rows_LIVE.csv` | 16039 | `e9730cb84683a2d8…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage4/flip_analysis.json` | 28317 | `72299ddd9af913af…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage4/flip_cutoff_rows.csv` | 196236 | `74c482076813c124…` | False | NUMERIC_MATCH |
| `reports/final_research_iteration/20261009T042008Z/stage6_reproduction_2/stage5/stop_analysis.json` | 16675 | `695dee7aba8dbdb0…` | False | NUMERIC_MATCH |

## Kept by category

### A — raw/irreplaceable data (184 files)
- `data/research/twap_forward_shadow.db` — 1 files, 319488 bytes
- `data/research/twap_forward_shadow.db-shm` — 1 files, 32768 bytes
- `data/research/twap_forward_shadow.db-wal` — 1 files, 0 bytes
- `data/research_export/A_market_summary` — 14 files, 231784 bytes
- `data/research_export/B_decisions` — 42 files, 19911754 bytes
- `data/research_export/P_paths` — 42 files, 25001213 bytes
- `data/research_export/manifests` — 42 files, 61352 bytes
- `data/research_export/official_resolution` — 2 files, 435427 bytes
- `data/research_export/outcome_provenance` — 2 files, 175104 bytes
- `data/research_partitions/.maintenance.lock` — 1 files, 0 bytes
- `data/research_partitions/C_2026-10-02.db` — 1 files, 47116288 bytes
- `data/research_partitions/C_2026-10-03.db` — 1 files, 263925760 bytes
- `data/research_partitions/C_2026-10-04.db` — 1 files, 243208192 bytes
- `data/research_partitions/C_2026-10-05.db` — 1 files, 183607296 bytes
- `data/research_partitions/C_2026-10-06.db` — 1 files, 274919424 bytes
- `data/research_partitions/C_2026-10-07.db` — 1 files, 223113216 bytes
- `data/research_partitions/C_2026-10-08.db` — 1 files, 29138944 bytes
- `data/research_partitions/maintenance_state.json` — 1 files, 2379 bytes
- `data/research_partitions/manifests` — 11 files, 6938 bytes
- `data/research_partitions/retention_log` — 16 files, 10940 bytes
- `logs/trade_journal.db` — 1 files, 3193483264 bytes

### B — canonical/reusable code (56 files)
- `scripts/analyze_weekend_liquidity.py` — 1 files, 89076 bytes
- `scripts/backtest_profit_lock.py` — 1 files, 60581 bytes
- `scripts/backtest_short_term_take_profit.py` — 1 files, 47433 bytes
- `scripts/backtest_simple_trend_hold.py` — 1 files, 74779 bytes
- `scripts/build_outcome_provenance.py` — 1 files, 6256 bytes
- `scripts/build_smart_money_wallets.py` — 1 files, 25832 bytes
- `scripts/calibration_shadow_report.py` — 1 files, 7397 bytes
- `scripts/compare_polymarket_chainlink_vs_binance.py` — 1 files, 7595 bytes
- `scripts/depth_risk_shadow_report.py` — 1 files, 3811 bytes
- `scripts/econ_gate_report.py` — 1 files, 15489 bytes
- `scripts/edge_attribution_report.py` — 1 files, 9400 bytes
- `scripts/empirical_probability_research.py` — 1 files, 28660 bytes
- `scripts/executable_fair_edge_report.py` — 1 files, 6544 bytes
- `scripts/execution_path_penalty_report.py` — 1 files, 4404 bytes
- `scripts/execution_penalty_report.py` — 1 files, 1853 bytes
- `scripts/fair_edge_bucket_shadow_report.py` — 1 files, 4129 bytes
- `scripts/fast_follow_execution_report.py` — 1 files, 3927 bytes
- `scripts/feed_health_report.py` — 1 files, 2899 bytes
- `scripts/fetch_official_resolutions.py` — 1 files, 6177 bytes
- `scripts/final_research_analysis.py` — 1 files, 23932 bytes
- `scripts/flip_regime_day_level.py` — 1 files, 7464 bytes
- `scripts/forward_shadow_report.py` — 1 files, 21120 bytes
- `scripts/hourly_attribution_report.py` — 1 files, 14398 bytes
- `scripts/hyperliquid_outcome_lead_lag_report.py` — 1 files, 6439 bytes
- `scripts/invalidation_counterfactual_report.py` — 1 files, 2661 bytes
- `scripts/lead_lag_latency_report.py` — 1 files, 1025 bytes
- `scripts/live_entry_quality_report.py` — 1 files, 35477 bytes
- `scripts/market_regime_report.py` — 1 files, 10066 bytes
- `scripts/mirrored_down_report.py` — 1 files, 13642 bytes
- `scripts/outcome_analysis.py` — 1 files, 13202 bytes
- `scripts/outcome_fast_follow_pnl_report.py` — 1 files, 1421 bytes
- `scripts/outcome_lead_lag_event_report.py` — 1 files, 2856 bytes
- `scripts/outcome_lead_lag_threshold_replay.py` — 1 files, 2232 bytes
- `scripts/penalty_simulation.py` — 1 files, 9492 bytes
- `scripts/pnl_attribution_report.py` — 1 files, 3806 bytes
- `scripts/pnl_reconcile_report.py` — 1 files, 7205 bytes
- `scripts/prediction_forensics.py` — 1 files, 68321 bytes
- `scripts/prediction_snapshot_analysis.py` — 1 files, 22603 bytes
- `scripts/pure_probe_report.py` — 1 files, 21271 bytes
- `scripts/pure_signal_probe.py` — 1 files, 50527 bytes
- `scripts/realized_edge_report.py` — 1 files, 38909 bytes
- `scripts/recent_buy_fill_report.py` — 1 files, 24407 bytes
- `scripts/reproduce_research_iteration.py` — 1 files, 8059 bytes
- `scripts/required_path_probability_replay.py` — 1 files, 36794 bytes
- `scripts/research_analysis.py` — 1 files, 95516 bytes
- `scripts/research_entry_analysis.py` — 1 files, 14902 bytes
- `scripts/research_flip_analysis.py` — 1 files, 14879 bytes
- `scripts/research_stop_analysis.py` — 1 files, 10396 bytes
- `scripts/residual_fragility_research.py` — 1 files, 48049 bytes
- `scripts/score_momentum_report.py` — 1 files, 17649 bytes
- `scripts/stop_forensics_report.py` — 1 files, 3466 bytes
- `scripts/strike_flip_risk_analysis.py` — 1 files, 63597 bytes
- `scripts/trade_db_report.py` — 1 files, 2149 bytes
- `scripts/trade_path_pnl_report.py` — 1 files, 8496 bytes
- `scripts/twap_fair_calibration_report.py` — 1 files, 7973 bytes
- `scripts/twap_forward_report.py` — 1 files, 18487 bytes

### E — historical evidence (468 files)
- `reports/empirical_probability/empirical_probability_calibration.csv` — 1 files, 24461 bytes
- `reports/empirical_probability/empirical_probability_checkpoint.csv` — 1 files, 161710 bytes
- `reports/empirical_probability/empirical_probability_checkpoint_summary.csv` — 1 files, 1961 bytes
- `reports/empirical_probability/empirical_probability_paired_comparison.csv` — 1 files, 2271 bytes
- `reports/empirical_probability/empirical_probability_regimes.csv` — 1 files, 3237 bytes
- `reports/empirical_probability/summary.md` — 1 files, 7744 bytes
- `reports/exit_execution_capability/capability.json` — 1 files, 492 bytes
- `reports/exit_execution_capability/summary.md` — 1 files, 984 bytes
- `reports/final_research/20261009T022133Z` — 22 files, 1820610 bytes
- `reports/final_research_iteration/20261009T042008Z` — 57 files, 4416903 bytes
- `reports/final_research_iteration/research_iteration_20261009T045322Z.md` — 1 files, 22572 bytes
- `reports/forward_shadow/bbo_path.csv` — 1 files, 1305623 bytes
- `reports/forward_shadow/candidate_entries.csv` — 1 files, 123346 bytes
- `reports/forward_shadow/combined180_results.csv` — 1 files, 7 bytes
- `reports/forward_shadow/combined300_results.csv` — 1 files, 7 bytes
- `reports/forward_shadow/config_overlap.csv` — 1 files, 931 bytes
- `reports/forward_shadow/data_quality.csv` — 1 files, 325 bytes
- `reports/forward_shadow/execution_quality.csv` — 1 files, 5821 bytes
- `reports/forward_shadow/fair_deterioration.csv` — 1 files, 2689206 bytes
- `reports/forward_shadow/hold_results.csv` — 1 files, 1152 bytes
- `reports/forward_shadow/live_vs_shadow.csv` — 1 files, 12524 bytes
- `reports/forward_shadow/loss_warnings.csv` — 1 files, 7 bytes
- `reports/forward_shadow/mfe_mae.csv` — 1 files, 3902 bytes
- `reports/forward_shadow/paired_entry_comparison.csv` — 1 files, 2613 bytes
- `reports/forward_shadow/recovery_after_drawdown.csv` — 1 files, 843 bytes
- `reports/forward_shadow/shadow_events_per_hour.csv` — 1 files, 6278 bytes
- `reports/forward_shadow/shadow_exit_results.csv` — 1 files, 5775 bytes
- `reports/forward_shadow/signal_reversal_analysis.csv` — 1 files, 1053 bytes
- `reports/forward_shadow/strike_cross_analysis.csv` — 1 files, 117280 bytes
- `reports/forward_shadow/summary.md` — 1 files, 1961 bytes
- `reports/forward_shadow/thesis_weakening_marks.csv` — 1 files, 7 bytes
- `reports/forward_shadow/tp20_results.csv` — 1 files, 1152 bytes
- `reports/forward_shadow/trail10_results.csv` — 1 files, 1334 bytes
- `reports/forward_shadow/trail5_results.csv` — 1 files, 1170 bytes
- `reports/forward_shadow/weekday_summary.csv` — 1 files, 7 bytes
- `reports/forward_shadow/weekend_shadow_summary.csv` — 1 files, 3860 bytes
- `reports/live_entry_quality/candidate_join_quality.csv` — 1 files, 850 bytes
- `reports/live_entry_quality/candidate_level.csv` — 1 files, 109901 bytes
- `reports/live_entry_quality/crossing_buckets.csv` — 1 files, 514 bytes
- `reports/live_entry_quality/edge_buckets.csv` — 1 files, 536 bytes
- `reports/live_entry_quality/gross_edge_buckets.csv` — 1 files, 539 bytes
- `reports/live_entry_quality/hour_blocks.csv` — 1 files, 521 bytes
- `reports/live_entry_quality/price_buckets.csv` — 1 files, 795 bytes
- `reports/live_entry_quality/safety_sigma_buckets.csv` — 1 files, 517 bytes
- `reports/live_entry_quality/shadow_reject_counterfactual.csv` — 1 files, 1748 bytes
- `reports/live_entry_quality/shadow_size_counterfactual.csv` — 1 files, 539 bytes
- `reports/live_entry_quality/strike_distance_buckets.csv` — 1 files, 769 bytes
- `reports/live_entry_quality/summary.md` — 1 files, 3894 bytes
- `reports/live_entry_quality/telemetry_health.csv` — 1 files, 313 bytes
- `reports/live_entry_quality/weekday_weekend.csv` — 1 files, 467 bytes
- `reports/prediction_snapshot/btc_disagreement_30s.csv` — 1 files, 219 bytes
- `reports/prediction_snapshot/entry_snapshots.csv` — 1 files, 1 bytes
- `reports/prediction_snapshot/extreme_residual_episodes.csv` — 1 files, 1 bytes
- `reports/prediction_snapshot/residual_30s.csv` — 1 files, 1 bytes
- `reports/prediction_snapshot/residual_bins.csv` — 1 files, 347 bytes
- `reports/prediction_snapshot/snapshot_quality.csv` — 1 files, 1 bytes
- `reports/prediction_snapshot/summary.md` — 1 files, 2705 bytes
- `reports/profit_lock_backtest/ambiguous_intrabar_cases.csv` — 1 files, 359258 bytes
- `reports/profit_lock_backtest/bootstrap.csv` — 1 files, 778 bytes
- `reports/profit_lock_backtest/combined_exit_replay.csv` — 1 files, 18429146 bytes
- `reports/profit_lock_backtest/combined_exit_results.csv` — 1 files, 773436 bytes
- `reports/profit_lock_backtest/data_quality.csv` — 1 files, 29330 bytes
- `reports/profit_lock_backtest/development_holdout.csv` — 1 files, 6218 bytes
- `reports/profit_lock_backtest/early_vs_current.csv` — 1 files, 827 bytes
- `reports/profit_lock_backtest/entry_config_summary.csv` — 1 files, 3818 bytes
- `reports/profit_lock_backtest/entry_economics_decomposition.csv` — 1 files, 558 bytes
- `reports/profit_lock_backtest/entry_exit_matrix.csv` — 1 files, 4610573 bytes
- `reports/profit_lock_backtest/entry_price_interaction.csv` — 1 files, 3265 bytes
- `reports/profit_lock_backtest/ever_positive_then_negative.csv` — 1 files, 73922 bytes
- `reports/profit_lock_backtest/giveback_analysis.csv` — 1 files, 1468 bytes
- `reports/profit_lock_backtest/mae_distribution.csv` — 1 files, 2422 bytes
- `reports/profit_lock_backtest/mfe_mae_by_entry.csv` — 1 files, 356281 bytes
- `reports/profit_lock_backtest/mfe_summary.csv` — 1 files, 1304 bytes
- `reports/profit_lock_backtest/mfe_thresholds.csv` — 1 files, 5914 bytes
- `reports/profit_lock_backtest/partial_tp_results.csv` — 1 files, 414837 bytes
- `reports/profit_lock_backtest/peak_to_exit_giveback.csv` — 1 files, 32468 bytes
- `reports/profit_lock_backtest/profit_lock_results.csv` — 1 files, 652705 bytes
- `reports/profit_lock_backtest/summary.md` — 1 files, 5260 bytes
- `reports/profit_lock_backtest/tail_loss_reduction.csv` — 1 files, 9139 bytes
- `reports/profit_lock_backtest/time_lock_results.csv` — 1 files, 643073 bytes
- `reports/profit_lock_backtest/timed_exit_audit.csv` — 1 files, 430957 bytes
- `reports/profit_lock_backtest/trailing_results.csv` — 1 files, 854067 bytes
- `reports/profit_lock_backtest/weekday_entry_exit_matrix.csv` — 1 files, 513087 bytes
- `reports/profit_lock_backtest/weekday_weekend_path.csv` — 1 files, 1419 bytes
- `reports/profit_lock_backtest/weekend_entry_exit_matrix.csv` — 1 files, 507209 bytes
- `reports/profit_lock_backtest/winner_retention.csv` — 1 files, 1016356 bytes
- `reports/protocol_v2_migration_assessment_20261009.md` — 1 files, 11295 bytes
- `reports/required_path_probability/calibration.csv` — 1 files, 1233 bytes
- `reports/required_path_probability/data_quality.csv` — 1 files, 7396 bytes
- `reports/required_path_probability/market_flip_timeline.csv` — 1 files, 20017 bytes
- `reports/required_path_probability/observation_probability.csv` — 1 files, 20017 bytes
- `reports/required_path_probability/paired_comparison.csv` — 1 files, 422 bytes
- `reports/required_path_probability/summary.md` — 1 files, 8043 bytes
- `reports/required_path_probability/threshold_crossings.csv` — 1 files, 1792 bytes
- `reports/research_analysis/adapter_l2` — 1 files, 5075 bytes
- `reports/research_analysis/capital_efficiency` — 8 files, 16200 bytes
- `reports/research_analysis/collection_interruption_forensics` — 4 files, 85414 bytes
- `reports/research_analysis/cycle_pnl_ledger` — 1 files, 7299 bytes
- `reports/research_analysis/engineering_audit` — 1 files, 7937 bytes
- `reports/research_analysis/engineering_readiness` — 1 files, 45443 bytes
- `reports/research_analysis/entry_stop_status` — 22 files, 34822 bytes
- `reports/research_analysis/outcome_decommission` — 3 files, 227329 bytes
- `reports/research_analysis/postfix_weekday_weekend` — 4 files, 179129 bytes
- `reports/research_analysis/prediction_freshness` — 5 files, 70421706 bytes
- `reports/research_analysis/preliminary_regime_comparison` — 6 files, 37358 bytes
- `reports/research_analysis/regime` — 2 files, 2044432 bytes
- `reports/research_analysis/regime_comparison` — 11 files, 128624 bytes
- `reports/research_analysis/regime_comparison_early_weekday` — 44 files, 1003749 bytes
- `reports/research_analysis/regime_comparison_monday_interim` — 6 files, 193823 bytes
- `reports/research_analysis/runtime_validation` — 1 files, 3908 bytes
- `reports/research_analysis/stop_lifecycle` — 4 files, 81666 bytes
- `reports/research_analysis/storage_attribution` — 8 files, 383939 bytes
- `reports/research_analysis/storage_backup` — 3 files, 54904 bytes
- `reports/research_analysis/storage_cleanup` — 5 files, 1492314 bytes
- `reports/research_analysis/storage_redesign` — 2 files, 62182 bytes
- `reports/research_analysis/weekend_replication_batch_1` — 11 files, 28486 bytes
- `reports/research_analysis/writer_storage` — 1 files, 27180 bytes
- `reports/runtime_validation/14245c4_60min_20261009_002806` — 8 files, 426898 bytes
- `reports/runtime_validation/d24b576_60min_runtime_20261008_225836_+0800.json` — 1 files, 4486885 bytes
- `reports/runtime_validation/d24b576_60min_runtime_20261008_225836_+0800.md` — 1 files, 14250 bytes
- `reports/runtime_validation/d24b576_60min_runtime_20261008_225836_+0800.runtime.log` — 1 files, 416288 bytes
- `reports/runtime_validation/monitor_validation.py` — 1 files, 8851 bytes
- `reports/second_opinion/data_validity_manifest.csv` — 1 files, 84594 bytes
- `reports/second_opinion/independent_system_audit_20261008T121546Z.json` — 1 files, 19737 bytes
- `reports/second_opinion/independent_system_audit_20261008T121546Z.md` — 1 files, 37356 bytes
- `reports/second_opinion/work_20261008T121546Z` — 52 files, 487140 bytes
- `reports/second_opinion_remediation/remediation_20261008_213039_+0800.json` — 1 files, 15378 bytes
- `reports/second_opinion_remediation/remediation_20261008_213039_+0800.md` — 1 files, 12003 bytes
- `reports/stop_forensics/post_entry_smart_money.csv` — 1 files, 12 bytes
- `reports/stop_forensics/session_pnl_guard_replay.csv` — 1 files, 5491 bytes
- `reports/stop_forensics/stop_candidate_comparison.csv` — 1 files, 12 bytes
- `reports/stop_forensics/stop_persistence_10s_review.csv` — 1 files, 12 bytes
- `reports/stop_forensics/stop_shadow_checkpoints.csv` — 1 files, 1422 bytes
- `reports/twap_forward/data_quality.csv` — 1 files, 174 bytes
- `reports/twap_forward/fast_spot_lead_lag.csv` — 1 files, 12 bytes
- `reports/twap_forward/market_twap_summary.csv` — 1 files, 2586 bytes
- `reports/twap_forward/projection_accuracy.csv` — 1 files, 581 bytes
- `reports/twap_forward/stop_twap_forensics.csv` — 1 files, 12 bytes
- `reports/twap_forward/storage_health.csv` — 1 files, 505 bytes
- `reports/twap_forward/summary.md` — 1 files, 296 bytes
- `reports/twap_forward/twap_checkpoints.csv` — 1 files, 12 bytes
- `reports/twap_forward/twap_cross_events.csv` — 1 files, 12 bytes
- `reports/twap_forward/twap_exit_urgency.csv` — 1 files, 12 bytes
- `reports/twap_forward/twap_projection_events.csv` — 1 files, 12 bytes
- `reports/twap_forward/twap_vs_polymarket_move.csv` — 1 files, 12 bytes
- `reports/twap_forward/twap_vs_smart_money.csv` — 1 files, 12 bytes
- `reports/unified_strategy_research/btc_candles_manifest.csv` — 1 files, 2544 bytes
- `reports/unified_strategy_research/btc_candles_used.csv` — 1 files, 7105598 bytes
- `reports/unified_strategy_research/control_strategies.csv` — 1 files, 205296 bytes
- `reports/unified_strategy_research/control_strategy_summary.csv` — 1 files, 8494 bytes
- `reports/unified_strategy_research/data_quality.csv` — 1 files, 13668 bytes
- `reports/unified_strategy_research/development_selected_holdout.csv` — 1 files, 1250 bytes
- `reports/unified_strategy_research/entry_price_buckets.csv` — 1 files, 2398 bytes
- `reports/unified_strategy_research/fetch_diagnostics.csv` — 1 files, 266 bytes
- `reports/unified_strategy_research/liquidity_regime_backtest.csv` — 1 files, 1020 bytes
- `reports/unified_strategy_research/liquidity_regime_frequency.csv` — 1 files, 207 bytes
- `reports/unified_strategy_research/liquidity_regimes.csv` — 1 files, 21465 bytes
- `reports/unified_strategy_research/local_liquidity_vs_pnl.csv` — 1 files, 234 bytes
- `reports/unified_strategy_research/local_public_fetch_diagnostics.csv` — 1 files, 2060 bytes
- `reports/unified_strategy_research/local_public_join.csv` — 1 files, 6040 bytes
- `reports/unified_strategy_research/local_stoploss_counterfactual.csv` — 1 files, 322 bytes
- `reports/unified_strategy_research/observation_window_entry_price.csv` — 1 files, 1453 bytes
- `reports/unified_strategy_research/observation_window_sensitivity.csv` — 1 files, 6739 bytes
- `reports/unified_strategy_research/observation_window_win_rate.csv` — 1 files, 1424 bytes
- `reports/unified_strategy_research/public_market_level.csv` — 1 files, 38037 bytes
- `reports/unified_strategy_research/public_market_sample.csv` — 1 files, 89179 bytes
- `reports/unified_strategy_research/public_multivariate_ols.csv` — 1 files, 639 bytes
- `reports/unified_strategy_research/public_refresh` — 12 files, 387660 bytes
- `reports/unified_strategy_research/public_time_of_day.csv` — 1 files, 2637 bytes
- `reports/unified_strategy_research/public_time_to_resolution.csv` — 1 files, 147764 bytes
- `reports/unified_strategy_research/public_time_to_resolution_comparison.csv` — 1 files, 6434 bytes
- `reports/unified_strategy_research/public_time_to_resolution_summary.csv` — 1 files, 10912 bytes
- `reports/unified_strategy_research/public_weekday_weekend.csv` — 1 files, 1823 bytes
- `reports/unified_strategy_research/public_weekend_hour_comparison.csv` — 1 files, 2208 bytes
- `reports/unified_strategy_research/public_weekend_hour_interaction.csv` — 1 files, 4347 bytes
- `reports/unified_strategy_research/public_weekly_blocked.csv` — 1 files, 5898 bytes
- `reports/unified_strategy_research/signal_disagreement.csv` — 1 files, 830 bytes
- `reports/unified_strategy_research/simple_backtest_all.csv` — 1 files, 14176635 bytes
- `reports/unified_strategy_research/simple_backtest_summary.csv` — 1 files, 1010343 bytes
- `reports/unified_strategy_research/strategy_confidence_intervals.csv` — 1 files, 768 bytes
- `reports/unified_strategy_research/strategy_drawdown.csv` — 1 files, 804 bytes
- `reports/unified_strategy_research/strategy_equity_curve.csv` — 1 files, 6373 bytes
- `reports/unified_strategy_research/summary.md` — 1 files, 7380 bytes
- `reports/unified_strategy_research/time_of_day_backtest.csv` — 1 files, 1244 bytes
- `reports/unified_strategy_research/trend_strength_buckets.csv` — 1 files, 1386 bytes
- `reports/unified_strategy_research/trend_threshold_sensitivity.csv` — 1 files, 7885 bytes
- `reports/unified_strategy_research/weekday_weekend_backtest.csv` — 1 files, 766 bytes
- `reports/unified_strategy_research/zero_fee_backtest_summary.csv` — 1 files, 709067 bytes
- `reports/work_handoff/research_and_settlement_handoff_20261009T040608Z.md` — 1 files, 6783 bytes
- `scripts/four_market_prediction_forensics.py` — 1 files, 36664 bytes
- `scripts/historical_freshness_recomputation.py` — 1 files, 36975 bytes

### F — uncertain (kept) (139 files)
- `reports/.DS_Store` — 1 files, 14340 bytes
- `reports/four_market_prediction_forensics/btc_disagreement.csv` — 1 files, 386 bytes
- `reports/four_market_prediction_forensics/data_quality.csv` — 1 files, 706 bytes
- `reports/four_market_prediction_forensics/entry_edge_classification.csv` — 1 files, 980 bytes
- `reports/four_market_prediction_forensics/fast_follow_case.csv` — 1 files, 14033 bytes
- `reports/four_market_prediction_forensics/repricing_events.csv` — 1 files, 6104 bytes
- `reports/four_market_prediction_forensics/repricing_events_5c_secondary.csv` — 1 files, 14494 bytes
- `reports/four_market_prediction_forensics/required_sigma_repricing.csv` — 1 files, 513 bytes
- `reports/four_market_prediction_forensics/residual_replication.csv` — 1 files, 660 bytes
- `reports/four_market_prediction_forensics/signal_lead_ranking.csv` — 1 files, 497 bytes
- `reports/four_market_prediction_forensics/summary.md` — 1 files, 8306 bytes
- `reports/prediction_forensics/counterfactual_filters.csv` — 1 files, 580 bytes
- `reports/prediction_forensics/data_quality.csv` — 1 files, 1530 bytes
- `reports/prediction_forensics/depth_shadow_events.csv` — 1 files, 12844 bytes
- `reports/prediction_forensics/entries.csv` — 1 files, 16526 bytes
- `reports/prediction_forensics/lead_lag_events.csv` — 1 files, 27192 bytes
- `reports/prediction_forensics/loser_timelines.csv` — 1 files, 147668 bytes
- `reports/prediction_forensics/probability_comparison.csv` — 1 files, 422 bytes
- `reports/prediction_forensics/repricing_market_level.csv` — 1 files, 5055 bytes
- `reports/prediction_forensics/repricing_model_comparison.csv` — 1 files, 3763 bytes
- `reports/prediction_forensics/repricing_prediction.csv` — 1 files, 76869 bytes
- `reports/prediction_forensics/required_path_entry_risk.csv` — 1 files, 10735 bytes
- `reports/prediction_forensics/signal_ablation.csv` — 1 files, 6712 bytes
- `reports/prediction_forensics/summary.md` — 1 files, 16727 bytes
- `reports/prediction_forensics/winner_loser_comparison.csv` — 1 files, 2007 bytes
- `reports/quote_freshness_replay_2026-10-01/README.md` — 1 files, 4091 bytes
- `reports/quote_freshness_replay_2026-10-01/empirical_probability` — 6 files, 226546 bytes
- `reports/quote_freshness_replay_2026-10-01/twap_forward` — 17 files, 1185003 bytes
- `reports/residual_fragility/data_quality.csv` — 1 files, 3416 bytes
- `reports/residual_fragility/entry_fragility.csv` — 1 files, 6479 bytes
- `reports/residual_fragility/fragility_matrix.csv` — 1 files, 1671 bytes
- `reports/residual_fragility/future_repricing.csv` — 1 files, 105876 bytes
- `reports/residual_fragility/known_loser_case_1790920800.csv` — 1 files, 26343 bytes
- `reports/residual_fragility/known_loser_case_1790939700.csv` — 1 files, 20051 bytes
- `reports/residual_fragility/residual_bins.csv` — 1 files, 3683 bytes
- `reports/residual_fragility/residual_model_comparison.csv` — 1 files, 8390 bytes
- `reports/residual_fragility/residual_observations.csv` — 1 files, 417176 bytes
- `reports/residual_fragility/summary.md` — 1 files, 7518 bytes
- `reports/residual_fragility/telemetry_gap_audit.csv` — 1 files, 4541 bytes
- `reports/residual_fragility/winner_loser_fragility.csv` — 1 files, 3012 bytes
- `reports/strike_flip_risk/adjacent_market_transitions.csv` — 1 files, 7 bytes
- `reports/strike_flip_risk/adjacent_strike_validation.csv` — 1 files, 3172 bytes
- `reports/strike_flip_risk/crossing_counts.csv` — 1 files, 18321 bytes
- `reports/strike_flip_risk/danger_zone_duration.csv` — 1 files, 52533 bytes
- `reports/strike_flip_risk/data_quality.csv` — 1 files, 29372 bytes
- `reports/strike_flip_risk/distance_by_lifecycle.csv` — 1 files, 4759 bytes
- `reports/strike_flip_risk/evidence_verdict.csv` — 1 files, 2081 bytes
- `reports/strike_flip_risk/final_margin.csv` — 1 files, 26285 bytes
- `reports/strike_flip_risk/flip_rate_by_distance.csv` — 1 files, 7764 bytes
- `reports/strike_flip_risk/hour_block_flip_risk.csv` — 1 files, 1969 bytes
- `reports/strike_flip_risk/leader_persistence.csv` — 1 files, 4098 bytes
- `reports/strike_flip_risk/market_strike_distance.csv` — 1 files, 238706 bytes
- `reports/strike_flip_risk/near_strike_probability.csv` — 1 files, 26885 bytes
- `reports/strike_flip_risk/safety_sigma.csv` — 1 files, 246026 bytes
- `reports/strike_flip_risk/strategy_pnl_by_distance.csv` — 1 files, 2458 bytes
- `reports/strike_flip_risk/strategy_strike_risk_join.csv` — 1 files, 92816 bytes
- `reports/strike_flip_risk/strategy_trend_vs_distance.csv` — 1 files, 1991 bytes
- `reports/strike_flip_risk/summary.md` — 1 files, 9204 bytes
- `reports/strike_flip_risk/weekday_weekend_distance.csv` — 1 files, 5941 bytes
- `reports/strike_flip_risk/weekend_crossing_tests.csv` — 1 files, 1783 bytes
- `reports/strike_flip_risk/weekend_effect_models.csv` — 1 files, 1864 bytes
- `reports/strike_flip_risk/weekend_flip_rate_tests.csv` — 1 files, 2914 bytes
- `reports/take_profit_backtest/ambiguous_intrabar_cases.csv` — 1 files, 8 bytes
- `reports/take_profit_backtest/candidate_trades.csv` — 1 files, 851561 bytes
- `reports/take_profit_backtest/confidence_intervals.csv` — 1 files, 1615 bytes
- `reports/take_profit_backtest/daily_projection.csv` — 1 files, 4407 bytes
- `reports/take_profit_backtest/data_quality.csv` — 1 files, 34198 bytes
- `reports/take_profit_backtest/development_holdout.csv` — 1 files, 25176 bytes
- `reports/take_profit_backtest/entry_price_buckets.csv` — 1 files, 1564 bytes
- `reports/take_profit_backtest/execution_cost_sensitivity.csv` — 1 files, 248417 bytes
- `reports/take_profit_backtest/hour_blocks.csv` — 1 files, 6498 bytes
- `reports/take_profit_backtest/local_stoploss_tp_conflict.csv` — 1 files, 1303 bytes
- `reports/take_profit_backtest/mfe_distribution.csv` — 1 files, 673 bytes
- `reports/take_profit_backtest/mfe_mae.csv` — 1 files, 851561 bytes
- `reports/take_profit_backtest/signal_sensitivity.csv` — 1 files, 1786 bytes
- `reports/take_profit_backtest/strike_distance_interaction.csv` — 1 files, 81 bytes
- `reports/take_profit_backtest/summary.md` — 1 files, 6046 bytes
- `reports/take_profit_backtest/tp_hit_rates.csv` — 1 files, 1171 bytes
- `reports/take_profit_backtest/tp_sl_matrix.csv` — 1 files, 25052 bytes
- `reports/take_profit_backtest/tp_time_distribution.csv` — 1 files, 1008 bytes
- `reports/take_profit_backtest/tp_timed_exit_matrix.csv` — 1 files, 35081 bytes
- `reports/take_profit_backtest/weekday_weekend.csv` — 1 files, 3657 bytes
- `reports/weekend_liquidity/execution_level.csv` — 1 files, 86401 bytes
- `reports/weekend_liquidity/liquidity_regime.csv` — 1 files, 46369 bytes
- `reports/weekend_liquidity/market_level.csv` — 1 files, 96601 bytes
- `reports/weekend_liquidity/parameter_sensitivity.csv` — 1 files, 777 bytes
- `reports/weekend_liquidity/public_fetch_diagnostics.csv` — 1 files, 190374 bytes
- `reports/weekend_liquidity/summary.md` — 1 files, 4638 bytes
- `reports/weekend_liquidity/time_to_resolution.csv` — 1 files, 406 bytes
- `reports/weekend_liquidity/trade_level.csv` — 1 files, 16850 bytes
- `reports/weekend_liquidity/weekday_vs_weekend.csv` — 1 files, 1441 bytes
- `reports/weekend_liquidity/weekday_vs_weekend_utc.csv` — 1 files, 1207 bytes
- `reports/weekend_liquidity_public_study_8w/execution_level.csv` — 1 files, 42718 bytes
- `reports/weekend_liquidity_public_study_8w/liquidity_regime.csv` — 1 files, 26209 bytes
- `reports/weekend_liquidity_public_study_8w/market_level.csv` — 1 files, 85317 bytes
- `reports/weekend_liquidity_public_study_8w/parameter_sensitivity.csv` — 1 files, 769 bytes
- `reports/weekend_liquidity_public_study_8w/public_fetch_diagnostics.csv` — 1 files, 44536 bytes
- `reports/weekend_liquidity_public_study_8w/public_market_sample.csv` — 1 files, 44536 bytes
- `reports/weekend_liquidity_public_study_8w/summary.md` — 1 files, 6626 bytes
- `reports/weekend_liquidity_public_study_8w/time_to_resolution.csv` — 1 files, 849 bytes
- `reports/weekend_liquidity_public_study_8w/trade_level.csv` — 1 files, 11284 bytes
- `reports/weekend_liquidity_public_study_8w/weekday_vs_weekend.csv` — 1 files, 2187 bytes
- `reports/weekend_liquidity_public_study_8w/weekday_vs_weekend_utc.csv` — 1 files, 2093 bytes
- `reports/weekend_liquidity_public_study_8w/weekly_blocked_comparison.csv` — 1 files, 763 bytes
- `reports/weekend_liquidity_public_study_pilot/execution_level.csv` — 1 files, 42718 bytes
- `reports/weekend_liquidity_public_study_pilot/liquidity_regime.csv` — 1 files, 26210 bytes
- `reports/weekend_liquidity_public_study_pilot/market_level.csv` — 1 files, 57697 bytes
- `reports/weekend_liquidity_public_study_pilot/parameter_sensitivity.csv` — 1 files, 769 bytes
- `reports/weekend_liquidity_public_study_pilot/public_fetch_diagnostics.csv` — 1 files, 4995 bytes
- `reports/weekend_liquidity_public_study_pilot/public_market_sample.csv` — 1 files, 4995 bytes
- `reports/weekend_liquidity_public_study_pilot/summary.md` — 1 files, 5494 bytes
- `reports/weekend_liquidity_public_study_pilot/time_to_resolution.csv` — 1 files, 840 bytes
- `reports/weekend_liquidity_public_study_pilot/trade_level.csv` — 1 files, 11284 bytes
- `reports/weekend_liquidity_public_study_pilot/weekday_vs_weekend.csv` — 1 files, 2128 bytes
- `reports/weekend_liquidity_public_study_pilot/weekday_vs_weekend_utc.csv` — 1 files, 2012 bytes
- `scripts/shadow_feature_probe.py` — 1 files, 31440 bytes
- `scripts/shadow_probe_report.py` — 1 files, 11192 bytes
- `scripts/shadow_veto_report.py` — 1 files, 9983 bytes

### G — operational tooling (19 files)
- `scripts/archive_lead_lag_research.py` — 1 files, 5813 bytes
- `scripts/backfill_redeem_activity.py` — 1 files, 8992 bytes
- `scripts/check_allowance.py` — 1 files, 15572 bytes
- `scripts/check_positions_and_redeem.py` — 1 files, 36991 bytes
- `scripts/compact_research_db.py` — 1 files, 1257 bytes
- `scripts/inspect_env_contract.py` — 1 files, 3732 bytes
- `scripts/live_dashboard.py` — 1 files, 19415 bytes
- `scripts/migrate_env_to_profile.py` — 1 files, 4299 bytes
- `scripts/record_polymarket_l2.py` — 1 files, 14456 bytes
- `scripts/replay_journal_signals.py` — 1 files, 5383 bytes
- `scripts/replay_session_pnl_guard.py` — 1 files, 4414 bytes
- `scripts/research_daily_export.py` — 1 files, 3824 bytes
- `scripts/research_exit_retention.py` — 1 files, 3068 bytes
- `scripts/research_maintenance.py` — 1 files, 2296 bytes
- `scripts/research_partition.py` — 1 files, 5562 bytes
- `scripts/reset_session_pnl_guard.py` — 1 files, 4137 bytes
- `scripts/storage_maintenance.py` — 1 files, 1841 bytes
- `scripts/validate_btc_1s_history.py` — 1 files, 2945 bytes
- `scripts/verify_exit_order_semantics.py` — 1 files, 3549 bytes

Per-file classification, reasons, references, git state and sha256 are in the JSON manifest.

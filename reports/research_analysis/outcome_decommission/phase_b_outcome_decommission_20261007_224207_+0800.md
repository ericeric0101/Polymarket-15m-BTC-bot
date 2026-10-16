# PHASE B OUTCOME DECOMMISSION

Baseline: e4e87ee0eb43a620b6f914f165578c165ee7d9e8. Audit Taipei timestamp: 20261007_224207_+0800.

## Shutdown and historical DB preservation

Graceful Ctrl-C of PID48701 authorized by this task. Writers stopped22:29:50.852, final900s-policy forced backup succeeded22:29:56.669, node.run returned22:30:06.938, PTY exit0. No remaining run_bot PID or old DB open handle. No restart/push. Launcher no-rollover return message is the expected manual-stop exit, not an automatic rebuild.

Old DB bytes=4685426688 (4.3636GiB), mtime_ns=1791383388052134157, SHA256=778d85d13a53301e26b68aaf8a1ec46b45cd84d0862ba00b077262ebe4670439. Post-stop/pre-edit and post-patch hash/size/mtime identical. Tables=snapshots, sqlite_sequence, reference_1s, lead_lag_decisions, lead_lag_markouts, latency_spans. No historical mutation/archive/delete/migration. Normal old bot shutdown flush completed before this preservation baseline.

## Strategy independence before removal

|Authority|DEPENDS_ON_OUTCOME|Exact source authority|
|---|---|---|
|maker fair price|NO|bot/spot_pricer.py:_compute_fair_probability/_build_forecast_state; bot/forecast_state.py|
|maker side|NO|bot/signal_engine.py:SignalEngine; bot/side_decision.py:_compute_side_decision_new|
|economic qualification|NO|execution/maker_engine.py; run_bot.py:_evaluate_quote_targets|
|L2 freshness|NO|bot/pricing_runtime.py:_get_orderbook_levels_for_instrument; bot/market_runtime.py:handle_order_book_deltas|
|directional gate|NO|bot/quote_service.py:evaluate_buy_entry_controls; run_bot.py:_evaluate_quote_targets|
|strike distance|NO|bot/spot_pricer.py:_market_strike_is_entry_eligible and forecast|
|TWAP/strike crossing|NO|bot/spot_pricer.py:_polymarket_chainlink_ws_loop; bot/twap_forward_shadow.py|
|p_ex / sigma|NO|bot/forecast_state.py; bot/spot_pricer.py:_build_forecast_state|
|settlement side|NO|bot/lifecycle_runtime.py; bot/twap_forward_shadow.py official Chainlink settlement|
|invalidation/flip|NO|bot/side_decision.py; bot/exit_engine.py|
|stop|NO|bot/exit_engine.py:ExitPolicyEngine; run_bot.py stop guards|
|session risk/PnL|NO|bot/session_pnl_guard.py; bot/risk_policy.py; bot/db_runtime.py|
|rollover|NO|bot/launcher.py:run_integrated_bot; bot/ops.py|
|prediction|NO|bot/prediction_research_snapshot.py:PredictionResearchSnapshotter|

Outcome-only forecast/economic methods were reachable exclusively from retired FF callers, not maker. Maker calibration still reads maker journal evidence; removed FF calibration query had no maker result authority. No threshold/formula changes.

## Dependency graph before / proposed action

Before: settings -> observer websocket + OutcomeLeadLagRuntime + shadow -> legacy LeadLagDB; market/spot callbacks -> Outcome ingress; quote -> raw cross-venue snapshots; startup -> FF-specific calibration; order submit/cancel + trend/forward/smart -> mixed legacy DB.

Categorization is per referenced symbol; generic Polymarket binary outcome fields are shared market semantics, not the Hyperliquid venue. Historical scripts/reports/docs do not start producers. No unresolved UNKNOWN.

|File|Symbol|Category|Reference lines|Caller / data authority|Phase B action|
|---|---|---|---|---|---|
|README.md|module|F. HISTORICAL_ANALYSIS_ONLY|113,121|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|bot/app_config.py|AppConfig|A. OUTCOME_RUNTIME_REMOVE|578|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/app_config.py|__post_init__|A. OUTCOME_RUNTIME_REMOVE|551,553,555,557,559,561,564|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/app_config.py|from_env|A. OUTCOME_RUNTIME_REMOVE|1155,1156,1157,1158,1159,1160,1161,1162,1163,1164,1165,1166,1167,1168,1170,1172,1173,1174,1175,1176,1177,1179,1182,1185,1188|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/db_runtime.py|_apply_empirical_execution_penalty_calibration|B. FAST_FOLLOW_RUNTIME_REMOVE|368|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/db_runtime.py|_apply_fast_follow_execution_penalty_calibration|B. FAST_FOLLOW_RUNTIME_REMOVE|329,331,332,333,334,352,353,354,355,356|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/fast_follow_economics.py|evaluate_fast_follow_economics|F. HISTORICAL_ANALYSIS_ONLY|20|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|bot/hyperliquid_outcome_observer.py|__init__|A. OUTCOME_RUNTIME_REMOVE|80,81,82|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/hyperliquid_outcome_observer.py|_on_message|A. OUTCOME_RUNTIME_REMOVE|300,314|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/hyperliquid_outcome_observer.py|_set_market|A. OUTCOME_RUNTIME_REMOVE|104|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/hyperliquid_outcome_observer.py|module|A. OUTCOME_RUNTIME_REMOVE|19,26|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/hyperliquid_outcome_observer.py|start|A. OUTCOME_RUNTIME_REMOVE|116,118|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/lead_lag_observation.py|_lead_lag_observation_on_quote|C. OUTCOME_RESEARCH_REMOVE|106,107,126,129|quote callback; external Outcome plus cached reference|remove cross-venue producer; canonical prediction/BTC retain non-Outcome reference|
|bot/lead_lag_observation.py|_lead_lag_snapshot_payload|C. OUTCOME_RESEARCH_REMOVE|46,73,74,75,76,77,78,79,80,81,82,83,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99,100,101,102|quote callback; external Outcome plus cached reference|remove cross-venue producer; canonical prediction/BTC retain non-Outcome reference|
|bot/live_entry_research.py|edge_semantics|F. HISTORICAL_ANALYSIS_ONLY|171|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|bot/market_runtime.py|handle_stop|A. OUTCOME_RUNTIME_REMOVE|1216,1222,1297|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/market_runtime.py|module|A. OUTCOME_RUNTIME_REMOVE|21|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/order_events.py|handle_order_canceled|B. FAST_FOLLOW_RUNTIME_REMOVE|651,652,653|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/order_events.py|handle_order_canceled|E. NON_OUTCOME_RESEARCH_KEEP|655,656,657|maker submit/cancel or trend/forward/smart recorder; Binance/Chainlink/Polymarket|keep payload/cadence; existing TWAP writer|
|bot/order_events.py|handle_order_filled|B. FAST_FOLLOW_RUNTIME_REMOVE|143,144,145,146,174,176,177,179,180,181,187,189,190,201,202,204,347,348|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/order_events.py|handle_order_rejection_like_event|B. FAST_FOLLOW_RUNTIME_REMOVE|746,747,748|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/order_submission.py|submit_maker_quote|E. NON_OUTCOME_RESEARCH_KEEP|575,576,577|maker submit/cancel or trend/forward/smart recorder; Binance/Chainlink/Polymarket|keep payload/cadence; existing TWAP writer|
|bot/outcome_lead_lag_exit_handoff.py|_ensure_night_loaded|B. FAST_FOLLOW_RUNTIME_REMOVE|176|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|_observe_counterfactual_quote|B. FAST_FOLLOW_RUNTIME_REMOVE|363,374,398,425|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|_persist_night|B. FAST_FOLLOW_RUNTIME_REMOVE|232|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|_record_blocked|B. FAST_FOLLOW_RUNTIME_REMOVE|299,305|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|_venue_compatible_fast_follow_quantity|B. FAST_FOLLOW_RUNTIME_REMOVE|65|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|execution_enabled|B. FAST_FOLLOW_RUNTIME_REMOVE|141|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|fast_follow_l2_precheck|B. FAST_FOLLOW_RUNTIME_REMOVE|110|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|module|B. FAST_FOLLOW_RUNTIME_REMOVE|27|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|on_quote|B. FAST_FOLLOW_RUNTIME_REMOVE|530,577,590,602,637,742,775,796,807,811,812,813,824,843,849,860,865,888,915,926,941,946,950,956,960,988,993|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_exit_handoff.py|record_candidate|B. FAST_FOLLOW_RUNTIME_REMOVE|340|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/outcome_lead_lag_ingress.py|module|A. OUTCOME_RUNTIME_REMOVE|7|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_ingress.py|publish_strategy_tick|A. OUTCOME_RUNTIME_REMOVE|11,20|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_ingress.py|record_hyperliquid_btc_probe|A. OUTCOME_RUNTIME_REMOVE|33,38|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_runtime.py|module|A. OUTCOME_RUNTIME_REMOVE|10,11|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_shadow.py|on_tick|A. OUTCOME_RUNTIME_REMOVE|45|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_shadow.py|record_candidate|A. OUTCOME_RUNTIME_REMOVE|21|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/outcome_lead_lag_state.py|OutcomeLeadLagStateConfig|F. HISTORICAL_ANALYSIS_ONLY|13|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|bot/outcome_lead_lag_state.py|module|F. HISTORICAL_ANALYSIS_ONLY|8|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|bot/prediction_research_snapshot.py|module|A. OUTCOME_RUNTIME_REMOVE|5|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/recovery.py|_rebuild_inventory_state_from_recent_buy_submit|B. FAST_FOLLOW_RUNTIME_REMOVE|243|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/research/health.py|strategy_health|D. SHARED_INFRASTRUCTURE_KEEP|93|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|bot/research/provenance.py|module|D. SHARED_INFRASTRUCTURE_KEEP|20|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|bot/settings.py|build_twap_research_db|A. OUTCOME_RUNTIME_REMOVE|54,62|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/settings.py|initialize_strategy_settings|A. OUTCOME_RUNTIME_REMOVE|824,826,860,861,862,870,877,880,882,884,899,900,903,904,909|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/settings.py|initialize_strategy_settings|B. FAST_FOLLOW_RUNTIME_REMOVE|865,868,869,871|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/settings.py|initialize_strategy_settings|E. NON_OUTCOME_RESEARCH_KEEP|844,849,895|maker submit/cancel or trend/forward/smart recorder; Binance/Chainlink/Polymarket|keep payload/cadence; existing TWAP writer|
|bot/settings.py|module|A. OUTCOME_RUNTIME_REMOVE|32,38,39,40,42,43,44|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|bot/spot_pricer.py|_build_fast_follow_forecast_state|B. FAST_FOLLOW_RUNTIME_REMOVE|1664,1679,1690|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|bot/spot_pricer.py|module|A. OUTCOME_RUNTIME_REMOVE|43|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|config/profiles/btc15_twap_v3.env|module|A. OUTCOME_RUNTIME_REMOVE|153,154,155,156,157,158,161,162,163,165|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|config/profiles/btc15_twap_v3.env|module|B. FAST_FOLLOW_RUNTIME_REMOVE|166,167,168,171,174,175,176,177|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|docs/fast_follow_retirement_phase_a.md|module|F. HISTORICAL_ANALYSIS_ONLY|3,20,35,36|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|monitoring/lead_lag_db.py|LeadLagDB|D. SHARED_INFRASTRUCTURE_KEEP|16|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/lead_lag_db.py|__init__|D. SHARED_INFRASTRUCTURE_KEEP|21|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/lead_lag_db.py|_init_schema|D. SHARED_INFRASTRUCTURE_KEEP|85,90|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/lead_lag_db.py|_writer|D. SHARED_INFRASTRUCTURE_KEEP|190|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/lead_lag_db.py|enqueue_snapshot|D. SHARED_INFRASTRUCTURE_KEEP|124,126|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/outcome_lead_lag_replay.py|module|D. SHARED_INFRASTRUCTURE_KEEP|7,8|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/pnl_attribution.py|_entry_source|D. SHARED_INFRASTRUCTURE_KEEP|46,50|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/pnl_attribution.py|load_fast_follow_pnl_summary|D. SHARED_INFRASTRUCTURE_KEEP|184,193,203,209|research/journal clients; payload/schema/queue|keep shared writer/readers; require explicit path, no default old DB|
|monitoring/trade_journal_db.py|TradeJournalDB|F. HISTORICAL_ANALYSIS_ONLY|105,108|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|monitoring/trade_journal_db.py|load_fast_follow_buy_markout_calibration|F. HISTORICAL_ANALYSIS_ONLY|827,865|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|monitoring/trade_journal_db.py|load_fast_follow_night_risk|F. HISTORICAL_ANALYSIS_ONLY|732,751,776|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|monitoring/trade_journal_db.py|load_recent_buy_submits|F. HISTORICAL_ANALYSIS_ONLY|1732|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|project_overview.md|module|F. HISTORICAL_ANALYSIS_ONLY|135,173,206,228,235,244,252,265,395,402,404,433,435,447,454,480,1384,1772,1785,1788,1858,1874,1879,2018,2023,2035,2037,2045,2055,2060,2074,2093,2094,2101,2131,2133,2142,2204,2232,2261,2280,2283,2308,2611|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|reports/forward_shadow/summary.md|module|F. HISTORICAL_ANALYSIS_ONLY|3|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|reports/four_market_prediction_forensics/summary.md|module|F. HISTORICAL_ANALYSIS_ONLY|19|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|reports/research_analysis/engineering_audit/summary.md|module|F. HISTORICAL_ANALYSIS_ONLY|64,65,108|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|reports/research_analysis/engineering_readiness/summary.md|module|F. HISTORICAL_ANALYSIS_ONLY|74,75,93,117,119,227|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|run_bot.py|_build_live_entry_research_snapshot|B. FAST_FOLLOW_RUNTIME_REMOVE|421,422|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|run_bot.py|_emit_strategy_status|A. OUTCOME_RUNTIME_REMOVE|4673|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|run_bot.py|_emit_strategy_status|B. FAST_FOLLOW_RUNTIME_REMOVE|4670,4763|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|run_bot.py|_evaluate_quote_targets|E. NON_OUTCOME_RESEARCH_KEEP|3611|maker submit/cancel or trend/forward/smart recorder; Binance/Chainlink/Polymarket|keep payload/cadence; existing TWAP writer|
|run_bot.py|_loaded_source_fingerprint|A. OUTCOME_RUNTIME_REMOVE|60,62|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|run_bot.py|_log_fast_follow_economics_block_throttled|B. FAST_FOLLOW_RUNTIME_REMOVE|486,491,494,497|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|run_bot.py|fast_follow_execution_penalty_allows|B. FAST_FOLLOW_RUNTIME_REMOVE|520,528,529,539,540,541,542,552,555,561,572,581,582,593,599|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|run_bot.py|module|B. FAST_FOLLOW_RUNTIME_REMOVE|176|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|run_bot.py|on_start|A. OUTCOME_RUNTIME_REMOVE|3866,3881,3883,3886,3887|settings/start/quote/shutdown; external Outcome observer|remove live construction/hooks/config; no Outcome ingest|
|run_bot.py|on_start|B. FAST_FOLLOW_RUNTIME_REMOVE|3864,3865|FF-only execution/shadow; Outcome candidates|remove FF runtime/calibration, retain historical ledger compatibility|
|scripts/analyze_weekend_liquidity.py|generate_report|F. HISTORICAL_ANALYSIS_ONLY|1450|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/analyze_weekend_liquidity.py|load_local_journal|F. HISTORICAL_ANALYSIS_ONLY|377,378,385,391,392,407|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/analyze_weekend_liquidity.py|module|F. HISTORICAL_ANALYSIS_ONLY|40,41,54,55|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/archive_lead_lag_research.py|main|F. HISTORICAL_ANALYSIS_ONLY|116|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/backtest_profit_lock.py|run|F. HISTORICAL_ANALYSIS_ONLY|409,410|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/backtest_short_term_take_profit.py|_local_stoploss_conflicts|F. HISTORICAL_ANALYSIS_ONLY|161|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/compact_research_db.py|main|F. HISTORICAL_ANALYSIS_ONLY|11,14|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/execution_path_penalty_report.py|execution_path|F. HISTORICAL_ANALYSIS_ONLY|24|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/fast_follow_execution_report.py|build_report|F. HISTORICAL_ANALYSIS_ONLY|54,60|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/feed_health_report.py|build_report|F. HISTORICAL_ANALYSIS_ONLY|35,51|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/forward_shadow_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|322|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/four_market_prediction_forensics.py|_fast_follow_case|F. HISTORICAL_ANALYSIS_ONLY|369|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/four_market_prediction_forensics.py|analyze|F. HISTORICAL_ANALYSIS_ONLY|448,449,492|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/hyperliquid_outcome_lead_lag_report.py|load_snapshots|F. HISTORICAL_ANALYSIS_ONLY|54,60,63,64|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/hyperliquid_outcome_lead_lag_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|139|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/lead_lag_latency_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|16|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/live_entry_quality_report.py|_shadow_reject_counterfactual|F. HISTORICAL_ANALYSIS_ONLY|108|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/live_entry_quality_report.py|load_candidates|F. HISTORICAL_ANALYSIS_ONLY|135,136,190,209,210,344,395,400|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/outcome_fast_follow_pnl_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|20|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/outcome_fast_follow_pnl_report.py|module|F. HISTORICAL_ANALYSIS_ONLY|13|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/outcome_lead_lag_event_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|54|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/outcome_lead_lag_threshold_replay.py|main|F. HISTORICAL_ANALYSIS_ONLY|22|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/outcome_lead_lag_threshold_replay.py|module|F. HISTORICAL_ANALYSIS_ONLY|13|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/research_analysis.py|position_lifecycle_analysis|F. HISTORICAL_ANALYSIS_ONLY|951,1012|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/stop_forensics_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|14,18,19|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/trade_path_pnl_report.py|_path|F. HISTORICAL_ANALYSIS_ONLY|33,34|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/trade_path_pnl_report.py|build_report|F. HISTORICAL_ANALYSIS_ONLY|64,65,108,110,111,122,126|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|scripts/twap_forward_report.py|main|F. HISTORICAL_ANALYSIS_ONLY|165,166,167|offline tool/historical documentation/recovery parser|keep archived pure parser/types; explicit read-only historical input|
|tests/test_btc_trend_source.py|test_fast_follow_forecast_accepts_fresh_underlying_twap_and_exposes_source_age|G. TEST_FIXTURE_ONLY|88,92|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_btc_trend_source.py|test_fast_follow_forecast_rejects_stale_underlying_twap|G. TEST_FIXTURE_ONLY|79,83|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_env_contract.py|test_fast_follow_forecast_freshness_is_independently_configurable|G. TEST_FIXTURE_ONLY|57,58,62|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_execution_path_penalty_report.py|test_execution_path_classifies_fast_follow_and_taker_exit_before_liquidity|G. TEST_FIXTURE_ONLY|6,7|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_economics.py|module|G. TEST_FIXTURE_ONLY|3|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_economics.py|test_fast_follow_economics_fails_closed_without_empirical_penalty|G. TEST_FIXTURE_ONLY|22,23|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_economics.py|test_fast_follow_economics_requires_positive_resolution_ev_after_taker_fee_and_penalty|G. TEST_FIXTURE_ONLY|6,7,11|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_reports.py|module|G. TEST_FIXTURE_ONLY|4|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_reports.py|test_fast_follow_report_separates_fok_and_precision_rejections|G. TEST_FIXTURE_ONLY|14,23,26|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_reports.py|test_feed_health_report_summarizes_recovery_and_outcome_disconnects|G. TEST_FIXTURE_ONLY|43|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|module|G. TEST_FIXTURE_ONLY|10|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|owner_and_candidate|G. TEST_FIXTURE_ONLY|23|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|test_economics_reason_collision_preserves_block_reason_and_detail_once|G. TEST_FIXTURE_ONLY|63,65|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|test_neutral_l2_stamp_preserves_exact_maker_delivery_threshold|G. TEST_FIXTURE_ONLY|94|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|test_production_wiring_has_no_fast_follow_owner_dispatch_or_maker_gate|G. TEST_FIXTURE_ONLY|75,81,84|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_fast_follow_retirement.py|test_signal_cannot_reserve_own_block_or_submit_even_with_legacy_live_config|G. TEST_FIXTURE_ONLY|32,33,41|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_forward_shadow_report.py|module|G. TEST_FIXTURE_ONLY|4|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_forward_shadow_report.py|test_forward_shadow_report_writes_required_outputs_and_status|G. TEST_FIXTURE_ONLY|10|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_hyperliquid_outcome_observer.py|module|G. TEST_FIXTURE_ONLY|4,10|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_hyperliquid_outcome_observer.py|test_observer_emits_btc_bbo_and_l2book_to_shadow_probe_only|G. TEST_FIXTURE_ONLY|105,106|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|module|G. TEST_FIXTURE_ONLY|5,8,9|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_event_report_excludes_late_markouts_and_deduplicates_candidate_second|G. TEST_FIXTURE_ONLY|121|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_lead_lag_db_batches_raw_snapshots_and_flushes_on_stop|G. TEST_FIXTURE_ONLY|12,13,14,16,20,27|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_lead_lag_db_persists_compact_reference_decision_and_latency|G. TEST_FIXTURE_ONLY|63,65|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_manual_retention_archives_only_expired_raw_partitions_and_keeps_markouts|G. TEST_FIXTURE_ONLY|137|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_reference_compaction_uses_global_market_sentinel_for_null_market_id|G. TEST_FIXTURE_ONLY|80,89|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_report_loads_only_quality_gated_rows_from_dedicated_db|G. TEST_FIXTURE_ONLY|36,37,39,42,43,48,51,52|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_lead_lag_db.py|test_writer_explicitly_closes_each_batch_connection|G. TEST_FIXTURE_ONLY|93,113|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_entry_research.py|_make_journal|G. TEST_FIXTURE_ONLY|261,267,271|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_entry_research.py|test_complete_net_edge_requires_both_costs_and_maker_uses_its_own_method|G. TEST_FIXTURE_ONLY|147|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_entry_research.py|test_net_edge_stays_unavailable_when_either_cost_is_missing|G. TEST_FIXTURE_ONLY|134|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|__init__|G. TEST_FIXTURE_ONLY|5366,5376|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_external_lead_lag_observation_enqueues_raw_snapshots_only|G. TEST_FIXTURE_ONLY|5392,5393|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_fast_follow_cached_forecast_cannot_bypass_stale_current_source|G. TEST_FIXTURE_ONLY|166,174,175,176,181,182,186,189|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_fast_follow_economics_block_warnings_are_throttled|G. TEST_FIXTURE_ONLY|213,224,225,229|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_fast_follow_economics_uses_configured_forecast_freshness|G. TEST_FIXTURE_ONLY|131,144,145,149,152,153,155,156,159|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_fast_follow_refuses_missing_outcome_specific_execution_penalty|G. TEST_FIXTURE_ONLY|192,200,201,202,206,209,210|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_live_path_regressions.py|test_startup_rehydrate_uses_fast_follow_intent_only_for_confirmed_external_inventory|G. TEST_FIXTURE_ONLY|1985,2023,2035|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_replay.py|module|G. TEST_FIXTURE_ONLY|1|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|_live_harness|G. TEST_FIXTURE_ONLY|355|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|historical_fast_follow_execution_only|G. TEST_FIXTURE_ONLY|27,32,34|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|module|G. TEST_FIXTURE_ONLY|9,10,11,12,14,17,19,22,23|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|order_event|G. TEST_FIXTURE_ONLY|910|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|persist_event|G. TEST_FIXTURE_ONLY|874|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_entry_only_fast_follow_has_no_reversal_implementation|G. TEST_FIXTURE_ONLY|1078|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_entry_only_fast_follow_never_cancels_an_existing_order_owner|G. TEST_FIXTURE_ONLY|1082,1113|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_entry_only_fast_follow_never_sells_an_opposite_existing_position|G. TEST_FIXTURE_ONLY|1049|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_allows_weekday_daytime_and_uses_local_date_risk_bucket|G. TEST_FIXTURE_ONLY|400|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_blocks_weekend_but_keeps_candidate_observable|G. TEST_FIXTURE_ONLY|423,448|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_failures_do_not_leak_across_sequential_maker_markets|G. TEST_FIXTURE_ONLY|686|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_l2_precheck_requires_full_fill_and_buffer|G. TEST_FIXTURE_ONLY|623,624,633|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_quantity_keeps_valid_quantity_when_maker_amount_is_already_cents_aligned|G. TEST_FIXTURE_ONLY|760,761|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_quantity_uses_venue_maker_amount_grid_without_increasing_size|G. TEST_FIXTURE_ONLY|751,754|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_rechecks_session_immediately_before_reservation|G. TEST_FIXTURE_ONLY|452,485,486|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_records_executable_bbo_counterfactual_markouts_for_blocked_signal|G. TEST_FIXTURE_ONLY|598,601,614|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_requires_explicit_market_token_mapping|G. TEST_FIXTURE_ONLY|578,593|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_restores_open_position_ownership_and_credits_sell_after_restart|G. TEST_FIXTURE_ONLY|766,771,796|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_side_lock_independence_does_not_override_opposite_inventory_guard|G. TEST_FIXTURE_ONLY|556,573|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_signal_pending_does_not_claim_normal_maker_ownership|G. TEST_FIXTURE_ONLY|656|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_submit_exception_rolls_back_local_reservation|G. TEST_FIXTURE_ONLY|666,682|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_fast_follow_waits_for_signal_side_quote_and_ignores_maker_side_lock|G. TEST_FIXTURE_ONLY|521,540,543,551|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_historical_economics_rejection_accepts_payload_reason_without_typeerror|G. TEST_FIXTURE_ONLY|1122,1123,1135,1136|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_hyperliquid_btc_probe_is_persisted_without_publishing_a_signal_tick|G. TEST_FIXTURE_ONLY|214,220,222,223,227,228,229|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_legacy_order_event_signature_rejects_intent_without_submitting_fok|G. TEST_FIXTURE_ONLY|973,975,976|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_aborts_and_rolls_back_when_risk_state_persistence_fails|G. TEST_FIXTURE_ONLY|867,880,899|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_accepts_explicit_ready_runtime_health|G. TEST_FIXTURE_ONLY|1018|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_applies_execution_penalty_check_by_default_when_bypass_is_disabled|G. TEST_FIXTURE_ONLY|1026,1031|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_buy_fill_consumes_exactly_one_weekday_risk_day_slot|G. TEST_FIXTURE_ONLY|728|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_cached_night_does_not_bypass_runtime_journal_failure|G. TEST_FIXTURE_ONLY|841,848,863|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_durably_records_intent_before_venue_submit|G. TEST_FIXTURE_ONLY|981,1000,1001,1002|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_intent_persist_failure_aborts_and_rolls_back_before_submit|G. TEST_FIXTURE_ONLY|903,916,935|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_malformed_runtime_health_fails_closed|G. TEST_FIXTURE_ONLY|1006|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_never_buys_when_global_maker_kill_switch_is_on|G. TEST_FIXTURE_ONLY|799,817|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_never_buys_when_trade_journal_is_not_ready|G. TEST_FIXTURE_ONLY|821,837|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_persists_explicit_outcome_entry_source|G. TEST_FIXTURE_ONLY|498,502,504,506|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_records_candidate_to_quote_handoff_once|G. TEST_FIXTURE_ONLY|509,516|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_terminal_fok_failure_releases_reservation_without_counting_fill|G. TEST_FIXTURE_ONLY|643|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_uses_sellable_five_point_five_shares_above_threshold|G. TEST_FIXTURE_ONLY|491,495|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_live_fast_follow_uses_ten_shares_at_or_below_high_price_threshold|G. TEST_FIXTURE_ONLY|393|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_shadow_also_records_live_follower_confirmation_without_order_authority|G. TEST_FIXTURE_ONLY|302|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_shadow_records_actual_markout_timing_and_late_quality_flag|G. TEST_FIXTURE_ONLY|280|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_outcome_lead_lag_state.py|test_state_does_not_arm_fast_follow_when_outcome_return_interval_exceeds_limit|G. TEST_FIXTURE_ONLY|133|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_p2_engineering.py|test_health_failure_isolation_no_db_scan_and_stale_cache|G. TEST_FIXTURE_ONLY|147|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_p2_engineering.py|test_health_projection_never_probes_journal_and_writer_failure_no_buy_veto|G. TEST_FIXTURE_ONLY|277|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_pnl_attribution.py|module|G. TEST_FIXTURE_ONLY|7|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_pnl_attribution.py|test_fast_follow_pnl_summary_uses_only_source_pure_settled_trades|G. TEST_FIXTURE_ONLY|127,146,148,156|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|module|G. TEST_FIXTURE_ONLY|24|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|test_leadlag_connection_close_failure_invalidates_success|G. TEST_FIXTURE_ONLY|564|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|test_leadlag_rejects_all_enqueue_paths_after_stop|G. TEST_FIXTURE_ONLY|285,288|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|test_leadlag_timeout_and_known_queue_drop_report_failure|G. TEST_FIXTURE_ONLY|458|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|test_leadlag_write_failure_is_not_successful_drain|G. TEST_FIXTURE_ONLY|275|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_precommit_blockers.py|test_shutdown_stops_producers_before_shared_research_writer_and_reports_failure|G. TEST_FIXTURE_ONLY|437,438,439|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|module|G. TEST_FIXTURE_ONLY|7|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|stopped_writer|G. TEST_FIXTURE_ONLY|26|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|test_both_writers_persist_to_existing_journal_after_research_failure|G. TEST_FIXTURE_ONLY|278,279,290,291|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|test_diagnostic_binding_failure_does_not_break_writer_startup|G. TEST_FIXTURE_ONLY|347,352|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|test_no_diagnostics_for_success_and_shutdown_unchanged|G. TEST_FIXTURE_ONLY|264,266|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_research_writer_failure_telemetry.py|test_pragma_failure_uses_actual_connect_boundary|G. TEST_FIXTURE_ONLY|123|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_runtime_env.py|test_active_profile_uses_single_tick_outcome_debounce|G. TEST_FIXTURE_ONLY|133|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_recovery.py|test_fast_follow_calibration_never_uses_maker_snapshot_as_fallback|G. TEST_FIXTURE_ONLY|486,489,491,492|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_recovery.py|test_fast_follow_markout_calibration_excludes_maker_rows_and_deduplicates_markets|G. TEST_FIXTURE_ONLY|450,475,482|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_recovery.py|test_night_risk_query_error_returns_none_instead_of_zero_risk|G. TEST_FIXTURE_ONLY|208|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_recovery.py|test_production_shutdown_flushes_final_trade_journal_backup|G. TEST_FIXTURE_ONLY|153,154,155|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_recovery.py|test_strategy_order_event_accepts_explicit_instrument_id|G. TEST_FIXTURE_ONLY|267,269,271|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_serialization.py|test_journal_recovers_fast_follow_buy_submit_for_ghost_cost_basis|G. TEST_FIXTURE_ONLY|56,60|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_serialization.py|test_journal_recovers_latest_fast_follow_night_risk|G. TEST_FIXTURE_ONLY|30,32,35,38|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_journal_serialization.py|test_journal_recovers_new_fast_follow_filled_and_pending_risk|G. TEST_FIXTURE_ONLY|44,46,50|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_trade_path_pnl_report.py|test_report_separates_maker_and_outcome_pnl_and_entry_timing|G. TEST_FIXTURE_ONLY|27,28,29,49,50,51|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|
|tests/test_twap_forward_shadow.py|test_twap_research_uses_a_dedicated_writer_not_the_shared_lead_lag_db|G. TEST_FIXTURE_ONLY|200,208|synthetic test fixture|remove obsolete runtime tests; keep historical compatibility/core regression tests|

## Runtime graph after / DB ownership

settings -> one explicit TWAP LeadLagDB -> prediction/TWAP/stop + trend-entry + forward-shadow + smart-money trajectory + order handoff/cancel latency. Journal owns authoritative execution/accounting; BTC1s Parquet unchanged. No observer/runtime/shadow task or socket, no old DB writer/reader. No new runtime DB. Five retained producer paths moved; stop/prediction already used TWAP.

Old generic LeadLagDB schema names/reference/snapshot helpers stay for historical readers and existing tests; mandatory constructor path prevents accidental old DB creation. build_twap_research_db rejects old filename, symlink and hardlink to known historical DB. One shared queue/worker only; failure diagnostics and shutdown flush preserved. Historical DB remains archival candidate after separate runtime proof.

Existing TWAP size cap500MiB remains exceeded and unchanged. Moving low-volume required streams does not make that cap healthy. New post-Phase-B validation must verify all intended streams, inspect shared queue/errors, and confirm old DB no open handles and unchangedsize/mtime. No claims of live deployment validation in this patch.

## Files / configuration / removed modules

Runtime modules removed: observer, Outcome runtime, ingress, shadow, FastFollow live/exit handoff, cross-venue observation (6). Three pure state/types/economics modules moved to monitoring/legacy for offline replay. OutcomeLeadLagConfig24 fields removed; legacy OUTCOME_/HYPERLIQUID_/FAST_FOLLOW_ env keys names-only warning and no authority. Current profile Outcome settings removed.

Changed files (plus new docs, tests, monitoring/legacy):
```
M	README.md
M	bot/app_config.py
M	bot/db_runtime.py
D	bot/fast_follow_economics.py
D	bot/hyperliquid_outcome_observer.py
D	bot/lead_lag_observation.py
M	bot/live_entry_research.py
M	bot/market_runtime.py
M	bot/order_events.py
M	bot/order_runtime.py
M	bot/order_submission.py
D	bot/outcome_lead_lag_exit_handoff.py
D	bot/outcome_lead_lag_ingress.py
D	bot/outcome_lead_lag_runtime.py
D	bot/outcome_lead_lag_shadow.py
D	bot/outcome_lead_lag_state.py
D	bot/outcome_lead_lag_types.py
M	bot/research/health.py
M	bot/research/provenance.py
M	bot/settings.py
M	bot/spot_pricer.py
M	config/profiles/btc15_twap_v3.env
M	docs/fast_follow_retirement_phase_a.md
M	monitoring/lead_lag_db.py
M	monitoring/outcome_lead_lag_replay.py
M	project_overview.md
M	run_bot.py
M	scripts/archive_lead_lag_research.py
M	scripts/fast_follow_execution_report.py
M	scripts/forward_shadow_report.py
M	scripts/hyperliquid_outcome_lead_lag_report.py
M	scripts/lead_lag_latency_report.py
M	scripts/outcome_lead_lag_event_report.py
M	scripts/outcome_lead_lag_threshold_replay.py
M	scripts/stop_forensics_report.py
M	scripts/twap_forward_report.py
M	tests/test_btc_trend_source.py
M	tests/test_env_contract.py
M	tests/test_fast_follow_economics.py
M	tests/test_fast_follow_retirement.py
D	tests/test_hyperliquid_outcome_observer.py
M	tests/test_live_path_regressions.py
D	tests/test_outcome_lead_lag_state.py
M	tests/test_p2_engineering.py
M	tests/test_precommit_blockers.py
M	tests/test_runtime_env.py
M	tests/test_trade_journal_recovery.py
```

## Validation

Focused core/decommission historical suite:365 passed before final added hardlink test. Separate decommission/neutralL2/historical suite11 passed. Full final suite:
```
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 19%]
........................................................................ [ 26%]
........................................................................ [ 33%]
........................................................................ [ 39%]
........................................................................ [ 46%]
........................................................................ [ 52%]
........................................................................ [ 59%]
........................................................................ [ 66%]
........................................................................ [ 72%]
........................................................................ [ 79%]
........................................................................ [ 86%]
........................................................................ [ 92%]
........................................................................ [ 99%]
........                                                                 [100%]
=============================== warnings summary ===============================
tests/test_redeem_script.py::test_nonce_advanced_without_receipt_reconciles_zero_position_without_resend
  /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/.venv/lib/python3.12/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
1088 passed, 1 warning in 18.60s
```

Baseline1160 -> final1088:79 obsolete runtime tests retired,7 new Phase B cases added; pure historical replay/economics/report tests retained. Maker/neutralL2/missingL2/side/stop/prediction/TWAP/Binance/rollover/dynamicadmission/storage/900sbackup tests remain passing. git diff --check passed. Strategy defaults and required modules untouched except removed Outcome hooks and writer routing.

## Remaining static references

Functional runtime Outcome ingest/execution/producer references=0; unexplained=0. Raw strings below remain only for compatibility, historical research, documentation or shared generic schema/retired-config guard. Runtime recovery of historical FF fills is intentional accounting safety; it never opens the old research DB or creates FastFollow objects. Do not equate historical event labels with producer authority.

|File:line|Classification|Remaining reference|
|---|---|---|
|bot/app_config.py:537|SHARED_GENERIC|retired = sorted(key for key in os.environ if key.startswith(("OUTCOME_", "HYPERLIQUID_", "FAST_FOLLOW_")))|
|bot/live_entry_research.py:172|TEST_COMPAT|comparable = str(method) == "fast_follow_resolution_ev_minus_fee_minus_markout"|
|bot/order_events.py:173|TEST_COMPAT|filled_entry_mode = "fast_follow"|
|bot/order_events.py:175|TEST_COMPAT|"entry_mode": "fast_follow",|
|bot/order_events.py:176|TEST_COMPAT|"entry_source": "outcome_fast_follow",|
|bot/prediction_research_snapshot.py:5|SHARED_GENERIC|decision. Persistence uses the existing bounded asynchronous LeadLagDB queue.|
|bot/recovery.py:243|TEST_COMPAT|'ORDER_FAST_FOLLOW_INTENT', 'ORDER_FAST_FOLLOW_SUBMIT'|
|bot/settings.py:32|SHARED_GENERIC|from monitoring.lead_lag_db import LeadLagDB|
|bot/settings.py:48|SHARED_GENERIC|def build_twap_research_db() -> LeadLagDB:|
|bot/settings.py:51|SHARED_GENERIC|historical = Path(__file__).resolve().parents[1] / "data/research/hyperliquid_lead_lag.db"|
|bot/settings.py:53|SHARED_GENERIC|if path.resolve().name == "hyperliquid_lead_lag.db" or same_historical:|
|bot/settings.py:54|SHARED_GENERIC|raise ValueError("Historical Hyperliquid DB cannot be used as a runtime research store")|
|bot/settings.py:55|SHARED_GENERIC|return LeadLagDB(db_path=str(path))|
|docs/fast_follow_retirement_phase_a.md:7|COMMENT/DOC|Outcome / Hyperliquid ingestion, observational lead-lag research, shared LeadLagDB|
|docs/fast_follow_retirement_phase_a.md:13|COMMENT/DOC|Previously initialize_strategy_settings created OutcomeFastFollowLive for|
|docs/fast_follow_retirement_phase_a.md:24|COMMENT/DOC|FAST_FOLLOW_EXECUTION_ENABLED = False additionally guards the retained compatibility|
|docs/fast_follow_retirement_phase_a.md:39|COMMENT/DOC|STATUS reports fast_follow_exec=disabled. The durable run manifest records|
|docs/fast_follow_retirement_phase_a.md:40|COMMENT/DOC|fast_follow_execution_enabled=false and effective_outcome_lead_lag_mode, while|
|docs/outcome_decommission_phase_b.md:3|COMMENT/DOC|Outcome/Hyperliquid network ingestion, reference probes, cross-venue snapshots,|
|docs/outcome_decommission_phase_b.md:17|COMMENT/DOC|There is one asynchronous research writer. LeadLagDB remains a shared schema/|
|docs/outcome_decommission_phase_b.md:20|COMMENT/DOC|Hyperliquid filename, resolved symlink or a hardlink to the known historical DB.|
|docs/outcome_decommission_phase_b.md:38|COMMENT/DOC|The historical data/research/hyperliquid_lead_lag.db remains in place. After a|
|monitoring/lead_lag_db.py:19|SHARED_GENERIC|class LeadLagDB:|
|monitoring/legacy/fast_follow_economics.py:20|HISTORICAL_ONLY|def evaluate_fast_follow_economics(*, fair_price: Decimal, limit_price: Decimal,|
|monitoring/legacy/outcome_lead_lag_state.py:8|HISTORICAL_ONLY|from monitoring.legacy.outcome_lead_lag_types import LeadLagDecision, ReferenceTick|
|monitoring/legacy/outcome_lead_lag_state.py:13|HISTORICAL_ONLY|feature_version: str = "outcome_lead_lag_v2"|
|monitoring/legacy/outcome_lead_lag_state.py:29|HISTORICAL_ONLY|_SIGNAL_SOURCES = frozenset({"outcome_btc_mark", "polymarket_twap"})|
|monitoring/legacy/outcome_lead_lag_state.py:111|HISTORICAL_ONLY|tick.source == "outcome_btc_mark"|
|monitoring/legacy/outcome_lead_lag_state.py:124|HISTORICAL_ONLY|outcome, twap = self._latest.get("outcome_btc_mark"), self._latest.get("polymarket_twap")|
|monitoring/legacy/outcome_lead_lag_state.py:188|HISTORICAL_ONLY|outcome_return, outcome_interval_ms = self._return_for_window("outcome_btc_mark", outcome, 1_000)|
|monitoring/legacy/outcome_lead_lag_state.py:190|HISTORICAL_ONLY|(window, self._return_for_window("outcome_btc_mark", outcome, window)[0])|
|monitoring/legacy/outcome_lead_lag_state.py:207|HISTORICAL_ONLY|if tick.source != "outcome_btc_mark":|
|monitoring/legacy/outcome_lead_lag_types.py:8|HISTORICAL_ONLY|ReferenceSource = Literal["outcome_btc_mark", "polymarket_twap", "polymarket_spot", "binance", "polymarket_bbo"]|
|monitoring/outcome_lead_lag_replay.py:7|HISTORICAL_ONLY|from monitoring.legacy.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig|
|monitoring/outcome_lead_lag_replay.py:8|HISTORICAL_ONLY|from monitoring.legacy.outcome_lead_lag_types import ReferenceTick|
|monitoring/pnl_attribution.py:46|HISTORICAL_ONLY|if explicit in {"outcome_fast_follow", "normal_maker"}:|
|monitoring/pnl_attribution.py:50|HISTORICAL_ONLY|return "outcome_fast_follow"|
|monitoring/pnl_attribution.py:184|HISTORICAL_ONLY|def load_fast_follow_pnl_summary(db_path: str  /  Path) -> dict[str, Any]:|
|monitoring/pnl_attribution.py:193|HISTORICAL_ONLY|if row["entry_source"] == "outcome_fast_follow"|
|monitoring/pnl_attribution.py:203|HISTORICAL_ONLY|if row["entry_source"] == "outcome_fast_follow"|
|monitoring/pnl_attribution.py:209|HISTORICAL_ONLY|and "outcome_fast_follow" in row["entry_sources"]|
|monitoring/trade_journal_db.py:105|TEST_COMPAT|"ORDER_SUBMIT", "ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_FILLED",|
|monitoring/trade_journal_db.py:108|TEST_COMPAT|"FAST_FOLLOW_RISK_STATE", "MARKET_BUY_COUNT_UPDATED",|
|monitoring/trade_journal_db.py:732|TEST_COMPAT|def load_fast_follow_night_risk(self, night_key: str) -> Optional[Dict[str, Any]]:|
|monitoring/trade_journal_db.py:751|TEST_COMPAT|WHERE event_type='FAST_FOLLOW_RISK_STATE'|
|monitoring/trade_journal_db.py:776|TEST_COMPAT|logger.error(f"TradeJournalDB load_fast_follow_night_risk failed; fast-follow BUYs blocked: {e}")|
|monitoring/trade_journal_db.py:827|TEST_COMPAT|def load_fast_follow_buy_markout_calibration(self, *, lookback_hours: float,|
|monitoring/trade_journal_db.py:865|TEST_COMPAT|calibration["source"] = "outcome_fast_follow_taker_first_market"|
|monitoring/trade_journal_db.py:1732|TEST_COMPAT|'ORDER_FAST_FOLLOW_INTENT', 'ORDER_FAST_FOLLOW_SUBMIT'|
|project_overview.md:5|COMMENT/DOC|Outcome/Hyperliquid network ingestion and Fast Follow runtime have been retired.|
|project_overview.md:9|COMMENT/DOC|prediction/TWAP evidence. data/research/hyperliquid_lead_lag.db is historical-only;|
|project_overview.md:143|COMMENT/DOC|- **Prediction/research storage:** LeadLagDB is the asynchronous sparse-event|
|project_overview.md:214|COMMENT/DOC|outcome_fast_follow, including allow-to-intent, signal-to-intent,|
|project_overview.md:252|COMMENT/DOC|equivalent with ORDER_FAST_FOLLOW_INTENT. Fast-follow builds a current|
|project_overview.md:260|COMMENT/DOC|zero. The former OUTCOME_FAST_FOLLOW_EXECUTION_PENALTY_PER_SHARE static|
|project_overview.md:273|COMMENT/DOC|source_observed_ts, and source_age_sec; fast_follow_source_stale cannot|
|project_overview.md:403|COMMENT/DOC|- Fast-follow records a durable ORDER_FAST_FOLLOW_INTENT after risk-state|
|project_overview.md:410|COMMENT/DOC|- Every new fast-follow intent and fill carries entry_source=outcome_fast_follow;|
|project_overview.md:412|COMMENT/DOC|(scripts/outcome_fast_follow_pnl_report.py) reports Outcome PnL only for|
|project_overview.md:424|COMMENT/DOC|A research-only Hyperliquid BTC BBO/L2Book cadence probe is available but|
|project_overview.md:441|COMMENT/DOC|research-only FAST_FOLLOW_COUNTERFACTUAL_ENTRY at the first fresh|
|project_overview.md:443|COMMENT/DOC|FAST_FOLLOW_COUNTERFACTUAL_MARKOUT observations using that entry ask and|
|project_overview.md:455|COMMENT/DOC|- scripts/outcome_lead_lag_threshold_replay.py replays the already-persisted|
|project_overview.md:488|COMMENT/DOC|- Live entry candidate snapshots are embedded in ENTRY_DECISION_TRACE (normal maker) and FAST_FOLLOW_QUOTE_HANDOFF / blocked / durable intent records (Outcome fast-follow), keyed by research_candidate_id. Maker IDs now identify one decision episode by market, instrument, intended token side, and unique episode start; minor quote changes reuse that ID. Episodes terminate on submit, side invalidation, market rollover, or a 15-minute research TTL. Fast-follow retains its immutable signal-created ID. Normal-maker snapshots are deduplicated except for material changes (price tick, edge/distance/risk/leader/crossing/eligibility/size bucket) with a 0.5-second minimum interval and a 7-second heartbeat. Research counters are summarized at most once per minute; the instrumentation is bounded in memory and never decides whether a trade is allowed.|
|project_overview.md:1392|COMMENT/DOC| /  Supported operational/manual  /  inspect_env_contract.py, migrate_env_to_profile.py, check_allowance.py, check_positions_and_redeem.py, replay_journal_signals.py, pnl_attribution_report.py, execution_path_penalty_report.py, fast_follow_execution_report.py, feed_health_report.py, archive_lead_lag_research.py, invalidation_counterfactual_report.py, verify_exit_order_semantics.py, execution_penalty_report.py, twap_fair_calibration_report.py, fair_edge_bucket_shadow_report.py, executable_fair_edge_report.py, backfill_redeem_activity.py. Evidence: README/current docs or current audit docs refer to them.  / |
|project_overview.md:1779|COMMENT/DOC|Hyperliquid Outcome observer is **mainnet WebSocket-only**, using the explicit|
|project_overview.md:1780|COMMENT/DOC|active BTC daily outcome id (HYPERLIQUID_OUTCOME_DAILY_MARKET_ID, initially|
|project_overview.md:1793|COMMENT/DOC|five-second cross-market snapshots in its own logs/hyperliquid_lead_lag.db|
|project_overview.md:1796|COMMENT/DOC|scripts/hyperliquid_outcome_lead_lag_report.py has one primary research|
|project_overview.md:1810|COMMENT/DOC|**Outcome WebSocket reliability guard (2026-09-11):** Hyperliquid may close|
|project_overview.md:1866|COMMENT/DOC|observed markouts into maker_buy, fast_follow_fok_buy, taker_exit_sell|
|project_overview.md:1882|COMMENT/DOC|cover quantity × OUTCOME_FAST_FOLLOW_L2_DEPTH_BUFFER (default **1.20**);|
|project_overview.md:1887|COMMENT/DOC|and submit timestamps. scripts/fast_follow_execution_report.py separates|
|project_overview.md:2026|COMMENT/DOC|the configuration snapshot. Add bot/outcome_lead_lag_types.py with|
|project_overview.md:2043|COMMENT/DOC|bot/outcome_lead_lag_state.py for OutcomeLeadLagState.apply(tick), with|
|project_overview.md:2045|COMMENT/DOC|Add bot/outcome_lead_lag_runtime.py, owned and started/stopped by the|
|project_overview.md:2053|COMMENT/DOC|journal.** Extend monitoring/lead_lag_db.py (or split its raw writer into|
|project_overview.md:2063|COMMENT/DOC|bot/outcome_lead_lag_shadow.py to translate a state decision plus current|
|project_overview.md:2068|COMMENT/DOC|scripts/hyperliquid_outcome_lead_lag_report.py with OOS, regime,|
|project_overview.md:2082|COMMENT/DOC|feature-flagged bot/outcome_lead_lag_exit_handoff.py that maps a confirmed|
|project_overview.md:2101|COMMENT/DOC|outcome_lead_lag_types, outcome_lead_lag_state, outcome_lead_lag_runtime,|
|project_overview.md:2102|COMMENT/DOC|outcome_lead_lag_ingress, and outcome_lead_lag_shadow provide immutable|
|project_overview.md:2109|COMMENT/DOC|outcome_lead_lag_exit_handoff is an explicit disabled guard that always|
|project_overview.md:2115|COMMENT/DOC|**Outcome WS heartbeat repair (2026-09-03):** The Hyperliquid mainnet observer|
|project_overview.md:2139|COMMENT/DOC|The default feature version is outcome_lead_lag_v2; a new run must retain|
|project_overview.md:2141|COMMENT/DOC|scripts/outcome_lead_lag_event_report.py accepts only timely records and|
|project_overview.md:2150|COMMENT/DOC|under feature version outcome_lead_lag_v4_entry_only. It grants bounded BUY|
|project_overview.md:2172|COMMENT/DOC|outcome_btc_mark can build/arm the signal and only the settlement-authority|
|project_overview.md:2212|COMMENT/DOC|not ORDER_FAST_FOLLOW_SUBMIT. A later Down confirmation submitted a 0.74 FOK|
|project_overview.md:2240|COMMENT/DOC|entry-only excess is durable as FAST_FOLLOW_OVERFILL_ACCEPTED. If verified|
|project_overview.md:2269|COMMENT/DOC|Each blocked confirmed signal records one durable FAST_FOLLOW_ENTRY_BLOCKED|
|project_overview.md:2288|COMMENT/DOC|fast_follow=<filled>/<max> pending=<n> risk_day=<Taipei-calendar-date>. filled|
|project_overview.md:2291|COMMENT/DOC|are retained in FAST_FOLLOW_RISK_STATE for historical audit.|
|project_overview.md:2316|COMMENT/DOC|logs/hyperliquid_lead_lag.db, its final inventory was 10,039 stored five-|
|project_overview.md:2619|COMMENT/DOC|- Events use the existing asynchronous lead_lag_db writer. Research errors|
|reports/forward_shadow/summary.md:3|COMMENT/DOC|Research DB: data/research/hyperliquid_lead_lag.db|
|reports/four_market_prediction_forensics/summary.md:19|COMMENT/DOC|- **Q6 wrong DOWN fast-follow cause: YES — the existing ex-market and market variables already contradicted it.** At the candidate, p_up_ex_market had fallen from about 0.92 but remained 0.67–0.76, the fresh UP mid was 0.695/0.705, official TWAP state remained UP, and required move was still a negative UP-favoring path distance. The trigger was an Outcome/TWAP follower event, not independent DOWN confirmation. See fast_follow_case.csv.|
|reports/research_analysis/engineering_audit/summary.md:64|COMMENT/DOC| /  Snapshot capture  /  prediction_research_snapshot to LeadLagDB  /  Bounded asynchronous queue, no execution authority.  / |
|reports/research_analysis/engineering_audit/summary.md:65|COMMENT/DOC| /  Research persistence  /  monitoring LeadLagDB  /  SQLite decision/markout/reference records.  / |
|reports/research_analysis/engineering_audit/summary.md:108|COMMENT/DOC| /  Prediction/TWAP snapshots  /  LeadLagDB bounded queue  /  runtime stops producer/runtime then DB writer  /  terminal stop reports failure on timeout, known writer error or lost queued data; see corrected contract above.  / |
|reports/research_analysis/engineering_readiness/summary.md:74|COMMENT/DOC| /  Prediction evidence  /  PredictionResearchSnapshotter → LeadLagDB.lead_lag_decisions  /  Common-timestamp source/receive ages, freshness, p_ex, TWAP, BBO and BTC-return fields already exist. New snapshots now carry an explicit schema version.  / |
|reports/research_analysis/engineering_readiness/summary.md:75|COMMENT/DOC| /  Cross-market research  /  LeadLagObservationMixin → LeadLagDB  /  Existing 5-second snapshots and 1-second references are separate from prediction snapshots by purpose; they are not duplicate prediction authorities.  / |
|reports/research_analysis/engineering_readiness/summary.md:93|COMMENT/DOC| /  Integrity/health  /  ResearchStore.integrity, LeadLagDB.research_health, prediction metrics, BTC writer metrics, DataEngine telemetry, TWAP/BTC disk guards  /  Keep component owners. The report identifies the missing unified read-only health view; no duplicate health subsystem or new blocking gate was added.  / |
|reports/research_analysis/engineering_readiness/summary.md:117|COMMENT/DOC|bot/stop_forensics_shadow.py, monitoring/lead_lag_db.py,|
|reports/research_analysis/engineering_readiness/summary.md:119|COMMENT/DOC|tests/test_research_provenance.py, tests/test_lead_lag_db.py, and|
|reports/research_analysis/engineering_readiness/summary.md:227|COMMENT/DOC|- Research health is componentized: LeadLagDB.research_health() exposes|
|scripts/analyze_weekend_liquidity.py:40|HISTORICAL_ONLY|"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_SUBMIT",|
|scripts/analyze_weekend_liquidity.py:41|HISTORICAL_ONLY|"ORDER_FAST_FOLLOW_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT",|
|scripts/analyze_weekend_liquidity.py:54|HISTORICAL_ONLY|"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_SUBMIT",|
|scripts/analyze_weekend_liquidity.py:55|HISTORICAL_ONLY|"ORDER_FAST_FOLLOW_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT",|
|scripts/analyze_weekend_liquidity.py:377|HISTORICAL_ONLY|"ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT", "ORDER_MAKER_INTENT",|
|scripts/analyze_weekend_liquidity.py:378|HISTORICAL_ONLY|"ORDER_FAST_FOLLOW_INTENT", "ORDER_TAKER_EXIT_SUBMIT",|
|scripts/analyze_weekend_liquidity.py:385|HISTORICAL_ONLY|submitted = next((event for event in events if event["event_type"] in {"ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT"}), None)|
|scripts/analyze_weekend_liquidity.py:391|HISTORICAL_ONLY|if any(event["event_type"].startswith("ORDER_FAST_FOLLOW") for event in events):|
|scripts/analyze_weekend_liquidity.py:392|HISTORICAL_ONLY|entry_source = "outcome_fast_follow"|
|scripts/analyze_weekend_liquidity.py:407|HISTORICAL_ONLY|"order_type": "fast_follow" if "fast_follow" in entry_source else ("maker" if data.get("maker") else "other"),|
|scripts/analyze_weekend_liquidity.py:1450|HISTORICAL_ONLY|event["event_type"] in {"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT"}|
|scripts/compact_research_db.py:11|HISTORICAL_ONLY|parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")|
|scripts/compact_research_db.py:14|HISTORICAL_ONLY|if not path.is_file() and args.db == "data/research/hyperliquid_lead_lag.db": path = Path("logs/hyperliquid_lead_lag.db")|
|scripts/execution_path_penalty_report.py:24|HISTORICAL_ONLY|return "fast_follow_fok_buy"|
|scripts/fast_follow_execution_report.py:55|HISTORICAL_ONLY|AND event_type IN ('ORDER_FAST_FOLLOW_SUBMIT', 'ORDER_FILLED', 'ORDER_REJECTED', 'ORDER_DENIED')|
|scripts/fast_follow_execution_report.py:61|HISTORICAL_ONLY|if event_type == "ORDER_FAST_FOLLOW_SUBMIT":|
|scripts/feed_health_report.py:35|HISTORICAL_ONLY|OR event_type LIKE 'HYPERLIQUID_OUTCOME_OBSERVER_%'"""|
|scripts/feed_health_report.py:51|HISTORICAL_ONLY|elif event_type == "HYPERLIQUID_OUTCOME_OBSERVER_DISCONNECTED":|
|scripts/four_market_prediction_forensics.py:369|HISTORICAL_ONLY|def _fast_follow_case(timeline: list[dict[str, Any]], candidate_ts: float) -> list[dict[str, Any]]:|
|scripts/four_market_prediction_forensics.py:448|HISTORICAL_ONLY|fast_follow = _fast_follow_case(timelines.get("btc-updown-15m-1790987400", []), candidate_ts)|
|scripts/four_market_prediction_forensics.py:449|HISTORICAL_ONLY|_write_csv(output / "fast_follow_case.csv", fast_follow)|
|scripts/four_market_prediction_forensics.py:492|HISTORICAL_ONLY|- **Q6 wrong DOWN fast-follow cause: YES — the existing ex-market and market variables already contradicted it.** At the candidate, p_up_ex_market had fallen from about 0.92 but remained 0.67–0.76, the fresh UP mid was 0.695/0.705, official TWAP state remained UP, and required move was still a negative UP-favoring path distance. The trigger was an Outcome/TWAP follower event, not independent DOWN confirmation. See fast_follow_case.csv.|
|scripts/hyperliquid_outcome_lead_lag_report.py:5|HISTORICAL_ONLY|The research question is whether the BTC mark received from Hyperliquid's|
|scripts/hyperliquid_outcome_lead_lag_report.py:60|HISTORICAL_ONLY|outcome_mark = _number(payload.get("hyperliquid_outcome_btc_mark"))|
|scripts/hyperliquid_outcome_lead_lag_report.py:72|HISTORICAL_ONLY|"ts": float(observed_ts_ms) / 1000.0, "outcome_btc_mark": outcome_mark,|
|scripts/hyperliquid_outcome_lead_lag_report.py:128|HISTORICAL_ONLY|"outcome_btc_mark_to_polymarket_twap": _horizon_pairs(|
|scripts/hyperliquid_outcome_lead_lag_report.py:129|HISTORICAL_ONLY|groups, leader_key="outcome_btc_mark", follower_key="polymarket_twap", snapshot_interval_sec=snapshot_interval_sec,|
|scripts/live_entry_quality_report.py:108|HISTORICAL_ONLY|side=str(row.get("outcome_side") or (row.get("wanted_side") if row.get("entry_source")=="outcome_fast_follow" else "") or "").upper()|
|scripts/live_entry_quality_report.py:135|HISTORICAL_ONLY|strategy_rows=conn.execute("SELECT id,ts,event_type,payload_json FROM strategy_events WHERE event_type IN ('ENTRY_DECISION_TRACE','ENTRY_RESEARCH_CANDIDATE_TERMINAL','ENTRY_RESEARCH_TELEMETRY_SUMMARY','FAST_FOLLOW_QUOTE_HANDOFF','FAST_FOLLOW_ENTRY_BLOCKED') ORDER BY id").fetchall()|
|scripts/live_entry_quality_report.py:136|HISTORICAL_ONLY|order_rows=conn.execute("SELECT id,ts,event_type,client_order_id,side,price,qty,status,expected_net_usdc,payload_json FROM order_events WHERE event_type IN ('ORDER_MAKER_INTENT','ORDER_SUBMIT','ORDER_FAST_FOLLOW_INTENT','ORDER_FAST_FOLLOW_SUBMIT','ORDER_DRY_RUN_SUBMITTED','ORDER_FILLED','FILL_MARKOUT') ORDER BY id").fetchall()|
|scripts/live_entry_quality_report.py:190|HISTORICAL_ONLY|"entry_source":snap.get("entry_source") or ("outcome_fast_follow" if "FAST_FOLLOW" in row["event_type"] else "normal_maker")})|
|scripts/live_entry_quality_report.py:209|HISTORICAL_ONLY|if typ in {"ORDER_MAKER_INTENT","ORDER_SUBMIT","ORDER_FAST_FOLLOW_SUBMIT","ORDER_FAST_FOLLOW_INTENT","ORDER_DRY_RUN_SUBMITTED"}:|
|scripts/live_entry_quality_report.py:210|HISTORICAL_ONLY|actual_submit=typ in {"ORDER_SUBMIT","ORDER_FAST_FOLLOW_SUBMIT","ORDER_DRY_RUN_SUBMITTED"}|
|scripts/live_entry_quality_report.py:344|HISTORICAL_ONLY|method=("fast_follow_resolution_ev_minus_fee_minus_markout" if source=="outcome_fast_follow" else "maker_quote_economics"),|
|scripts/live_entry_quality_report.py:395|HISTORICAL_ONLY|r.get("entry_source")=="outcome_fast_follow"|
|scripts/live_entry_quality_report.py:400|HISTORICAL_ONLY|complete_edge_candidates=sum(bool(r.get("edge_cost_complete")) for r in candidates.values() if r.get("entry_source")=="outcome_fast_follow")|
|scripts/outcome_fast_follow_pnl_report.py:13|HISTORICAL_ONLY|from monitoring.pnl_attribution import load_fast_follow_pnl_summary|
|scripts/outcome_fast_follow_pnl_report.py:20|HISTORICAL_ONLY|summary = load_fast_follow_pnl_summary(Path(args.db))|
|scripts/outcome_lead_lag_threshold_replay.py:13|HISTORICAL_ONLY|from monitoring.outcome_lead_lag_replay import ReplayConfig, replay_rows|
|scripts/outcome_lead_lag_threshold_replay.py:34|HISTORICAL_ONLY|WHERE source IN ('outcome_btc_mark', 'polymarket_twap')|
|scripts/stop_forensics_report.py:18|HISTORICAL_ONLY|if not db_path.is_file() and args.db == "data/research/hyperliquid_lead_lag.db" and Path("logs/hyperliquid_lead_lag.db").is_file():|
|scripts/stop_forensics_report.py:19|HISTORICAL_ONLY|db_path = Path("logs/hyperliquid_lead_lag.db")|
|scripts/trade_path_pnl_report.py:33|HISTORICAL_ONLY|if source == "outcome_fast_follow" or str(order_id).startswith("BTC-15M-FAST-FOLLOW-BUY-"):|
|scripts/trade_path_pnl_report.py:34|HISTORICAL_ONLY|return "outcome_fast_follow"|
|scripts/trade_path_pnl_report.py:64|HISTORICAL_ONLY|'ORDER_MAKER_INTENT', 'ORDER_FAST_FOLLOW_INTENT', 'ORDER_SUBMIT',|
|scripts/trade_path_pnl_report.py:65|HISTORICAL_ONLY|'ORDER_FAST_FOLLOW_SUBMIT', 'ORDER_FILLED'|
|scripts/trade_path_pnl_report.py:108|HISTORICAL_ONLY|if event_type in {"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT"}:|
|scripts/trade_path_pnl_report.py:110|HISTORICAL_ONLY|route = "outcome_fast_follow" if event_type == "ORDER_FAST_FOLLOW_INTENT" else "normal_maker"|
|scripts/trade_path_pnl_report.py:111|HISTORICAL_ONLY|if event_type == "ORDER_FAST_FOLLOW_INTENT":|
|scripts/trade_path_pnl_report.py:122|HISTORICAL_ONLY|if event_type in {"ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT"}:|
|scripts/trade_path_pnl_report.py:126|HISTORICAL_ONLY|route = "outcome_fast_follow" if event_type == "ORDER_FAST_FOLLOW_SUBMIT" else "normal_maker"|
|scripts/twap_forward_report.py:165|HISTORICAL_ONLY|db = Path("data/research/hyperliquid_lead_lag.db")|
|scripts/twap_forward_report.py:166|HISTORICAL_ONLY|if not db.is_file() and args.db == "data/research/hyperliquid_lead_lag.db":|
|scripts/twap_forward_report.py:167|HISTORICAL_ONLY|db = Path("logs/hyperliquid_lead_lag.db")|
|tests/test_env_contract.py:58|TEST_COMPAT|monkeypatch.setenv("OUTCOME_FAST_FOLLOW_MAX_FORECAST_AGE_SEC", "4.5")|
|tests/test_env_contract.py:60|TEST_COMPAT|assert not hasattr(config, "outcome_lead_lag")|
|tests/test_execution_path_penalty_report.py:6|TEST_COMPAT|def test_execution_path_classifies_fast_follow_and_taker_exit_before_liquidity():|
|tests/test_execution_path_penalty_report.py:7|TEST_COMPAT|assert execution_path("BTC-15M-FAST-FOLLOW-BUY-1", "BUY", "taker") == "fast_follow_fok_buy"|
|tests/test_fast_follow_economics.py:3|TEST_COMPAT|from monitoring.legacy.fast_follow_economics import evaluate_fast_follow_economics|
|tests/test_fast_follow_economics.py:6|TEST_COMPAT|def test_fast_follow_economics_requires_positive_resolution_ev_after_taker_fee_and_penalty():|
|tests/test_fast_follow_economics.py:7|TEST_COMPAT|allowed = evaluate_fast_follow_economics(|
|tests/test_fast_follow_economics.py:11|TEST_COMPAT|blocked = evaluate_fast_follow_economics(|
|tests/test_fast_follow_economics.py:22|TEST_COMPAT|def test_fast_follow_economics_fails_closed_without_empirical_penalty():|
|tests/test_fast_follow_economics.py:23|TEST_COMPAT|result = evaluate_fast_follow_economics(|
|tests/test_fast_follow_reports.py:4|TEST_COMPAT|from scripts.fast_follow_execution_report import build_report, rejection_class|
|tests/test_fast_follow_reports.py:14|TEST_COMPAT|def test_fast_follow_report_separates_fok_and_precision_rejections(tmp_path):|
|tests/test_fast_follow_reports.py:23|TEST_COMPAT|_event(conn, "order_events", "ORDER_FAST_FOLLOW_SUBMIT", {"limit_price": 0.61}, client_order_id=order_id, side="BUY", price=.61, qty=10, reason=None)|
|tests/test_fast_follow_reports.py:26|TEST_COMPAT|_event(conn, "order_events", "ORDER_FAST_FOLLOW_SUBMIT", {}, client_order_id=other, side="BUY", price=.60, qty=10, reason=None)|
|tests/test_fast_follow_reports.py:43|TEST_COMPAT|_event(conn, "strategy_events", "HYPERLIQUID_OUTCOME_OBSERVER_DISCONNECTED", {"retry_delay_sec": 4, "error_type": "ConnectionClosedError"})|
|tests/test_fast_follow_retirement.py:20|TEST_COMPAT|assert not hasattr(host,'fast_follow_l2_update_ts_by_inst')|
|tests/test_forward_shadow_report.py:4|TEST_COMPAT|from monitoring.lead_lag_db import LeadLagDB|
|tests/test_forward_shadow_report.py:10|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:5|TEST_COMPAT|from monitoring.lead_lag_db import LeadLagDB|
|tests/test_lead_lag_db.py:8|TEST_COMPAT|from scripts.hyperliquid_outcome_lead_lag_report import load_snapshots|
|tests/test_lead_lag_db.py:9|TEST_COMPAT|from scripts.outcome_lead_lag_event_report import load_quality_gated_markouts, summarize|
|tests/test_lead_lag_db.py:12|TEST_COMPAT|def test_lead_lag_db_batches_raw_snapshots_and_flushes_on_stop(tmp_path):|
|tests/test_lead_lag_db.py:13|TEST_COMPAT|db_path = tmp_path / "hyperliquid_lead_lag.db"|
|tests/test_lead_lag_db.py:14|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:36|TEST_COMPAT|db_path = tmp_path / "hyperliquid_lead_lag.db"|
|tests/test_lead_lag_db.py:37|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:43|TEST_COMPAT|"twap_age_sec": 0.2, "hyperliquid_outcome_btc_mark": 100.0, "twap_price": 99.0,|
|tests/test_lead_lag_db.py:52|TEST_COMPAT|"twap_age_sec": 0.2, "hyperliquid_outcome_btc_mark": 101.0, "twap_price": 100.0,|
|tests/test_lead_lag_db.py:59|TEST_COMPAT|"ts": 100.0, "outcome_btc_mark": 100.0, "polymarket_twap": 99.0, "binance_price": 101.0,|
|tests/test_lead_lag_db.py:63|TEST_COMPAT|def test_lead_lag_db_persists_compact_reference_decision_and_latency(tmp_path):|
|tests/test_lead_lag_db.py:65|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:66|TEST_COMPAT|db.enqueue_reference_1s(run_id="r", slug="s", market_id=1, bucket_epoch_ms=1_000, source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=1_000_000_000)|
|tests/test_lead_lag_db.py:80|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:89|TEST_COMPAT|assert rows == [(LeadLagDB.GLOBAL_MARKET_ID, 7_700_100)]|
|tests/test_lead_lag_db.py:93|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "lead_lag.db"))|
|tests/test_lead_lag_db.py:121|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:137|TEST_COMPAT|db = LeadLagDB(str(db_path))|
|tests/test_lead_lag_db.py:142|TEST_COMPAT|source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=old_ns,|
|tests/test_lead_lag_db.py:146|TEST_COMPAT|source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=fresh_ns,|
|tests/test_live_entry_research.py:134|TEST_COMPAT|result = edge_semantics(probability=.8, entry_price=.7, method="fast_follow_resolution_ev_minus_fee_minus_markout", **kwargs)|
|tests/test_live_entry_research.py:147|TEST_COMPAT|execution_penalty_per_share=.02, method="fast_follow_resolution_ev_minus_fee_minus_markout")|
|tests/test_live_entry_research.py:261|TEST_COMPAT|snap = {"candidate_id": candidate_id, "slug": slug, "entry_source": "outcome_fast_follow",|
|tests/test_live_entry_research.py:267|TEST_COMPAT|conn.execute("INSERT INTO strategy_events VALUES(1, 1, 'FAST_FOLLOW_QUOTE_HANDOFF', ?)",|
|tests/test_live_entry_research.py:271|TEST_COMPAT|conn.execute("INSERT INTO order_events VALUES(1, 1.1, 'ORDER_FAST_FOLLOW_SUBMIT', 'coid', 'BUY', .82, 5, 'SUBMITTED', .2, ?)",|
|tests/test_live_path_regressions.py:1874|TEST_COMPAT|def test_startup_rehydrate_uses_fast_follow_intent_only_for_confirmed_external_inventory(tmp_path):|
|tests/test_live_path_regressions.py:1912|TEST_COMPAT|run_id="test", event_type="ORDER_FAST_FOLLOW_INTENT", side="BUY",|
|tests/test_live_path_regressions.py:1924|TEST_COMPAT|run_id="test", event_type="ORDER_FAST_FOLLOW_INTENT", side="BUY",|
|tests/test_outcome_decommission.py:12|TEST_COMPAT|from monitoring.lead_lag_db import LeadLagDB|
|tests/test_outcome_decommission.py:27|TEST_COMPAT|db=LeadLagDB(*args, **kwargs);created.append(db);return db|
|tests/test_outcome_decommission.py:28|TEST_COMPAT|monkeypatch.setattr(settings,'LeadLagDB',writer)|
|tests/test_outcome_decommission.py:37|TEST_COMPAT|for name in ('lead_lag_db','hyperliquid_outcome_observer','outcome_fast_follow_live',|
|tests/test_outcome_decommission.py:38|TEST_COMPAT|'outcome_lead_lag_runtime','outcome_lead_lag_shadow'):|
|tests/test_outcome_decommission.py:42|TEST_COMPAT|assert not list(tmp_path.rglob('hyperliquid_lead_lag.db'))|
|tests/test_outcome_decommission.py:43|TEST_COMPAT|assert not hasattr(h.app_config,'outcome_lead_lag')|
|tests/test_outcome_decommission.py:58|TEST_COMPAT|old=tmp_path/'hyperliquid_lead_lag.db';old.write_bytes(b'historical evidence')|
|tests/test_outcome_decommission.py:63|TEST_COMPAT|with pytest.raises(ValueError,match='Historical Hyperliquid'):|
|tests/test_outcome_decommission.py:69|TEST_COMPAT|with pytest.raises(TypeError):LeadLagDB()|
|tests/test_outcome_decommission.py:77|TEST_COMPAT|assert not any(name in source for name in ('OutcomeFastFollowLive','HyperliquidOutcomeObserver',|
|tests/test_outcome_decommission.py:79|TEST_COMPAT|assert not hasattr(IntegratedBTCStrategy,'fast_follow_execution_penalty_allows')|
|tests/test_outcome_decommission.py:80|TEST_COMPAT|assert not hasattr(IntegratedBTCStrategy,'_build_fast_follow_forecast_state')|
|tests/test_outcome_decommission.py:82|TEST_COMPAT|assert 'ORDER_FAST_FOLLOW' not in inspect.getsource(IntegratedBTCStrategy)|
|tests/test_outcome_decommission.py:83|TEST_COMPAT|assert 'outcome_fast_follow_live' not in inspect.getsource(order_events)|
|tests/test_outcome_decommission.py:88|TEST_COMPAT|from scripts.fast_follow_execution_report import build_report|
|tests/test_outcome_decommission.py:96|TEST_COMPAT|root=tmp_path/'repo';old=root/'data/research/hyperliquid_lead_lag.db'|
|tests/test_outcome_decommission.py:101|TEST_COMPAT|with pytest.raises(ValueError,match='Historical Hyperliquid'):settings.build_twap_research_db()|
|tests/test_outcome_lead_lag_replay.py:1|TEST_COMPAT|from monitoring.outcome_lead_lag_replay import ReplayConfig, replay_rows|
|tests/test_outcome_lead_lag_replay.py:7|TEST_COMPAT|("run", "slug", "outcome_btc_mark", 7_700_000, 100_000_000),|
|tests/test_outcome_lead_lag_replay.py:9|TEST_COMPAT|("run", "slug", "outcome_btc_mark", 7_700_600, 1_100_000_000),|
|tests/test_p2_engineering.py:147|TEST_COMPAT|lead_lag_db=SimpleNamespace(research_health=fail),|
|tests/test_pnl_attribution.py:7|TEST_COMPAT|load_fast_follow_pnl_summary,|
|tests/test_pnl_attribution.py:127|TEST_COMPAT|def test_fast_follow_pnl_summary_uses_only_source_pure_settled_trades(tmp_path):|
|tests/test_pnl_attribution.py:146|TEST_COMPAT|_event(conn, "order_events", "ORDER_FILLED", {"slug": open_slug, "entry_source": "outcome_fast_follow"},|
|tests/test_pnl_attribution.py:148|TEST_COMPAT|_event(conn, "order_events", "ORDER_FILLED", {"slug": mixed, "entry_source": "outcome_fast_follow"},|
|tests/test_pnl_attribution.py:156|TEST_COMPAT|summary = load_fast_follow_pnl_summary(db)|
|tests/test_precommit_blockers.py:24|TEST_COMPAT|from monitoring.lead_lag_db import LeadLagDB|
|tests/test_precommit_blockers.py:275|TEST_COMPAT|db = LeadLagDB(str(tmp_path / 'db.sqlite'))|
|tests/test_precommit_blockers.py:285|TEST_COMPAT|db = LeadLagDB(str(tmp_path / 'db.sqlite'))|
|tests/test_precommit_blockers.py:438|TEST_COMPAT|outcome_lead_lag_runtime=SimpleNamespace(stop=lambda: calls.append("runtime")),|
|tests/test_precommit_blockers.py:439|TEST_COMPAT|lead_lag_db=writer, twap_research_db=writer, smart_money_tracker=None,|
|tests/test_precommit_blockers.py:459|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "writer.db"))|
|tests/test_precommit_blockers.py:565|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "db.sqlite"))|
|tests/test_research_writer_failure_telemetry.py:7|TEST_COMPAT|from monitoring.lead_lag_db import LeadLagDB|
|tests/test_research_writer_failure_telemetry.py:26|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "research.db"))|
|tests/test_research_writer_failure_telemetry.py:123|TEST_COMPAT|monkeypatch.setattr("monitoring.lead_lag_db.sqlite3.connect", lambda *a, **k: conn)|
|tests/test_research_writer_failure_telemetry.py:264|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "research.db"))|
|tests/test_research_writer_failure_telemetry.py:266|TEST_COMPAT|db.set_failure_diagnostics(journal=j, writer_name="lead_lag_db", run_id="r")|
|tests/test_research_writer_failure_telemetry.py:278|TEST_COMPAT|for name, target in [("lead_lag_db", "hyperliquid_lead_lag.db"), ("twap_research_db", "twap_forward_shadow.db")]:|
|tests/test_research_writer_failure_telemetry.py:279|TEST_COMPAT|db = LeadLagDB(str(tmp_path / target))|
|tests/test_research_writer_failure_telemetry.py:290|TEST_COMPAT|assert {p["writer_name"] for p in payloads} == {"lead_lag_db", "twap_research_db"}|
|tests/test_research_writer_failure_telemetry.py:291|TEST_COMPAT|assert {p["db_target"] for p in payloads} == {"hyperliquid_lead_lag.db", "twap_forward_shadow.db"}|
|tests/test_research_writer_failure_telemetry.py:347|TEST_COMPAT|db = LeadLagDB(str(tmp_path / "research.db"))|
|tests/test_research_writer_failure_telemetry.py:352|TEST_COMPAT|db.set_failure_diagnostics(journal=UnavailableJournal(), writer_name="lead_lag_db", run_id="r")|
|tests/test_trade_journal_recovery.py:154|TEST_COMPAT|outcome_lead_lag_runtime=None,|
|tests/test_trade_journal_recovery.py:155|TEST_COMPAT|lead_lag_db=None,|
|tests/test_trade_journal_recovery.py:208|TEST_COMPAT|assert db.load_fast_follow_night_risk("2026-09-08") is None|
|tests/test_trade_journal_recovery.py:267|TEST_COMPAT|event_type="ORDER_FAST_FOLLOW_INTENT",|
|tests/test_trade_journal_recovery.py:269|TEST_COMPAT|instrument_id="FAST_FOLLOW.INST",|
|tests/test_trade_journal_recovery.py:271|TEST_COMPAT|assert captured[0]["instrument_id"] == "FAST_FOLLOW.INST"|
|tests/test_trade_journal_recovery.py:450|TEST_COMPAT|def test_fast_follow_markout_calibration_excludes_maker_rows_and_deduplicates_markets(tmp_path):|
|tests/test_trade_journal_recovery.py:475|TEST_COMPAT|calibration = db.load_fast_follow_buy_markout_calibration(|
|tests/test_trade_journal_recovery.py:482|TEST_COMPAT|assert calibration["source"] == "outcome_fast_follow_taker_first_market"|
|tests/test_trade_journal_serialization.py:30|TEST_COMPAT|def test_journal_recovers_latest_fast_follow_night_risk(tmp_path):|
|tests/test_trade_journal_serialization.py:32|TEST_COMPAT|db.log_strategy_event("run-1", "FAST_FOLLOW_RISK_STATE", {|
|tests/test_trade_journal_serialization.py:35|TEST_COMPAT|db.log_strategy_event("run-2", "FAST_FOLLOW_RISK_STATE", {|
|tests/test_trade_journal_serialization.py:38|TEST_COMPAT|assert db.load_fast_follow_night_risk("2026-09-08") == {|
|tests/test_trade_journal_serialization.py:44|TEST_COMPAT|def test_journal_recovers_new_fast_follow_filled_and_pending_risk(tmp_path):|
|tests/test_trade_journal_serialization.py:46|TEST_COMPAT|db.log_strategy_event("run", "FAST_FOLLOW_RISK_STATE", {|
|tests/test_trade_journal_serialization.py:50|TEST_COMPAT|assert db.load_fast_follow_night_risk("2026-09-08") == {|
|tests/test_trade_journal_serialization.py:56|TEST_COMPAT|def test_journal_recovers_fast_follow_buy_submit_for_ghost_cost_basis(tmp_path):|
|tests/test_trade_journal_serialization.py:60|TEST_COMPAT|"run", "ORDER_FAST_FOLLOW_SUBMIT", side="BUY", price=0.67, qty=10,|
|tests/test_trade_path_pnl_report.py:27|TEST_COMPAT|(5, "2026-09-25T13:30:00+08:00", "ORDER_FAST_FOLLOW_INTENT", "fok-1", "BUY", .8, 5, {"slug": "btc-updown-15m-1790314200", "signal_age_ms": 98}, "down-token"),|
|tests/test_trade_path_pnl_report.py:28|TEST_COMPAT|(6, "2026-09-25T13:30:01+08:00", "ORDER_FAST_FOLLOW_SUBMIT", "fok-1", "BUY", .8, 5, {}, "down-token"),|
|tests/test_trade_path_pnl_report.py:29|TEST_COMPAT|(7, "2026-09-25T13:30:02+08:00", "ORDER_FILLED", "fok-1", "BUY", .8, 5, {"entry_source": "outcome_fast_follow", "slug": "btc-updown-15m-1790314200"}, "down-token"),|
|tests/test_trade_path_pnl_report.py:49|TEST_COMPAT|assert report["by_path"]["outcome_fast_follow"]["filled_buy_events"] == 1|
|tests/test_trade_path_pnl_report.py:50|TEST_COMPAT|assert report["by_path"]["outcome_fast_follow"]["realized_net_usdc"] == 0|
|tests/test_trade_path_pnl_report.py:51|TEST_COMPAT|assert report["by_path"]["outcome_fast_follow"]["mean_signal_to_intent_ms"] == 98|
|tests/test_twap_forward_shadow.py:200|TEST_COMPAT|def test_twap_research_uses_a_dedicated_writer_not_the_shared_lead_lag_db(monkeypatch, tmp_path):|

## Commit / deployment boundary

One local commit only if validation and staging clean. No push. Bot remains stopped. SAFE_FOR_POST_PHASE_B_DRY_RUN_VALIDATION=YES; a manual/authorized restart and preservation/open-handle check are still required before historical archival.

Local commit: 33896ce9ba123708999c896bd7c2fd4023412d5f
Tracked tree clean; generated report remains untracked.

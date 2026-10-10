# MONDAY COLLECTION INTERRUPTION FORENSICS

Snapshot scope: read-only analysis of the existing offline snapshot `data/analysis_snapshots/20261005_185454_+0800/`; no new regime comparison was run. Frozen checkout HEAD is `c8eeaaacb660a32fd1be305115ed31fd1c67c5fa`. The process's loaded source revision is not recorded, so current source and actual runtime are kept separate.

## 1. Executive diagnosis

The 30 `INTERRUPTED` slots are not harmless clean-boundary artifacts: all 30 have multiple strategy `run_id`s and a real largest snapshot gap of at least 60 seconds; the observed range is 104.5–466.6s (median 252.5s), and 29/30 are at least 120s. The later 27 have gaps 172.1–466.6s (median 268.2s; P90 371.1s). The multi-run label is too coarse as a *cause*, but it does not invent the material data loss in these markets.

The strongest quantitative correlate is a much higher strategy-instance/run turnover rate after the morning cutoff: 10 starts early versus 46 later; median recorded strategy runtime fell from 40.9 to 11.7 minutes. The run journal measures stop-to-next-strategy-start at median 90.1s, P90 97.5s and max 243.3s. These are strategy/run boundaries, not proof of Python process restarts. Available evidence lacks exact stop-source or process PID, so it cannot distinguish stale-instrument lifecycle rollover from watchdog, discovery retry or process restart for every boundary.

A second real issue is the TWAP research DB repeatedly exceeding its 500 MB configured guard. There are 55 recorded guard events, all `db_size_cap`, from 2026-10-05T02:25:56+08:00 to 2026-10-05T18:44:27+08:00; measured size rose from 500.0 to 581.3 MB, while observed free disk stayed at least 15.9 GB. Source inspection says this suppresses optional TWAP shadow material after the guard, but does not gate `PREDICTION_RESEARCH_SNAPSHOT` enqueue. Prediction rows persist on both sides of long gaps, so this is not evidence of prediction-writer failure.

Primary verdict: **MULTIPLE_CAUSES**, with high confidence for real run-boundary and same-run capture gaps plus storage-guard activation; low confidence for exact lifecycle triggers because Oct 5 rollover/watchdog logs are absent. Hourly rollover alone cannot explain the late pattern.

## 2. run_id semantics

`bot/settings.py` creates `strategy.run_id = run_<epoch>_<uuid>` during strategy setup. `bot/launcher.py::_build_node_for_cycle` creates a fresh `IntegratedBTCStrategy` for each rebuilt Nautilus `TradingNode`, so an internal node cycle creates a new run ID inside the same launcher process. A top-level process restart also creates a new strategy/run ID. Therefore a `run_id` transition proves a new strategy instance, **not** an OS/Python process restart.

The journal has 56 `strategy_runs` started during Monday 00:00–18:54 Taipei; 55 have an end time and one remained open at snapshot cutoff. The prediction rows also reference 56 run IDs, but the sets differ: one run started before midnight contributes to Monday 00:00 data, while one short run started Monday (`run_1791154036_1dffb3b0`, 06:49:27–06:50:02, selected 06:45 market) has no persisted prediction snapshots. The journal has no PID or process-start identity. Top-level process starts and exact internal-node-cycle count cannot be separated from this evidence.

## 3. Scheduled hourly rollover behavior

Current source defaults: `AUTO_NODE_ROLLOVER_ENABLED=1` unless explicitly false; scheduled interval 3600s; cooldown 3s; max consecutive failures 5. Current source also uses market-discovery retry 15s, node engine disconnect wait 15s, client disconnect-task drain 60s, cancellation drain 5s. The actual runtime environment may override the default, and the process's imported revision/configuration is unavailable.

Five recorded strategy runtimes are compatible with an hourly timer (four cluster around 3535–3542s; one is 3274s and ambiguous). In the market-level cross-run gap pairs, one transition matches a 3535s candidate and one matches the ambiguous 3274s candidate. That is **1 strong / up to 2 plausible** interrupted-market overlaps out of 30, not 27. Exact scheduled stop times and expected-slot intersections cannot be certified without dated rollover logs. The other 28–29 interrupted markets need another explanation; “cross-run” is only an observation.

Lifecycle diagram from current source:

`RUN -> [3600s auto timer OR strategy lifecycle stale-instrument request OR quote-watchdog recovery OR market-discovery unavailable/retry OR exception/unrequested node return] -> STOP REQUEST -> strategy shutdown -> research writer stop -> node.run returns -> client disconnect task drain/cancel -> dispose -> engine disconnect check -> fresh event loop -> market discovery/build -> next node -> prediction capture resumes`.

The quote watchdog can first resubscribe in place; only exhausted recovery requests a node rollover. Strategy lifecycle requests a rollover after waiting-phase search reaches three misses, with `MARKET_NEXT_POLL_SEC` defaulting to 15s. Discovery unavailable waits 15s before retry. An exception is logged and consumes the failure budget; the source stops after 5 consecutive failures. These are source possibilities, not proven triggers for each run.

## 4. Process restart vs internal node rollover

The available `logs/bot/terminal_bot.log` ends on 2026-04-18 and has no Oct 5 startup/cycle lines; other bot logs are empty or unrelated. The Oct 5 `strategy_events` table has no lifecycle/watchdog/shutdown rows in this window, while `strategy_runs` has run records. A read-only `pgrep` request was rejected by host process-list permissions. The default `/tmp/polymarket-btc-15m-live.lock` is absent; that does not exclude a custom lock path or simulation process.

`TOP_LEVEL_PROCESS_STARTS`: **NOT DETERMINABLE**. `INTERNAL_NODE_CYCLES`: **56 strategy-instance/run starts observed; process restarts cannot be subtracted**. Current checkout HEAD is the frozen hash, but actual running code revision is **UNVERIFIED**.

## 5. Root causes of 30 INTERRUPTED markets

The interim cohort audit used `run_count>1` as the `INTERRUPTED` slot rule. The repository's generic `ResearchStore.get_market_coverage()` is broader: it labels a market interrupted when either run count >1 **or** a snapshot gap exceeds 15s. In this Monday slot audit all 30 cross-run markets have actual substantial gaps, so none is an `EXPECTED_ROLLOVER_CONTINUOUS` false positive. The observed median largest gap is 252.5s and 8/30 have T-300. Per-market run IDs, gap, coverage, freshness, checkpoint and diagnostic code are in `root_cause_by_market.csv`.

Exact triggers cannot be recovered because dated Oct 5 logs and journal lifecycle events are absent. No market is labelled a watchdog/discovery/crash cause without direct evidence. One strong hourly-compatible transition matches a market; the remaining gaps align with short strategy runs and need lifecycle telemetry for exact attribution.

## 6. Root causes of 27/48 later interruptions

All 27 later `INTERRUPTED` slots are cross-run and have a measured snapshot hole: median 268.2s, P90 371.1s, range 172.1–466.6s; 8 are ≥300s. The run journal shows 46 strategy instances later, median lifetime 702.2s, including 30 lifetimes under 15 minutes. Frequent node/strategy turnover plus restart/discovery gaps is the best-supported explanation; hourly rollover is not sufficient.

Cause-by-market grouping is `RUN_BOUNDARY / TRIGGER_UNKNOWN` for all 27. Exact gap, coverage and T-300 details are in the CSV. This does not imply Polymarket failed to list those markets.

## 7. Large-gap analysis

From prediction payload timestamps there are 104 individual gaps ≥30s, 65 ≥60s, 51 ≥120s, and 10 ≥300s across 74 markets with any prediction row. At ≥120s, 38 cross a run ID and 13 occur within one run; at ≥300s the split is 9 cross-run / 1 same-run.

Largest observed gap: 466.6s on `btc-updown-15m-1791172800`, 12:06:20–12:14:07 Taipei, crossing `run_1791172967_4ba1fd57` → `run_1791173579_1cb7be31`. `large_gaps.csv` includes every ≥30s gap, adjacent run IDs, and the last/next BTC/TWAP/market quote freshness flags. Rows on both sides prove persistence before/after, not writer activity during the hole. The 13 same-run ≥120s gaps mean node rebuilds cannot explain every gap.

## 8. Research writer health

The snapshot contains 26,243 Monday prediction rows; captures continue after the DB guard. One 35.7-second strategy run (`run_1791154036_1dffb3b0`) has a `strategy_runs` row but zero prediction rows. That proves a brief collector-silent strategy instance, not whether it exited before capture or the enqueue/writer failed. The 55 guard events indicate DB-size cap activation, not queue-full or SQLite-write error. Source semantics: `TwapForwardShadow._persist()` suppresses optional TWAP events after `_storage_guard_triggered`, except `MARKET_TWAP_SUMMARY`; `PredictionResearchSnapshotter.capture()` enqueues directly and keeps accepted/dropped/errors only in in-memory counters/log metrics. Those counters and queue depth are not persisted in this snapshot. Writer backpressure, DB lock errors or drops during gaps **cannot be ruled out but are not evidenced**. Persisted large holes line up more directly with run boundaries.

## 9. Feed freshness decomposition

Percent fresh among observed prediction rows (not wall-clock coverage):

| Component | Early true | Later true |
|---|---:|---:|
| BTC | 69.82% | 63.08% |
| TWAP/reference | 99.81% | 99.87% |
| Polymarket UP quote | 48.88% | 52.38% |
| Polymarket DOWN quote | 49.23% | 52.36% |
| p_ex | 95.10% | 96.22% |
| sigma | 95.11% | 96.24% |
| Joint | 52.32% | 50.84% |

Among captured rows, joint freshness is visibly constrained by market quotes (~50–52%) and BTC (~63–70%); TWAP and p_ex are usually fresh (>95%). Joint freshness shifts modestly (52.32% → 50.84%), so it does not explain the near-halving in snapshot counts. No feed packet telemetry exists for missing intervals.

## 10. Watchdog activity

Watchdog trigger count, timestamps and recovery-to-snapshot latency are **NOT MEASURABLE**. The source watchdog resubscribes first and can escalate only after recovery exhaustion; October logs and lifecycle telemetry that would prove the sequence are missing. “Zero triggers” is not supported.

## 11. Market discovery activity

Source default retry is 15s when no usable Gamma slug/token IDs resolve. No dated Oct 5 discovery retry, Gamma gap, missing-token or instrument-resolution log is available, so counts/durations cannot be established. All 14 `OTHER` slots have prediction rows but no stored `MARKET_TWAP_SUMMARY`; two missing slots have neither predictions nor summary in this snapshot. This is local collection absence, not proof of listing failure.

## 12. Shutdown/disconnect latency

`strategy_runs.ended_at` is written during strategy shutdown before writer/client/node teardown completes. From that recorded stop to the next strategy start, median elapsed time is 90.1s, P90 97.5s, maximum 243.3s. This combines shutdown and discovery/build and is not an exact node.run-return/dispose measurement. It proves material recovery downtime but cannot isolate whether the 60s client drain or 15s engine wait was used. No pending task, timeout, event-loop-closed, OOM, CPU, descriptor or disk-full evidence exists in the available Oct data. The DB guard is separate from free disk (minimum observed 15.9 GB).

## 13. Explanation of OTHER markets

All 14 are `PREDICTION_ONLY_NO_MARKET_TWAP_SUMMARY`. The 09:30 slot has only 16 prediction rows and also has insufficient timeline; some others have >1 run and measurable gaps despite `OTHER`. The absent summary is why these do not enter the canonical summary/timeline cohort. The 06:30 and 09:15 slots are `NO_PREDICTION_OR_SUMMARY_IN_SNAPSHOT`; 18:45 was active/unsettled and appropriately excluded from terminal outcomes. The per-market CSV includes the reason and row counts.

## 14. Current classifier artifact check

Any multi-run market was promoted to `INTERRUPTED`, which can conflate cause with quality in principle. Here all 30 have actual gaps ≥104.5s; none is a clean/continuous cross-run false positive. Conversely, five matched markets have same-run gaps ≥120s but were not called `INTERRUPTED` by the interim run-count shortcut; the generic ResearchStore rule catches >15s gaps, but that full rule was not used by the custom slot table. The 30 labels are not false positives for data loss, though the shortcut can miss single-run gaps and it does not identify cause.

## 15. Quality-based reclassification

Using a diagnostic 120s material-gap threshold (analysis-only, not a strategy threshold), while retaining settlement/timeline coverage, joint freshness and T-300 requirements:

| Classification | Current | Quality-based |
|---|---:|---:|
| Synchronized usable | 23 | 21 |
| Synchronized low quality | 6 | 3 |
| Interrupted / major gap | 30 | 35 |
| Other + missing unresolved | 16 | 16 |

Quality-based usable count falls because the timestamps also reveal five single-run markets with ≥120s holes; only 21 matched markets pass the prior coverage/freshness/checkpoint tests with no ≥120s gap. Three are low quality without a ≥120s hole; the 35 major-gap group includes the 30 current interruptions and five single-run markets. This confirms actual collection quality problems beyond a pure run-boundary labeling artifact.

## 16. Root-cause verdict

**MULTIPLE_CAUSES**.

- Strongly supported: frequent strategy/run boundaries, ~90s median stop-to-next-start gap, and 30 cross-run markets with actual major snapshot holes.
- Strongly supported: the 500 MB research DB guard activated repeatedly and suppressed optional TWAP shadow evidence; it did not disable the direct prediction snapshot enqueue path.
- Observed but insufficient to explain count loss: captured-row freshness is low mainly on market quotes/BTC while joint freshness remained near 51%.
- Unresolved: exact mix of scheduled timer, stale-instrument lifecycle, watchdog, discovery, or process restart; needed runtime logs/PID history are unavailable.

## 17. Recommended action

| Priority | Recommendation | Requirement |
|---|---|---|
| P1 | At the next controlled restart, record cycle source/index, process PID/start time, stop-request→run-return, writer stop, dispose, discovery and first-snapshot times. This is needed to attribute remaining gaps. | CODE CHANGE; NEXT CONTROLLED RESTART |
| P1 | Review the 500 MB research DB guard and durable archive/rotation path; the cap was exceeded on all 55 checks after first activation. | CONFIG/CODE CHANGE; NEXT CONTROLLED RESTART |
| P1 | Review lifecycle stale-instrument behavior and market discovery after 46 later run starts. Keep strategy parameters frozen during this investigation. | READ-ONLY LOG REVIEW; any change only after controlled review |
| P2 | Separate `run_boundary`, measured `largest_gap`, and `coverage` in the analysis; multi-run is not a root-cause code and the shortcut misses single-run gaps. | ANALYSIS-ONLY CHANGE; NO RESTART |
| P2 | Preserve existing snapshotter accepted/dropped/error and queue telemetry around gaps; those counters currently live only in memory/logs. | CODE/OBSERVABILITY CHANGE; NEXT CONTROLLED RESTART |

No evidence here supports an unplanned stop/restart. At the 18:54 snapshot, the 18:45 market had prediction rows and its run record was open; that is a point-in-time fact, not a live PID check.

## Answers A–H

A. **Is the bot actually crashing repeatedly?** Not established. Repeated strategy instances are visible, but crash/exception/startup logs and PID history are missing. Do not call them crashes.

B. **Is hourly node rollover intentional?** Yes in current source by default (3600s), subject to runtime override; the process's actual setting is unknown.

C. **Does run_id change mean process restart?** No. It means a new strategy instance; an internal TradingNode rebuild creates one in the same Python process.

D. **How many of 30 INTERRUPTED markets represent real major data loss?** 30/30 have measured ≥60s holes; 29/30 have ≥120s. Exact triggers remain unknown.

E. **Why did later Monday quality deteriorate?** Run starts rose from 10 early to 46 later, median runtime fell 40.9→11.7 min, and 27/48 later slots were multi-run with median 268s largest gaps. The 500 MB optional TWAP guard was active repeatedly; captured-row freshness did not fall enough to explain the count loss.

F. **Can the Monday dataset still be used after quality filtering?** Yes for exploratory work with explicit quality strata: 21 matched markets pass the previous coverage/freshness/T-300 tests and have no ≥120s gap. Do not treat all matched markets as continuous.

G. **Should collection continue without restart right now?** Snapshot evidence does not justify an immediate restart; collection persisted and the latest run remained open at cutoff. Present process health/PID could not be verified.

H. **What should change at the next controlled restart?** Preserve cycle/process/rollover logs, review DB cap plus archival policy, and persist queue/drop/error health. Separate analysis labels by measured gaps. No strategy parameter change is recommended.

## Safety confirmation

- source code modified? **NO**
- config modified? **NO**
- `.env` modified? **NO**
- bot stopped? **NO**
- bot restarted? **NO**
- signal sent to bot? **NO**
- second bot launched? **NO**
- active DB mutated? **NO**
- active Parquet mutated? **NO**
- commit/push? **NO**

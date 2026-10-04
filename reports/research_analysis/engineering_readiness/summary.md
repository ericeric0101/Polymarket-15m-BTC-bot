# Engineering readiness audit — 2026-10-04

## Pre-commit blocker corrections (2026-10-04; prepared, not deployed)

This correction supersedes the earlier readiness claims below where they
conflict. The running collection process and its DB/Parquet data remain untouched.

- Monthly reporting reconstructs completed `MARKET_CYCLE_PNL` events in a
  journal-owned background worker, on initial request, finalized-cycle/redeem
  notification and a five-minute fallback refresh. Live realized-delta handling
  reads cached values only. Reporting failure yields unavailable monthly
  display and never changes journal/BUY health. The cache can lag a finalized
  event until the background refresh completes; it is not another PnL ledger.
- Durable session accounting (realized PnL and high-water) survives guard mode
  changes. Only policy flags are recomputed on a mode change; same-mode sticky
  locks remain durable. Missing-state reconstruction still has legacy limitations.
- Prometheus `trading_position_close_*` metrics have strategy-run and instance
  labels and explicitly measure position-close activity. They exclude canonical
  settlement/session/month accounting. Old `trading_live_realized_pnl` consumers
  must migrate their observability queries; no trading authority changes.
- ResearchStore owns JSON parsing, stable row-order dedupe and settlement access
  for the current canonical modes. Malformed/non-object payload exclusions are
  counted by integrity. Older standalone tools remain compatibility paths.
- MarketEvidence recursively freezes mappings/sequences, including direct
  construction; display preserves run identity and captured freshness/provenance.
- Entry anchors require fresh evidence at or before entry. Partial opening-order
  fills aggregate; scale-ins retain the original lifecycle and have separate
  aggregate fields. Legacy client-order grouping remains explicitly ambiguous.
  Identity joins and legacy slug/instrument fallbacks have different labels.
  Execution timestamp does not stand in for an unavailable stop-trigger timestamp.
- Fresh inventory after a full close receives a new lifecycle ID. Persisted
  identity is restored during existing inventory recovery without changing cost
  basis. Duplicate journal events with durable fill identity are excluded offline;
  this is not a new venue-fill deduplication authority.
- Writer success now requires terminal acceptance stop, terminated worker,
  empty queue/buffers and no known write failure or dropped accepted data.
  Research writers stop after background producers and final strategy events;
  the shared TWAP writer result is checked. Failure/timeout means incomplete
  shutdown, not success. SIGKILL remains non-draining.
- Git status failure is UNKNOWN (`null`), not clean. Profile is explicit when
  available and UNKNOWN otherwise. Config hash covers only the documented safe
  section allowlist, not every environment/instrumentation setting. Tracked diff
  hash still excludes untracked contents; old provenance is not backfilled.

Research results retain their cohort boundaries: the earlier preliminary
weekend `<0.5sigma` estimate of about 12.9% and the later entry/stop report's
12/46 (26.1%) are different cohorts/checkpoint selections. Neither is a live gate.
Generated cohort CSVs remain local and were not regenerated against active data.

Blocker regression validation: **59 regression cases** in
`tests/test_precommit_blockers.py`; complete suite **871 passed** with the
existing upstream websockets deprecation warning. `git diff --check` passes.
No commit/push or deployment was performed.

## Scope and operating safety

This is an engineering/readiness audit of the current working tree, not a
deployment. The long-running TEST_DRY_RUN process was left untouched. No bot,
websocket, runtime DB, or Parquet file was started, stopped, restarted,
migrated, or written by this pass. The worktree was already dirty at audit
start; those existing edits and reports were preserved. This pass is not
committed or pushed.

No active DB was opened for analysis. All executable tests used temporary test
databases and synthetic inputs. Runtime metadata/schema changes described
below take effect only on the next controlled process start.

## 1. Architecture inventory

| Boundary | Current implementation / owner | Finding |
|---|---|---|
| Live decisions and orders | `run_bot.py`, `bot/`, Nautilus execution/data clients | Existing runtime remains the only order/stop/TP/sizing authority. Research code is not imported as a decision authority. |
| Run identity | `strategy.run_id`; `strategy_runs` | Run IDs existed, but commit/config/schema provenance was not consistently persisted. Extended the existing `strategy_runs.notes_json`; no second run registry. |
| Prediction evidence | `PredictionResearchSnapshotter` → `LeadLagDB.lead_lag_decisions` | Common-timestamp source/receive ages, freshness, p_ex, TWAP, BBO and BTC-return fields already exist. New snapshots now carry an explicit schema version. |
| Cross-market research | `LeadLagObservationMixin` → `LeadLagDB` | Existing 5-second snapshots and 1-second references are separate from prediction snapshots by purpose; they are not duplicate prediction authorities. |
| Dense BTC history | `BTC1sHistoryCollector` → UTC Zstandard Parquet parts | Existing passive observer reuses Binance feed; no new websocket or SQLite sink. |
| Stop/lifecycle research | `StopForensicsShadow`, `POSITION_LIFECYCLE_ENTRY`, `ResearchStore` / `research_analysis.py` | The inherited working-tree lifecycle ID and stop joins are extended with schema tags. Historical join completeness is limited by old rows lacking identity/snapshots. |
| Offline data access | `bot/research/store.py`, `MarketEvidence` | Read-only SQLite URI; no schema creation. Snapshot decoding, dedupe, coverage and integrity live here. Added run-provenance joining here. |
| Offline analyses/replays | `scripts/research_analysis.py`, `required_path_probability_replay.py`, `replay_session_pnl_guard.py`, Outcome replay tools | Several purpose-specific replay/report tools exist. There is not yet one unified market/decision/accounting replay harness. |
| Operational presentation | existing `STATUS`, `[PNL]`, prediction/queue health logs, Prometheus | No second UI added. Existing status now displays cached monthly target/progress for V2 without an extra DB query. |

## 2. Duplicate-functionality matrix and authority decisions

| Capability | Implementations found | Canonical authority / disposition |
|---|---|---|
| Prediction snapshots | 1-second synchronized prediction snapshots; 5-second Outcome lead/lag snapshots | Different evidence contracts. Keep both; do not merge or add another per-second writer. |
| Dense price history | bounded in-memory TWAP window; BTC 1-second Parquet | TWAP memory is live calculation input; Parquet is offline history. Keep split. |
| Completed-market PnL | `MARKET_CYCLE_PNL`; `pnl_attribution.py`; Prometheus counters; dashboard fields | `MARKET_CYCLE_PNL` is canonical completed-cycle/session/month reconstruction source. Attribution report is an independent audit calculation; Prometheus/dashboard position-close displays have a narrower accounting scope and are not authorities. Reconciliation differences remain reportable. |
| Session BUY protection | `SessionPnlGuard`, persisted `session_pnl_state`, status/log projections | Guard decision is the only session BUY authority; persisted state plus canonical completed cycles support restart. `shadow_target_scaled_v2` reports `shadow_would_block` without changing BUY authority. |
| Order/position state | Nautilus venue/cache state, local inventory/order maps, trade journal events, lifecycle research IDs | Venue/cache and local ledger drive current runtime actions; journal supports recovery/forensics. No single explicit cross-layer state-machine object exists; do not treat research event sequence as execution authority. |
| Replay | required-path, session-PnL, Outcome and market research replay scripts | Keep current specialist tools. Consolidating them into a common deterministic harness is deferred until input/evaluation contracts are specified; do not create a competing replay now. |
| Orderbook/depth | adapter-local live book, periodic `DEPTH_RISK_SHADOW`, quote/depth risk gates | Live book is execution input; 15-second depth shadow is counterfactual evidence. Sparse decision-boundary full-L2 snapshots are not yet canonical. Do not add a second full L2 stream. |
| Integrity/health | `ResearchStore.integrity`, `LeadLagDB.research_health`, prediction metrics, BTC writer metrics, DataEngine telemetry, TWAP/BTC disk guards | Keep component owners. The report identifies the missing unified read-only health view; no duplicate health subsystem or new blocking gate was added. |
| Prometheus collectors | process-global default registry plus strategy construction during node rebuild | Existing working-tree change reuses already registered collectors, addressing duplicate registration in the same process. Position-close metrics are explicitly scoped by strategy run and instance; they do not claim canonical accounting scope. |

## 3. Changes made in this pass

- Added `bot/research/provenance.py`: one-time secret-filtered config normalization,
  deterministic config hash, git commit/branch/dirty flag, tracked-diff hash,
  UTC/Taipei start times, runtime identity and explicit schema cohort versions.
- Extended the existing `log_strategy_run_start` path to put the manifest in
  `strategy_runs.notes_json`. Failure to produce the manifest is logged and
  cannot stop startup. No secrets or path-valued settings are serialized.
- Added prediction, research-event and lifecycle schema tags at their existing
  sparse writer boundaries. No per-tick DB write was added.
- Added read-only `ResearchStore.get_run_provenance()` and
  `research_analysis.py provenance`; `integrity` now includes that provenance
  summary. Legacy records are classified `LEGACY_UNKNOWN`, never backfilled.
- Extended existing `STATUS` with the already reconstructed/cached monthly PnL,
  target, progress, age, V2 mode, and lock reason. No extra journal query is
  performed by the periodic status line.
- Updated `project_overview.md` with a short authority map; full detail is here.

Files touched specifically by this pass:
`bot/research/provenance.py`, `bot/research/store.py`, `bot/ops.py`,
`bot/prediction_research_snapshot.py`, `bot/order_events.py`,
`bot/stop_forensics_shadow.py`, `monitoring/lead_lag_db.py`,
`scripts/research_analysis.py`, `bot/db_runtime.py`, `run_bot.py`,
`tests/test_research_provenance.py`, `tests/test_lead_lag_db.py`, and
`project_overview.md`. Other dirty files/reports were inherited from earlier
work and are not attributed to this pass.

## 4. Run/config/schema provenance

The existing `strategy_runs` row is the sole run record. Its `notes_json`
contains a `run_manifest` with run ID, UTC/Taipei start, git commit/branch,
dirty flag, config hash, safe normalized strategy/research settings, runtime
version, execution mode, TEST_DRY_RUN/LIVE state, and schema versions. Config
hash is deterministic over a fixed allowlist of strategy/research sections;
secret/token/wallet/path-shaped keys are excluded. Dirty diff hash covers
tracked-file diff only; untracked files set `git_dirty=true` but are not
included in that digest. Git metadata lookup is bounded and best-effort.

Prediction, generic research decisions/snapshots, lifecycle evidence, run
manifest and trade-journal schema versions are now explicit for newly written
records. Existing records remain legacy/unversioned. The trade-journal schema
version comes from `TradeJournalDB.SCHEMA_VERSION`, not a copied constant.
`ResearchStore provenance` groups runs by commit/config/schema and reports
market totals, weekday/weekend counts, time range, and legacy count.

No historical config hash can be reconstructed reliably from current `.env` or
source defaults; legacy cohorts must not be pooled as if proven equivalent.

## 5. Replay readiness and no-lookahead

Existing tools cover distinct tasks: required-path historical path replay,
session PnL guard descriptive replay, Outcome lead/lag replay, and canonical
snapshot research. Required-path replay has explicit end-time exclusion and a
test for no-lookahead; empirical probability research also uses only data
strictly earlier than the evaluation market. These guarantees apply to those
tools, not automatically to every shadow/report script.

Replay taxonomy:

- `EXACT_REPLAY`: pure deterministic policy functions when every input at time
  *t* is persisted with source/receive time and the decision function/version.
- `APPROXIMATE_REPLAY`: counterfactual entry/exit fills from market mid/BBO or
  incomplete depth; reports must retain that label.
- `NOT_REPLAYABLE`: exact queue priority, venue fills, or missing historical L2
  cannot be recreated from current snapshots.

A single canonical harness with MARKET / DECISION / ACCOUNTING modes is
**DEFERRED**. Creating it now would risk duplicating live policy instead of
calling existing pure functions; current reports remain purpose-specific.

## 6. PnL, order lifecycle and reconciliation

`MARKET_CYCLE_PNL` is the durable completed-market event used for restart
session/month reconstruction. Reconciliation updates/reuses an existing cycle
instead of appending another; startup resolved-cycle reconciliation is
idempotency-tested. Session PnL state is a durable BUY-only guard projection
that is reconstructed from completed cycles after restart, avoiding a second
addition of interim SELL fills. Fill-level realized PnL is still necessary for
immediate in-session guard updates. `pnl_attribution.py` independently computes
cash-flow attribution and reports differences against cycle PnL; it is an
audit ledger, not the authority.

The practical order path maps venue/cache events through submit/ack/cancel/
partial/fill and inventory open/exit/settlement/redeem. Existing recovery and
tests cover partial-fill counting, recent BUY cost-basis fallback, journal
failure, startup settlement reconciliation and session-guard restart. There is
no one canonical lifecycle enum spanning venue, local inventory and journal;
durable journal rows and lifecycle shadow IDs must not be mistaken for a
complete venue reconciliation state machine. Duplicate venue-fill identity
auditing and restart-mid-order integration remain technical debt.

## 7. Decision traces and orderbook evidence

`ENTRY_DECISION_TRACE` is already sparse/throttled at candidate/eligibility
boundaries; prediction snapshots can be forced at entry decision. Stop shadow
records adverse episode/checkpoints, actual stop continuation and settlement.
Session guard persists updates/locks. These cover much of “why entered/why
blocked/what happened after stop,” but there is no single structured
`STOP_DECISION`/`EXECUTION_DECISION` contract carrying all input evidence.

The existing 15-second `DEPTH_RISK_SHADOW` stores hypothetical ladder economics;
runtime depth gates use existing locally available orderbook. No new full L2
stream was introduced. Capturing bounded levels only at actual entry/stop
submit/fill boundaries remains **DEFERRED**; existing periodic depth samples
are not equivalent to a synchronized decision-boundary book.

## 8. Fault injection, shutdown and restart

The test suite already includes SQLite lock/disk-write failures, async writer
shutdown/drain, bounded DataEngine queue and suppression, stale quote/watchdog
recovery, node-stop idempotency, journal recovery, partial fills, startup
settlement idempotency, BTC-Parquet queue/writer failure and session-guard
restart. Full suite passed below.

Not every requested fault has an integrated end-to-end test: notably combined
DataEngine + multiple writer + node rollover failure, duplicate fill
reconciliation across venue/journal, and simultaneous storage pressure. No
destructive fault was run against the active process or databases.

Writers use independent owners and bounded queues; shutdown drains research
DB/Parquet workers with a completion result and logs timeouts. SIGKILL remains
non-draining for memory buffers. Operators should allow the existing staged
graceful shutdown to complete before any controlled restart.

## 9. Runtime health, storage and clocks

- DataEngine already reports queue depth/peak/rates, latency percentiles,
  quote coalescing, L2 suppression and open/recovered gaps. Watchdog/freshness
  rules remain existing safety authorities. A derived `HEALTHY / DEGRADED /
  CRITICAL` classification is **DEFERRED** because no common threshold contract
  exists; this pass adds no second DataEngine BUY gate.
- Research health is componentized: `LeadLagDB.research_health()` exposes
  queue/drop/write counts; prediction snapshotter reports interval/freshness;
  BTC writer reports completed/written/dropped bars and latency. No aggregate
  health score was fabricated.
- Storage guards are not global: BTC Parquet has its own free-space stop;
  TWAP optional-event guard checks its configured DB/free-space limits. Shared
  research DB growth from other writers is not covered by the TWAP cap. There
  is no single safe aggregate storage summary yet; no retention/deletion was
  performed.
- Prediction evidence carries source and receive timestamps; DataEngine quote
  telemetry separates raw WS receipt, adapter emission/coalescing, engine
  publication and strategy receipt. BTC Parquet buckets by exchange/source
  time. Order-event/journal insertion time is persistence time and must not be
  used as market-event time when an event/source timestamp exists. Some legacy
  order events lack venue source timestamps, limiting fine lead/lag claims.

## 10. Existing analysis and Target-Scaled Guard V2

Same-price × required-move-sigma tables already exist in the canonical
`research_analysis.py` path; they are descriptive and sample-size/data-quality
limited. No additional model was added.

`SessionPnlGuard` already supports `legacy`, `target_scaled_v2`, and
`shadow_target_scaled_v2`. Shadow mode computes the target-scaled state and
`shadow_would_block` but returns BUY allowed. Existing threshold/restart tests
remain the authority; this pass only exposes cached monthly progress in the
existing status line. The active process's loaded mode was not inspected or
changed.

## 11. Classification

| Feature | Classification | Notes |
|---|---|---|
| Existing run IDs / `strategy_runs` | `ALREADY_EXISTED` | Existing identity and notes container retained. |
| Immutable safe run manifest + config hash | `EXTENDED` | New manifest written once per future run into existing row. |
| Prediction/research/lifecycle schema tags | `EXTENDED` | New rows only; no DB migration/backfill. |
| Read-only run provenance query | `NEW_BUT_OFFLINE_ONLY` | `research_analysis.py provenance`; safe to run on DB snapshots. |
| Position lifecycle identity / stop shadow joins | `ALREADY_EXISTED` / `EXTENDED` | Inherited dirty implementation retained; this pass added version tags. |
| Entry/stop decision trace | `ALREADY_EXISTED` | Coverage remains incomplete; no duplicate stream added. |
| PnL/cycle reconciliation | `ALREADY_EXISTED` | Authority and idempotency tests already exist. |
| Prometheus registry reuse | `ALREADY_EXISTED` in starting dirty tree | Node rebuild collector reuse already present; no separate metric system added. |
| Monthly PnL in existing operational STATUS | `EXTENDED` | Read-only cached projection; no extra status-time DB query. |
| Unified deterministic replay harness | `DEFERRED` | Existing replay tools have distinct contracts. |
| Unified DataEngine/research/storage health view | `DEFERRED` | Existing component metrics retained; no arbitrary thresholds. |
| Sparse decision-boundary L2 snapshots | `DEFERRED` | Existing periodic depth shadow remains. |
| Current active run receives new metadata | `BLOCKED` until future restart | Intentionally not deployed during collection. |

## 12. Validation and performance

- Focused provenance/store/snapshot/lead-lag/session/live regression:
  **237 passed**.
- Full suite: **812 passed** in the historical pre-fix run, one upstream `websockets.legacy` deprecation
  warning.
- `git diff --check`: passed.
- Post-fix monthly reconstruction is background reporting I/O; realized-delta updates perform no monthly query. Run manifest is generated once at strategy start;
  git subprocess calls are bounded and best-effort. Schema tags are attached to
  already-enqueued sparse research payloads. STATUS reads in-memory cached
  monthly values. No active DB or writer was touched.

## 13. Immediate offline use vs next restart

Immediately, after the branch is reviewed, an operator can run
`PYTHONPATH=. .venv/bin/python scripts/research_analysis.py provenance --db
<read-only research snapshot> --journal <read-only journal snapshot>` and
`... integrity ...` to inspect prior run metadata. Legacy runs will appear
unknown. Do not point analysis at DBs currently being collected when a safe
snapshot is required.

New run manifests, prediction/research/lifecycle schema tags, and expanded
STATUS appear only after a future controlled restart. This pass does not
authorize that restart. Follow the existing deployment policy: complete
weekend plus ~2 synchronized weekday days, finish the market, stop once with
Ctrl+C, wait for graceful drain, review/commit separately, then perform one
controlled restart.

## 14. Required confirmations

| Check | Result |
|---|---|
| Running bot stopped/restarted? | **NO / NO** |
| Another bot or websocket launched? | **NO / NO** |
| Active DB mutated/migrated? | **NO / NO** |
| Current collection semantics changed? | **NO** |
| Entry / stop / TP / sizing / prediction semantics changed? | **NO** |
| Session guard authority changed in running bot? | **NO** |
| New live signal or BUY gate added? | **NO** |
| Dashboard redesigned? | **NO** |
| Immediate restart required? | **NO** |

Remaining highest-value engineering debt: (1) define one no-lookahead replay
input/evaluation contract before consolidating specialist replays; (2) specify
and test durable venue-fill identity/restart-mid-order reconciliation; (3)
design a read-only aggregate DataEngine/research/storage health view using
existing metrics and explicit thresholds; (4) add bounded L2 snapshots at
decision boundaries only if the current local book can be captured without
blocking the order path.

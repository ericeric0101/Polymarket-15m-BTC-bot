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

## P2 source-development completion (2026-10-04; not deployed)

This section records work after checkpoint `0c0af23`. Earlier sections are
historical audit snapshots; their "not committed" statements describe those
passes, not the later source-control checkpoint. This P2 pass is uncommitted,
unpushed and undeployed. The current collector and all active data remain
untouched. Tests and benchmarks use synthetic inputs/temporary databases only.

### Reuse audit before implementation

| Target | Initial classification | Reused authority / missing piece |
|---|---|---|
| P2.1 replay | PARTIAL | `bot/journal_replay`, specialist required-path/Outcome/session tools, SessionPnlGuard and InventoryLedger; missing common captured-evidence progression/classification. |
| P2.2 reconciliation | PARTIAL | execution_events cancel/reject, InventoryLedger, recovery, journal idempotency, research lifecycle ID; missing read-only cross-source diagnostic result. |
| P2.3 health | PARTIAL | component counters, journal health, DataEngine warning and watchdog owners; missing common read-only projection. |
| P2.4 research quality | PARTIAL | snapshotter bounded intervals/counters, LeadLag and BTC metrics; missing recent freshness/gap projection including silence. |
| P2.5 trace | PARTIAL | sparse ENTRY_DECISION_TRACE, guard updates, order events, stop shadow events; missing common structured evidence envelope. |
| P2.6 L2 | PARTIAL | canonical local cache books, existing depth calculations; missing bounded persisted raw levels at sparse boundaries. |
| P2.7 Store | PARTIAL | shared snapshot/settlement/provenance access already complete; important journal paths still decode separately. |
| P2.8 index | PARTIAL | idempotent composite index primitive exists; missing closed-copy preconditions and representative benchmark. |
| P2.9 clocks | PARTIAL | separate source/receipt fields and entry as-of rule; missing shared compatibility/as-of contract. |
| P2.10 storage | PARTIAL | existing BTC/TWAP disk checks and manual archive tool; missing explicit storage summary/readiness projection. |

Legacy scenario/forensic tools remain compatibility paths, not new canonical
loaders. Their formulas were not copied into another policy implementation.
Existing archive `--apply` deletes source rows and its default inspection path
initializes a manifest: neither path was run during this pass.

### P2 completion matrix

| Target | Final status | Implementation / validation | Runtime impact / restart |
|---|---|---|---|
| P2.1 | COMPLETE | existing `bot/journal_replay.replay_evidence`, canonical `research_analysis replay`; repeatability, future-field exclusion, classifications and CLI tests | Offline only; no restart needed for offline copies. |
| P2.2 | COMPLETE | existing `bot/execution_events.audit_reconciliation`, integrity integration; duplicate/conflicting fill, partial cancel, restart, quantity, finalization and reopen tests | Detection only; no runtime recovery change. |
| P2.3 | COMPLETE | `bot/research/health.py`, cached journal accessor, existing STATUS; subdomains, UNKNOWN, reasons, warning projection and failure isolation | In-memory projections after a future controlled restart. |
| P2.4 | COMPLETE | existing prediction snapshotter/BTC/LeadLag counters; recent gap/freshness/drop/silence tests | Bounded recent capture history, no cadence/freshness change; future restart. |
| P2.5 | COMPLETE | existing evidence module, DB-runtime sparse journal boundaries, stop shadow events; structure, sparsity, timing and failure tests | Bounded annotation only at existing emissions; future restart. |
| P2.6 | COMPLETE | bounded L2 serializer in existing evidence module, local cache book and stop bid levels; top-five, unavailable/invalid book and failure tests | Raw L2 uses the existing async writer only; journal holds references; no feed or sizing authority; future restart. |
| P2.7 | COMPLETE | ResearchStore journal parsing/opening and time range; canonical capital/lifecycle/replay readers, dedupe/readonly/revision tests | Offline analysis; no source DB writes. |
| P2.8 | COMPLETE | existing indexing module: synthetic benchmark and confirmed closed-copy migration; index definition/idempotency/query-plan tests | No active index applied. |
| P2.9 | COMPLETE | shared `clocks.py` parser/as-of/compatibility; entry join delegates; source/future/nested/restart ordering tests | Offline contract and honest trace clocks; no feed timestamp/freshness change. |
| P2.10 | COMPLETE | explicit `storage-summary`, cached disk owners, archival readiness; throttling/failure/no-action tests | No runtime recursive scan or automatic archival; existing checks cached after future restart. |

COMPLETE means the defined safe foundation/projection/detection scope is
implemented and tested. It does not claim full venue execution replay,
complete historical event identity or deployment into the running process.
Those limits are represented explicitly in outputs and below.

### Replay scope and no-lookahead

The canonical entry point accepts `--replay-kind MARKET|DECISION|ACCOUNTING`,
`--decision-ts` UTC seconds and explicit offline `--db`/`--journal` copies.
Optional run/slug filters use ResearchStore. MARKET replays deduplicated
captured snapshots. DECISION currently evaluates the existing pure session
guard on completed-cycle events; `--guard-mode` must be explicit. ACCOUNTING
reuses InventoryLedger on recorded fills and excludes identified duplicates.

The shared selector requires evidence time at or before the decision, checks
explicit source/receive/evidence clocks (including nested inputs), and excludes
late-written snapshot revisions. The entry anchor uses the same selector with
its existing eight-second age and joint-fresh requirements. Existing specialist
path/Outcome backtests remain specialist evaluations, not newly copied logic.

`EXACT_REPLAY` is pure evaluation with explicitly asserted complete initial
history/config/fees/identity; it does not assert counterfactual fills. Unknown
history or fee/fill identity yields `APPROXIMATE_REPLAY`. Missing policy config,
missing initial inventory, repeated finalized-market accounting or invalid
required input is rejected or `NOT_REPLAYABLE`. Canonical journal replay is
always approximate when sequencing on PERSIST_TS rather than venue time.
Full entry/exit policies remain NOT_REPLAYABLE without their persisted inputs;
no coupled live strategy was refactored to manufacture a replay result.

### Reconciliation source map and limits

- Intent/submitted: existing durable order/entry intent and `ORDER_*_SUBMIT`.
- Acknowledged/cancelled/rejected: venue/cache events and execution_events
  adapters remain authoritative; cancellation can coexist with partial fills.
- Partial/filled/open/closed: fill events update the existing InventoryLedger;
  the diagnostic uses the same arithmetic, not a second PnL ledger.
- Exiting: existing exit engine/taker order state; no research state transitions
  authorize orders. Settlement/redeem uses existing cycle reconciliation.
- Research lifecycle identity remains the existing
  `slug|instrument|first-opening-client-order` annotation, never order authority.

Diagnostics return CONSISTENT / RECOVERABLE_MISMATCH / UNRESOLVED_MISMATCH /
CRITICAL_INCONSISTENCY, source issue codes and a fill-only quantity projection.
They never recover/liquidate/cancel. Missing initial fills, token-fee inputs,
legacy fill identity and unrecorded settlement inventory transitions cannot be
made exact. Venue/local snapshots are supplied explicitly (mocked in tests);
no production venue was contacted. Repeated known redeem transaction identity
is diagnosed; different transactions may legitimately redeem partial amounts.

### Runtime health, quality and sparse trace

Subdomains are MarketData (`Data` in STATUS), Research, Storage and Execution.
Every state includes reason codes and original component metrics. UNKNOWN
wins over healthy when a component is missing; observed faults retain higher
severity. Research gap/freshness warnings describe evidence usability, not
new trading thresholds. Existing DataEngine warning and watchdog pending state
are projected; existing execution journal readiness can report
BUY_SAFETY_BLOCK_ACTIVE. Health never returns or applies BUY permission.

The journal accessor used here is `runtime_health_snapshot`, **not**
`runtime_health`: the latter can perform an existing recovery probe when
unhealthy. Recent quality holds at most 600 samples and reports a five-minute
window, latest gap including silence, joint-fresh percentage and recent drops.
Lifetime component errors/drops remain source counters and are not erased by
this projection. The existing STATUS loop prints terse states; detailed state
and reason changes print only on transitions. No second status daemon exists.

Trace schema 1 annotates the existing ENTRY_DECISION_TRACE, sparse session
updates, order submit/fill and stop candidate/actual-fill/cleared events.
Unavailable evidence is null. Trace capture time is separate from policy
trigger time; unknown trigger time stays null. A stop candidate or recovered
adverse episode is an explicitly named **shadow observation**, not an
independent trading stop/HOLD decision. Historical why-HOLD coverage remains
limited where no corresponding decision was persisted.

Local books are serialized at most five already sorted levels per side; stop
candidates may have bid-only depth. This bounded evidence can support future
$5/$10/$25/$50 and 1c/2c/5c offline capacity estimates, subject to captured depth
coverage. No BBO-to-L2 fabrication, queue-priority/fill assumption, new feed,
continuous full L2 stream or live capacity sizing was added. Legacy/fill events
without a book stay L2_NOT_AVAILABLE. Raw L2 persistence uses the existing async TWAP research writer only. Entry/
order journal trace contains a reference and enqueue status, without raw book
levels; enqueue is not proof of durable persistence. Stop shadow already uses
the existing async writer. Writer failure cannot affect the decision or journal
result, and integrity exposes sparse L2 row counts.

### Timestamp / clock contract

| Fields / source | Actual semantics |
|---|---|
| Binance exchange trade timestamp / `_binance_ws_price_source_ts` | SOURCE_TS, exchange milliseconds normalized to UTC seconds. |
| `_binance_ws_price_ts` / tick `received_at_ts` | RECEIVE_TS on host wall clock. |
| Chainlink updated/observation timestamp | SOURCE_TS; TWAP observation field is bound to the exact received tick. |
| `_polymarket_chainlink_price_ts`, `_twap_price_ts` | RECEIVE_TS, separate from observation time. |
| Nautilus quote `ts_event` | Venue SOURCE_TS when available; existing fallback to local time is not proof of source clock. |
| Nautilus `ts_init`, raw WS receipt / adapter emission / strategy receipt | Host creation/receive processing clocks; monotonic duration metrics are separate. |
| Prediction `snapshot_ts`, `decision_epoch_ns` | Local capture/DECISION_TS, epoch seconds/ns. Source/receive fields retain their captured provenance; legacy fallback p_ex clocks can be UNKNOWN. |
| Entry `observed_ts` and new explicit decision trace time | Recorded decision observation, distinct from journal insertion. |
| `actual_stop_ts` and sparse execution annotation time | Local fill-handler observation; unavailable stop-trigger time remains null. |
| Journal `ts`, row ID | PERSIST_TS and durable insertion order, not venue event time. |
| `time.monotonic`, perf counters | Process-local durations; cannot compare across process restarts. |

`compare_clocks` compares only known matching kinds and named origins, or an
explicit cross-domain justification. Otherwise it returns
CLOCKS_NOT_COMPARABLE with no lead/delta. Negative source/receive skew is an
observation when comparison is explicitly justified, not an impossible exchange
clock invariant. Naive datetime strings remain unknown rather than using the
machine timezone. Restart/multi-run selection uses stable persistence ordering
and does not assert globally synchronized exchange clocks.

### Index benchmark and safe lifecycle

Synthetic 100,000-row temp SQLite DB; three repetitions, median milliseconds.
Event/lifecycle filtering parses JSON after run/slug selection, matching the
canonical Store path. No production or copied active DB was opened.

| Query | Rows | Before ms | Composite-index ms |
|---|---:|---:|---:|
| market timeline | 1,000 | 6.493 | 2.250 |
| run timeline | 25,000 | 28.838 | 49.569 |
| event type + slug | 1,000 | 7.678 | 2.907 |
| time range | 500 | 6.110 | 0.673 |
| lifecycle + slug | 1,000 | 7.861 | 2.343 |

Before: SCAN + temporary ORDER BY B-tree. After: SEARCH using
`idx_lead_lag_run_slug_time`; the run-only query still uses a temporary ORDER BY
B-tree and is slower in this fixture. The existing composite candidate has
measurable market/time-range value, but is **not** a universal optimization.
No additional overlapping index was introduced and no index was deployed.

`migrate_closed_copy(path, closed_copy_confirmed=True)` requires a known closed
offline copy, verifies quick_check and the existing index definition, reports
intended SQL and whether created, refuses missing files, and is idempotent.
ResearchStore never calls it. Operators must verify the copy's provenance and
that it has no writer; the flag is an explicit precondition, not an automatic
process detector. `research_analysis.py index-benchmark` only makes a temp DB.

### Storage and archival readiness

`storage-summary --db <OFFLINE_RESEARCH_COPY> --journal <OFFLINE_JOURNAL_COPY>
--btc-dir <OFFLINE_HISTORY_COPY>` is explicit read-only measurement. It exposes
free bytes, DB/WAL sizes, optional Parquet total and supplied queue depths; the
measurement cache is throttled for repeated callers. Recursive Parquet totals
are **offline only**, never in STATUS/event/tick callbacks. Runtime storage
health reads existing TWAP/BTC disk-check caches and configured guard outcomes;
it adds no competing disk threshold or deletes/moves data.

Future lifecycle: ACTIVE while collection/writers are active; CLOSED after
collection is finished and graceful writer drain is verified; ARCHIVABLE only
after an integrity-checked, count/hash-verified independent backup. Readiness
returns no automatic action. On a future authorized closed copy, use the
existing index helper, verify query plans, and compare copy integrity/counts.
Inspect archive candidates via existing `eligible_partitions` with a read-only
connection; destructive retention remains separately authorized. No active
files are copied/moved/indexed/archived by this pass.

### Performance and duplicate-authority re-audit

New tick-path work is limited to appending a bounded quality sample at an
already eligible snapshot, storing cached disk/quote health where checks
already exist, and adding the existing warning outcome to its telemetry report.
No new live SQLite query, filesystem scan, git subprocess, network feed,
background writer or periodic logger was introduced. Sparse trace/L2 work
occurs at existing emission boundaries; at most five levels per side are
serialized through the existing async writer only. Existing journal writes
serialize a bounded trace/reference payload without raw L2; this
pass does not remove their pre-existing synchronous I/O. Replay, integrity,
index benchmarking/migration and recursive storage measurement are offline.

| Concept | Single authority after this pass |
|---|---|
| PnL | Existing cycle journal + SessionPnlGuard/InventoryLedger; replay calls their pure primitives. |
| Order/lifecycle | Existing venue/cache/recovery/inventory and lifecycle annotation; diagnostic has no execution action. |
| Replay | Existing journal_replay foundation through the canonical CLI; specialist tools remain documented compatibility paths. |
| Health | One derived health module, reading component owners; no permission/BUY method. |
| Store | ResearchStore read-only opening/JSON parsing/filters/dedupe; raw legacy forensic scripts remain compatibility tools. |
| Decision trace | One evidence envelope attached to existing sparse event owners, no new decision evaluator or stream. |
| L2 | Existing local cache book, one bounded serializer, existing writers. |
| Storage | Existing disk guards; explicit offline summary/readiness without new thresholds/actions. |
| Timestamps | One clocks parser/as-of/compatibility contract; entry join delegates. |

Deferred by design: full venue/journal reconciliation state machine and live
repair; exact queue-priority/counterfactual execution; entry/exit replay with
unpersisted inputs; historical L2 backfill; graphical dashboard. Future
dashboard consumers must use these authoritative projections, not independently
calculate signals/PnL/positions/health. Activation and index/archival operations
remain separately controlled after collection. None is required to run the
new offline tests on synthetic/copy data.

### Final validation and checkpoint recommendation

- P2 focused regression: **58 passed**, synthetic/temp data only.
- Full suite: **929 passed**, with the single known upstream websockets
  deprecation warning; all 871 baseline tests remain passing.
- `git diff --check`: passed.
- The malformed-slug integrity fixture exposed a provenance IndexError; unknown
  market epochs now appear in `unclassified_market_count` instead of crashing.
- Incomplete-history oversells are UNRESOLVED; they become provably CRITICAL
  only when complete history is explicitly asserted. Conflicting duplicate-fill
  fees are included in identity-conflict checks.
- No commit/push/deploy, bot stop/restart, additional collector, active DB/Parquet
  mutation, live gate or strategy semantics change occurred.

Verdict: **P2_COMPLETE_READY_FOR_CHECKPOINT** for this source-development scope.
A future authorized checkpoint can separate (1) offline replay/clock/Store/
reconciliation/index/storage foundation, (2) runtime health/quality projections,
(3) sparse trace/L2 annotations plus architecture documentation. Shared CLI,
DB-runtime and regression-test hunks must follow dependency order; commits and
push require separate operator authorization. Deployment is not part of that
checkpoint.

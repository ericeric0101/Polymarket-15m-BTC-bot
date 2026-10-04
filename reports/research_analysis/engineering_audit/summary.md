# Offline research engineering audit

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

This audit was performed while a long-running dry-run collector remained
untouched. No source SQLite/Parquet file was migrated, indexed, copied, or
written by this work.

## Architecture map

| Area | Current owner | Boundary |
|---|---|---|
| Settlement/required path/p_ex/freshness | spot_pricer and twap_forward_shadow | Runtime is canonical; offline readers consume values only. |
| Snapshot capture | prediction_research_snapshot to LeadLagDB | Bounded asynchronous queue, no execution authority. |
| Research persistence | monitoring LeadLagDB | SQLite decision/markout/reference records. |
| BTC dense history | btc_1s_history | Passive callback aggregation, bounded Parquet writer queue. |
| Offline access | bot/research/store.py | Read-only SQLite URI, JSON parsing, restart-aware dedupe. |
| Offline representation | bot/research/evidence.py | Frozen MarketEvidence; projection only. |
| Offline reports | scripts/research_analysis.py | Canonical CLI; old scripts remain compatibility tools. |

## Consolidation

- Canonical analysis no longer owns settlement-summary SQL parsing:
  ResearchStore get_settlements owns that read-only schema access.
- Current canonical modes centralize snapshot JSON decoding, run/slug filtering
  and deterministic duplicate-key handling in ResearchStore; older standalone
  scripts retain compatibility readers.
- MarketEvidence preserves persisted provenance/freshness fields and does not
  reimplement required-path, probability, or freshness calculations.
- Old scripts are not deleted in this deployment; migration to the canonical
  CLI is intentionally incremental.

## Evidence and store status

MarketEvidence is read-only and contains identity, time/regime context and the
original canonical payload. ResearchStore provides prediction snapshots,
settlements, coverage, and integrity checks. It opens databases read-only and
is not imported by live execution.

## Query/index finding

Synthetic development SQLite benchmark, 100,000 rows, query constrained by
run_id + slug and ordered by timestamp:

| State | Query plan | Single-run time |
|---|---|---:|
| Before | SCAN + temporary ORDER BY B-tree | 5.552 ms |
| run_id, slug, decision_epoch_ns index | indexed SEARCH | 0.911 ms |

This establishes an index candidate only. No index was created in the
currently running research DB. Add it after the collection window through a
controlled migration and benchmark against a safe backup.

## Shutdown/restart audit

| Producer | Writer | Current shutdown path | Finding |
|---|---|---|---|
| Prediction/TWAP snapshots | LeadLagDB bounded queue | runtime stops producer/runtime then DB writer | terminal stop reports failure on timeout, known writer error or lost queued data; see corrected contract above. |
| BTC 1s bars | Parquet worker queue | collector stop after background producers stop | terminal stop reports failure on timeout, known writer error or lost queued data; see corrected contract above. |
| Trade journal | backup worker | final trade DB stop after final strategy events | existing final backup path retained. |

SIGKILL cannot drain Python queues or flush in-memory bars. Use one Ctrl+C and
wait for the logged shutdown stages. Changes in this audit take effect only
after a later controlled restart.

## Market/run continuity

ResearchStore market coverage groups by canonical slug and exposes run IDs,
restart count, first/last snapshot, largest gap, and coverage ratio. Offline
studies must classify multiple run IDs or a greater-than-15-second gap as
interrupted rather than counting the same market twice.

## Capital and capacity

The research metrics module adds pure offline capital minutes, profit per
dollar-minute, and bounded top-of-book capacity. Current snapshots do not
consistently persist full L2 levels or actual executable fills, so 1c/2c/5c
capacity and net shadow edge remain data-quality-limited. No sizing or
execution rule was added.

## Dashboard and deployment

The terminal dashboard already owns live presentation. No new runtime panel
was wired because that would change the active collection version. MarketEvidence
is ready for a later read-only panel after collection ends.

Deploy this branch only after weekend plus weekday collection ends, wait for a
clean Ctrl+C shutdown, then restart once at a market boundary.

# Phase B Outcome runtime retirement

Outcome/Hyperliquid network ingestion, reference probes, cross-venue snapshots,
Fast Follow shadow/execution, associated calibration, startup/shutdown hooks and
STATUS fields are removed. Obsolete Outcome environment settings no longer form
an AppConfig section; explicitly supplied legacy keys produce a names-only
startup warning and cannot enable any runtime component.

Runtime storage ownership:

| Store | Producers |
|---|---|
| `logs/trade_journal.db` | strategy, order, accounting and writer-failure evidence |
| `data/research/twap_forward_shadow.db` | prediction, TWAP, stop forensics, trend-entry, forward-shadow, post-entry smart-money, order handoff/cancel latency |
| BTC1s Parquet | existing BTC reference collector |

There is one asynchronous research writer. `LeadLagDB` remains a shared schema/
writer implementation with an explicit mandatory path; its legacy table and
column names are preserved. Runtime construction rejects the historical
Hyperliquid filename, resolved symlink or a hardlink to the known historical DB.
No new runtime DB is introduced and no historical rows are migrated.

The five moved producer paths preserve payloads, cadence, bounded queues and
non-fatal writer semantics. Existing TWAP size/free-space guards are unchanged;
a pre-existing DB above its cap remains an operational research limitation.
Consolidation does not resolve that limitation or promise unlimited retention.

Pure historical replay types/state/economics are isolated in `monitoring/legacy`.
Offline Outcome CLIs require an explicit restored historical input. Changed
readers use SQLite read-only mode, so a missing DB cannot silently become empty.
Historical journal labels and late-fill classification remain readable for
accounting recovery; they cannot create an Outcome order or network subscription.

No maker formula/economic threshold, L2 freshness (2-second default), stop/flip,
p_ex/sigma, Chainlink settlement, Binance trend, session PnL guard, 3-hour rollover,
instrument admission, 900-second backup policy or retention policy changes.

The historical `data/research/hyperliquid_lead_lag.db` remains in place. After a
separately authorized dry-run start, validate no open handle and unchanged
size/mtime before any separately authorized archival. This patch does not restart
or deploy the bot.

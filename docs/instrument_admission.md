# Runtime Polymarket instrument admission

Startup discovery populates a finite provider/cache universe. Lifecycle discovery
previously resolved later slugs without loading their instruments, eventually
forcing `stale_instrument_lifecycle` node recovery.

`bot.instrument_admission` reuses the active Polymarket provider and the existing
node asyncio loop. Gamma fetch/parsing is asynchronous; the public
`load_ids_async` loads the UP/DOWN pair. Both objects must match the slug and
opposite outcomes before public `cache.add_instrument` publication. Pair
publication shares the market-selection lock, with no await between additions.
Pending and failed slugs are excluded from selection, including partial cache
publication or the adapter's periodic provider publication. No private cache
entries are deleted or edited.

Admission never subscribes, selects a market, or changes side/pricing state.
Existing lifecycle/reload selection subsequently applies the existing current
quote/L2 and next-pair quote-only prewarm reconciliation. When the last cached
market is selected and no prewarm pair exists, its immediate next slug is queued
for admission. WAITING discovery also requests admission before cache selection.
The original three-miss stale-lifecycle fallback is unchanged.

## Bounds and failure handling

The unchanged 3h scheduled timer spans 12 fifteen-minute slots. One next-market
prewarm slot and one startup/boundary buffer give **14 distinct dynamic markets
per node**, including failed requests. The profile's startup cap is 6 markets,
so this path admits at most 20 markets / 40 BTC instruments per node. A different
startup cap changes that total, but never the 14-market dynamic-growth bound.

There is one queued/running admission globally, at most three attempts per slug,
and a 20-second asynchronous timeout. Retries use the existing lifecycle polling
interval; IDs cannot change across retries. Hitting the bound refuses admission
and leaves normal lifecycle recovery authoritative, even if exposure defers the
scheduled reset. No instrument eviction or unlimited rolling cache is assumed.

Node stop requests and strategy teardown fence/cancel admission. Expired targets,
targets older than the selected market, and completions during stopping are
discarded. Admission failure remains non-fatal. Provider-side partial objects
may remain until node reset, but cannot select a quarantined pair. A successful
retry can publish the full pair and release quarantine.

REQUESTED, STARTED, COMPLETED and FAILED events use the existing asynchronous
journal enqueue authority. They describe pair loading/publication, not a venue
subscription acknowledgement. No quote/delta persistence is added. Writer,
watchdog, settlement, calibration, shutdown ordering and trading rules remain
unchanged. Synthetic tests validate lifecycle/cache/subscription contracts;
live continuity still requires a separately authorized deployment and cohort.

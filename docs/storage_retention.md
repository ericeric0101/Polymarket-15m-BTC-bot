# Storage retention

This policy manages known runtime artifacts, not authoritative database size.
Primary SQLite databases, WAL/SHM, BTC history, source/tests/git, user reports,
unknown files and unclassified analysis inputs are never cleanup candidates.
Consequently it cannot guarantee a global repository-size ceiling while those
protected inputs grow. Archival of authoritative data remains a separate task.

## Authority and configuration

`monitoring.storage_retention.StoragePolicy` owns retention limits and storage
watermarks. The existing journal worker and Loguru consume it; there is no new
maintenance thread or cleanup on quote/order/STATUS paths.

|Environment setting|Default|Meaning|
|---|---:|---|
|BACKUP_RETENTION_COUNT|2|Maximum managed completed images; valid values 1–2|
|ANALYSIS_SNAPSHOT_DEFAULT_TTL_DAYS|7|TTL beginning at explicit classification|
|LOG_RETENTION_DAYS|7|Compressed operational-log age limit|
|LOG_RETENTION_MAX_FILES|50|Rotated file count cap, roughly 1 GB before gzip at existing 20 MB rotation|
|STORAGE_WARNING_FREE_GIB|20|Baseline warning watermark|
|STORAGE_CRITICAL_FREE_GIB|10|Baseline critical watermark|

Defaults derive from the existing 10 GiB research and 5 GiB BTC guards: critical
uses the research floor, warning reserves twice that space. EMERGENCY is below
max(5 GiB, one journal image). Effective CRITICAL also includes the next backup
budget; effective WARNING includes twice that budget. No percentage rule or
trading gate is added. Existing research/BTC guard values and DB-size caps are
unchanged; these observational watermarks do not clear their sticky state.

The journal backup budget from `1fa2ba0` is preserved exactly:
`2 * image_bytes + existing_temporary_rollback_bytes`. It covers a destination
image, rollback overhead, and one image-sized primary reserve. Telemetry estimates
image size conservatively from main+WAL and the most recent backup measurement;
the actual preflight still uses SQLite page_count/page_size on the read-only
source. Another process can consume space after preflight; SQLite error handling,
dirty state, cooldown and the last published backup remain the recovery path.

## Backups

Runtime retains its existing single fixed completed target (below the cap of 2),
15-minute interval, worker serialization, atomic replacement, read-only source,
space guard, failure cooldown and forced final shutdown flush. No additional
full image is created merely to implement retention.

An interprocess advisory lock serializes runtime publication with maintenance.
After successful SQLite online backup, connection close and atomic publication,
`backup_retention.json` records the completed file identity, source path and time.
No expensive integrity scan runs per backup. Crash-before-metadata or an
invalid/stale manifest makes maintenance retain the file. Metadata uses a fixed
bounded temporary file and fsync/atomic rename. Backup content durability and
journal-write semantics are unchanged.

Extra completed images require valid completion metadata; unknown older `.db`
files are never inferred to be backups. Maintenance only prunes beyond the cap
if a newer retained image passes read-only quick_check. Current target is always
protected. If no newer good witness exists, preserve the older image even if a
cap cannot be met. Primary source paths recorded in metadata are never pruned.

Known unpublished temporary targets must be older than 24 hours, have no open
handles, and be unowned under the shared lock. Recent/in-use/unknown temporary
files are retained. Open-file detection uses lsof and fails closed on missing
commands, timeout or permission warnings. No WAL/SHM is removed.

## Analysis snapshots

Existing directories remain unclassified and retained. `retention.json` must
explicitly say PINNED or TEMPORARY; TEMPORARY expiry starts at classification,
not the old directory mtime. The known freshness_audit_20261006_231849_+0800
snapshot is protected by default even without metadata. Converting any pinned
snapshot requires explicit --unpin with --temporary. Classification is an
operator attestation that this is an offline disposable input; reproducibility-
critical snapshots should be pinned.

Only immediate directories with the documented snapshot DB/manifest members
are eligible. Unexpected members, symlinks/hardlinks, open files, WAL/SHM or bad
metadata retain the whole group. Interrupted apply leaves remaining files
unclassified/retained if its metadata has already been removed. No broad rmtree,
filename-only age deletion, or retroactive classification is performed.

## Logs and maintenance

The existing dashboard Loguru file sink rotates at 20 MB, compresses with gzip,
and uses the shared retention selector for 7 days / 50 rotated files, whichever
limit is reached first. Enqueue keeps retention/open-file checks off the caller.
The current log and arbitrary/manual logs are never candidates. Only Loguru's
completed timestamp-named compressed terminal_bot logs are managed. Existing
old logs are not retroactively renamed or deleted.

Examples (no bot startup):

```sh
.venv/bin/python scripts/storage_maintenance.py
.venv/bin/python scripts/storage_maintenance.py --protect data/analysis_snapshots/example
.venv/bin/python scripts/storage_maintenance.py --pin example --apply
.venv/bin/python scripts/storage_maintenance.py --temporary disposable_example --apply
.venv/bin/python scripts/storage_maintenance.py --apply
```

Without --apply, including classification requests, no files/locks/metadata are
created. Apply prints each exact intended path, acquires producer locks, replans,
rechecks identity and open handles, then unlinks via anchored directory
file descriptors without following symlink parents. Priority: stale temp,
expired compressed logs, redundant verified backups, explicitly expired
snapshots. Apply can retain more files than proposed if state changes.

Five-minute storage telemetry runs only on the existing journal worker and
reports free GiB (minimum of actual primary/backup volumes), journal/main WAL,
backup directory/count, analysis-snapshot directory, log directory, state and
next estimated backup budget. State transitions are logged once per change;
failed measurements are throttled too. No active-data cleanup was run during
implementation. Configuration/source changes take effect on the next operator-
controlled startup; implementation does not restart the bot.

## Full-backup write amplification

`DEFAULT_BACKUP_INTERVAL_SEC` in TradeJournalDB is the sole production default
(900 seconds); constructor overrides remain available for tests/explicit callers.
The launcher does not supply another interval. The primary journal remains WAL
with synchronous=NORMAL; normal restart reads the primary, not the backup.
The 15-minute RPO is a recovery-image target for primary loss/corruption, not a
guarantee under disk pressure or failed backups; lost risk/session evidence may
require operator reconciliation before trading. This is a deliberate increase
from the previous 30-second backup RPO. WAL does not replace an independent backup
or guarantee power-loss durability of every recent NORMAL transaction.

Committed schema/run/order/event/session/reconciliation changes mark the existing
dirty flag. A clean interval records BACKUP_SKIPPED_UNCHANGED without reading DB
pages or relying on mtime, page count, data_version or max row IDs (updates need
coverage too). This covers this class's writes, not unsupported external writers.
Writes during publication keep the next interval dirty. Clean shutdown, including
scheduled rollover teardown, forces the same serialized publisher even when clean;
there is no additional before/after rollover backup system. Startup schema writes
mark dirty, but startup does not force another full image.

Successful backup and unchanged skip logs occur only per publication/interval,
never per journal write. Existing cached health retains success age, image size,
preflight free space, failure stage and retry state. Atomic replacement, one fixed
completed target, cap <=2, conservative space budget and primary-write priority
remain unchanged. No APFS/Time Machine snapshot deletion is performed. The live
Python process keeps its old backup cadence until an operator-controlled restart.

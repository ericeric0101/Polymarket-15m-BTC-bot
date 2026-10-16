# Pass D — storage/runtime/backup
- Growth (UTC days Oct3-7): journal payload 200-293 MB/day (08_tj_growth.txt); TWAP/research store ~150-230 MB/day, dominated by PREDICTION_RESEARCH_SNAPSHOT (05_twap_by_day_type.txt). Combined ~0.4-0.5 GB/day logical, before indexes/WAL/backup copies.
- Free space: 12 Gi at 12:15Z, 16.46 GB at 12:35Z with bot stopped and no auditor deletions (19_apfs_snapshots.txt). Local snapshots: 4 TM hourly (2026-10-08 03:00..06:00 local) + 3 com.apple.os.update incl. MSUPrepareUpdate.
- Guards: TWAP guard free<10 GB -> labels CRITICAL but required events + prediction snapshots continue; backup preflight 2*image+tmp (~6.35 GB) refuses backup but primary keeps growing. No hard stop for primary journal growth found.
- Runtime (log only, 2026-10-07 07:34-23:28 +08): DataEngine qlat p95 ~11-16 ms, depth 0-1, rate ~14-15/s; BTC1S writer dropped=0, queue 0. EVENT_LOOP_CONSUMER_TIMING rows ~22.7 KB each (30.9 MB/day). Final shutdown backup 6.16 s for 3.17 GB.
- Rollover: 3 h cycles 10831-10835 s; stop->start ~15 s in log; prediction snapshot max-gap p90 = 195 s across all markets (12_manifest_summary).

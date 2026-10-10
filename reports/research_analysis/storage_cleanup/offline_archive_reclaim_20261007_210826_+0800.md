# Controlled offline archive reclaim

## Graceful shutdown

唯一 PID31469 原 terminal session16625 送 Ctrl-C。stop callback開始
2026-10-07 20:58:58.667 Taipei；research writers stop完成20:59:01.983，
journal final backup完成20:59:12.485。node.run return20:59:22.764，
terminal exit code0，之後ps確認無任何run_bot。未SIGKILL、未restart。
launcher最後的「Node returned without explicit rollover request」是manual
interrupt後停止而不重建的既有訊息，不是另啟bot。

## Stable verification / provenance

共13個DB以 mode=ro&immutable=1 讀取；先確認相關WAL不存在或0 bytes、
無open handles。所有quick_check=ok，page_count、SHA256、size、mtime、
inode記於 data/analysis_archives/offline_verification_inventory.json。
主DB/唯一backup/受保護freshness raw的6個DB在封存完成後再hash，
與維護前完全相同。未VACUUM、未checkpoint、未改schema或DB內容。
Shutdown由SQLite正常收尾移除runtime WAL/SHM，分析沒有手動刪除它們。
backups/.tmp已由正常publish完成，不存在stale orphan需清理。

三個較早snapshot直接引用於既有early/interim/postfix reports；derived
conclusions仍保留，current corrected canonical cohort使用freshness_audit
與其corrected sidecars，不直接依賴這三組舊raw。這些historical cutoff仍
獨有且不能被較新DB取代，因此只以精確cold archive保留後刪raw。
Freshness audit兩個raw DB完整保留，不因archive已驗證就改變其protection。

Legacy logs/hyperliquid DB保存Sept7-19獨有資料；current data/research DB
是另一個檔案，不刪。歷史CLI default仍指舊raw path，README明確要求
先還原或傳staging --db，不改CLI。舊DB空WAL及SHM也一併逐位元封存，
確認無open handles後才移除對應cold bundle members。

## Archive publication and restore

串流tar.zst，不產生大型uncompressed tar duplicate。Sorted filenames、
uid/gid/name/mtime canonicalized；DB/metadata content保持原bytes。
每個archive內嵌manifest，外部manifest含archive SHA256與各member hashes。
zstd -t、列出所有members、安全temporary restore、逐檔SHA256比對
全部通過後，重新檢查original identity/hash/open handles，才逐條exact path
unlink並rmdir空snapshot目錄。無broad rm、glob deletion或git clean。
Restore測試沒有再次跑quick_check，但restored DB SHA256完全相等於
已quick_check=ok的原始DB，保持相同的SQLite內容。

|Archive|Raw GiB|Compressed GiB|Net GiB|Archive SHA256|
|---|---:|---:|---:|---|
| 20261005_065718_+0800.tar.zst | 2.5004 | 0.1745 | 2.3259 | 23363f0aa5f2f6e7e8125649015464b478e468b4689c603ff70bd1752cb0d449 |
| 20261005_185454_+0800.tar.zst | 2.6417 | 0.1860 | 2.4558 | 21b873c26a9e84e7ae06af57376c94a0efd97316a714d387a81b699e66e1aa42 |
| 20261006_064237_+0800.tar.zst | 2.9160 | 0.2081 | 2.7079 | 13b1f69ae54d3ef29c503322c6f45bcb9785a361dadaa813131381a13370f066 |
| legacy_hyperliquid_lead_lag_20261007_210534_+0800.tar.zst | 0.9255 | 0.1754 | 0.7502 | 857b87a31960322149698d490908de75ddb58ccdba4d835b0cefc7056f29170c |

Restore procedures: data/analysis_archives/README.md。

## Space

|Metric|Before GiB|After GiB|
|---|---:|---:|
|Filesystem free|14.5597|22.8221|
|Repo logical, including .git|24.9377|16.6980|
|Analysis snapshots|11.5186|3.4604|
|Analysis archives + manifests|0.0000|0.7440|

Free-space gain=8.2624GiB；
exact raw-minus-archive net=8.2397GiB。細小差異包含filesystem
其他活動/metadata，不能據此證明APFS COW causality。25GiB未達，不刪protected
資料以追目標。量測在最終report寫入前，因此最後數KB文件未包含。

## Restart readiness and separate runtime finding

Tracked tree clean，git working/cached diff check通過。HEAD正確，next full
process restart預期載入900秒備份、unchanged skip、PhaseA execution=False、
storage retention及freshness semantics v2。儲存/DB integrity條件已達成。

但本次讀取terminal累積Nautilus stderr才發現重複KeyError('retry_at')，
stack: bot/adapter_overrides.py publish_l2_snapshot_if_due line919。
例示timestamp2026-10-07T12:15:20Z（20:15 Taipei），並非本次archive造成。
同一HEAD仍有該路徑：skipped_unsubscribed分支setdefault(id,{})與後續
state['retry_at']缺省初始化可能衝突，需獨立重現/確認；此任務不改碼。
先前只查Loguru檔案的「近期無ERROR」不能代表Nautilus terminal全路徑
無錯誤。錯誤影響範圍尚未充分驗證，因此不給overall safe restart certification，
SAFE_TO_MANUALLY_RESTART=NO；storage readiness本身為YES。

## Machine-readable result

```text
OFFLINE_ARCHIVE_RECLAIM
BOT_STOPPED=YES
BOT_PID=31469
GRACEFUL_SHUTDOWN=YES
HEAD=b034b4ecadf0714b8f88d6f879936924bbd593f8
TRADE_JOURNAL_DB_GIB=2.9198
TWAP_DB_GIB=1.1232
HYPERLIQUID_ACTIVE_DB_GIB=4.3049
RECOVERY_BACKUP_GIB=2.9198
RETAINED_BACKUP_INTEGRITY=OK
SNAPSHOT_BUNDLES_EVALUATED=4
SNAPSHOT_BUNDLES_ARCHIVED=3
SNAPSHOT_RAW_DIRS_REMOVED=3
FRESHNESS_AUDIT_RAW_RETAINED=YES
LEGACY_HYPERLIQUID_ARCHIVED=YES
LEGACY_HYPERLIQUID_RAW_REMOVED=YES
RAW_GIB_ARCHIVED=8.9837
ARCHIVE_GIB_CREATED=0.7440
NET_GIB_RECLAIMED=8.2397
FREE_SPACE_BEFORE_GIB=14.5597
FREE_SPACE_AFTER_GIB=22.8221
TARGET_20_GIB_MET=YES
TARGET_25_GIB_MET=NO
ACTIVE_DATABASES_DELETED=NO
ACTIVE_WAL_SHM_DELETED=NO
SOLE_RECOVERY_BACKUP_DELETED=NO
PROTECTED_FRESHNESS_RAW_DELETED=NO
ARCHIVE_RESTORE_TESTS=4 archives passed zstd integrity; all members restored SHA256 identical; original DB quick_check OK
SAFE_TO_MANUALLY_RESTART=NO
STORAGE_READY_FOR_RESTART=YES
EXPECTED_BACKUP_INTERVAL_AFTER_RESTART_SEC=900
CODE_CHANGED=NO
COMMIT=NONE
PUSHED=NO
BOT_RESTARTED=NO
```

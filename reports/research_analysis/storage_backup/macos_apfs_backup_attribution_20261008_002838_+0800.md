# MACOS APFS BACKUP ATTRIBUTION

## 結論與證據界限

一次完整SQLite備份確實重現約2.958GiB的free下降，但目前pathname下repo淨配置量只增加64KiB。20分鐘仍未回收，不能稱只是幾秒暫時double-allocation。最合理假設是Time Machine snapshot保留被atomic replace換掉的舊備份區塊：00:02:42快照先於00:08:20備份、舊inode消失、沒有deleted-open handle、減少量接近一張image。這是強烈時間/量級一致性，不是特定APFS extent ownership直接證明；不能宣稱已完全解釋此前5.2GiB。

## Static baseline

Bot0；HEAD=33896ce9ba123708999c896bd7c2fd4023412d5f；起始2026-10-07T23:58:16.086089+08:00。Source logical=3172360192 allocated=3182026752 inode=200895787 mtime_ns=1791386883120380305。Old backup logical=3172315136 allocated=3179253760 inode=227592145。Repo initial logical=14050992243 allocated=14188261376。st_blocks*512為檔案報告的allocation，不等於全volume獨占physical extents；clones/shared extents NOT_MEASURABLE。

```text
Filesystem        Size    Used   Avail Capacity iused ifree %iused  Mounted on
/dev/disk3s1s1   228Gi    17Gi    20Gi    46%    447k  208M    0%   /
Filesystem     1024-blocks      Used Available Capacity iused     ifree %iused  Mounted on
/dev/disk3s1s1   239362496  17585440  20824380    46%  446774 208243800    0%   /
```

Container disk3/Data disk3s5/APFS4KiB allocation block，與VM/System共享container空間；df / 的free不是repo專屬配額。完整diskutil metadata在JSON，不更動磁碟。

## Snapshot / deleted-open inventory

最初Data TM快照1個：2026-10-07-230042；idle期間增加2026-10-08-000242，完成時2個，均Purgeable Yes。System volume另3個OS-update snapshots，沒有把它們算成3個Time Machine快照。System快照不直接包含Data repo；Data快照可能包含舊backup。backupd初始Running0，但仍發現後續local snapshot；Running0不是不存在快照的證明。沒有清除/薄化快照。

大於100MiB deleted/unlinked files：baseline及post各0。lsof使用目前使用者可見權限，不宣稱root全機可見；小型deleted plist/cache不解釋GiB。沒有舊backup/temp open handle。

## Idle control

21次樣本間隔30sec，10分鐘完整。free_delta=end-start，負值=減少：-0.158581GiB；range=0.159126GiB。Repo logical delta=-41bytes、allocated delta=-4096bytes。沒有multiGiB idle collapse，但有約0.159GiB背景變動；其process原因未測得。

## Exactly one production-equivalent backup

2026-10-08T00:08:20.112021+08:00 -> 2026-10-08T00:08:25.664340+08:00，5.552秒，success=true。直接用未改TradeJournalDB.flush_backup(force=True)，productionmaintenance_lock、readonlysource、spaceguard、source.backup(destination)、os.replace、completedbackupmetadata全保留；不跑constructor(避免schema初始化/worker)，沒有cp/cloning、沒有第二次backup、没有修改900sec政策。手動force label=forced_boundary，不是新增periodic cadence。當時productionguard required=6344720384bytes/free=21153779712bytes，實驗另保留5GiB margin。Source SHA前後相同、size/inode/mtime/allocation相同。

|時間|階段|Free GiB|舊final inode|temp/new inode|temp allocated GiB|
|---|---|---:|---:|---:|---:|
|2026-10-08T00:08:20.114618+08:00|backup|19.699226|227592145|227606139|0.001774|
|2026-10-08T00:08:21.146017+08:00|backup|19.285755|227592145|227606139|0.414066|
|2026-10-08T00:08:22.157587+08:00|backup|18.754227|227592145|227606139|0.945316|
|2026-10-08T00:08:23.170498+08:00|backup|18.222641|227592145|227606139|1.476566|
|2026-10-08T00:08:24.213376+08:00|backup|17.566322|227592145|227606139|2.132816|
|2026-10-08T00:08:25.361150+08:00|backup|16.847363|227592145|227606139|2.851566|

舊final inode=227592145，temp及newfinal同inode=227606139，支持atomic rename。每1秒取樣temp peak配置=2.851566GiB，是採樣lower bound，5.552秒內最後image完成時可能更高；完成newbackup allocated=2.960941GiB。當temp存在，舊final配置仍約2.961GiB，double-allocation可直接觀察。

## Reclamation / accounting

Recovery定義為完成期最低free到指定post時點的正向回升，summary clamp0；signed negative另列，表示未回收且有額外背景下降。UNRECOVERED為backup前last idle到post10min的df淨差，包含背景drift，不把每byte都歸因backup。

|Post|時間|Free GiB|signed recovery GiB|baseline deficit GiB|
|---|---|---:|---:|---:|
|post_immediate|2026-10-08T00:08:25.664349+08:00|16.743443|0.000000|2.957668|
|post_30|2026-10-08T00:08:56.103728+08:00|16.743137|-0.000305|2.957973|
|post_60|2026-10-08T00:09:26.102891+08:00|16.738911|-0.004532|2.962200|
|post_120|2026-10-08T00:10:26.095689+08:00|16.734985|-0.008457|2.966125|
|post_300|2026-10-08T00:13:26.095562+08:00|16.724361|-0.019081|2.976749|
|post_600|2026-10-08T00:18:26.184348+08:00|16.680710|-0.062733|3.020401|
|post_1200|2026-10-08T00:28:25.675336+08:00|16.667393|-0.076050|3.033718|

backup image logical=2.954491GiB；finalbackupallocated增量=32768bytes，source unchanged；repo10min logical增量=77824bytes，allocated增量=65536bytes；immediate df drop-2.957607GiB尚不能由pathname allocation淨量解釋。未直接量到old extents是否被snapshot pin/shared extents，因而此差額不能當已證實snapshot exclusive bytes。

## Hypothesis classification

|假設|分類|判斷界限|
|---|---|---|
|FULL_BACKUP_DOUBLE_ALLOCATION|SUPPORTED|舊backup與新temp同時存在/配置可見|
|APFS_COW|PLAUSIBLE|快照-retention機制/時點合理；exact blocks未量到|
|ATOMIC_REPLACE|SUPPORTED|tempinode成為final，oldfinalinode被替換|
|DELAYED_RECLAMATION|NOT_SUPPORTED|觀察到20min無正向回收；不排除更久或snapshot到期後回收|
|TIME_MACHINE_SNAPSHOT|PLAUSIBLE|存在已確證，特定backup extents保留仍屬PLAUSIBLE|
|OTHER_APFS_SNAPSHOT|NOT_SUPPORTED|3個System snapshot不是Data repo直接保留證據|
|DELETED_OPEN_FILES|NOT_SUPPORTED|無使用者可見大型unlinkedfile/backuphandle|
|PURGEABLE_ACCOUNTING|NOT_MEASURABLE|snapshot標purgeable但未取得精確purgeable-byte/available-for-important-usage|
|OUTSIDE_PROCESS_WRITES|NOT_MEASURABLE|有小幅idle/backgrounddrift；fs_usage需root，未量到processbytes|
|REAL_REPO_GROWTH|NOT_SUPPORTED|repo64KiB不能解釋3GiB，非完全zero growth|

未獲安全的clone/sharedextent或snapshot-exclusivebytes查詢方法，標NOT_MEASURABLE。fs_usage只嘗試5秒即要求root；未自動sudo/收集敏感內容/終止外部程序。單次事件隔離支持fullbackup創建造成瞬間減少，之後持續缺口最符合snapshot保留，但本實驗沒有無snapshot counterfactual，不能證明唯一原因或回溯完整5.2GiB。

## Engineering options（未實作）

|選項/優先|Expected storage impact|Recovery safety|Implementation risk|Research impact|
|---|---|---|---|---|
|1.另一實體filesystem/externalSSD目的地|把每次~3GiB image allocation移出系統Data；若目的地仍被snapshot會在該volume累積|必須驗證restore、destination可用性、保留primary|LOW-MEDIUM；拔除/容量/掛載失敗須fail-safe|NONE|
|2.更低頻periodic backup|降低fullcopy IO及跨snapshot代數；不消除單次image分配|增加RPO遺失窗口；journal recovery需明確接受|LOW code / MEDIUM recovery|NONE evidence，崩潰恢復範圍增大|
|3.只rollover/shutdownfullbackup|最多~3h週期+正常shutdown；頻率大降|突然斷電/kill前latestbackup更舊|MEDIUM recovery；不可先做|NONE正常，崩潰可丟更多資料|
|4.daily/size-basedTradeJournalrollover|只備份active小partition；冷data不重複fullcopy|跨partitionexposure/PnL/identity/intent恢復必須完整|HIGH|NONE若canonicaljoin/provenance完整，否則HIGH|
|5.rolling small partitions|最有效限制activeimage及snapshotretainedimage尺寸|同上，跨run/riskauthority複雜|HIGH|NONE設計正確才成立|
|6.immutablecoldpartitions+compression|冷歷史大小大降且不反覆備份；節省%須實測非猜測|hash/manifest/restore/active-recovery boundary不可破|MEDIUM-HIGH|NONE若lossless且可還原|
|7.incrementalbackup/WAL-awarearchival|可望寫changedpages避免每次fullimage；不能假稱SQLitebackupAPI目前已持續增量|WALsequence/原子基準/恢復鏈需驗證，不可livecpWAL|HIGH|NONE正確實作，錯誤可致資料不完整|
|8.同container另一APFSvolume|通常不增加physicalfree、不保證解決snapshotretention|需恢復與snapshotpolicy審查|MEDIUM|NONE|

最小風險方向先評估獨立實體backup目的地；長期partition+coldcompression可處理持續成長，但須另輪設計與恢復測試。不建議直接disablebackup或purgesnapshots。

## Safety

沒有bot、code/config/commit/push/backup-policy變更。Source SHA/stat未變。FILES_DELETED=0代表沒有手動cleanup或DB/snapshot刪除；授權的唯一backup正常os.replace會解除old pathname inode並原子更新backup與retention metadata，這屬實驗本身，不能宣稱backupfile未變。沒有VACUUM/WALSHMdelete/checkpoint。

```text
MACOS_APFS_BACKUP_ATTRIBUTION

BOT_RUNNING=NO
SOURCE_DB_GIB=2.9544906616210938
BACKUP_DB_GIB=2.9544906616210938
IDLE_FREE_SPACE_DELTA_GIB=-0.15858078002929688
SINGLE_BACKUP_PEAK_FREE_DROP_GIB=2.9576683044433594
POST_BACKUP_RECOVERY_1MIN_GIB=0
POST_BACKUP_RECOVERY_5MIN_GIB=0
POST_BACKUP_RECOVERY_10MIN_GIB=0
UNRECOVERED_AFTER_10MIN_GIB=3.0204010009765625
TIME_MACHINE_LOCAL_SNAPSHOTS=2
OTHER_APFS_SNAPSHOTS=3
DELETED_OPEN_LARGE_FILES=0
FULL_BACKUP_DOUBLE_ALLOCATION=SUPPORTED
APFS_COW=PLAUSIBLE
ATOMIC_REPLACE=SUPPORTED
DELAYED_RECLAMATION=NOT_SUPPORTED
TIME_MACHINE_SNAPSHOT=PLAUSIBLE
OTHER_APFS_SNAPSHOT=NOT_SUPPORTED
DELETED_OPEN_FILES=NOT_SUPPORTED
PURGEABLE_ACCOUNTING=NOT_MEASURABLE
OUTSIDE_PROCESS_WRITES=NOT_MEASURABLE
REAL_REPO_GROWTH=NOT_SUPPORTED
BEST_SUPPORTED_ROOT_CAUSE=Full SQLite image allocated before atomic replace; persistent ~image-sized loss is consistent with pre-backup Time Machine snapshot retention, but exact retained extents remain unmeasured.
TOP_ENGINEERING_MITIGATION=Evaluate backup destination on a separate physical filesystem/external SSD while preserving verified recovery images; no policy change made.
CODE_CHANGED=NO
ACTIVE_DB_MUTATED=NO
FILES_DELETED=0
BOT_RESTARTED=NO
COMMIT=NONE
PUSHED=NO
```

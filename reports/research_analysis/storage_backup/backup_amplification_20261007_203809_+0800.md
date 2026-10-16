# TradeJournal backup amplification audit

## 診斷與證據

目前 running process 仍使用舊碼；本修改沒有載入、沒有 restart。原本預設
30 秒是完成備份後的等待時間；每次 read-only source SQLite online backup
写入固定 temporary，再 os.replace 到同一 completed target。每次建立完整
image，不是跨次持續的 incremental backup handle。沒有第二個週期性 authority。

Source: `logs/trade_journal.db`；destination: `backups/trade_journal.db`；
temp: `backups/.trade_journal.db.tmp`。實測 destination 2.911732 GiB，
相鄰 publication mtime 間隔 40.755856 秒（單一觀測，不能代表長期分布），
約 88.33 次／小時。備份在少量已提交頁面變更後仍重建全檔。
完整 image 的臨時額外 allocation 約 2.912 GiB；primary + old completed
+ new temporary 約 8.735 GiB，未含 WAL、APFS snapshots 或 rollback。
現有 preflight 為 2 * image_bytes + existing temp rollback_bytes，預留另一
image 大小供 primary/WAL/緊急成長，未縮減。跨 filesystem／競爭消耗仍可能
導致預檢後失敗，既有 failure cooldown 保留。

Read-only `tmutil listlocalsnapshots /` 列出三個 com.apple.os.update 快照，
沒有 com.apple.TimeMachine 快照。`df -h .` 約 14 GiB available。
APFS_COW_CAUSALITY=PLAUSIBLE：完整 replace 可保留被 snapshot 引用的舊
blocks，但沒有 block 級 attribution。TIME_MACHINE_LOCAL_SNAPSHOTS_PRESENT=NO
（此次命令可見範圍），系統更新快照存在不代表 Time Machine 在保留每代備份。
未取得 snapshot creation window 與 replacement 的精確相關證據，不能宣稱
已確認數 GiB/hour 的自由空間變化原因。沒有刪除 snapshot／歷史 DB。

## RPO 與最小修正

Primary 為 WAL / synchronous=NORMAL，normal restart 從 primary 恢復市場
risk counters、session PnL、shadow lifecycle／研究 evidence。NORMAL 對最近
交易不保證突然斷電完全 durable。備份不是每次下單的交易安全 prerequisite，
也不是 normal restart authority；primary 遺失／損壞時 backup 具有風險與
forensic 恢復價值，但不能自動信任舊 risk state。

選擇 15 分鐘 recovery image target（不是 guaranteed RPO）：較 30 分鐘
保守，同時大幅降低完整重寫。若 primary 遺失，最後 15 分鐘以上的風險
state 可能缺失，需 venue／operator reconciliation；低磁碟／失敗會使 RPO
更長。此改變明確記錄，保留 fail-closed primary readiness。

唯一預设 authority: monitoring/trade_journal_db.py DEFAULT_BACKUP_INTERVAL_SEC=900。
launcher/settings 未提供第二個值；constructor 的 explicit overrides 保留。
worker 每 900 秒 eligibility 檢查；dirty=false 跳過 full image，記錄
BACKUP_SKIPPED_UNCHANGED。沿用已提交寫入的 dirty authority，補足 schema、
run start、reconciliation 的 schedule，覆蓋 updates 而不依賴 max row ID、
mtime、page count、data_version；僅涵蓋此 class 管理的寫入。
Concurrent commit 在 publication 期間維持 dirty，下一個周期補備份。

Shutdown／scheduled rollover 共用 TradeJournalDB.stop -> flush_backup(force=True)，
可绕過 interval/cooldown，包括 clean source；沒有新增 rollover 前後雙重
備份。Startup schema mark dirty，不额外強制 full backup。Migration 的一次性
online copy 是 migration source->primary，不是競爭 backup scheduler。

保留單一 worker、flush lock、interprocess maintenance lock、read-only source、
atomic publication、一個 completed target（retention cap <=2）、失敗 cleanup、
low-disk defer/cooldown。sole known-good target 不原地覆寫，失敗不降級 primary
write health。保留既有 stale-temp maintenance，不新增 live cleanup。
新增成功/跳過 log 每 publication/interval 一次，cached health 提供 image/free
space/success timestamp；不逐筆寫 DB telemetry。

## Before / after（logical rewrite，不是 retained disk usage）

|模型|次／日|GiB／小時|GiB／日|
|---|---:|---:|---:|
|原30秒理論上限|2880|349.41|8385.79|
|目前單一40.76秒觀測外推|2119.94|257.20|6172.70|
|新15分週期理論上限|96|11.65|279.53|
|新週期+每3小時shutdown 8次保守上限|104|12.62|302.82|

Periodic-only reduction=96.67%；含8次 boundary 比舊periodic基線減少
96.39%。實際 duration、startup reset、unchanged skip、exposure
defer 與失敗會影響頻率。DB 增長會使每次 image 變大；這不是磁碟容量上限。

## 替代方案

A. 降低 full backup 頻率：採用，最小変更。
B. Persistent incremental handle：需重新設計 publication/lifetime，單次 backup
API pages stepping 不等於跨次 incremental；不採用。
C. WAL archive：需一致性、sequence/recovery/rotation 協定，不採用。
D. DB partition rollover：schema/reader/risk continuity 改動，不納入。
E. Closed archive compression：歷史資料另案，可節省空间，不改本 patch。
F. Change log：複製恢复系統与交易一致性风险，不採用。

## 驗證與安全

Focused storage/journal/rollover: 86 passed。Full suite: 1156 passed，只有
已知 websockets deprecation warning。git diff --check passed。
Coverage: default authority、900秒 due/30秒不due、unchanged skip、changed
source、forced shutdown/boundary、serial races、cooldown、failed temporary
cleanup、low disk primary safety、sole good target、retention、stale temp、
interval-bounded logging、run metadata commits。現有 rollover tests 保留。
No strategy/research/freshness/config/LeadLagDB/TWAP/analysis snapshot change；
無 active DB mutation，無 bot restart，無 push。Report 留 untracked。

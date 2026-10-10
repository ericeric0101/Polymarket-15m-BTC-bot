# Safe storage reclaim before restart

本次無 DELETE_SAFE 候選，無刪除／壓縮／搬移／分類 metadata 修改。
既有 maintenance dry-run candidate_count=0。所有 active/source DB、WAL/SHM、
current log、backup publisher temp、sole recovery target、BTC/Parquet、source、
config、tests、reports 都受保護。snapshot 身份/大小/mtime 保持不變。

PID31469 唯一 dry-run，loaded3b50637、backup cadence仍舊30秒；HEAD b034b4e
未透過此次操作載入。lsof多次採樣確認 journal/main WAL/SHM、current log、
backup temp及tmp-journal由bot開啟；兩個research DB連線短暫未被採樣捕捉，
但recent journal/prediction timestamps與writer配置證實仍是active required。
open=no僅代表採樣，不作inactive/delete證據。

## 清理優先次序

A. backup temp正在liveworker使用，不是stale orphan。
B. 只有一個completed TradeJournal target，必須保留。
C/D. 四組journal/TWAP snapshot均被existing report/recomputation引用，
共11.519GiB；不能因較新snapshot存在就刪除。沒有retention.json，
freshness_audit另有default protection。
E. logs/hyperliquid_lead_lag.db 約0.926GiB，舊decision range Sept7-19，
active data/research decision range從Sept21開始；獨有historical evidence，
ARCHIVE_RECOMMENDED，不能DELETE_SAFE。歷史CLI仍預设讀舊路徑，現行
canonical regime不依賴它不等於歷史reproducibility不依賴它。
F. legacy terminal_bot.log.gz 未符合現行timestamp-named rotated log
selector，unknown origin保留；無eligible expired compressed logs。

## Inventory

|Absolute path|GiB|mtime Taipei|Classification|Reference/reason|
|---|---:|---|---|---|
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/logs/hyperliquid_lead_lag.db | 0.926 | 2026-09-23T07:40:32.193739+08:00 | ARCHIVE_RECOMMENDED | storage_attribution_20261007_090622.md; legacy CLI defaults |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/logs/trade_journal.db | 2.914 | 2026-10-07T20:44:57.020849+08:00 | ACTIVE_REQUIRED | runtime/recovery authority |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/research/hyperliquid_lead_lag.db | 4.292 | 2026-10-07T20:45:13.062536+08:00 | ACTIVE_REQUIRED | runtime/recovery authority |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/research/twap_forward_shadow.db | 1.120 | 2026-10-07T20:45:00.059508+08:00 | ACTIVE_REQUIRED | runtime/recovery authority |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261006_064237_+0800/twap_forward_shadow_snapshot.db | 0.679 | 2026-10-06T06:43:34.126203+08:00 | PROTECTED_RESEARCH | postfix_weekday_weekend/postfix_stability_and_regime_20261006_065413_+0800.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261006_064237_+0800/trade_journal_snapshot.db | 2.237 | 2026-10-06T06:42:46.374739+08:00 | PROTECTED_RESEARCH | postfix_weekday_weekend/postfix_stability_and_regime_20261006_065413_+0800.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_185454_+0800/twap_forward_shadow_snapshot.db | 0.569 | 2026-10-05T18:55:43.969911+08:00 | PROTECTED_RESEARCH | regime_comparison_monday_interim/summary.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_185454_+0800/trade_journal_snapshot.db | 2.072 | 2026-10-05T18:55:04.699992+08:00 | PROTECTED_RESEARCH | regime_comparison_monday_interim/summary.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/freshness_audit_20261006_231849_+0800/twap_forward_shadow_snapshot.db | 0.896 | 2026-10-06T23:19:09.903914+08:00 | PROTECTED_RESEARCH | prediction_freshness/historical_freshness_recomputation_20261007_000418_+0800.json |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/freshness_audit_20261006_231849_+0800/trade_journal_snapshot.db | 2.564 | 2026-10-06T23:26:06.822935+08:00 | PROTECTED_RESEARCH | prediction_freshness/historical_freshness_recomputation_20261007_000418_+0800.json |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_065718_+0800/twap_forward_shadow_snapshot.db | 0.521 | 2026-10-05T06:58:48.452391+08:00 | PROTECTED_RESEARCH | regime_comparison_early_weekday/summary.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_snapshots/20261005_065718_+0800/trade_journal_snapshot.db | 1.979 | 2026-10-05T06:58:11.380280+08:00 | PROTECTED_RESEARCH | regime_comparison_early_weekday/summary.md |
| /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/backups/trade_journal.db | 2.914 | 2026-10-07T20:45:12.442258+08:00 | RECOVERY_BACKUP_REQUIRED | runtime/recovery authority |

完整 open/writer/reader/protection/newer-authority fields 見 inventory.json。
Legacy SHA256=6c76e140abdf981a99cebf4a7bc3589a3ea2d4aa6c8ed8512a2c4499fc8e8934

## Validation

Backup quick_check=ok for published inode 227380197 (3129737216 bytes),
elapsed 100.6s。但舊bot在check期間替換target，
same_generation=false，因此不能聲稱目前path最新generation也完成quick_check。
保留completed target，completion metadata為sqlite_online_backup。沒有鎖住
worker、沒有額外複製3GiB、沒有active DB integrity scan。若必須對最新generation
取得穩定proof，需另行授權graceful stop或安全保留穩定image；此次不執行。

Journal及prediction newest IDs從969247/285668增至969331/285749；
queue=0/drops=0，recent structured errors=0，console errors=0。
Tracked tree clean, cached/working diff check通過。未修改DB/WAL/SHM。

Free before=14.368GiB；after=14.598GiB。
Repo logical bytes excluding .git before=24.830GiB；
after=24.835GiB。
Cleanup reclaimed=0GiB。差額是bot ongoing growth/temp/APFS/system activity，
不能算成cleanup收益；瞬時測量可能落於backup temp不同階段。
20/25GiB目標未達。SAFE_TO_RESTART_ON_B034B4E=NO（未達本任務保守headroom
與latest-generation verification標準；不是指新code測試失敗）。
不為達目標刪unique/protected資料。不停止、不重啟、無commit/push。

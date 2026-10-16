# POST_RESTART_RUNTIME_VALIDATION

觀測：2026-10-07 21:24:58–22:11 Asia/Taipei，唯一 PID 48701，run_id run_1791379514_211dd618。
Loaded SHA: e4e87ee0eb43a620b6f914f165578c165ee7d9e8；durable manifest dry-run=true，Fast Follow execution=false，effective mode=shadow，cycle_idx=1。Tracked tree clean。

## 執行與 L2

新 run 未見 FF ownership/veto/FOK intent/submit、FAST_FOLLOW_ERROR、record_blocked TypeError 或 retry_at KeyError。Order events 有5筆 ORDER_DRY_RUN_SUBMITTED，非真實下單。
已觀察21:30、21:45、22:00交接；current quote=2/current L2=2/prewarm quote=2/logical L2=2，cycle未重建。18759次策略L2 callbacks；603筆 entry trace 有正的 nearby depth，佐證中立 l2_update_ts_by_inst gate正常提供depth。2秒 freshness threshold未變。Missing_l2：6筆 ENTRY_DECISION_TRACE、5筆 BUY_PATH_DIAGNOSTIC，不相加視為獨立拒單。Explicit stale_l2標籤0；目前來源會把缺失/過期L2同樣傳為None，因此無法獨立計數全部stale情況。
沒有Polymarket L2 transport reconnect測例；Chainlink於22:01:08因Cloudflare1001重連不等於L2 reconnect。DECISION_POINT_L2在此run的TWAP DB為0，不以不存在的證據宣稱其持久化通過；native callbacks及maker depth證據成立。

## 備份

Periodic publishes：21:40:23.582、21:55:31.381、22:10:37.578。
間隔907.799s、906.197s，含完成時間；configured interval900s。Periodic=3，forced/lifecycle=0，unchanged skips=0（持续寫入，未實際觸發此支路）。沒有30–40秒重複全量publication。22:11:18 backups只有一個completed DB、retention metadata及lock，沒有殘留temp；備份未做active integrity scan。

## Writers / freshness

Journal events=3822，prediction rows=2290；最新health queue=1/20000，drops=0，write_errors=0，diagnostic enqueue failures=0。所有2290 predictions freshness_clock_semantics_version=2。未見新SQLite_FULL/CANTOPEN、backup failure、研究writer failure。既有TWAP db_size_cap導致derived Storage=CRITICAL仍存在；journal centralized storage policy last reported NORMAL，不可混淆兩者。

|Market start Taipei|Slug|Rows|Joint fresh|Pct|
|---|---|---:|---:|---:|
|21:15|btc-updown-15m-1791378900|171|156|91.23%|
|21:30|btc-updown-15m-1791379800|796|780|97.99%|
|21:45|btc-updown-15m-1791380700|748|728|97.33%|
|22:00|btc-updown-15m-1791381600|575|569|98.96%|

21:15是啟動partial市場，22:00尚未完成；完整新市場21:30 joint=780/796=97.99%、21:45 joint=728/748=97.33%。此為freshness統計，非完整SYNCHRONIZED_USABLE gate。

## 磁碟

|Time Taipei|Free GiB|
|---|---:|
|Pre-start21:22|22.7926|
|2026-10-07T21:40:37.144538+08:00|22.7306|
|2026-10-07T21:55:00.902582+08:00|22.7007|
|2026-10-07T22:00:02.271475+08:00|24.4621|
|2026-10-07T22:05:02.164828+08:00|24.2353|
|2026-10-07T22:11:01.377876+08:00|20.9383|

End22:11:18=20.9364GiB。淨少1.8562GiB，但途中有升至24.4621GiB的外部/檔案系統變動。第三次backup附近由24.2353降至20.9383，約3.30GiB，沒有殘留temp。Primary DB淨增約0.0169GiB、TWAP約0.0108GiB、Hyperliquid約0.0411GiB，無法解釋全部filesystem差額。只證明時間相關，未確認APFS/COW或其他程序原因。不用這段噪音推估長期GiB/h。
STORAGE_SHORT_TERM_BEHAVIOR=RAPID_DECLINE（episodic，backup附近大幅下降）；舊30秒publication已不再出現，但仍有數GiB級磁碟下降，不可宣稱storage問題完全解除。

## Verdict

SAFE_TO_CONTINUE_24H_DRY_RUN=NO：不是已觀察交易故障，而是24h無人監控驗證未通過；TWAP既有容量guard及backup附近大幅磁碟變動仍需研究。依使用者要求保留bot運行，不自行停止或重啟。3h rollover尚未到期，未判失敗。
CODE_CHANGED=NO / COMMIT=NONE / PUSHED=NO。分析未直接修改active DB/WAL/Parquet，正常bot持續寫入。監控完成後automation PAUSED。

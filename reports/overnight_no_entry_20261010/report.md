# 2026-10-10 夜間 LIVE 無送單原因稽核

結論：台北週末 calendar policy 明確禁止新的 LIVE BUY。這是所有市場共通、且足以阻止下單的 gate；不是沒有訊號，也不能以此次零成交推論 entry alpha 或止損有效性。全程唯讀分析，沒有修改 runtime、設定、auth-preflight 或 DB，沒有操作交易程序、沒有 push。

## 資料截止與執行狀態

資料用單一 SQLite mode=ro／query_only read transaction 擷取，最大 strategy/order id 保留於 summary.json。第二 run 尚運行，因此此報告是截至該 snapshot 的分析，不是假裝已結束的整夜報告。截取時最新 strategy event 為台北 05:25:32；程序檢查仍發現 PID 53572 執行 run_bot.py --live。

| run | 台北開始 | 台北結束／snapshot 最新事件 | cycle |
|---|---|---|---|
| run_1791565374_d860173c | 01:03:36 | run ended 04:02:56 | 1 |
| run_1791576205_96f9321a | 04:04:01 | 05:25:32，尚未 ended | 2 |

兩段 manifest 的 HEAD 均為 f1a134e999edd3f2e03fff80f22a67b8a13242ec，config_hash 同為 cf77c6b336d69e59caf42d8055dc6f77171b121e50b4ff899d8385a0a3eaa52a，pid 均53572。這是同一 parent process 的 node rollover，不是兩個同時交易的程序。COLLECTION_LIFECYCLE 有 cycle1 disconnect complete(clean=true) 與 cycle2 new node/strategy started。

從首次 strategy started 至 snapshot 最新事件約4小時22分，已超過原 Tiny-LIVE 兩小時規格；沒有在四市場後整體停機。本報告沒有替使用者停止程序。

## 為何一單也沒有

1. bot/entry_session_policy.py:45–52：new_buy_session_decision 使用 Asia/Taipei 真實日曆，weekday>=5 時回傳 allowed=False / taipei_weekend_observation_only。2026-10-10 為週六，兩段 run 全在禁開 BUY 的區間。
2. bot/quote_runtime.py:200 起：session_forced_sell_only 取消既有 maker BUY、保留 protective exit。cycle1 的 ENTRY_SESSION_BUY_BLOCKED=157、cycle2=66，合計223筆；所有 reason 都是 taipei_weekend_observation_only，18個獨立市場均有紀錄。事件有節流，223不是機會或樣本數。
3. bot/quote_runtime.py:369 起：在 mutable desired quotes 經 sizing/confirmation 後再套週末 gate，將 BUY should_quote=False、force_cancel_existing=True。
4. bot/order_submission.py:93 起：若仍到達最後提交入口，再以同一 session gate 擋下 BUY。這次零正式 submit；未出現 ORDER_SKIP_ENTRY_SESSION 不表示 gate 未生效，因為更早已擋下。
5. balance_forced_sell_only 是診斷名稱混用：quote_runtime 將 session_forced_sell_only OR 到 forced_sell_only，quote_service.py:1916–1918 將 BUY 的通用 forced_sell_only 都寫成 balance_forced_sell_only。此名稱無法區分餘額／週末／journal／rehydration。這次有明確週末事件；ACCOUNT_SUMMARY 共809筆，usdc_balance 均23.489383，沒有 SESSION_BUY_LOCKED。沒有進入真正提交邊界，不能據此宣稱 allowance／venue balance preflight 完全已驗證。

兩段 MAIN_SIGNAL_CANDIDATE_LIVE 共424筆，其中347筆 main_candidate_side 是 BUY_UP/BUY_DOWN，覆蓋18個市場。因此「完全沒訊號」不成立；這些是重複採樣且不是可成交的正式 entry eligibility。

典型受阻候選：01:05:31 active_side=DOWN、score=-0.242161、price=.63、TTE569.65秒、robust_net=.05、market_buy_count=0、inventory_qty=0，但 should_quote=False / balance_forced_sell_only。04:08:10 active_side=UP、score=.224795、price=.61、TTE409.83秒也相同。score過某一門檻不代表通過所有 entry／depth／execution條件；移除週末 gate 後是否成交未驗證。

另外 research_snapshot.weekday_weekend 使用 America/New_York（bot/live_entry_research.py:classify_time_et），所以 Friday ET會標 weekday；LIVE authority 用 Taipei Saturday。這是時區定義不同，不能以 research 的 weekday 標籤認定週末 gate 出錯。is_taipei_weeknight_entry_session 的舊校準分類會把週六凌晨算前一晚，實際 new_buy_session_decision 則不使用它。

## 次要 entry gate 診斷

下表是事件次數，同一市場反覆評估、不同gate會交替出現，不能當成獨立樣本或計算「哪個 gate造成多少成交損失」。週末 gate仍是跨市場的硬阻擋。

| ORDER event | 次數 |
|---|---:|
| ENTRY_EDGE_OBSERVATION | 467 |
| ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW | 476 |
| ORDER_SKIP_LOCKED_SIDE_INVALIDATED | 722 |
| ORDER_OBSERVE_BUY_BLOCKED | 187 |
| ORDER_SKIP_DIRECTIONAL_ENTRY_GATE | 758 |
| ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE | 688 |
| ORDER_SKIP_TWAP_REFERENCE_DEGRADED | 3 |

具體門檻紀錄：first_entry_too_early 為TTE>600秒；directional_entry_gate樣本 required_score_abs=.20；directional_first_entry_gate樣本 required_score_abs=.22。鎖定方向失效也反覆出現。這些是既有策略 gate，不應為了四市場觀察而放寬。

## 市場與結果（市場為單位）

18個unique市場、19個run-market段（04:00跨rollover重複，只算一個市場）；17個市場有MARKET_SETTLEMENT。01:00為startup warm-up、04:00受node切換中斷；05:15在snapshot時尚未結束。15段其餘市場有settlement，不代表已全面通過無gap與entry/stop telemetry完整性驗證。所有市場均無正式 submit/fill。

| 台北市場開始 | run cycle | 狀態 | BUY候選觀測 | 週末block觀測 | runtime結果 |
|---|---|---|---:|---:|---|
| 01:00 | 1 | startup截斷 | 17 | 12 | DOWN |
| 01:15 | 1 | 有settlement紀錄 | 21 | 12 | UP |
| 01:30 | 1 | 有settlement紀錄 | 21 | 14 | UP |
| 01:45 | 1 | 有settlement紀錄 | 19 | 13 | UP |
| 02:00 | 1 | 有settlement紀錄 | 22 | 14 | DOWN |
| 02:15 | 1 | 有settlement紀錄 | 22 | 14 | DOWN |
| 02:30 | 1 | 有settlement紀錄 | 19 | 13 | DOWN |
| 02:45 | 1 | 有settlement紀錄 | 21 | 12 | DOWN |
| 03:00 | 1 | 有settlement紀錄 | 19 | 13 | UP |
| 03:15 | 1 | 有settlement紀錄 | 21 | 13 | DOWN |
| 03:30 | 1 | 有settlement紀錄 | 17 | 12 | DOWN |
| 03:45 | 1 | 有settlement紀錄 | 20 | 13 | DOWN |
| 04:00 | 1 | 此run尚無settlement | 3 | 2 | NA |
| 04:00 | 2 | startup截斷 | 17 | 11 | UP |
| 04:15 | 2 | 有settlement紀錄 | 17 | 10 | DOWN |
| 04:30 | 2 | 有settlement紀錄 | 22 | 14 | UP |
| 04:45 | 2 | 有settlement紀錄 | 22 | 14 | UP |
| 05:00 | 2 | 有settlement紀錄 | 18 | 12 | UP |
| 05:15 | 2 | 此run尚無settlement | 9 | 5 | NA |

runtime outcome來源為 canonical_twap，未以網路查新的官方resolution，不能宣稱這些結果等同已驗證的官方winner。17筆MARKET_CYCLE_PNL均為0，兩run合計recorded realized cycle PnL=0；沒有成交，無法分析fill latency、真實sizing、protective SELL、cross後持倉PnL或stop-vs-hold counterfactual。

已執行既有 scripts/stop_timing_report.py，輸出 stop_timing_run1.json / stop_timing_run2.json。STOP_TIMING_* rows=0、positions=0，符合沒有實際LIVE position才不觸發這套position telemetry的設計；不是足以證明telemetry失效，也不是已驗證真實cross/breaker路徑。

## 資料／執行異常與限制

- 03:35:35 TWAP_REFERENCE_DEGRADED，age約11.95秒；03:35:39 POLYMARKET_TWAP_SILENT_STALL，silence約15.17秒。所在03:30市場有3筆ORDER_SKIP_TWAP_REFERENCE_DEGRADED。不能把所有零送單歸因於這一短暫事件，也不能未核對path便把該市場標正式完整有效。
- TWAP WS_DISCONNECTED 共4筆；WS_RECOVERED共6筆（含startup）。SLOW_CONSUMER_CALLBACK共117筆，僅是告警事件數，未推斷影響成交或未量化全部gap。
- MAIN_SIGNAL的raw realized sigma在419/424觀測非空、default input在4筆、authoritative strike在423/424筆。資料並非整晚缺strike／sigma；這些非空率不保證freshness gate全部通過。
- 本機journal中沒有正式ORDER_SUBMIT／ORDER_FILLED／ORDER_REJECTED；未進行venue open-order/position讀取，不能宣稱venue/local reconciliation已通過，也不能用零journal訂單證明沒有外部訂單。
- 原Tiny-LIVE沒有完成四市場後自動停止，也沒有遵守兩小時上限；已發生原先留待後續任務的node rollover。這是實驗控制條件未達成，不是entry gate的bug。

## 儲存與備份

- logs/trade_journal.db：2,390,507,520 bytes；mtime 2026-10-10T05:25:32.372405+08:00。
- logs/trade_journal.db-wal：0 bytes；mtime 2026-10-10T05:25:34.297556+08:00。
- logs/trade_journal.db-shm：32,768 bytes；mtime 2026-10-10T05:25:34.297719+08:00。
- backups/trade_journal.db：2,376,122,368 bytes；mtime 2026-10-10T04:03:01.783883+08:00。
- snapshot filesystem free：約12.33 GiB。
- 與上一回本機觀測的trade DB 2,345,553,920 bytes相比，增加42.87 MiB；上一回不是精確LIVE T0，因此不報成嚴格MIB/hour或每完整市場成長。此為DB主檔增長，非所有DB/備份總成長。
- backup_retention.json記錄04:03:01.987已完成sqlite_online_backup，size與backup檔案一致。這是rollover時local backup完成證據；尚未做backup全檔integrity check，不宣稱iCloud已同步或final shutdown backup PASS。
- ICLOUD_SYNC=UNVERIFIED，未重啟CloudDocs。沒有連續磁碟監測，MIN_FREE_SPACE=UNKNOWN。此次free snapshot約12.33GiB；未聲稱整晚都未低於guard。

## 決策

無送單主因已VERIFIED：台北週六LIVE observation-only策略。本次可驗證週末gate與部分資料收集／rollover／local backup，但不能驗證真實下單、成交、share_v1實際fill、保護性SELL或cross-vs-breaker timing。下一次實盤驗證的前提應先核對預定時段是否允許BUY，以及四市場/兩小時停止控制是否由操作者確實執行；不應因零成交修改score或取消週末policy。

BOT_STOPPED_AT_ANALYSIS=NO；NO_PUSH=YES。策略、設定、auth-preflight均未修改。

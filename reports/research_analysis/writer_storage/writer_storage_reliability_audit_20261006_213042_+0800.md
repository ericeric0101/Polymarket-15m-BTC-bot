# WRITER / STORAGE RELIABILITY AUDIT

唯讀審查；runtime baseline ad7cc22，SIMULATION/dry-run。最新 current-window durable telemetry：2026-10-06T21:25:52.140+08:00；research 讀取截止 2026-10-06T21:26:04.140+08:00。歷史對照包含 49aa9ca 的 13:30 市場。

結論：已確認 failed batch 移出 queue 後不 retry/requeue；ad7cc22 第一 cycle 至少 74 個 accepted prediction 未持久保存。底層 exception class/message 仍未知。第二 cycle 最近 30 分鐘 write_errors 增量為 0、accepted prediction 與 durable rows 對齊，但過去資料缺損不會因此恢復。建議唯一下一步：**PERSIST_WRITER_EXCEPTION_TELEMETRY**，不先猜 locking、不改 retry／transaction 行為。

## 1. Persistence architecture

| 資料／writer authority | Target | Queue／batch／cadence | 執行／connection／transaction | Retry／errors／shutdown |
| --- | --- | --- | --- | --- |
| PredictionResearchSnapshotter → LeadLagDB | data/research/twap_forward_shadow.db | 共用 queue 20,000；≤100 queue items/batch；get wait 0.5s、立即 drain；prediction eligible interval 1s、health 約60s | lead-lag-db-writer daemon；每 batch 新 SQLite conn；caller 只 enqueue；同 batch一個 commit | 無 retry/requeue；write_errors與queue_drops；stop join預設3s |
| TwapForwardShadow → 同一 twap_research_db | 同上 lead_lag_decisions | TWAP material/checkpoint、summary與prediction共用queue；storage檢查300s | 共用同一writer instance；canonical summary guard後仍可 enqueue | guard抑制optional TWAP；失敗語義同LeadLagDB |
| Decision evidence / sparse L2 / StopForensicsShadow → twap_research_db | 同上 lead_lag_decisions | 共用 queue；sparse既有事件cadence，無獨立writer | bot/order_events.py、bot/research/evidence.py、bot/stop_forensics_shadow.py 只 enqueue | accepted evidence != persisted；共用writer stop |
| LeadLagDB lead_lag_db / trend / forward / Outcome observation / latency | data/research/hyperliquid_lead_lag.db | 另一個獨立20,000 queue；≤100 mixed items/batch；Forward health hourly | 另一條同名daemon thread；每batch新conn；snapshots/reference_1s/lead_lag_decisions/markouts/latency_spans | 同樣無retry；本輪亦找到write_errors；兩DB不是同一檔 |
| TradeJournalDB execution/order/strategy/settlement/reconciliation | logs/trade_journal.db（operations authority） | critical writes通常caller同步；低頻telemetry queue256，由backupworker drain；無統一journal batch100 | 每method短命conn；各operation commit；不同callbacks/worker可寫同一journal | method依criticality log/health/return，無統一自動batch retry；stop drain、backup |
| TradeJournalDB backup / monthly report | data/backups/trade_journal.db / read-only monthly projection | backup dirty coalescing；default30s，startup warmup亦设30s；worker wait1s；monthly另worker | backup使用SQLite backup API及atomic publish；report只讀 | backup failure retainsdirty重試；stop join workers2s後force flush |
| BTC1sHistoryCollector | data/btc_history_1s/BTCUSDT_1s_*_part-*.parquet | queue300 rows；batch300或30s；disk檢查60s；metrics60s | 既有worker；按UTCday，temporary part→fsync→atomic rename；不寫SQLite | exceptiondisable observer、清batch；no retry；queue-full drops；stop join5s並核對bars |

Exact writer responsible for 87：**monitoring.lead_lag_db.LeadLagDB，strategy.twap_research_db**。Prediction、TWAP、stop/evidence 全共用它，不能把 counter 只歸因某個 payload type。另有獨立 strategy.lead_lag_db 也發生錯誤，詳第3節。

來源：bot/settings.py build_twap_research_db / initialize_strategy_settings；monitoring/lead_lag_db.py；monitoring/trade_journal_db.py；bot/btc_1s_history.py。執行 journal與 research writer 分離；本輪未觸碰任何writer或策略。

## 2. write_errors semantics

| 所有同名 increment path | 計數單位 | 是否證明row loss |
| --- | --- | --- |
| LeadLagDB._writer except Exception（connect、PRAGMA、JSON serialization、SQL execute/executemany、commit任一步） | 每次進入batch exception handler +1；不是每row；每batch≤100 items | 可能丟失0–100 queue items；commit結果含糊時需另外查驗 |
| LeadLagDB._writer finally conn.close except Exception | 每次close exception +1；同batch可能再加一次 | commit已成功時可能0 loss；decision_rows_written亦已加 |
| ForwardShadowExperiment._emit except Exception | research capture/enqueue exception +1，不是SQLite writer counter | 只有capture失敗，不證明DB transaction失敗 |
| record_forward_shadow_quote wrapper except Exception | quote側capture exception +1 | 不同scope，不可加總為writer failed rows |

PREDICTION_RESEARCH_HEALTH.write_errors 來自 db.research_health()，是上述前兩個 batch/close path 的 lifetime counter。FORWARD_SHADOW_CAPTURE_HEALTH 先展開 capture counters、再 **db_health，故 top-level write_errors 由 DB counter 覆蓋；write_errors_this_hour 仍可能是capture delta，db_write_errors_this_hour 才是writer delta。

queue_drops 只計 Queue.Full；晚到 enqueue 在 stop後被拒另計 late_enqueue_rejections。這三種 counter 都不涵蓋所有資料損失。decision_rows_written 僅在 commit成功後加總 decision items，包含 health/summary/trace/L2等，不是 prediction-only。Prediction console「written」／health accepted_total 是 enqueue 成功，不是commit acknowledgment。

## 3. Error-episode timeline

完整掃描可用 TWAP durable health；觀察到 4 個正增量 interval、2 個連續 episode，沒有其他可見正增量。interval是前後成功保存health之間的上界，不是每個例外的精確時間：錯誤時health本身也可能掉批，部分interval約120s而非通常60s。未知first/last exception timestamp不能偽裝精確。

| 台北interval bounds | Revision/cycle | Run ID | Active slug | write_errors before→after / Δ | Accepted prediction Δ | Durable prediction Δ (snapshot timestamp window) | All-decision written Δ | Sample queue / drops | Largest same-market gap±30s | DataEngine process/s | Loop lag max ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-06T13:35:53.583+08:00 → 2026-10-06T13:37:53.803+08:00 | 49aa9ca/3 | run_1791261985_8f4edaaa | btc-updown-15m-1791264600 | 0→45 / +45 | 103 | 70 | 89 | 1→0 / 0 | 20.874 | 14.901 | 26.000 |
| 2026-10-06T19:25:47.238+08:00 → 2026-10-06T19:27:47.504+08:00 | ad7cc22/1 | run_1791279466_720c2e47 | btc-updown-15m-1791285300 | 0→26 / +26 | 103 | 85 | 107 | 1→0 / 0 | 13.459 | 13.846 | 13.000 |
| 2026-10-06T19:27:47.504+08:00 → 2026-10-06T19:29:48.754+08:00 | ad7cc22/1 | run_1791279466_720c2e47 | btc-updown-15m-1791285300 | 26→76 / +50 | 123 | 77 | 102 | 0→0 / 0 | 13.459 | 14.315 | 23.000 |
| 2026-10-06T19:29:48.754+08:00 → 2026-10-06T19:30:49.017+08:00 | ad7cc22/1 | run_1791279466_720c2e47 | btc-updown-15m-1791285300,btc-updown-15m-1791286200 | 76→87 / +11 | 31 | 21 | 25 | 0→0 / 0 | 10.108 | 13.170 | 410.000 |

Episode A：13:35:53–13:37:53，49aa9ca cycle3，13:30市場；最大gap13:36:39.100→13:36:59.974=20.874s。Episode B：19:25:47–19:30:49，ad7cc22 cycle1，19:15市場及19:30handoff；最大gap19:27:48.193→19:28:01.652=13.459s。snapshot名義1s，實際由capture/eligibility cadence決定；不以每秒一列補造missing數。

Event-loop 指標單位為 ms；13/23/410 是毫秒，不是秒。較高410ms窗口涵蓋正常settlement/handoff，不能推定多秒owner-loop starvation。DataEngine處理rate約13–15/s，沒有與先前quote-generation失效相同的collapse。All-decision written與accepted prediction不是同一母體，不做直接相減。

| TWAP run | Revision/cycle | Last observed write_errors | Queue drops | 最後accepted counter | 同snapshot timestamp prefix durable prediction | Prefix缺額 |
| --- | --- | --- | --- | --- | --- | --- |
| run_1791261985_8f4edaaa | 49aa9ca/3 | 45 | 0 | 3341 | 3308 | 33 |
| run_1791279466_720c2e47 | ad7cc22/1 | 87 | 0 | 8262 | 8188 | 74 |
| run_1791290298_434b4d2b | ad7cc22/2 | 0 | 0 | 2125 | 2125 | 0 |

另一個 research DB：掃描 FORWARD_SHADOW_CAPTURE_HEALTH，亦只在同兩個run看到非零writer counter，但hourly bounds較粗：

| hyperliquid_lead_lag.db writer run | Lower health bound | Upper health bound | Before | After | Δ | Queue drops | Queue depth at upper |
| --- | --- | --- | --- | --- | --- | --- | --- |
| run_1791261985_8f4edaaa | 2026-10-06T13:00:00.114+08:00 | 2026-10-06T14:00:00.102+08:00 | 0 | 382 | 382 | 0 | 7 |
| run_1791279466_720c2e47 | 2026-10-06T19:00:00.054+08:00 | 2026-10-06T20:00:00.082+08:00 | 0 | 972 | 972 | 0 | 30 |

ad7cc22 TWAP=87、另一DB writer=972，禁止把不同writer的counter混成一個 prediction loss總數。兩者都有失敗是直接證據；是否同時發生同一底層exception，hourly遙測無法證明。BTC歷史cycle1 console曾顯示WRITER_UNAVAILABLE／storage guard active；無durable exact failure timestamp/message，不能合併成第三個已定位exception episode。

## 4. Exception evidence

**NO_EXCEPTION_EVIDENCE**（目標 SQLite writer 的 exception class/message）。兩個 LeadLagDB handler 都是 except Exception → counter；沒有logger、exception_type、sqlite_errorcode/name、失敗stage或payload kind 記錄。structured health只存aggregate counter，無法倒推 OperationalError / IntegrityError / ProgrammingError。

已搜尋現存 logs/bot/terminal_bot.log、logs/dry_run_bot.log 與相關 writer/database exception關鍵字；主要file logs並非當前2026-10-06runtime輸出。讀取現行PTY可見健康guard和shutdown incomplete警示，但未取得與兩段episode對應的可識別SQLite exception。這不代表console歷史所有stderr都完整保存，亦不代表無例外。SQLite locked／disk I/O／disk full／readonly／closed DB／schema／JSON serialization等仍是未判定候選，不能挑一個作root cause。

## 5. SQLite connection/thread model

| 設定 | LeadLagDB（兩DB） | TradeJournalDB |
| --- | --- | --- |
| connection ownership | 不是一條persistent connection；每batch由writer thread建、用、關，schema-init另connection | 每operation新conn；caller/telemetryworker/backup可各建conn |
| check_same_thread | 未覆寫，Python default True；沒有跨thread共用同一conn | 同樣default True；各thread各用自己的conn |
| journal_mode | 每_connect執行 WAL；source read確認 wal | 每_connect WAL |
| busy timeout | sqlite3.connect(timeout=5.0)，約5000ms；無另PRAGMA busy_timeout override | timeout=10秒 |
| synchronous | 每writer connection明確 NORMAL | NORMAL |
| transaction | Python default legacy implicit transaction；首個DML開txn；batch commit；未開autocommit | 多數method operation commit；connection context按operation回滾 |
| lifetime/reconnect | 每batch總是重建；沒有專門after-error reconnect branch | 每次operation重建；health probe另path |
| same-file concurrent writes | 常態一writer instance/DB；多producer只enqueue；舊cycle未stop完成可能重疊但未證實 | 同步callback與telemetryworker可能競爭同journal；SQLite序列化write txn |

唯讀 audit connection 的 PRAGMAs 為 {'journal_mode': 'wal', 'page_count': 227967, 'page_size': 4096, 'busy_timeout': 3000, 'synchronous': 2}。busy_timeout=3000、synchronous=2 是 audit connection 自己的預設，不是活躍writer的5s/NORMAL；journal_mode=WAL才是可由reader確認的DB持久設定。未對activeDB設定任何PRAGMA journal_mode/synchronous。

TWAP file所有runtime寫入集中至settings建立的twap_research_db：prediction、TWAPsummary/material、stop-forensics、decision/L2 evidence。HL file集中至另一lead_lag_db：Outcome觀察/reference、trend/forward shadow、latency等。離線archive/invalidation/pnl工具存在可寫DB的操作，但本輪不執行，亦無證據它們在episode期間同時運作。不要把其存在當作contention已發生的證據。

## 6. Backup/read interaction

Journal backup 是另一個DB的SQLite online backup，不是把TWAP activeDB VACUUM／copy。Rollover writer stop先於journal final backup。49aa9ca episode在13:36，ad7cc22 episode在19:25–19:30；ad7cc22 scheduled stop20:37:47，兩者不重疊。既有snapshot manifests：10/05 06:57、18:54、10/06 06:42，與兩段已定位TWAP episode不同；不能排除未記錄的其他readers/backup。

WAL一般允許reader與writer並行，但同檔只有一個writer；長read transaction可阻止checkpoint完整前進，使WAL增長，特定cleanup/recovery/locking情況仍可能SQLITE_BUSY。因此「任何read一定阻塞write」與「WAL永遠不會busy」都不成立。[SQLite WAL官方說明](https://www.sqlite.org/wal.html)。

Online backup會在步進時讀取source；source仍可被其他connection更新，不是整段時間持有寫入鎖。[SQLite Backup API](https://www.sqlite.org/backup.html)。本輪只讀source建立/private/tmp離線副本，發生於21:23:58之後，不能是早先19:25episode的原因；未手動checkpointactive WAL。無source writer timing/error telemetry可證明過去analysis reads造成例外，歸因為 INSUFFICIENT_EVIDENCE。

## 7. Failed-batch behavior

**FAILED_BATCH_DROPPED**（pre-commit failure path）。先get/remove queue items、填入local rows，才開始connect/serialize/insert/commit；失敗只加counter，local rows在下個迴圈被新list覆蓋，沒有retry、requeue或dead-letter。writer繼續處理下一batch。queue_depth=0並不表示成功寫完。

混合batch所有DML在同一transaction。程式未explicit rollback；正常sqlite close會rollback未commit transaction。[SQLite connection-close語義](https://www.sqlite.org/c3ref/close.html)。不把executemany中的部分INSERT直接當成部分commit；正常情況未commit全batch回滾。但commit自身回傳例外／close失敗時的結果需資料查驗；close-only error可能所有rows已成功，最終狀態不能只看counter。

connect每次新建減少下一batch沿用poisoned connection的機會；_connect內PRAGMA在return前失敗時外層conn仍None、沒有顯式close已配置的local connection，清理由物件生命週期處理。這是可檢查exception stage的理由，不是已證實connection lifecycle root cause。stop()要求thread結束、queue空、write_errors==0、queue_drops==0；歷史error即使已恢復仍使stop返回False。

## 8. Quantified data loss

採用accepted counter與同run、同snapshot timestamp prefix 的durableprediction rows核對；一個prefix內(run,slug,snapshot_ts)無重複。第一cycle在20:37:08.677 accepted=8262，之後本輪完整DB讀取仍只找到ts≤該cutpoint的8188prediction，缺額74。該run早已於20:37:50停止、nextcycle開始；missing prefix不能用後來新capture的33列抵銷。

| 範圍 | Confirmed missing accepted predictions | Minimum | Defensible maximum | Unknown |
| --- | --- | --- | --- | --- |
| 49aa9ca 13:35–13:37 | 33（103 accepted vs70 durable；run finalprefix亦3341−3308=33） | 33 | 該observed counterprefix缺額33；其餘event types未知 | final counter之後的capture、非prediction queue items、每batch組成 |
| ad7cc22 19:25–19:30 | 74（18+46+10；closed-runprefix8262−8188） | 74 | observed predictionprefix deficit74；完整run最高loss UNKNOWN | lasthealth20:37:08到shutdown的final acceptance/errors未保存 |
| ad7cc22 current cycle2 | 0 | 0 | latestmatched prefix0；仍不可保證每種decision event完整 | 非predictionrows沒有complete accepted counters |

主baseline CONFIRMED_DATA_LOSS_ROWS=AT_LEAST_74_PREDICTIONS；另計 prior49aa9ca至少33，共審查範圍至少107 accepted predictions未保存。DATA_LOSS_EXACT=NO（完整run／所有類型loss未知）；74是已觀察prefix的明確deficit，並非用87錯誤估算。沒有個別snapshot sequence IDs／failedbatch payload，不能逐列還原遺失內容。對已觀察87errors，source每batch≤100，僅能給0–8700 mixed queue items的寬鬆理論上限，不能當作實際prediction損失；close-onlyerrors可能0 loss。HL972的row loss無精確counter reconciliation，UNKNOWN，不能972×100報成已丟列。

## 9. Storage/Research health guard root cause

| TWAP guard timestamp | Run | DB total MiB | Configured cap MiB | Free GiB | Min free GiB | Trigger |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-06T17:38:49.896+08:00 | run_1791279466_720c2e47 | 841.070 | 500.0 | 24.739 | 10.0 | db_size_cap |
| 2026-10-06T20:39:29.126+08:00 | run_1791290298_434b4d2b | 881.691 | 500.0 | 34.521 | 10.0 | db_size_cap |

Storage=CRITICAL 主因是 **TWAP_GUARD: db_size_cap**，不是SQLite宣告disk-full：841.07／881.69MiB超過500MiB，trigger時free24.74／34.52GiB遠高於10GiB。guard在TwapForwardShadow instance內sticky到rebuild；只抑制optional TWAP material/checkpoint，canonical summaries例外允許，prediction/direct evidence不經這個guard，故StorageCRITICAL與prediction繼續produced可以同時成立。

| Health來源 | Projection／latch語義 | 本輪狀態 |
| --- | --- | --- |
| Historical writer error counter | 任意write_errors>0→WRITE_OR_CAPTURE_ERROR / CRITICAL；同instance不自行歸零 | cycle1TWAP87/HL972會一直CRITICAL；cycle2新writer重新0 |
| TWAP storage guard | triggered→EXISTING_STORAGE_GUARD_ACTIVE / CRITICAL；sticky直到instance更換 | cycle2DB仍大於cap，又立即觸發；不是單靠counter latch |
| BTC storage/failure guard | free<5GiB觸發／observer failure→enabledFalse→WRITER_UNAVAILABLE；記錄cachedguard | cycle1console曾有btc failure/guard；exact reason未durably保存；cycle2consoleenabledTrue |
| Recent prediction gap | >3×interval→DEGRADED | 最近仍可見約6秒gap，因此恢復不等於perfect cadence |
| Freshness | persisted_joint_fresh_pct<100→DEGRADED | 此名稱其實只反映accepted capture品質，不是commit audit |
| Execution journal | readyFalse→CRITICAL；journal獨立健康來源 | currentconsoleExecutionHEALTHY；不推定TWAPSQLitequickcheck代替journalhealth |

Research與Storage domains分開聚合：historicalwritererrors造成ResearchCRITICAL，不直接進Storagedomain；Storage由TWAP/BTCguard決定。HEALTH_LATCHED=YES（writer歷史counter與TWAPguard都有sticky語義）。CURRENT_ACTIVE_WRITE_FAILURE尚未觀察；DATABASE_HEALTH_GUARD不是目前Storagecap原因；quick_check不能清除cap guard。

## 10. Current writer health

| 指標 | 最新可用值 |
| --- | --- |
| Run/cycle | run_1791290298_434b4d2b / 2 |
| Latestdurable healthtimestamp | 2026-10-06T21:25:52.140+08:00 |
| Currentwriterinstance write_errors | 0 |
| Sameprocess observedhistoricalTWAPerrors | 87 |
| Current queue depth / capacity | 0 / 20000 |
| Queue drops / late rejection | 0 / 0 |
| Acceptedprediction / sameprefixdurablerows | 2125 / 2125 |
| Latest durable prediction observation timestamp | 2026-10-06T21:26:03.094+08:00 |
| Exactcommit / last_persist_ts | UNKNOWN (field explicitly null) |

| Trailingwindow（以latesthealth為end） | Error Δ | Counterbaseline | Sampled queue max | Prediction rows | Median same-market cadence s | Largest same-market gap s |
| --- | --- | --- | --- | --- | --- | --- |
| 5m | 0 | 2026-10-06T21:20:51.439+08:00 | 0 | 252 | 1.166 | 2.843 |
| 15m | 0 | 2026-10-06T21:10:46.904+08:00 | 1 | 665 | 1.188 | 6.016 |
| 30m | 0 | 2026-10-06T20:55:43.378+08:00 | 1 | 1403 | 1.172 | 6.016 |

Offlinequick_check：**['ok']**；read-only source online backup至 /private/tmp/writer_audit_twap_20261006_212358.db，開始 2026-10-06T21:23:58.747+08:00，backup+check約5.184s，size934940672 bytes；tablecounts={'lead_lag_decisions': 219297, 'snapshots': 0, 'reference_1s': 0, 'lead_lag_markouts': 0, 'latency_spans': 0}。這是副本integritycheck，不在activeDB上跑，也不證明未遺失acceptedrows。

CURRENTLY_RECOVERED_WITH_HISTORICAL_ERRORS（process層解讀）；currentwriterinstance在上述window為健康、0error。這不表示先前instance資料回補，也不表示Storage已恢復HEALTHY。SQL可見最新row只能證明query前commit成功，snapshot timestamp不是精確persist timestamp；sampledqueue maxima不是未採樣interval的真max。

## 11. Correlation with lifecycle/admission/rollover

**WRITER_ERRORS_INDEPENDENT** 作為主要episode起始的時間歸類，非底層因果證明。19:25:47之後開始累積，19:27:47已26、19:29:48已76；該次handoff與admission 要到19:30:18.645才request，started19:30:18.663、completed19:30:21.617。大部分errors早於這次admission；最後76→87 interval跨過settlement/handoff/admission，因此末尾有時間重疊，但不能因此把整段episode歸因admission。

Scheduledrolloverstop20:37:47.678，與19:25–19:30episode相距逾1h；startup17:38:46亦相距逾1h。第一cyclewriterstop20:37:50.819–20:37:51.461後journalfinalbackup，並非錯誤爆發前的DB操作。13:36episode在13:30市場中段，不是startup/settlementboundaries。Journalbackupworker通常30s循環，沒有durable每次start/end，無法對其秒級排除／建立correlation。

ROLLOVER_CORRELATION=NO_OBSERVED_CORRELATION；ADMISSION_CORRELATION=NO_CLEAR_CAUSAL_LINK（末段有overlap）。Reader/backuphealthquerycorrelation=INSUFFICIENT_EVIDENCE。

## 12. Primary failure classification

**FAILED_BATCH_DROP_SEMANTICS**。對「失敗後為何已接受資料會消失」confidence HIGH：source明確dequeue-before-write、no retry/requeue；closed-runaccepted/durableprefix缺額吻合episode差額74。對「最初為何write/close拋例外」confidence LOW／UNKNOWN；沒有exception，不選SQLITE_LOCK_CONTENTION、DISK_OR_FILESYSTEM_ERROR或SCHEMA_OR_DATA_ERROR。

Counterevidence／limits：close-only errors可能commit成功且0loss；另HLwriter也失敗，Storagecap/BTCguard是獨立現象，可能有更廣泛的環境因素但尚未識別。DataEnginerate保持、looplag多為毫秒，不支持把sourcewritererrors直接歸為owner-loop starvation。Offlinequickcheckok不能排除過去transientI/O／locking／serialization，也不證明failedbatch曾重試。這不是HEALTH_GUARD_LATCH_ONLY：有實際accepted prediction缺額。

## 13. Fix-option comparison

| Option | Expectedbenefit | Runtime / duplication / ordering / shutdownrisk | Complexity |
| --- | --- | --- | --- |
| A durableexceptionclass/message/stage | 直接識別connect/PRAGMA/serialize/insert/commit/close原因、batchtype/count與durability狀態 | 低；用獨立既有journalauthority、節流避免errorstorm；不可同failedDBrecursively寫；不改order | 低–中；推薦唯一下一步 |
| B boundedretry/backoff | 暫時錯誤可能恢復 | 中；commit-resultunknown會重複autoincrementdecisions；會延迟queue/shutdown且阻塞後batch | 中 |
| C requeuefailedbatch | 避免立即丟失已acceptedrows | 中–高；queue滿、逆序、無限requeue、同batch重複與shutdownrace；需retrybudget/idempotency | 中–高 |
| D reconnectafterfailure | 針對壞connection | 低benefit；目前已每batchnewconn；不能回收被dropbatch；close/PRAGMAstage需先識別 | 低–中 |
| E busy_timeoutadjust | 若exception確為busy可能改善 | timeout已有5s；延長會增加in-flight/unobservedqueue與shutdown時間；未知原因不能猜 | 低code／中opsrisk |
| F WAL/transactionmodechange | 若有mode問題可能改善 | 已WAL/NORMAL；改動durability/locking/transaction語義風險；無證據必要 | 中–高 |
| G singlewriterauthority | 若multiple同filewriterscontention可改善 | 已有一writer/target；把兩不同DB合併可能增加sharedbottleneck與ordering依賴 | 高 |
| H healthcounterrecovery | 區分歷史與當前failure | 不能找回missingrows；直接reset會掩蓋audit/shutdownloss；需保留lifetime並新增recentrate | 低–中 |
| I smallerbatch | 若batchsize/data問題可降低blast radius | 目前≤100且通常快速drain小batch；更多conn/commit增加I/O，無故障stage證據 | 低code／中operationalrisk |
| J telemetry-onlynextpatch | 恢復後先取得下次exactexception，保留diagnosticsemantics | 不能避免下一次loss；應明確no guarantee；需durable、低頻、boundedexceptionpayload | 低–中 |

只推薦A；不是A+B+C的合併行為修補。先保存exception，再依真實errorclass決定retry／requeue是否安全與是否需要idempotency；目前的健康guard／counterreset不應優先掩蓋資料缺損。

## 14. Recommended smallest fix

**PERSIST_WRITER_EXCEPTION_TELEMETRY**，本輪僅建議、不實作。兩個LeadLagDB都需要能保存target、batchstage、exceptionclass、簡短message、可得sqlite_errorcode/name、batchitemcount/typecounts、commit是否已ack、run/cycle、failuretimestamp與boundedsummary。closefailure與precommitfailure必須分開。失敗資料庫不能是唯一telemetry target；重用既有低頻durablejournalauthority，對錯誤burst節流並保存suppressed_count。避免存完整predictionpayload／secrets，不新增per-quote輸出或thread。

理由：例外未知、當前writer已恢復；這符合telemetry-first要求。已確認drop風險仍存在，應把下次fault診斷列為工程follow-up，但這個read-onlyaudit不授權修補、resethealth、調整cap或restart。

## 15. Safety

| 項目 | 確認 |
| --- | --- |
| Source modified? | NO |
| Config modified? | NO |
| Commit / push? | NO / NO |
| Bot stop / restart / anotherlaunch? | NO / NO / NO |
| ActiveDB mutation byanalysis? | NO |
| ActiveParquet mutation? | NO |
| Strategy/watchdog/researchcadencechanged? | NO |
| ActiveVACUUM/migrate/index/manualWALcheckpoint? | NO |
| DBcopymechanism | read-onlySQLite online backup API；從未plaincp |
| Repooutput | 只有本generatedMarkdown，untracked |
| Temporaryauditfiles | /private/tmp scripts/projections/offlinesnapshot；非reposource／activedata |

## Final compact summary

```text
WRITER_NAME=LeadLagDB / strategy.twap_research_db
DB_TARGET=data/research/twap_forward_shadow.db
WRITE_ERRORS_TOTAL=87 observed across ad7cc22 cycles; current instance=0 (HL separate=972)
WRITE_ERRORS_LAST_30M=0
QUEUE_DROPS=0
EXCEPTION_STATUS=NO_EXCEPTION_EVIDENCE
PRIMARY_FAILURE_CLASS=FAILED_BATCH_DROP_SEMANTICS
FAILED_BATCH_BEHAVIOR=FAILED_BATCH_DROPPED
CONFIRMED_DATA_LOSS_ROWS=AT_LEAST_74_PREDICTIONS_ad7cc22; prior49aa9ca_additional33
DATA_LOSS_EXACT=NO
CURRENT_WRITER_HEALTH=CURRENTLY_RECOVERED_WITH_HISTORICAL_ERRORS
STORAGE_CRITICAL_REASON=TWAP_GUARD_db_size_cap (>500MiB); historicalBTCguard separately
HEALTH_LATCHED=YES
ROLLOVER_CORRELATION=NO_OBSERVED_CORRELATION
ADMISSION_CORRELATION=NO_CLEAR_CAUSAL_LINK
RECOMMENDED_FIX=PERSIST_WRITER_EXCEPTION_TELEMETRY
SOURCE_MODIFIED=NO
BOT_RESTARTED=NO
```

證據限制：durable health可能自身drop、沒有逐batchexception/commitack、無globalwriteracceptedcounter或exact final flush stats；本報告完整呈現可觀察episode與prefixdeficit，不宣稱重建所有未保存exception或所有historicalmissingrows。


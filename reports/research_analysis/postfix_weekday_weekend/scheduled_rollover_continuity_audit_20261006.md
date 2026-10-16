# SCHEDULED ROLLOVER CONTINUITY AUDIT

稽核基準：`01c87aadb09092eeb6257a77c9d0e48270ec2e5b`。只讀既有離線快照 `data/analysis_snapshots/20261006_064237_+0800/`，共同截止 2026-10-06 06:42:37.025825 Asia/Taipei。六個 run 的 manifest 均對應此 commit，同一 process；五次 scheduled rollover。沒有重新讀取或備份 active DB。

## 1. Current rollover architecture

launcher 在 node.build 完成後、node.run 前啟動 3600 秒 timer；到期只檢查可保護的 inventory／非終態 SELL，不檢查市場邊界。stop callback dispatch 回 owner loop，先 fence DataEngine producer，再正常 stop。shutdown → clean disconnect/drain → dispose → 3 秒 cooldown → discovery/build → kernel connect/reconcile/portfolio → strategy on_start recovery/calibration → research 恢復。

`new_node_built` 代表 build 完成，不代表 connected/ready。`new_strategy_started` 位於 on_start 的 instrument wait、inventory recovery、settlement reconciliation、risk guards、strike recovery、calibration 之後，並非 on_start 入口。source: bot/launcher.py:889、run_bot.py:3839、Nautilus system/kernel.py:start_async。

## 2. Five rollover timelines

以下時間均為 2026-10-06 Taipei、顯示到毫秒；durations 用記錄的 wall timestamps 計算。journal row ts 與 payload stage ts 有數毫秒差異，本表 stage 使用 payload ts。

| Stage | #1 → cycle2 | #2 → cycle3 | #3 → cycle4 | #4 → cycle5 | #5 → cycle6 |
|---|---|---|---|---|---|
|stop dispatch|02:14:36.396|03:15:08.607|04:15:48.140|05:16:21.710|06:16:56.764|
|stop request|02:14:36.396|03:15:08.608|04:15:48.140|05:16:21.710|06:16:56.764|
|owner-loop callback start|02:14:36.399|03:15:08.608|04:15:48.140|05:16:21.710|06:16:56.773|
|strategy shutdown start|02:14:36.403|03:15:08.615|04:15:48.145|05:16:21.719|06:16:56.777|
|research writer stop/flush start|02:14:39.230|03:15:11.606|04:15:51.420|05:16:24.812|06:17:00.534|
|research writer stop/flush complete|02:14:39.733|03:15:12.386|04:15:52.157|05:16:25.501|06:17:01.029|
|journal stop/backup bundle start|02:14:39.734|03:15:12.387|04:15:52.167|05:16:25.502|06:17:01.029|
|strategy teardown end|02:14:43.994|03:15:22.432|04:15:57.167|05:16:32.219|06:17:08.510|
|residual wait start|02:14:44.003|03:15:22.505|04:15:57.186|05:16:32.228|06:17:08.518|
|residual wait end|02:14:54.009|03:15:32.513|04:16:07.190|05:16:42.232|06:17:18.523|
|data disconnect start|02:14:54.024|03:15:32.527|04:16:07.204|05:16:42.247|06:17:18.536|
|data disconnect end|02:14:54.145|03:15:32.645|04:16:07.314|05:16:42.364|06:17:18.652|
|execution disconnect start|02:14:54.025|03:15:32.528|04:16:07.205|05:16:42.248|06:17:18.537|
|execution disconnect end|02:14:54.025|03:15:32.529|04:16:07.205|05:16:42.248|06:17:18.537|
|engine disconnect wait start|02:14:54.013|03:15:32.518|04:16:07.194|05:16:42.236|06:17:18.526|
|engine disconnect wait end|02:14:54.145|03:15:32.646|04:16:07.314|05:16:42.364|06:17:18.653|
|engine stop start|02:14:54.145|03:15:32.646|04:16:07.314|05:16:42.364|06:17:18.653|
|engine stop end|02:14:54.149|03:15:32.649|04:16:07.317|05:16:42.367|06:17:18.656|
|kernel stop complete|02:14:54.149|03:15:32.650|04:16:07.317|05:16:42.367|06:17:18.656|
|node.run return|02:14:54.151|03:15:32.653|04:16:07.321|05:16:42.370|06:17:18.658|
|dispose complete|02:14:54.181|03:15:32.689|04:16:07.342|05:16:42.386|06:17:18.681|
|next build/discovery start|02:14:57.193|03:15:35.734|04:16:10.353|05:16:45.397|06:17:21.693|
|next node built|02:15:08.476|03:15:48.341|04:16:21.919|05:16:56.856|06:17:32.884|
|post-recovery strategy marker|02:16:13.156|03:16:50.837|04:17:18.869|05:18:01.756|06:18:30.764|
|first observed fresh quote|02:16:23.286|03:17:00.738|04:17:28.524|05:18:12.036|06:18:40.058|
|first persisted prediction snapshot timestamp|02:16:23.288|03:17:00.739|04:17:28.526|05:18:12.036|06:18:40.059|
|first joint-fresh research snapshot|02:16:39.808|03:17:13.988|04:17:41.981|05:18:25.986|06:18:55.047|

| Startup milestone（normal subscription task start 仍 UNKNOWN） | #1 | #2 | #3 | #4 | #5 |
|---|---|---|---|---|---|
|requests_scheduled command|02:15:40.324|03:16:20.336|04:16:52.802|05:17:28.554|06:18:03.811|
|MARKET_STRIKE_RECOVERED|未發出|未發出|04:17:15.772|05:17:57.977|06:18:27.269|
|fast-follow calibration result|02:16:09.028|03:16:47.478|04:17:15.826|05:17:58.040|06:18:27.333|
|strong-directional calibration result|02:16:13.068|03:16:50.751|04:17:18.780|05:18:01.676|06:18:30.679|

rollover_due 未單獨持久化；只能由 3600s policy 與 stop_request 推算，不能提供假精確時間。owner-loop callback 有精確記錄。on_start 入口、normal subscription task start/ack、outcome observer stop、各 background worker stop、journal flush vs DB backup 各自起訖沒有獨立 durable point，均 UNKNOWN。outcome observer/background/orders 位於 strategy_teardown_start 到 research_writer_stop_start 區段；journal/backup/dashboard 位於 pre_journal_stop 到 teardown_end 區段。

first prediction 確實存在於 offline DB，但 snapshot_ts 不是 SQLite COMMIT ack，first prediction written 的精確 commit 時間 UNKNOWN。五次首列 joint_fresh 都 False；上表另列第一個 joint_fresh，避免把資料恢復等同同步可用恢復。

## 3. Gap attribution

| 項目，秒 | #1 | #2 | #3 | #4 | #5 |
|---|---:|---:|---:|---:|---:|
|pre-stop|0.225|9.374|1.059|1.280|0.810|
|SHUTDOWN_TIME|17.755|24.046|19.181|20.660|21.894|
|return → build（dispose/drain/cooldown）|3.041|3.080|3.032|3.028|3.035|
|NODE_REBUILD_TIME|11.283|12.607|11.566|11.458|11.191|
|STARTUP_TIME（複合）|64.680|62.496|56.950|64.900|57.879|
|MARKET_DATA_WARMUP / RESEARCH_RESUME（複合）|10.132|9.901|9.658|10.281|9.295|
|owner-loop dispatch delay|0.003|0.000|0.000|0.001|0.009|
|observer/background/orders bundle|2.827|2.991|3.275|3.094|3.756|
|research writer flush|0.504|0.781|0.737|0.689|0.495|
|journal/backup/dashboard bundle|4.260|10.046|5.000|6.716|7.481|
|fixed trader residual wait|10.006|10.008|10.004|10.005|10.005|
|data disconnect|0.120|0.117|0.110|0.117|0.116|
|execution disconnect|0.000|0.000|0.000|0.000|0.000|
|engine disconnect wait|0.132|0.128|0.120|0.129|0.127|

上表後半是前半 shutdown 的子階段，Data/Exec disconnect 並行；不能加總重複計時。research writer startup／strategy warmup 沒有獨立 timer；marker→prediction 的 9–10s 不是純 network latency。

| Cycle | last prediction | first prediction | gap sec |
|---|---|---|---:|
|1|02:14:36.171|02:16:23.288|107.117318|
|2|03:14:59.233|03:17:00.739|121.505261|
|3|04:15:47.081|04:17:28.526|101.445704|
|4|05:16:20.430|05:18:12.036|111.606804|
|5|06:16:55.954|06:18:40.059|104.105180|

Median **107.117318s**；max **121.505261s**；合計 **545.780267s** 未觀測區間。排序：①啟動複合 56.95–64.90s，②shutdown 17.76–24.05s，其中固定 residual wait 10s，③discovery/build 11.19–12.61s，④post-marker research/data resume 9.30–10.28s，⑤cooldown/cleanup 約3s。#2 pre-stop 9.37s 涵蓋正常換市場，不能全算 rollover 額外等待。

**PRIMARY_GAP_SOURCE=MULTIPLE_STAGES**。最大桶是 startup，但不能選 STARTUP_CALIBRATION_DOMINANT：後三次 requests_scheduled→MARKET_STRIKE_RECOVERED 花 22.97/29.42/23.46s，且發生在 calibration 之前；strike recovered→post-calibration marker 只有 3.10/3.78/3.50s。node built→requests_scheduled 約30.9–31.9s 包含 kernel/client/instrument/startup 等未知細項。source 存在 synchronous bootstrap thread.join(timeout=30)、startup DB queries、Gamma reconciliation，僅為候選，不是已證實執行30s。

固定10s residual wait 已由 source `_await_trader_residuals` 的 asyncio.sleep(timeout_post_stop) 確認；不能把它當 disconnect 10s timeout 或任意移除安全檢查。

## 4. Original rollover rationale

`55b73c4b70cd620031c22e9ea3d0345b2402f87e`（2026-02-20）加入 enabled=1、預設1800s、可配置interval，logger 明示 market refresh。`6cb57b8d68bc2a28af73c02eb47d7d780d07aa78`（2026-08-21，Fix node lifecycle operational defaults）改為硬編碼3600s，comment 說 operational recovery / established hourly policy。未找到 memory/resource leak、DB、websocket 或歷史 starvation 必須每小時重建的明確證據。相關 tests 保證 safe stop callback、disconnect/drain、exit protection、research shutdown，不證明3600是必要期限。

**ROLLOVER_REQUIRED=INSUFFICIENT_EVIDENCE**。五次都在一小時前被截斷，不能證明3h/6h/12h安全，也不能僅由 coalescing fix 宣告原有recovery完全過時。

## 5. Intra-cycle health trends

每列是 elapsed runtime 自 new_strategy_started 起的15分鐘區段，排除 startup_lag 與 marker 前視窗。gen/raw 為已持久化 inclusive elapsed share，並非 CPU；lag/queue 欄為每個60s summary P95 的區段中位數，不能稱成 pooled request P95。rate 為 quote callback/s。

| Cycle | elapsed min | windows | gen share % | raw share % | lag P95 ms | queue P95 ms | rate/s |
|---|---|---:|---:|---:|---:|---:|---:|
|1|0–15|13|12.28|4.48|6.00|9.31|13.73|
|1|15–30|15|20.41|4.11|11.00|11.01|13.50|
|1|30–45|15|30.88|3.98|15.00|13.53|14.11|
|1|45–60|14|32.54|3.83|10.00|13.15|13.76|
|2|0–15|14|25.60|3.73|10.50|13.40|13.83|
|2|15–30|15|26.84|4.55|11.00|12.34|14.03|
|2|30–45|15|26.22|4.18|12.00|13.52|13.82|
|2|45–60|14|26.93|3.78|13.00|16.33|13.91|
|3|0–15|13|27.39|3.73|13.00|13.45|13.98|
|3|15–30|15|33.76|4.17|12.00|15.76|14.38|
|3|30–45|15|38.45|3.75|18.00|16.90|14.57|
|3|45–60|15|30.69|3.06|12.00|17.87|13.33|
|4|0–15|14|16.95|2.97|19.00|17.21|12.34|
|4|15–30|15|21.12|4.33|13.00|15.16|13.31|
|4|30–45|15|24.00|3.94|15.00|14.93|13.85|
|4|45–60|15|10.42|3.38|11.00|15.06|11.59|
|5|0–15|14|8.06|4.12|7.50|11.38|11.89|
|5|15–30|15|13.44|4.53|8.00|11.43|12.43|
|5|30–45|15|16.54|4.96|10.00|11.61|12.99|
|5|45–60|14|34.11|4.18|11.50|14.95|14.26|
|6|0–15|14|29.00|3.97|10.50|14.23|13.98|
|6|15–30|10|40.77|3.37|15.00|19.05|14.25|

Cycle6只有約24min，不能驗證60min。各cycle quote receive age 區段中位數大致0.26–0.36s，cycle4末段0.023s；整個cycle max依序4.245、4.675、4.847、4.219、4.905、2.509s。joint freshness有升有降，不能由joint率把BTC/analytic freshness問題誤稱quote stream崩潰。

logical quote tokens 2–4、logical L2 tokens2、current quote tokens2、prewarm0–2；已完成 observations unique venue assets通常4，中間可3，startup requests_scheduled可0，不能當作無subscription。沒有跨cycle累積成長。writer queue_depth max六cycle皆1，drops/write_errors/late_enqueue_rejections皆0；queue_capacity20000，health約每60s觀測，並非連續監控。RSS/open task/resource count未持久化，UNKNOWN。

沒有觀察到接近60min一致的 throughput collapse／queue爆增／quote staleness。gen share在cycle1、5升高，cycle4末段下降，因此不能說所有成本完全平坦，也不能證明更長runtime無leak。steady loop lag max214ms；38–45s startup stall與shutdown 7–14s lag必須獨立看待。**HEALTH_DEGRADES_NEAR_60MIN=NO（限已觀測指標；RSS/resources UNKNOWN）**。

## 6. Research-quality cost

| Market open | 影響 | 其餘限制 |
|---|---|---|
|02:00|stop在settlement前，canonical settlement absent|joint freshness23.93%，仍低於25%|
|02:15|opening缺83.29s|joint24.46%，缺fresh T-30|
|03:00|gap只擦過末尾0.77s|仍usable，settlement有|
|03:15|opening缺120.74s|仍GOOD/usable|
|04:15|101.45s hole、2 runs、INTERRUPTED|joint69.30%，五anchors都有|
|05:15|111.61s hole、2 runs、INTERRUPTED|joint46.19%，另缺fresh T-60/T-30|
|06:15|104.11s hole、2 runs、INTERRUPTED|joint64.75%，五anchors都有|

7個市場與gap交會；6個有明顯opening/terminal/內部截斷；4個分類受影響（1個缺settlement、3個INTERRUPTED）；其中只有2個其他已觀測gate皆通過，rollover是可識別唯一排除項。不是3個INTERRUPTED都可自動救回。

22個observed市場含1個active；完成21，usable13：**13/21=61.90%**。若沿用上一輪全observed分母則13/22=59.09%。僅解除rollover interruption條件、保留其他既有品質條件的機械反事實：**15/21=71.43%**（或15/22=68.18%）；是條件式potential admission，沒有補造snapshot，也不是修改後保證yield。

prediction rows：資料庫沒有記錄每個未capture的預定tick，精確lost-row N UNKNOWN。沒有writer drop證據。可以確定545.78s相鄰prediction跨cycle未觀測時間；periodic snapshots依quote觸發與節流，不能將秒數當精確lost rows。span-based coverage約97.8%仍可能有100s內部hole，不能只用coverage_ratio判斷continuity。

## 7. Option comparison

| Option | 每次預期gap | complexity | runtime risk | research benefit | semantic risk | subscription ownership risk |
|---|---|---|---|---|---|
|A disable scheduled entirely|消除scheduled gaps；故障recovery gap仍存在|表面低、正確解耦需修改|高/未知；現行enabled=0在node return後break，連watchdog後重建也受影響|最大potential|strategy state長期不重置，需驗證|單node低；recovery缺失風險|
|B 3h / 6h / 12h|仍約101–122s/次；頻率約1/3、1/6、1/12|低，localized interval|3h中/未知；6h/12h未驗證|nominal盲區比例約3%→1%/0.5%/0.25%|不改threshold；state lifetime延長需驗證|低，仍serial clean teardown|
|C align boundary|每次gap仍約100–120s，移到新market開頭|中|較低但需authority/flush barrier|避免切碎舊market；新market仍缺opening|settlement排序錯誤可改accounting|低，仍single owner|
|D defer while active|同C，可能延至下一個安全boundary|中|若一直有position需保留existing exit protection|同C|只看ACTIVE旗標不足，REDUCE_ONLY也是market內|低，authority serialization必要|
|E warm replacement|potential overlap減gap，數字UNKNOWN|高|双node/execution client/帳務競態高|可能大|高|高，重複subscription/dispatch|
|F preserve research across rebuild|potential接近continuous，數字UNKNOWN|高|independent collector/lifecycle一致性風險|最大potential|join/provenance/freshness drift|高，feed ownership須重設|
|G optimize current path|各已量測bucket可節省部分；最終gap UNKNOWN|中|取決於是否動到recovery/accounting安全前置|可減每次gap|calibration/reconciliation不能直接skip|低到中|

## 8. Market-boundary feasibility

現有 lifecycle 有 SETTLING→WAITING，且有expiry timestamp；可作為設計基礎，但没有已完成的atomic safe-to-rollover API。MARKET_PHASE_CHANGE(to=SETTLING)先寫，再_cancel_active_maker_orders與_record_market_settlement；讀到phase不代表settlement及research writer已持久化。scheduled worker目前只檢查exposure，不與lifecycle authority同步。

安全邊界需owner lifecycle確認canonical settlement處理完成、research enqueue/flush完成、無protected inventory/SELL，並保證新市場未再進entry。這需要一個明確handoff barrier，不能單憑時間或phase輪詢。BTC15m市場連續，沒有天然100s idle window。延期至boundary最長約15min，另加settlement grace／existing exposure defer（有持倉不能承諾上限）；gap仍約100–120s，移到下個market前段。依此cohort最大121.51s加boundary處理；不是未來hard worst-case bound，網路/bootstrap timeout可更長。T-300出現在開場600s後通常不被此gap直接覆蓋，但opening price／strike evidence仍需驗證。

## 9. Recommended smallest fix

**RECOMMENDED_FIX=EXTEND_ROLLOVER_INTERVAL**，下一個候選為3600→10800s（3h），保留AUTO_NODE_ROLLOVER_ENABLED、watchdog stop/rebuild、clean disconnect/drain與exposure defer。只改interval，不混入boundary/warm handoff/calibration重構。

理由：沒有60min崩潰證據，排程是五次gap的確定trigger；3h保留有界operational reset，避免直接disabled的recovery耦合，且對subscription ownership的改動最小。這是待授權的repair/validation proposal，未證實3h安全；後續需觀察完整3hcycle，檢查同樣throughput/freshness/queue/lag與existing resource metrics，不在本轮部署或加instrumentation。

## 10. Safety

source modified=NO；config modified=NO；commit=NO；push=NO；bot stopped=NO；bot restarted=NO；another bot launched=NO；active DB mutated=NO；active Parquet mutated=NO。只讀既有offline snapshot與source/git history；只產生本local untracked report與/private/tmp one-off analysis scripts，沒有測試或匯入會啟動bot的entrypoint。

```text
ROLLOVER_REQUIRED=INSUFFICIENT_EVIDENCE
PRIMARY_GAP_SOURCE=MULTIPLE_STAGES
MEDIAN_ROLLOVER_GAP_SEC=107.117318
MAX_ROLLOVER_GAP_SEC=121.505261
MARKETS_DAMAGED_BY_ROLLOVER=4 classification-affected (7 intersected; 2 solely excluded by rollover)
USABLE_RATE_CURRENT=13/21=61.90% completed (13/22=59.09% all observed)
USABLE_RATE_WITHOUT_ROLLOVER_ESTIMATE=15/21=71.43% conditional (15/22=68.18%)
HEALTH_DEGRADES_NEAR_60MIN=NO
RECOMMENDED_FIX=EXTEND_ROLLOVER_INTERVAL
```

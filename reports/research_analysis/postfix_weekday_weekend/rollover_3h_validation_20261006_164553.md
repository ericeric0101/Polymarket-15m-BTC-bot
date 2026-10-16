# 3-HOUR ROLLOVER — POST-RESTART VALIDATION

唯讀固定視圖共同截止：**2026-10-06T16:45:53.017+08:00**。讀取 source DB 的 mode=ro + query_only + BEGIN transaction，journal/research pin times：['2026-10-06T16:45:53.017+08:00', '2026-10-06T16:45:53.017+08:00']。只在 /private/tmp 保存 scoped telemetry/payload 投影並離線計算；沒有 source DB backup、write、checkpoint、index、migration 或 Parquet 操作。

## 結論

新 PID 41760 已載入49aa9cad9963b596138302221f06831a86f1e470，scheduled interval authority為10800s。但五次實際 rollover都是strategy_requested / stale_instrument_lifecycle；尚無cycle跑滿3h，scheduled_auto_rollover事件0，不能宣稱實際node已每3h換一次。重啟後 18/26 = 69.23% 完成市場通過完整品質gate；週二穩定修復期累積43個合格市場。

## Runtime 與觸發来源

同一process_instance_id=process_1791253384_41760_f14f9289，六run manifest均git_commit=49aa9cad9963b596138302221f06831a86f1e470、config_hash=b86082ac5c66ec1fc1fb59edcb89ccd261591f25aa2cf7924aac2a83a3b49d81。新PID/重新import、先前啟動console auto_rollover=on(10800s)，加上單一source authority bot/launcher.py:680，支持interval已載入。僅看manifest git_commit不足以證明舊PID載入新code（manifest可能讀磁碟HEAD）。

| cycle | run_id | started Taipei | ended Taipei |
|---|---|---|---|
|1|run_1791253399_d0ff0054|2026-10-06T10:24:20.708+08:00|2026-10-06T11:30:56.512+08:00|
|2|run_1791257485_084da14f|2026-10-06T11:32:26.162+08:00|2026-10-06T12:45:57.051+08:00|
|3|run_1791261985_8f4edaaa|2026-10-06T12:47:28.799+08:00|2026-10-06T14:00:56.918+08:00|
|4|run_1791266486_408fbe92|2026-10-06T14:02:31.414+08:00|2026-10-06T15:15:56.578+08:00|
|5|run_1791270984_2337bdc7|2026-10-06T15:17:22.952+08:00|2026-10-06T16:30:57.545+08:00|
|6|run_1791275488_682e8353|2026-10-06T16:32:33.519+08:00|ACTIVE|

第一個cycle僅67.55min至stop，之後四個約74.43–74.46min；stop request間隔約75min。各run處理5個不同市場；原startup console明示load_slugs=5/instrument_ids=10。source launcher限定provider load_ids；run_bot._start_reload_timer只是cache.instruments與_find_btc_instrument，未從provider補進新market；lifecycle._search_next_market從Gamma找到next slug，但cache無可選instrument，WAITING搜尋連續失敗後stop。五次durable stop context都有lifecycle_reason=stale_instrument_lifecycle及next_market_slug。這是現有lifecycle recovery，不是10800timer錯設或owner-loop starvation。

watchdog trigger1（cycle2，11:32:36 quote_subscription_timeout），durable fresh confirmation約0.35s後發出，非五次rollover原因；fresh confirmation不等同所有subscribe task完成。

## Rollover 時間

| Cycle | 最後prediction | Stop | 下一prediction | 全gap s | stop→resume s | shutdown s | build s | startup s |
|---|---|---|---|---:|---:|---:|---:|---:|
|1|11:29:59.366+08:00|11:30:53.771+08:00|11:32:36.220+08:00|156.854|102.449|18.625|11.303|59.427|
|2|12:44:59.162+08:00|12:45:53.718+08:00|12:47:38.289+08:00|159.127|104.571|19.014|11.183|61.855|
|3|13:59:58.411+08:00|14:00:53.843+08:00|14:02:40.938+08:00|162.528|107.096|19.732|11.224|63.579|
|4|15:14:59.166+08:00|15:15:53.893+08:00|15:17:32.638+08:00|153.472|98.745|18.024|11.229|56.774|
|5|16:29:59.277+08:00|16:30:53.884+08:00|16:32:43.165+08:00|163.889|109.281|21.721|11.278|63.604|

完整gap median=159.127s，max=163.889s，sum=795.868s。原5次scheduled gap median107.117s、max121.505s。現在每次last_prediction→stop約54–55s，主要是到期後settling/search/waiting；不能全部算純shutdown。stop→resume median=104.571s，原106.892s；沒有實質startup加速證據。

現在shutdown median19.01s、cleanup/cooldown3.03s、build11.23s、startup61.86s、post-start marker→prediction9.65s。固定residual wait仍10s，writer flush0.53–0.90s。new_strategy_started仍是recovery/calibration後標記，startup是複合桶；first snapshot是持久化payload的snapshot timestamp，不是exactcommit ack。

## 合格條件與效率

完全沿用postfix13-market報告gate：canonical settlement可用，ResearchStore FULL/GOOD；單一run且market內最大gap≤15s；span≥600s；joint freshness≥25%；fresh UP mid或fresh DOWN補數，joint_fresh=True且settlement_state_side有效；五checkpoint T-300/180/120/60/30皆能在±12s nearest rule取到。ACTIVE不進completed分母。

| 指標 | 原5h報告 | 本次重啟後 |
|---|---:|---:|
|實際觀測時長 h|5.449|6.380|
|完成市場|21|26|
|SYNCHRONIZED_USABLE|13|18|
|完成市場合格率|61.90%|69.23%|
|合格市場/h|2.386|2.821|
|相對合格產出/h改善|—|18.26%|

最新cutoff覆蓋10:15–16:30共26個completed slot，全部有prediction與canonical settlement；16:45為active。10:15是人工重啟跨run市場，仍排除。若只算重啟後完整開場市場，分母25、18/25=72%；主比較保留初始邊界市場，和舊報告一致不挑分母。

## 每市場品質與排除原因

| Market open | Status | Canonical quality | Snapshots | Joint % | Span s | max intra-target gap s | Missing fresh checkpoints |
|---|---|---|---:|---:|---:|---:|---|
|10:15|INTERRUPTED|INTERRUPTED|109|87.16|328.53|6.23||
|10:30|SYNCHRONIZED_USABLE|FULL|763|83.75|880.93|2.98||
|10:45|SYNCHRONIZED_USABLE|FULL|678|78.02|879.91|5.72||
|11:00|SYNCHRONIZED_USABLE|FULL|765|80.39|880.32|3.31||
|11:15|SYNCHRONIZED_USABLE|FULL|747|72.96|880.43|5.31||
|11:30|SYNCHRONIZED_USABLE|GOOD|672|64.14|743.02|3.53||
|11:45|SYNCHRONIZED_USABLE|GOOD|739|56.16|880.53|3.40||
|12:00|SYNCHRONIZED_USABLE|GOOD|635|66.93|879.94|6.08||
|12:15|SYNCHRONIZED_USABLE|FULL|692|79.34|880.80|5.64||
|12:30|SYNCHRONIZED_USABLE|FULL|836|73.80|880.33|3.07||
|12:45|SYNCHRONIZED_USABLE|GOOD|604|83.11|740.85|5.10||
|13:00|SYNCHRONIZED_USABLE|GOOD|623|52.97|881.19|5.52||
|13:15|SYNCHRONIZED_USABLE|GOOD|758|40.77|880.70|3.28||
|13:30|INTERRUPTED|INTERRUPTED|624|48.88|879.90|20.87||
|13:45|SYNCHRONIZED_USABLE|GOOD|699|57.65|879.19|4.23||
|14:00|SYNCHRONIZED_USABLE|GOOD|469|52.03|738.72|6.14||
|14:15|SYNCHRONIZED_USABLE|GOOD|710|58.45|880.65|5.64||
|14:30|SYNCHRONIZED_USABLE|GOOD|664|68.07|879.52|5.85||
|14:45|SYNCHRONIZED_USABLE|GOOD|775|69.03|880.79|3.42||
|15:00|SYNCHRONIZED_USABLE|GOOD|785|69.04|880.06|3.71||
|15:15|MISSING_FRESH_CHECKPOINT|GOOD|594|45.62|745.73|5.44|60,30|
|15:30|MISSING_FRESH_CHECKPOINT|GOOD|565|25.31|879.92|6.21|120,60,30|
|15:45|MISSING_FRESH_CHECKPOINT|GOOD|599|25.04|880.75|5.85|180,60|
|16:00|MISSING_FRESH_CHECKPOINT|GOOD|573|39.27|879.69|5.73|60|
|16:15|MISSING_FRESH_CHECKPOINT|GOOD|729|33.88|880.73|5.40|30|
|16:30|PARTIAL|PARTIAL|637|22.92|736.77|5.90||
|16:45|ACTIVE|UNUSABLE|27|11.11|33.45|2.35|300,180,120,60,30|

10:15全run合併跨人工restart gap106.394s，表中target-only gap6.23s不代表全market連續。13:30同run在13:36:39.100→13:36:59.974有20.874s洞，canonical INTERRUPTED；同cycle research health首次於13:37:53觀測write_errors=45，journal未持久化具體error訊息，不能宣稱45都是prediction rows或已證明gap由error造成。

15:15/15:30/15:45/16:00/16:15各缺fresh [60,30]/[120,60,30]/[180,60]/[60]/[30]。不是沒有snapshot：nearest raw row存在、但joint fresh不通過，部分有負market_source_age/BTC age；另有正age過大。不能全稱upstream斷線，也不能為提高yield放寬no-lookahead。16:30五anchors都有但joint freshness22.92%低於25%，PARTIAL。故15:15後六個completed市場均未新增正式合格市場。

## 穩定度

steady summaries（startup後90秒且完整window在其後）約每60s；median DataEngine rate13.3–14.5/s，loop lag P95約8–9ms、queue P95約11.6–13ms。10組連續≤1/s windows全在最後market已到期後WAITING期間，不能判作active-market吞吐崩潰。所有writer queue_depth max1、queue_drops0；cycle3 write_errors45，其餘cycle0。這是資料品質需關注，不能只由低queue或bot存活稱完全健康。

## 目前可比較的平日市場池

週二穩定修復期累積43個獨立合格market = 原PID20132之前25個 + 新PID41760重啟後18個；原5h的13是25中的子集，06:42到重啟前另有12個。不要只把13+18當目前總數。全部config_hash相同、research/prediction schema版本1、probability_model_version=existing_twap_average_approx_v1；完整core schema沿用，工程interval變更未更動研究/策略語意。這是資料資格，不是已完成time-of-day matching或已證明regime effect。

全庫週一另外5個通過同一嚴格機械gate，品質候選總數48；但屬legacy/不同修復期，不把48直接當單一工程基準的正式pool。本次建議comparison主池用週二43，並保留run/config/provenance分層。

## 判斷與安全

效率有小幅改善，不能只由18>13判斷；完成market合格率與每小時產出均略升。但改interval尚未達成每3h才rebuild，實際cache/lifecycle約75min recovery仍有，restart path未加速，後段freshness/writer問題限制新增yield。觀測差異不能全歸因interval，time-of-day/quality分布亦不同。下一個值得唯讀診斷的來源是instrument provider/cache持續補充與freshness/寫入errors；本輪沒有修復。

source/config modified=NO；commit=NO；push=NO；bot stop/restart=NO；another bot launched=NO；active DB/Parquet mutated by analysis=NO。只新增本local untracked Markdown與/tmp唯讀投影；bot自身正常寫入持續。

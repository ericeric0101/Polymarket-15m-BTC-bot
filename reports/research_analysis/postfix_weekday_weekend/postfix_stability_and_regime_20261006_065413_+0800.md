# 5-HOUR POST-FIX VALIDATION

本輪僅讀取資料，未修改 source/config、策略、既有資料或 bot。新快照使用 SQLite online backup，source `mode=ro` + `query_only` + 固定唯讀 transaction；未 plain cp、VACUUM、migrate、建 index 或手動 checkpoint WAL。兩份目的快照 quick_check 均為 `ok`。

快照目錄：`data/analysis_snapshots/20261006_064237_+0800`。Journal/research 分別於 2026-10-06T06:42:37.025825+08:00 / 2026-10-06T06:43:32.448593+08:00 固定讀取視圖；所有跨 DB 分析採較早的 journal cutoff 2026-10-06T06:42:37.025+08:00。因此 research 晚於 cutoff 的 47 prediction rows 不參與分析。第一個未完成的分頁備份 `20261006_064012_+0800` 未使用；只停止該分析程序，未操作 bot。

## 1. Runtime provenance

**PATCH_CONFIRMED_LOADED**。六個 manifest 的完整 `git_commit` 均為 `01c87aadb09092eeb6257a77c9d0e48270ec2e5b`；STRATEGY_START、consumer timing、transport 共同記錄 `runtime_git_revision=01c87aa`、`runtime_source_fingerprint=a58e6912ed2adf80`。依 repo 的 import-time fingerprint 函式唯讀重算得到相同 fingerprint（涵蓋 adapter_overrides），generation counters 也實際存在。

同一 `process_instance_id=process_1791220458_20132_533012ce`，cycle 1–6。Manifest git_dirty=true，但 dirty_diff_hash=null；本轮 tracked tree 乾淨，原有 untracked reports 存在。Fingerprint 是該函式列出的 module 集合，非所有依賴的全映像證明。

| cycle | run_id | started_at | ended_at |
| --- | --- | --- | --- |
| 1 | run_1791220474_2ed1742e | 2026-10-05T17:15:39.669852+00:00 | 2026-10-05T18:14:39.229345+00:00 |
| 2 | run_1791224107_573fabab | 2026-10-05T18:16:13.157082+00:00 | 2026-10-05T19:15:11.603490+00:00 |
| 3 | run_1791227746_2be3a494 | 2026-10-05T19:16:50.838433+00:00 | 2026-10-05T20:15:51.420572+00:00 |
| 4 | run_1791231380_2435b627 | 2026-10-05T20:17:18.869703+00:00 | 2026-10-05T21:16:24.808970+00:00 |
| 5 | run_1791235015_758df0b8 | 2026-10-05T21:18:01.756765+00:00 | 2026-10-05T22:17:00.532654+00:00 |
| 6 | run_1791238651_7f90656a | 2026-10-05T22:18:30.764685+00:00 | UNKNOWN |

窗口從 2026-10-06 01:15:39.670 Taipei 到 06:42:37.026，約 5 小時 27 分；durable journal 最後事件為 2026-10-06T06:42:33.378+08:00. 全部都是 TEST_DRY_RUN（含 shadow），不將模擬績效稱為 live fills。


## 2. Stability verdict

**STABILITY_MATERIALLY_IMPROVED_BUT_FAILURES_REMAIN**。原先 quote generation 使 steady-state owner loop 漸進飢餓、DataEngine 趨近 0–1/s、quote watchdog timeout 的組合未重現。五次 scheduled node rollover 仍導致 101.446–121.505 秒的 prediction boundary gaps；三個跨 run 市場為 canonical INTERRUPTED，一個已完成市場無 canonical settlement。

不能僅因 bot 存活就宣告 STABILITY_FIX_VALIDATED。正常收集的 13 個市場可保留；全時段 continuous research collection 及 hourly rollover 尚不能無條件視為安全。


## 3. Coalescing effectiveness

324 persisted timing summaries；183 個 clean-market 完整窗口（window start 在該 market open 之後、requested>0、排除 startup lag）用於 clean distribution。以 clean median 比 pre-fix starvation reference：

- Execution rate reduction：71.57%。
- Cumulative generation time reduction：70.93%。
- Generation share 減少：64.90 個百分點（91.5% → 26.60%）。
- Steady-state worst loop lag：214 ms，相對 54,000 ms 約減少 99.60%；包含啟動時全 run worst 則為 45,061 ms，僅減少 16.55%。不能把兩種 scope 混為一談。

| metric | clean median | clean P90 | clean P95 | clean worst | all-window worst |
| --- | --- | --- | --- | --- | --- |
| requested/min | 42055.915 | 68513.529 | 72114.370 | 105219.380 | 105219.380 |
| executed/min | 15093.926 | 20194.885 | 21191.783 | 23818.860 | 25560.020 |
| coalesced/min | 27003.582 | 48818.359 | 54059.391 | 81400.521 | 81400.521 |
| coalescing ratio | 0.638 | 0.739 | 0.766 | 0.851 | 0.851 |
| generation ms/min | 15957.252 | 23035.021 | 27435.573 | 40277.480 | 42059.481 |
| generation share % | 26.595 | 38.392 | 45.726 | 67.129 | 70.099 |
| raw ingress share % | 3.964 | 5.128 | 5.334 | 5.906 | 8.130 |

全 run worst generation window：2026-10-06T06:37:33.347+08:00–2026-10-06T06:38:33.349+08:00，`btc-updown-15m-1791239400`，share 70.10%，42059.5 ms/min；lag max 76.0 ms、DataEngine 15.62/s。Clean worst window：2026-10-06T06:12:59.789+08:00，`btc-updown-15m-1791237600`，share 67.13%。

原 raw callback timing 包含同步 quote generation，修補後 generation 移到另一個 owner-loop callback。因此 raw share 大降部分是計時邊界轉移，不能直接解讀成獨立 CPU 工作減少 95%。各 handler 是 inclusive elapsed，不加總 nested shares。Pre-fix 是已知失敗窗口 reference，並非流量／市場匹配的 controlled benchmark。


## 4. Owner-loop / DataEngine health

| metric | median of windows | P90 of windows | P95 of windows | worst window |
| --- | --- | --- | --- | --- |
| median loop lag | 2.000 | 2.000 | 2.000 | 4.000 |
| loop lag P95 | 12.000 | 23.000 | 31.500 | 52.000 |
| loop lag P99 | 19.000 | 37.800 | 40.900 | 58.000 |
| loop lag max | 28.000 | 46.600 | 52.000 | 72.000 |
| queue wait median | 1.183 | 1.763 | 2.059 | 8.186 |
| queue wait P95 | 14.906 | 26.073 | 30.907 | 43.883 |
| queue wait P99 | 35.363 | 43.515 | 45.827 | 51.209 |
| queue wait max | 41.801 | 55.365 | 74.685 | 261.544 |
| DataEngine events/sec | 13.700 | 14.766 | 15.000 | 15.698 |

這些是已持久化窗口 quantiles 的分布，並非 raw samples pooled 全 run P95/P99；reservoir 原始樣本未持久化，無法精確重建 pooled quantiles。

全 run 1,150 個 transport queue windows：process rate median 13.577/s、P95 15.515/s、minimum 4.759/s；未出現連續兩個 recorded ≤1/s 窗口。Quote queue window P95 median 11.118 ms、worst P95 51.557 ms；worst P99 187.682 ms、max 257.617 ms。Consumer timing 的所有資料型別 queue wait max 為 261.544 ms。

全 run loop-lag max 45,061 ms；六個 >1s 事件均在新 strategy start 後約 2–3 秒、first prediction 前，屬啟動邊界延遲。其餘窗口 max 214 ms（01:49:40 summary），未見 progressive 17–54s steady-state lag。這不證明 startup 沒有阻塞，只說其 scope 與前次收集途中的 starvation 不同。

645 timing observations 被 bounded collector 丟棄；此數不是 prediction drops。Transport telemetry 為 throttled sampling，未覆蓋空窗時不能當作 latency=0。


## 5. Watchdog / recovery incidents

QUOTE_WATCHDOG_TRIGGERED=0、QUOTE_REFRESH_REQUESTED/STARTED/COMPLETED/FAILED/REFCOUNT_BLOCKED 全部為 0；scheduled stop_request=5，watchdog/manual rollover=0。Durable scope 未出現 quote_resubscribe_timeout；也沒有 console-only 證據被當作根因。

六次 TWAP reference silent-stall/reconnect 與 quote watchdog 分開：

| Taipei | slug | cycle | silence sec | recovery delay sec | reported feed unavailable sec | nearby loop max ms | nearby queue max ms | nearby engine /s | quality |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-06T02:13:43.258+08:00 | btc-updown-15m-1791223200 | 1 | 15.109 | 3.994 | 3.645 | UNKNOWN | UNKNOWN | UNKNOWN | PARTIAL_USABLE |
| 2026-10-06T03:31:14.810+08:00 | btc-updown-15m-1791228600 | 3 | 15.183 | 5.293 | 4.817 | 31.000 | 41.795 | 13.483 | SYNCHRONIZED_USABLE |
| 2026-10-06T04:06:32.262+08:00 | btc-updown-15m-1791230400 | 3 | 15.521 | 4.177 | 3.831 | 21.000 | 43.904 | 13.699 | SYNCHRONIZED_USABLE |
| 2026-10-06T05:26:53.770+08:00 | btc-updown-15m-1791234900 | 5 | 15.725 | 8.524 | 8.175 | 29.000 | 37.477 | 9.893 | INTERRUPTED |
| 2026-10-06T05:46:08.136+08:00 | btc-updown-15m-1791236700 | 5 | 15.228 | 3.990 | 3.640 | 24.000 | 30.443 | 12.433 | SYNCHRONIZED_USABLE |
| 2026-10-06T06:40:16.044+08:00 | btc-updown-15m-1791239400 | 6 | 15.155 | 4.116 | 3.742 | 16.000 | 45.576 | 13.433 | PARTIAL_USABLE |

全部 TWAP stalls 有後續 recovered，未觸發 node rollover；current official TWAP/p_ex 可短暫變 stale，非相同的 DataEngine collapse。Feed silence 與 feed_unavailable 量測起點不同，不相加。所有捕獲的 130 slow traces 與 boundary-delay candidate windows 見附錄；slow trace 本身有 10 秒 rate limit，不能視為完整逐 callback 清單。


## 6. Market handoffs and node rollovers

| cycle | cause | slug | stop | run_return | shutdown_sec | startup_from_return_sec | first_fresh_quote_after_return_sec | first_prediction_gap_sec |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | scheduled_auto_rollover | btc-updown-15m-1791223200 | 2026-10-06T02:14:36.396775+08:00 | 2026-10-06T02:14:54.152738+08:00 | 17.756 | 79.004 | 89.134 | 107.117 |
| 2 | scheduled_auto_rollover | btc-updown-15m-1791226800 | 2026-10-06T03:15:08.608042+08:00 | 2026-10-06T03:15:32.660055+08:00 | 24.052 | 78.178 | 88.078 | 121.505 |
| 3 | scheduled_auto_rollover | btc-updown-15m-1791231300 | 2026-10-06T04:15:48.140309+08:00 | 2026-10-06T04:16:07.325750+08:00 | 19.185 | 71.544 | 81.199 | 101.446 |
| 4 | scheduled_auto_rollover | btc-updown-15m-1791234900 | 2026-10-06T05:16:21.710299+08:00 | 2026-10-06T05:16:42.371148+08:00 | 20.661 | 79.386 | 89.665 | 111.607 |
| 5 | scheduled_auto_rollover | btc-updown-15m-1791238500 | 2026-10-06T06:16:56.764367+08:00 | 2026-10-06T06:17:18.659529+08:00 | 21.895 | 72.105 | 81.399 | 104.105 |

Startup duration 定義為 node_run_return → next STRATEGY_START；first fresh quote 是 durable fresh-quote 觀測的上界，不是未記錄的精確第一 tick。五次 recorded stop 皆為 scheduled_auto_rollover。

Hourly rollover **不是所有市場皆 research-safe**：02:00 市場停止於結束前約 24 秒而失去 settlement；04:15／05:15／06:15 市場被分割為多 run 並留下 101–112 秒洞。03:15 市場從 03:17 才開始，opening-path 不能使用，雖其終局 anchors 與 canonical GOOD gate 可用。

| old | new | boundary | ready | first_observed_fresh_quote_delay_sec | prediction_boundary_gap_sec | nearby_same_run_gap_sec | cross_run | clean |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1791220500 | btc-updown-15m-1791221400 | 2026-10-06T01:30:00+08:00 | 2026-10-06T01:30:19.536650+08:00 | 19.655 | 19.750 | 5.885 | NO | NO |
| btc-updown-15m-1791221400 | btc-updown-15m-1791222300 | 2026-10-06T01:45:00+08:00 | 2026-10-06T01:45:18.739362+08:00 | 19.278 | 19.922 | 6.135 | NO | NO |
| btc-updown-15m-1791222300 | btc-updown-15m-1791223200 | 2026-10-06T02:00:00+08:00 | 2026-10-06T02:00:18.709016+08:00 | 18.959 | 18.973 | 6.135 | NO | NO |
| btc-updown-15m-1791223200 | btc-updown-15m-1791224100 | 2026-10-06T02:15:00+08:00 | 2026-10-06T02:16:23.294001+08:00 | 83.287 | 107.117 | 5.785 | YES | NO |
| btc-updown-15m-1791224100 | btc-updown-15m-1791225000 | 2026-10-06T02:30:00+08:00 | 2026-10-06T02:30:18.809055+08:00 | 18.948 | 19.000 | 5.785 | NO | YES |
| btc-updown-15m-1791225000 | btc-updown-15m-1791225900 | 2026-10-06T02:45:00+08:00 | 2026-10-06T02:45:18.694219+08:00 | 18.724 | 19.545 | 3.943 | NO | YES |
| btc-updown-15m-1791225900 | btc-updown-15m-1791226800 | 2026-10-06T03:00:00+08:00 | 2026-10-06T03:00:18.893133+08:00 | 19.125 | 20.296 | 5.636 | NO | YES |
| btc-updown-15m-1791226800 | btc-updown-15m-1791227700 | 2026-10-06T03:15:00+08:00 | 2026-10-06T03:17:00.748018+08:00 | 120.738 | 121.505 | 5.670 | YES | YES |
| btc-updown-15m-1791227700 | btc-updown-15m-1791228600 | 2026-10-06T03:30:00+08:00 | 2026-10-06T03:30:18.827829+08:00 | 18.996 | 20.195 | 5.670 | NO | YES |
| btc-updown-15m-1791228600 | btc-updown-15m-1791229500 | 2026-10-06T03:45:00+08:00 | 2026-10-06T03:45:19.033309+08:00 | 19.334 | 20.117 | 2.896 | NO | YES |
| btc-updown-15m-1791229500 | btc-updown-15m-1791230400 | 2026-10-06T04:00:00+08:00 | 2026-10-06T04:00:19.046674+08:00 | 618.785 | 20.135 | 5.986 | NO | YES |
| btc-updown-15m-1791230400 | btc-updown-15m-1791231300 | 2026-10-06T04:15:00+08:00 | 2026-10-06T04:15:18.622374+08:00 | 18.643 | 18.773 | 5.986 | YES | NO |
| btc-updown-15m-1791231300 | btc-updown-15m-1791232200 | 2026-10-06T04:30:00+08:00 | 2026-10-06T04:30:18.688515+08:00 | 19.155 | 20.609 | 6.046 | YES | YES |
| btc-updown-15m-1791232200 | btc-updown-15m-1791233100 | 2026-10-06T04:45:00+08:00 | 2026-10-06T04:45:18.729957+08:00 | 18.697 | 19.134 | 6.046 | NO | YES |
| btc-updown-15m-1791233100 | btc-updown-15m-1791234000 | 2026-10-06T05:00:00+08:00 | 2026-10-06T05:00:18.590957+08:00 | 18.929 | 19.815 | 5.873 | NO | YES |
| btc-updown-15m-1791234000 | btc-updown-15m-1791234900 | 2026-10-06T05:15:00+08:00 | 2026-10-06T05:15:18.680693+08:00 | 18.737 | 20.153 | 5.951 | YES | NO |
| btc-updown-15m-1791234900 | btc-updown-15m-1791235800 | 2026-10-06T05:30:00+08:00 | 2026-10-06T05:30:01.754613+08:00 | 1.988 | 2.390 | 6.187 | YES | YES |
| btc-updown-15m-1791235800 | btc-updown-15m-1791236700 | 2026-10-06T05:45:00+08:00 | 2026-10-06T05:45:18.764810+08:00 | 18.900 | 20.727 | 6.187 | NO | YES |
| btc-updown-15m-1791236700 | btc-updown-15m-1791237600 | 2026-10-06T06:00:00+08:00 | 2026-10-06T06:00:18.573840+08:00 | 19.227 | 19.782 | 4.293 | NO | YES |
| btc-updown-15m-1791237600 | btc-updown-15m-1791238500 | 2026-10-06T06:15:00+08:00 | 2026-10-06T06:15:18.633765+08:00 | 18.945 | 20.057 | 3.203 | YES | NO |
| btc-updown-15m-1791238500 | btc-updown-15m-1791239400 | 2026-10-06T06:30:00+08:00 | 2026-10-06T06:30:18.506825+08:00 | 18.635 | 18.958 | 3.203 | YES | NO |

一般同 run handoff 的跨 market prediction gap 為 18.973–20.727 秒；此為 market 間邊界 gap，不能與 market 內 same-run gap 混用。前後 subscription samples 為鄰近紀錄，不是原子化 handoff 時刻 refcount。下表保留其詳細值：

| new | before_counts | after_counts |
| --- | --- | --- |
| btc-updown-15m-1791221400 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791222300 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791223200 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":0,"unique_venue_assets":4,"logical_quote_tokens":2,"logical_l2_tokens":2} |
| btc-updown-15m-1791224100 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":0,"unique_venue_assets":2,"logical_quote_tokens":2,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":0,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791225000 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791225900 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791226800 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791227700 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":0,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791228600 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791229500 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791230400 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791231300 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":0,"unique_venue_assets":4,"logical_quote_tokens":2,"logical_l2_tokens":2} |
| btc-updown-15m-1791232200 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791233100 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":3,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791234000 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791234900 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":0,"unique_venue_assets":4,"logical_quote_tokens":2,"logical_l2_tokens":2} |
| btc-updown-15m-1791235800 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":0,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791236700 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791237600 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |
| btc-updown-15m-1791238500 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":0,"unique_venue_assets":4,"logical_quote_tokens":2,"logical_l2_tokens":2} |
| btc-updown-15m-1791239400 | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":0,"logical_quote_tokens":4,"logical_l2_tokens":2} | {"current_quote_tokens":2,"current_l2_tokens":2,"prewarm_quote_tokens":2,"unique_venue_assets":4,"logical_quote_tokens":4,"logical_l2_tokens":2} |

7,564 個 quote transport records 對照同 market prediction 的 UP/DOWN instrument IDs，mismatch=0；可觀測範圍未見 current/prewarm 身份混用，不能因此證明未被記錄的每個 tick。


## 7. Writer and storage health

| cycle | run_id | capture_attempts_total | accepted_total | snapshot_persisted | decision_rows_written | dropped_total | queue_drops | write_errors | queue_depth_max | late_enqueue_rejections | health_rows |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | run_1791220474_2ed1742e | 2929 | 2838 | 2866 | 3591 | 0 | 0 | 0 | 1 | 0 | 59 |
| 2 | run_1791224107_573fabab | 2969 | 2876 | 2886 | 3542 | 0 | 0 | 0 | 1 | 0 | 59 |
| 3 | run_1791227746_2be3a494 | 3198 | 3033 | 3047 | 3767 | 0 | 0 | 0 | 1 | 0 | 59 |
| 4 | run_1791231380_2435b627 | 2812 | 2721 | 2745 | 3354 | 0 | 0 | 0 | 1 | 0 | 59 |
| 5 | run_1791235015_758df0b8 | 2660 | 2607 | 2616 | 3224 | 0 | 0 | 0 | 1 | 0 | 59 |
| 6 | run_1791238651_7f90656a | 1281 | 1227 | 1270 | 1470 | 0 | 0 | 0 | 1 | 0 | 24 |

共同 cutoff 下 prediction persisted=15430。最後 health snapshots 的 accepted 合計 15,302，與逐 row persisted 15,430 差異是 metrics 60 秒更新／shutdown tail；不是丟資料證據。Capture attempts 15,849 大於 accepted：source 在 market window guard 前累加 eligible，因此不可將其差值直接叫 dropped。319 research health snapshots 的 dropped/errors/queue_drops/write_errors/late_enqueue_rejections 均為 0，observed queue depth 最大 1。Market-level accepted/rejected 精確 counters 未持久化；表內 zero drops 是所屬 run 已觀測 health 的結果，非未觀測 shutdown tail 的完整證明。

BTC1S：唯讀檢查截止 journal snapshot 前完成的 634 個 Parquet 檔，窗口內 16427 rows／16427 unique seconds、duplicate=0、read errors=0。最大 source-bar gap 103 秒，五個最大洞均跨 scheduled node restart。1s history 是有交易才產生 bar 的 source series，不將所有 missing seconds 算 writer drop。BTC writer dropped/depth/enabled 未找到這輪 durable counters，標為 UNKNOWN；不能宣稱零掉資料。

Journal：本輪 24423 strategy events 持久化，目的快照 quick_check=ok；未找到 journal writer 全 run enqueue/drop/retry counters，標為 UNKNOWN。Sync journal timings 只表示觀察到的呼叫，不能還原完整 write health。Storage verdict：**NO_CURRENT_WRITE_FAILURE_OBSERVED**，prediction writer zero observed write_errors，SQLite quick_check=ok、Parquet readable；沒有 durable current Storage status/guard snapshot，不能強行判定 EXISTING_STORAGE_GUARD_ONLY，更不能從舊 Storage=CRITICAL 推導本輪 writer 故障。


## 8. Market-level quality table

Classification 依既有 `ResearchStore.get_market_coverage()`：settlement absent/span<60→UNUSABLE；多 run 或 gap>15s→INTERRUPTED；span<600 或 joint<25%→PARTIAL；其餘 GOOD/FULL。將 GOOD/FULL 再限定 canonical settlement + 五個 joint-fresh canonical anchors（T300/T180/T120/T60/T30、±12 秒 nearest）+ 未見 writer/drop/steady collapse 反證，才標 SYNCHRONIZED_USABLE。**沒有把 15 秒 gate 放寬到 120 秒，也沒有救回 interrupted markets。** 此完整-anchor restriction 是本輪 cohort admission，未改寫 canonical source。

21 個已完成 + 1 active = 22 observed slots；從 01:15 到 06:30 每個預期 slot 都有 prediction，missing market=0。Scope 不包含 startup 前 01:00 市場。

Actual covered duration 是 first-to-last span，並非將洞刪除後的有效積分時數。Prediction freshness percentages 分母是 observed rows，非 wall time。Watchdog 的 last_valid_quote stale max 沒有逐 interval 持久化，因此 UNKNOWN；receive-age proxy 不能代替該狀態值。同樣 min sustained rate 為相鄰兩個 recorded queue windows 的 pair maximum，不是逐秒連續原始分布。

| slug | open/end Taipei | cycle_idx | run_id | collection start/end | expected duration s | actual span s | largest same-run gap s | overall gap s | snapshots | eligible | written | dropped(observed health) | queue drops(observed health) | p_ex fresh % | market mid fresh % | joint fresh % | quote stale max | max receive-age proxy s | DataEngine min /s | min sustained pair /s | queue worst P95 ms | queue worst P99 ms | queue max ms | max loop lag ms | watchdog | refresh requests | node rollover during market | handoff clean | settlement | canonical quality | classification | reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1791220500 | 01:15:00/01:30:00 | [1] | ["run_1791220474_2ed1742e"] | 01:15:49/01:29:59 | 900 | 850.001 | 5.885 | 5.885 | 661 | UNKNOWN/per-market | 661 | 0 | 0 | 98.487 | 43.722 | 17.700 | UNKNOWN | 4.245 | 6.774 | 9.505 | 18.909 | 24.942 | 85.378 | 44765.000 | 0 | 0 | NO | STARTUP_PARTIAL | YES | PARTIAL | PARTIAL_USABLE | canonical span<600 or joint freshness<25% |
| btc-updown-15m-1791221400 | 01:30:00/01:45:00 | [1] | ["run_1791220474_2ed1742e"] | 01:30:19/01:44:59 | 900 | 879.701 | 5.159 | 5.159 | 726 | UNKNOWN/per-market | 726 | 0 | 0 | 99.862 | 43.113 | 18.182 | UNKNOWN | 2.173 | 8.180 | 8.964 | 20.264 | 39.754 | 47.386 | 63.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | PARTIAL | PARTIAL_USABLE | canonical span<600 or joint freshness<25% |
| btc-updown-15m-1791222300 | 01:45:00/02:00:00 | [1] | ["run_1791220474_2ed1742e"] | 01:45:19/01:59:59 | 900 | 880.710 | 6.135 | 6.135 | 706 | UNKNOWN/per-market | 706 | 0 | 0 | 99.858 | 53.258 | 24.221 | UNKNOWN | 4.322 | 7.325 | 7.597 | 22.164 | 48.229 | 127.825 | 214.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | PARTIAL | PARTIAL_USABLE | canonical span<600 or joint freshness<25% |
| btc-updown-15m-1791223200 | 02:00:00/02:15:00 | [1] | ["run_1791220474_2ed1742e"] | 02:00:18/02:14:36 | 900 | 857.209 | 2.819 | 2.819 | 773 | UNKNOWN/per-market | 773 | 0 | 0 | 98.189 | 49.935 | 23.933 | UNKNOWN | 1.535 | 12.335 | 12.922 | 15.242 | 43.411 | 45.304 | 40.000 | 0 | 0 | YES | READY_WITH_BOUNDARY_DELAY | NO | UNUSABLE | PARTIAL_USABLE | completed without canonical settlement |
| btc-updown-15m-1791224100 | 02:15:00/02:30:00 | [2] | ["run_1791224107_573fabab"] | 02:16:23/02:29:59 | 900 | 816.660 | 5.785 | 5.785 | 646 | UNKNOWN/per-market | 646 | 0 | 0 | 98.607 | 50.619 | 24.458 | UNKNOWN | 3.978 | 9.000 | 9.050 | 18.428 | 31.338 | 37.476 | 44841.000 | 0 | 0 | NO | STARTUP_PARTIAL | YES | PARTIAL | PARTIAL_USABLE | canonical span<600 or joint freshness<25% |
| btc-updown-15m-1791225000 | 02:30:00/02:45:00 | [2] | ["run_1791224107_573fabab"] | 02:30:18/02:44:59 | 900 | 880.231 | 3.943 | 3.943 | 739 | UNKNOWN/per-market | 739 | 0 | 0 | 99.865 | 69.418 | 47.226 | UNKNOWN | 1.734 | 6.747 | 9.261 | 19.312 | 50.790 | 52.349 | 52.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791225900 | 02:45:00/03:00:00 | [2] | ["run_1791224107_573fabab"] | 02:45:18/02:59:58 | 900 | 880.105 | 3.225 | 3.225 | 755 | UNKNOWN/per-market | 755 | 0 | 0 | 99.868 | 80.927 | 59.470 | UNKNOWN | 2.543 | 8.270 | 9.521 | 23.230 | 43.399 | 45.634 | 48.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791226800 | 03:00:00/03:15:00 | [2] | ["run_1791224107_573fabab"] | 03:00:19/03:14:59 | 900 | 880.108 | 5.636 | 5.636 | 746 | UNKNOWN/per-market | 746 | 0 | 0 | 99.866 | 88.606 | 77.078 | UNKNOWN | 4.675 | 8.134 | 8.725 | 38.460 | 46.902 | 50.611 | 42.000 | 0 | 0 | YES | READY_WITH_BOUNDARY_DELAY | YES | FULL | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791227700 | 03:15:00/03:30:00 | [3] | ["run_1791227746_2be3a494"] | 03:17:00/03:29:58 | 900 | 778.061 | 5.670 | 5.670 | 653 | UNKNOWN/per-market | 653 | 0 | 0 | 98.469 | 91.424 | 89.433 | UNKNOWN | 4.847 | 6.726 | 6.929 | 28.925 | 106.943 | 107.008 | 42630.000 | 0 | 0 | NO | STARTUP_PARTIAL | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791228600 | 03:30:00/03:45:00 | [3] | ["run_1791227746_2be3a494"] | 03:30:18/03:44:59 | 900 | 880.222 | 2.896 | 2.896 | 798 | UNKNOWN/per-market | 798 | 0 | 0 | 98.622 | 95.363 | 92.607 | UNKNOWN | 1.752 | 11.085 | 11.195 | 51.557 | 187.682 | 257.617 | 62.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | FULL | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791229500 | 03:45:00/04:00:00 | [3] | ["run_1791227746_2be3a494"] | 03:45:19/03:59:59 | 900 | 879.827 | 2.635 | 2.635 | 791 | UNKNOWN/per-market | 791 | 0 | 0 | 99.368 | 91.530 | 88.243 | UNKNOWN | 2.095 | 10.900 | 13.090 | 45.145 | 61.633 | 73.699 | 66.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | FULL | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791230400 | 04:00:00/04:15:00 | [3] | ["run_1791227746_2be3a494"] | 04:00:19/04:14:59 | 900 | 880.574 | 5.986 | 5.986 | 782 | UNKNOWN/per-market | 782 | 0 | 0 | 98.465 | 85.294 | 79.923 | UNKNOWN | 2.219 | 6.759 | 11.611 | 30.600 | 47.720 | 49.711 | 52.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | FULL | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791231300 | 04:15:00/04:30:00 | [3,4] | ["run_1791227746_2be3a494","run_1791231380_2435b627"] | 04:15:18/04:29:58 | 900 | 879.903 | 5.527 | 101.446 | 544 | UNKNOWN/per-market | 544 | 0 | 0 | 97.794 | 88.419 | 69.301 | UNKNOWN | 4.049 | 5.753 | 6.301 | 17.807 | 35.819 | 41.075 | 38066.000 | 0 | 0 | YES | INTERRUPTED | YES | INTERRUPTED | INTERRUPTED | canonical multi-run or >15s gap |
| btc-updown-15m-1791232200 | 04:30:00/04:45:00 | [4] | ["run_1791231380_2435b627"] | 04:30:19/04:44:59 | 900 | 880.437 | 6.046 | 6.046 | 750 | UNKNOWN/per-market | 750 | 0 | 0 | 99.867 | 80.533 | 66.267 | UNKNOWN | 2.395 | 8.554 | 8.888 | 20.081 | 94.120 | 135.986 | 53.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791233100 | 04:45:00/05:00:00 | [4] | ["run_1791231380_2435b627"] | 04:45:18/04:59:59 | 900 | 880.387 | 3.482 | 3.482 | 793 | UNKNOWN/per-market | 793 | 0 | 0 | 99.622 | 83.102 | 67.213 | UNKNOWN | 2.342 | 4.759 | 11.960 | 23.193 | 37.180 | 63.465 | 47.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791234000 | 05:00:00/05:15:00 | [4] | ["run_1791231380_2435b627"] | 05:00:18/05:14:58 | 900 | 879.654 | 5.873 | 5.873 | 633 | UNKNOWN/per-market | 633 | 0 | 0 | 99.842 | 58.136 | 40.758 | UNKNOWN | 4.219 | 6.362 | 7.150 | 17.075 | 34.982 | 42.979 | 56.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791234900 | 05:15:00/05:30:00 | [4,5] | ["run_1791231380_2435b627","run_1791235015_758df0b8"] | 05:15:18/05:29:59 | 900 | 880.861 | 5.951 | 111.607 | 459 | UNKNOWN/per-market | 459 | 0 | 0 | 95.425 | 72.331 | 46.187 | UNKNOWN | 4.905 | 4.965 | 5.163 | 18.304 | 18.304 | 19.970 | 45061.000 | 0 | 0 | YES | INTERRUPTED | YES | INTERRUPTED | INTERRUPTED | canonical multi-run or >15s gap |
| btc-updown-15m-1791235800 | 05:30:00/05:45:00 | [5] | ["run_1791235015_758df0b8"] | 05:30:01/05:44:58 | 900 | 896.186 | 6.187 | 6.187 | 631 | UNKNOWN/per-market | 631 | 0 | 0 | 99.842 | 54.517 | 39.461 | UNKNOWN | 4.811 | 7.128 | 7.756 | 19.792 | 29.684 | 31.408 | 72.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791236700 | 05:45:00/06:00:00 | [5] | ["run_1791235015_758df0b8"] | 05:45:18/05:59:59 | 900 | 880.545 | 4.293 | 4.293 | 732 | UNKNOWN/per-market | 732 | 0 | 0 | 98.497 | 68.716 | 51.913 | UNKNOWN | 2.715 | 7.714 | 8.678 | 17.679 | 38.051 | 48.654 | 39.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791237600 | 06:00:00/06:15:00 | [5] | ["run_1791235015_758df0b8"] | 06:00:19/06:14:58 | 900 | 879.662 | 3.195 | 3.195 | 765 | UNKNOWN/per-market | 765 | 0 | 0 | 99.085 | 86.536 | 64.314 | UNKNOWN | 1.657 | 6.880 | 11.487 | 18.385 | 42.868 | 49.016 | 44.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | YES | GOOD | SYNCHRONIZED_USABLE |  |
| btc-updown-15m-1791238500 | 06:15:00/06:30:00 | [5,6] | ["run_1791235015_758df0b8","run_1791238651_7f90656a"] | 06:15:18/06:29:59 | 900 | 880.729 | 3.203 | 104.105 | 712 | UNKNOWN/per-market | 712 | 0 | 0 | 98.174 | 82.163 | 64.747 | UNKNOWN | 2.509 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | 38940.000 | 0 | 0 | YES | INTERRUPTED | YES | INTERRUPTED | INTERRUPTED | canonical multi-run or >15s gap |
| btc-updown-15m-1791239400 | 06:30:00/06:45:00 | [6] | ["run_1791238651_7f90656a"] | 06:30:18/06:42:35 | 757.026 | 737.333 | 2.787 | 2.787 | 635 | UNKNOWN/per-market | 635 | 0 | 0 | 98.425 | 90.236 | 81.102 | UNKNOWN | 1.769 | 12.003 | 13.213 | 42.796 | 85.748 | 88.877 | 102.000 | 0 | 0 | NO | READY_WITH_BOUNDARY_DELAY | NO | UNUSABLE | PARTIAL_USABLE | active at snapshot |

Canonical anchors：

| slug | canonical_quality | anchors | status |
| --- | --- | --- | --- |
| btc-updown-15m-1791220500 | PARTIAL | {"300":true,"180":true,"120":true,"60":true,"30":false} | PARTIAL_USABLE |
| btc-updown-15m-1791221400 | PARTIAL | {"300":true,"180":true,"120":true,"60":true,"30":true} | PARTIAL_USABLE |
| btc-updown-15m-1791222300 | PARTIAL | {"300":true,"180":true,"120":true,"60":true,"30":false} | PARTIAL_USABLE |
| btc-updown-15m-1791223200 | UNUSABLE | {"300":true,"180":true,"120":true,"60":true,"30":true} | PARTIAL_USABLE |
| btc-updown-15m-1791224100 | PARTIAL | {"300":true,"180":true,"120":true,"60":true,"30":false} | PARTIAL_USABLE |
| btc-updown-15m-1791225000 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791225900 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791226800 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791227700 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791228600 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791229500 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791230400 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791231300 | INTERRUPTED | {"300":true,"180":true,"120":true,"60":true,"30":true} | INTERRUPTED |
| btc-updown-15m-1791232200 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791233100 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791234000 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791234900 | INTERRUPTED | {"300":true,"180":true,"120":true,"60":false,"30":false} | INTERRUPTED |
| btc-updown-15m-1791235800 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791236700 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791237600 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | SYNCHRONIZED_USABLE |
| btc-updown-15m-1791238500 | INTERRUPTED | {"300":true,"180":true,"120":true,"60":true,"30":true} | INTERRUPTED |
| btc-updown-15m-1791239400 | UNUSABLE | {"300":true,"180":true,"120":false,"60":false,"30":false} | PARTIAL_USABLE |



## 9. Clean post-fix weekday cohort

Total observed=22；SYNCHRONIZED_USABLE=13；PARTIAL_USABLE=6（4 個 low joint freshness、1 個 completed without settlement、1 個 active）；INTERRUPTED=3；DIAGNOSTIC_ONLY=0；INSUFFICIENT_DATA=0。Usable rate=13/22=59.09%；completed denominator=13/21=61.90%。

Nominal market duration=3.25 小時；actual first-to-last span 合計 3.154 小時（仍非所有 snapshots joint-fresh）。Earliest usable open=02:30；latest usable open=06:00、end=06:15，皆為 2026-10-06 Tuesday Taipei。

| slug | open | end | snapshot_count | joint_fresh_pct |
| --- | --- | --- | --- | --- |
| btc-updown-15m-1791225000 | 2026-10-06T02:30:00+08:00 | 2026-10-06T02:45:00+08:00 | 739 | 47.226 |
| btc-updown-15m-1791225900 | 2026-10-06T02:45:00+08:00 | 2026-10-06T03:00:00+08:00 | 755 | 59.470 |
| btc-updown-15m-1791226800 | 2026-10-06T03:00:00+08:00 | 2026-10-06T03:15:00+08:00 | 746 | 77.078 |
| btc-updown-15m-1791227700 | 2026-10-06T03:15:00+08:00 | 2026-10-06T03:30:00+08:00 | 653 | 89.433 |
| btc-updown-15m-1791228600 | 2026-10-06T03:30:00+08:00 | 2026-10-06T03:45:00+08:00 | 798 | 92.607 |
| btc-updown-15m-1791229500 | 2026-10-06T03:45:00+08:00 | 2026-10-06T04:00:00+08:00 | 791 | 88.243 |
| btc-updown-15m-1791230400 | 2026-10-06T04:00:00+08:00 | 2026-10-06T04:15:00+08:00 | 782 | 79.923 |
| btc-updown-15m-1791232200 | 2026-10-06T04:30:00+08:00 | 2026-10-06T04:45:00+08:00 | 750 | 66.267 |
| btc-updown-15m-1791233100 | 2026-10-06T04:45:00+08:00 | 2026-10-06T05:00:00+08:00 | 793 | 67.213 |
| btc-updown-15m-1791234000 | 2026-10-06T05:00:00+08:00 | 2026-10-06T05:15:00+08:00 | 633 | 40.758 |
| btc-updown-15m-1791235800 | 2026-10-06T05:30:00+08:00 | 2026-10-06T05:45:00+08:00 | 631 | 39.461 |
| btc-updown-15m-1791236700 | 2026-10-06T05:45:00+08:00 | 2026-10-06T06:00:00+08:00 | 732 | 51.913 |
| btc-updown-15m-1791237600 | 2026-10-06T06:00:00+08:00 | 2026-10-06T06:15:00+08:00 | 765 | 64.314 |

這 13 個市場是唯一可保留給後續 formal research 的新 weekday market cohort；PARTIAL/INTERRUPTED 不混入。


## 10. Remaining engineering risks

仍有 scheduled rollover 的停機／重新啟動洞及 missing settlement；本輪未修復。另有 TWAP feed silence、BTC freshness 限制，最早四個市場 joint freshness 僅 17.7–24.5%。Generation 在 worst window 仍達 70.1% elapsed share，與目前低 lag 並存，不能宣告無資源風險。

Freshness/coverage 指標僅涵蓋 persisted records；缺少 journal/BTC writer counters 與 startup stage 細分，不將 UNKNOWN 填 0。No extra runtime sampling、feed 或程式改動。


# WEEKDAY VS WEEKEND REGIME COMPARISON

**Stage B quantitative comparison 未啟動。** 本輪 13 clean markets <30，依附件 TOO_SMALL 門檻，沒有足夠 clean post-fix 樣本進入比較。以下完成 cohort definition/comparability audit 並明確列出未執行項目，不將 degraded Monday 或 partial markets 補進來湊數。

## 11. Cohort definitions

WEEKDAY：上述 13 個 Tuesday post-fix usable markets，與舊 Monday diagnostic cohort 完全分開；9568 prediction rows，13 canonical outcomes，五個 anchors 各 N=13（僅 availability audit，不計算 flips）。

WEEKEND：沿用前次 `regime_comparison_early_weekday/synchronized_cohorts/markets.csv` 的 weekend membership，144 unique markets，逐 row 再驗證，不把 previous report 的 clean 字樣當作新 gate。Coverage distribution：{"PARTIAL":18,"INTERRUPTED":66,"GOOD":38,"FULL":22}；GOOD/FULL=60，其中五個 joint-fresh anchors及 canonical settlement 都存在的 51 市場通過 payload/coverage 部分 gate。其餘 93 不進此候選 baseline。Old cohort lacks equivalent consumer/writer diagnostics，故這 51 並非已證明完整通過新 runtime health gate。

Weekday Taipei hour distribution：{"2":2,"3":4,"4":3,"5":3,"6":1}；re-filtered weekend Taipei hour distribution：{"0":3,"1":1,"2":4,"3":3,"4":1,"5":2,"6":2,"7":1,"9":2,"10":2,"11":1,"12":2,"13":2,"14":2,"15":1,"16":3,"17":1,"18":1,"19":5,"20":5,"21":3,"22":3,"23":1}。Weekend 日期依 slug 為 2026-10-03/04（Saturday/Sunday）；精確候選 slug 名單見附錄。


## 12. Cohort comparability

**PARTIALLY_CONFIRMED**（整體 cohort/schema 相容性）；核心 96 個共同 prediction keys 的 signature、probability_model_version 及核心欄位 availability contract 與旧資料一致。新 post-fix payload 額外增加 research_schema_version=1、prediction_schema_version=1；weekend payload 兩個 version keys 缺席，不能宣稱版本標記完全相同。這不構成已證實的語義不相容，亦不能替代完整 runtime/config provenance。

| cohort | rows | research_schema_version | prediction_schema_version | probability_model_version | probability_model_mode | field signatures |
| --- | --- | --- | --- | --- | --- | --- |
| POSTFIX_WEEKDAY | 15430 | ["1"] | ["1"] | ["existing_twap_average_approx_v1"] | ["FINAL_WINDOW_PARTIAL_INTEGRAL","FINAL_WINDOW_RAW_UNAVAILABLE","PRE_FINAL_WINDOW_APPROX","UNAVAILABLE"] | {"7f34c981fe02":15430} |
| LEGACY_WEEKEND | 79037 | ["None"] | ["None"] | ["existing_twap_average_approx_v1"] | ["FINAL_WINDOW_PARTIAL_INTEGRAL","FINAL_WINDOW_RAW_UNAVAILABLE","PRE_FINAL_WINDOW_APPROX","UNAVAILABLE"] | {"9093e7c1e39a":79037} |

| field | weekday key present | weekday non-null | weekend key present | weekend non-null |
| --- | --- | --- | --- | --- |
| snapshot_ts | 15430 | 15430 | 79037 | 79037 |
| market_slug | 15430 | 15430 | 79037 | 79037 |
| time_left_sec | 15430 | 15430 | 79037 | 79037 |
| joint_fresh | 15430 | 15430 | 79037 | 79037 |
| settlement_state_side | 15430 | 15430 | 79037 | 79037 |
| required_move_sigma | 15430 | 15244 | 79037 | 77834 |
| required_move_bps | 15430 | 15304 | 79037 | 78229 |
| p_up_ex_market | 15430 | 15274 | 79037 | 78129 |
| market_mid_up | 15430 | 6527 | 79037 | 37833 |
| market_mid_down | 15430 | 6493 | 79037 | 38331 |
| market_mid_up_fresh | 15430 | 15430 | 79037 | 79037 |
| market_mid_down_fresh | 15430 | 15430 | 79037 | 79037 |
| p_ex_fresh | 15430 | 15430 | 79037 | 79037 |
| btc_fresh | 15430 | 15430 | 79037 | 79037 |
| twap_fresh | 15430 | 15430 | 79037 | 79037 |

Old weekend 缺 run-level provenance，與 payload schema incompatible 是不同命題。新／舊完整 key signatures 分別為 7f34c981fe02／9093e7c1e39a，差異恰為兩個 schema version keys；去除這兩個新增 keys 後，共同96-key signature 均為9093e7c1e39a，probability model version相同。核心 field contract 可比有 payload證據，但舊 schema version／runtime config身份仍 UNKNOWN；null availability仍需checkpoint gate。新 weekday 只覆蓋 02–06 時、僅 Tuesday；weekend 橫跨更多 hour，time-of-day、BTC volatility、probability、strike distance、spread、T-minus coverage 和 freshness confounding 尚未匹配。無外部 feed、無權重／optimized threshold。Stage B 未啟動，故不把這些 confounders 當作已控制。


## 13. Flip rates

T−5m／T−2m weekday/weekend N、flip rate、effect size、Wilson CI、Fisher/two-proportion tests：**NOT_RUN_STAGE_B_GATE**。Availability weekday N=13；不重用 pre-fix Monday 的 2/17，也不把 15,430 snapshot rows 当 independent N。


## 14. Strike/path behavior

新 22 市場都有最後 authoritative `polymarket_crypto_price_twap_open` strike provenance；13 clean 皆在 exact-strike subset，proxy-strike subset=0。Strike distance/crossings/reversals/winner margin 的 regime comparison 未執行；03:15 市場 opening-path coverage 不完整，即使 terminal anchors 可用也不能冒充完整 path。


## 15. p_ex vs market

核心 schema 可比，不能因此宣稱 p_ex alpha 或 lead。Mean/median/IQR/P10/P90 divergence、direction agreement、Brier/calibration：NOT_RUN_STAGE_B_GATE。保留資料待更大 clean cohort。


## 16. Existing shadow/strategy evidence

18 個 SHADOW_SIM_CYCLE_RESULT 與候選事件存在；其中可能涵蓋 partial/interrupted markets，未作 regime 績效比較。Old weekend 缺完整 run config provenance，不能確認策略／shadow measurement semantics 全部一致；未報 EV、net EV、profit factor 或 stop counterfactual，未從 win rate 推論 alpha。


## 17. Robustness checks

All clean weekday=13；exact-strike-only=13。Weekday 小時 02–06 的 weekend payload-gate eligible candidates=12（hour-restricted availability，非完成 matched distribution）。排除 scheduled rollover 的 stop/start market 後 weekday eligibility=11。窗口 elevated lag exclusions尚未定義 new threshold，未臨時創造。沒有 key metric regime comparison，故不報「方向穩健」或 robust p-values。


## 18. Sample-size status

**TOO_SMALL**：13 independent usable markets，五個 canonical anchors各可用 N=13，低於 30；100 markets 是 formal checkpoint，非顯著性保證。下一個 admission target=30，再到50/100。以本輪 completed usable rate 13/21≈61.9% ×4 markets/hour，增加17 usable 約需6.9小時；若到100，增加87 約35.1小時。此為固定窗口 observed usable-rate extrapolation，不保證後续 quality。


## 19. Regime verdict

**INSUFFICIENT_CLEAN_WEEKDAY_SAMPLE**。

Current post-fix weekday data 可保留給 formal research？**YES，仅上述13市场，opening-path限制仍适用**。

Old degraded Monday cohort 可用於 formal regime comparison？**NO**。

Existing weekend baseline 可保留？**YES，保留原始歷史 baseline，再以相同 payload/coverage gate 篩選51候選；不能把原144市場全稱新標準 clean，也不能假裝舊資料具有新 runtime diagnostics。**


## 20. Research recommendation

繼續收集，不改 live strategy／watchdog。修補已降低 generation 工作且 steady-state feed delivery 正常，但 scheduled rollover 缺口／settlement capture 必須持續標記與排除；本輪只驗證、不修復。先累積≥30 strict usable markets再做 exploratory比較；≥100再 formal checkpoint，並保留時間匹配與相同 measurement semantics gate。不推論 weekday優於weekend，不進行策略優化。


## Appendix A. Startup loop-lag candidates


| cycle | run_id | summary Taipei | market | max lag ms | owner callback cost | recovered | watchdog |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | run_1791220474_2ed1742e | 2026-10-06T01:16:39.604+08:00 | btc-updown-15m-1791220500 | 44765.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |
| 2 | run_1791224107_573fabab | 2026-10-06T02:17:09.094+08:00 | btc-updown-15m-1791224100 | 44841.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |
| 3 | run_1791227746_2be3a494 | 2026-10-06T03:17:50.755+08:00 | btc-updown-15m-1791227700 | 42630.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |
| 4 | run_1791231380_2435b627 | 2026-10-06T04:17:22.043+08:00 | btc-updown-15m-1791231300 | 38066.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |
| 5 | run_1791235015_758df0b8 | 2026-10-06T05:18:57.990+08:00 | btc-updown-15m-1791234900 | 45061.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |
| 6 | run_1791238651_7f90656a | 2026-10-06T06:18:32.883+08:00 | btc-updown-15m-1791238500 | 38940.000 | UNKNOWN; boundary delay only | first prediction / normal subsequent telemetry | 0 |


## Appendix B. All captured slow callback traces

Duration 是已完成呼叫的 inclusive elapsed／await-inclusive／boundary-delay；background=False owner 表示非 owner loop，不判定因果。Callbacks 已返回，並不代表整個服務已恢復。Quote stale peak 未逐 incident 持久化；附市場 age proxy與鄰近 timing window，不將它混為 watchdog peak。


| Taipei | cycle | market | handler | owner_loop | duration ms | nearby lag max ms | nearby queue max ms | nearby engine /s | market receive-age max proxy s | quality | watchdog intervention | rollover cause |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-06T01:15:42.238+08:00 | 1 | btc-updown-15m-1791220500 | loop_lag | YES | 44765.000 | 44765.000 | 57.017 | 11.296 | 4.245 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:16:34.179+08:00 | 1 | btc-updown-15m-1791220500 | fee_lookup_await | NO | 1011.631 | 44765.000 | 57.017 | 11.296 | 4.245 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:21:34.863+08:00 | 1 | btc-updown-15m-1791220500 | fee_lookup_await | NO | 970.182 | 11.000 | 16.712 | 13.632 | 4.245 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:26:37.651+08:00 | 1 | btc-updown-15m-1791220500 | fee_lookup_await | NO | 976.951 | 17.000 | 21.927 | 13.299 | 4.245 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:28:08.887+08:00 | 1 | btc-updown-15m-1791220500 | adapter_quote_generation | YES | 148.777 | 38.000 | 109.375 | 10.833 | 4.245 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:32:58.489+08:00 | 1 | btc-updown-15m-1791221400 | fee_lookup_await | NO | 1025.839 | 31.000 | 30.908 | 14.366 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:37:36.717+08:00 | 1 | btc-updown-15m-1791221400 | adapter_quote_generation | YES | 110.502 | 29.000 | 62.631 | 12.964 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:37:59.922+08:00 | 1 | btc-updown-15m-1791221400 | fee_lookup_await | NO | 1048.287 | 37.000 | 77.796 | 13.532 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:38:23.702+08:00 | 1 | btc-updown-15m-1791221400 | adapter_quote_generation | YES | 130.409 | 37.000 | 77.796 | 13.532 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:41:00.281+08:00 | 1 | btc-updown-15m-1791221400 | journal_order | NO | 182.261 | 32.000 | 36.780 | 13.862 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:43:02.719+08:00 | 1 | btc-updown-15m-1791221400 | fee_lookup_await | NO | 1020.139 | 30.000 | 40.808 | 13.398 | 2.173 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:45:18.961+08:00 | 1 | btc-updown-15m-1791222300 | lifecycle_market_selection | NO | 231.366 | 45.000 | 41.055 | 10.698 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:46:15.009+08:00 | 1 | btc-updown-15m-1791222300 | adapter_quote_generation | YES | 181.791 | 35.000 | 37.337 | 14.066 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:46:48.350+08:00 | 1 | btc-updown-15m-1791222300 | fee_lookup_await | NO | 979.053 | 36.000 | 35.252 | 14.229 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:49:19.003+08:00 | 1 | btc-updown-15m-1791222300 | loop_lag | YES | 214.000 | 214.000 | 38.497 | 13.867 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:51:49.401+08:00 | 1 | btc-updown-15m-1791222300 | fee_lookup_await | NO | 1075.216 | 35.000 | 38.359 | 14.866 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:53:59.858+08:00 | 1 | btc-updown-15m-1791222300 | fee_lookup_await | NO | 979.881 | 11.000 | 41.988 | 14.999 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:56:49.558+08:00 | 1 | btc-updown-15m-1791222300 | adapter_quote_generation | YES | 335.135 | 74.000 | 127.812 | 14.114 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:57:22.846+08:00 | 1 | btc-updown-15m-1791222300 | status_timer | NO | 100.877 | 74.000 | 127.812 | 14.114 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T01:59:01.109+08:00 | 1 | btc-updown-15m-1791222300 | fee_lookup_await | NO | 993.239 | 42.000 | 46.055 | 8.949 | 4.322 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:01:31.218+08:00 | 1 | btc-updown-15m-1791223200 | fee_lookup_await | NO | 961.927 | 13.000 | 41.180 | 14.215 | 1.535 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:04:51.751+08:00 | 1 | btc-updown-15m-1791223200 | fee_lookup_await | NO | 1002.202 | 14.000 | 42.247 | 13.916 | 1.535 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:09:53.587+08:00 | 1 | btc-updown-15m-1791223200 | fee_lookup_await | NO | 990.160 | 33.000 | 41.699 | 14.166 | 1.535 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:16:15.369+08:00 | 2 | btc-updown-15m-1791224100 | loop_lag | YES | 44841.000 | 44841.000 | 49.384 | 10.390 | 3.978 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:16:51.419+08:00 | 2 | btc-updown-15m-1791224100 | fee_lookup_await | NO | 1029.495 | 44841.000 | 49.384 | 10.390 | 3.978 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:17:12.576+08:00 | 2 | btc-updown-15m-1791224100 | data_engine_queue_wait | YES | 163.872 | 29.000 | 169.237 | 14.315 | 3.978 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:21:53.104+08:00 | 2 | btc-updown-15m-1791224100 | fee_lookup_await | NO | 1027.627 | 58.000 | 40.013 | 14.850 | 3.978 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:26:56.063+08:00 | 2 | btc-updown-15m-1791224100 | fee_lookup_await | NO | 995.499 | 30.000 | 43.816 | 13.799 | 3.978 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:31:49.230+08:00 | 2 | btc-updown-15m-1791225000 | fee_lookup_await | NO | 994.484 | 23.000 | 34.064 | 13.998 | 1.734 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:35:18.580+08:00 | 2 | btc-updown-15m-1791225000 | fee_lookup_await | NO | 966.597 | 11.000 | 37.527 | 14.312 | 1.734 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:36:49.469+08:00 | 2 | btc-updown-15m-1791225000 | adapter_quote_generation | YES | 395.119 | 27.000 | 37.142 | 13.699 | 1.734 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:37:24.565+08:00 | 2 | btc-updown-15m-1791225000 | journal_event | NO | 141.999 | 28.000 | 36.529 | 14.763 | 1.734 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:40:21.065+08:00 | 2 | btc-updown-15m-1791225000 | fee_lookup_await | NO | 1027.935 | 13.000 | 44.696 | 14.033 | 1.734 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:46:17.012+08:00 | 2 | btc-updown-15m-1791225900 | journal_event | NO | 319.072 | 26.000 | 44.137 | 13.816 | 2.543 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:52:44.690+08:00 | 2 | btc-updown-15m-1791225900 | fee_lookup_await | NO | 992.671 | 26.000 | 38.943 | 13.915 | 2.543 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:54:47.021+08:00 | 2 | btc-updown-15m-1791225900 | journal_event | NO | 508.269 | 27.000 | 36.470 | 15.165 | 2.543 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:55:50.110+08:00 | 2 | btc-updown-15m-1791225900 | fee_lookup_await | NO | 964.194 | 11.000 | 38.816 | 14.633 | 2.543 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T02:57:30.628+08:00 | 2 | btc-updown-15m-1791225900 | adapter_quote_generation | YES | 106.854 | 34.000 | 74.796 | 14.716 | 2.543 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:00:50.145+08:00 | 2 | btc-updown-15m-1791226800 | data_engine_queue_wait | YES | 102.083 | 31.000 | 102.083 | 13.613 | 4.675 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:01:31.443+08:00 | 2 | btc-updown-15m-1791226800 | fee_lookup_await | NO | 1014.933 | 27.000 | 43.013 | 14.278 | 4.675 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:02:24.059+08:00 | 2 | btc-updown-15m-1791226800 | journal_event | NO | 200.380 | 18.000 | 47.970 | 14.549 | 4.675 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:06:31.519+08:00 | 2 | btc-updown-15m-1791226800 | fee_lookup_await | NO | 1026.908 | 17.000 | 46.203 | 13.523 | 4.675 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:11:32.586+08:00 | 2 | btc-updown-15m-1791226800 | fee_lookup_await | NO | 984.149 | 36.000 | 40.259 | 14.800 | 4.675 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:16:53.009+08:00 | 3 | btc-updown-15m-1791227700 | loop_lag | YES | 42630.000 | 42630.000 | 37.356 | 11.566 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:18:21.786+08:00 | 3 | btc-updown-15m-1791227700 | fee_lookup_await | NO | 975.151 | 45.000 | 35.662 | 14.314 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:19:30.787+08:00 | 3 | btc-updown-15m-1791227700 | adapter_quote_generation | YES | 159.418 | 26.000 | 36.882 | 14.581 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:21:48.256+08:00 | 3 | btc-updown-15m-1791227700 | journal_event | NO | 264.542 | 26.000 | 39.505 | 14.666 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:22:43.602+08:00 | 3 | btc-updown-15m-1791227700 | maker_worker_lock_and_start | YES | 139.936 | 28.000 | 141.383 | 14.116 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:23:03.293+08:00 | 3 | btc-updown-15m-1791227700 | journal_order | NO | 105.058 | 18.000 | 38.055 | 13.983 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:23:23.220+08:00 | 3 | btc-updown-15m-1791227700 | fee_lookup_await | NO | 999.893 | 18.000 | 38.055 | 13.983 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:28:25.313+08:00 | 3 | btc-updown-15m-1791227700 | fee_lookup_await | NO | 1001.859 | 52.000 | 49.301 | 11.724 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:30:00.222+08:00 | 3 | btc-updown-15m-1791227700 | data_engine_queue_wait | YES | 126.508 | 62.000 | 126.508 | 11.297 | 4.847 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:31:31.917+08:00 | 3 | btc-updown-15m-1791228600 | fee_lookup_await | NO | 996.274 | 31.000 | 41.795 | 13.483 | 1.752 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:36:32.464+08:00 | 3 | btc-updown-15m-1791228600 | fee_lookup_await | NO | 1024.276 | 12.000 | 38.647 | 14.633 | 1.752 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:36:48.187+08:00 | 3 | btc-updown-15m-1791228600 | fee_lookup_await | NO | 1059.349 | 12.000 | 38.647 | 14.633 | 1.752 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:38:53.459+08:00 | 3 | btc-updown-15m-1791228600 | adapter_quote_generation | YES | 119.777 | 11.000 | 257.606 | 13.950 | 1.752 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:41:49.829+08:00 | 3 | btc-updown-15m-1791228600 | fee_lookup_await | NO | 1103.903 | 31.000 | 35.989 | 14.500 | 1.752 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:45:19.262+08:00 | 3 | btc-updown-15m-1791229500 | lifecycle_market_selection | NO | 256.839 | 25.000 | 74.436 | 12.099 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:46:31.961+08:00 | 3 | btc-updown-15m-1791229500 | fee_lookup_await | NO | 1127.078 | 66.000 | 52.534 | 13.466 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:51:21.327+08:00 | 3 | btc-updown-15m-1791229500 | journal_order | NO | 383.727 | 36.000 | 48.152 | 13.881 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:51:35.445+08:00 | 3 | btc-updown-15m-1791229500 | fee_lookup_await | NO | 978.881 | 36.000 | 48.152 | 13.881 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:56:36.096+08:00 | 3 | btc-updown-15m-1791229500 | fee_lookup_await | NO | 1023.527 | 59.000 | 28.177 | 14.766 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:57:13.933+08:00 | 3 | btc-updown-15m-1791229500 | status_timer | NO | 161.766 | 41.000 | 43.332 | 15.000 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T03:58:44.225+08:00 | 3 | btc-updown-15m-1791229500 | status_timer | NO | 133.300 | 24.000 | 40.549 | 15.332 | 2.095 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:01:14.483+08:00 | 3 | btc-updown-15m-1791230400 | journal_event | NO | 100.273 | 15.000 | 48.936 | 14.333 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:01:33.410+08:00 | 3 | btc-updown-15m-1791230400 | fee_lookup_await | NO | 983.409 | 15.000 | 48.936 | 14.333 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:01:49.031+08:00 | 3 | btc-updown-15m-1791230400 | journal_order | NO | 104.408 | 15.000 | 48.936 | 14.333 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:06:35.965+08:00 | 3 | btc-updown-15m-1791230400 | fee_lookup_await | NO | 1059.114 | 21.000 | 43.904 | 13.699 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:11:15.818+08:00 | 3 | btc-updown-15m-1791230400 | status_timer | NO | 207.174 | 45.000 | 42.167 | 13.450 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:11:37.912+08:00 | 3 | btc-updown-15m-1791230400 | fee_lookup_await | NO | 1299.030 | 45.000 | 42.167 | 13.450 | 2.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:17:21.039+08:00 | 4 | btc-updown-15m-1791231300 | loop_lag | YES | 38066.000 | 38066.000 | UNKNOWN | 0.000 | 4.049 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:17:35.138+08:00 | 4 | btc-updown-15m-1791231300 | fee_lookup_await | NO | 1040.793 | 26.000 | 48.227 | 12.032 | 4.049 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:22:35.941+08:00 | 4 | btc-updown-15m-1791231300 | fee_lookup_await | NO | 994.389 | 29.000 | 37.064 | 12.254 | 4.049 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:24:55.558+08:00 | 4 | btc-updown-15m-1791231300 | adapter_quote_generation | YES | 120.235 | 179.000 | 244.692 | 13.510 | 4.049 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:27:36.913+08:00 | 4 | btc-updown-15m-1791231300 | fee_lookup_await | NO | 1060.902 | 45.000 | 41.066 | 11.457 | 4.049 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:34:53.781+08:00 | 4 | btc-updown-15m-1791232200 | maker_worker_lock_and_start | YES | 127.077 | 50.000 | 135.981 | 13.699 | 2.395 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:35:10.824+08:00 | 4 | btc-updown-15m-1791232200 | fee_lookup_await | NO | 972.137 | 50.000 | 135.981 | 13.699 | 2.395 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:37:07.287+08:00 | 4 | btc-updown-15m-1791232200 | fee_lookup_await | NO | 1022.691 | 30.000 | 43.166 | 13.776 | 2.395 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:42:08.010+08:00 | 4 | btc-updown-15m-1791232200 | fee_lookup_await | NO | 991.218 | 34.000 | 42.846 | 13.097 | 2.395 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:45:19.022+08:00 | 4 | btc-updown-15m-1791233100 | lifecycle_market_selection | NO | 328.250 | 35.000 | 68.781 | 9.178 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:47:47.049+08:00 | 4 | btc-updown-15m-1791233100 | fee_lookup_await | NO | 1368.392 | 33.000 | 38.404 | 13.848 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:52:48.029+08:00 | 4 | btc-updown-15m-1791233100 | fee_lookup_await | NO | 975.398 | 43.000 | 53.167 | 14.367 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:53:01.753+08:00 | 4 | btc-updown-15m-1791233100 | status_timer | NO | 121.382 | 43.000 | 53.167 | 14.367 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:55:02.018+08:00 | 4 | btc-updown-15m-1791233100 | status_timer | NO | 140.867 | 33.000 | 41.801 | 13.633 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:57:05.255+08:00 | 4 | btc-updown-15m-1791233100 | adapter_quote_generation | YES | 143.088 | 26.000 | 112.451 | 13.733 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T04:57:48.323+08:00 | 4 | btc-updown-15m-1791233100 | fee_lookup_await | NO | 972.511 | 47.000 | 48.400 | 15.698 | 2.342 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:00:18.880+08:00 | 4 | btc-updown-15m-1791234000 | lifecycle_market_selection | NO | 309.558 | 32.000 | 60.251 | 12.864 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:01:31.288+08:00 | 4 | btc-updown-15m-1791234000 | fee_lookup_await | NO | 1099.052 | 42.000 | 43.400 | 13.499 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:02:12.565+08:00 | 4 | btc-updown-15m-1791234000 | status_timer | NO | 144.003 | 42.000 | 43.400 | 13.499 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:06:05.730+08:00 | 4 | btc-updown-15m-1791234000 | journal_order | YES | 100.863 | 29.000 | 108.473 | 11.933 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:06:33.030+08:00 | 4 | btc-updown-15m-1791234000 | fee_lookup_await | NO | 1032.515 | 34.000 | 38.641 | 13.466 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:08:57.202+08:00 | 4 | btc-updown-15m-1791234000 | data_engine_queue_wait | YES | 131.150 | 38.000 | 135.054 | 11.166 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:11:18.250+08:00 | 4 | btc-updown-15m-1791234000 | journal_event | NO | 136.594 | 20.000 | 31.594 | 11.648 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:11:36.141+08:00 | 4 | btc-updown-15m-1791234000 | fee_lookup_await | NO | 987.957 | 19.000 | 38.323 | 11.590 | 4.219 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:18:03.908+08:00 | 5 | btc-updown-15m-1791234900 | loop_lag | YES | 45061.000 | 45061.000 | 71.464 | 10.165 | 4.905 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:18:43.269+08:00 | 5 | btc-updown-15m-1791234900 | fee_lookup_await | NO | 969.681 | 45061.000 | 71.464 | 10.165 | 4.905 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:23:44.308+08:00 | 5 | btc-updown-15m-1791234900 | fee_lookup_await | NO | 1030.204 | 11.000 | 26.457 | 12.147 | 4.905 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:28:32.021+08:00 | 5 | btc-updown-15m-1791234900 | journal_order | NO | 109.698 | 11.000 | 28.763 | 6.107 | 4.905 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:28:47.035+08:00 | 5 | btc-updown-15m-1791234900 | fee_lookup_await | NO | 994.089 | 11.000 | 28.763 | 6.107 | 4.905 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:33:04.898+08:00 | 5 | btc-updown-15m-1791235800 | fee_lookup_await | NO | 978.821 | 72.000 | 24.991 | 12.499 | 4.811 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:38:06.708+08:00 | 5 | btc-updown-15m-1791235800 | fee_lookup_await | NO | 1017.317 | 32.000 | 39.600 | 12.199 | 4.811 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:43:06.917+08:00 | 5 | btc-updown-15m-1791235800 | fee_lookup_await | NO | 1057.296 | 16.000 | 29.822 | 8.933 | 4.811 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:45:23.707+08:00 | 5 | btc-updown-15m-1791236700 | maker_worker_lock_and_start | YES | 118.905 | 24.000 | 119.257 | 12.000 | 2.715 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:47:05.888+08:00 | 5 | btc-updown-15m-1791236700 | fee_lookup_await | NO | 967.557 | 11.000 | 33.811 | 13.700 | 2.715 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:50:52.056+08:00 | 5 | btc-updown-15m-1791236700 | fee_lookup_await | NO | 962.537 | 13.000 | 32.354 | 13.949 | 2.715 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:55:53.638+08:00 | 5 | btc-updown-15m-1791236700 | fee_lookup_await | NO | 994.735 | 23.000 | 37.322 | 13.350 | 2.715 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T05:59:56.359+08:00 | 5 | btc-updown-15m-1791236700 | journal_event | NO | 209.651 | 22.000 | 44.073 | 9.649 | 2.715 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:02:35.530+08:00 | 5 | btc-updown-15m-1791237600 | fee_lookup_await | NO | 994.189 | 32.000 | 38.376 | 13.880 | 1.657 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:06:54.196+08:00 | 5 | btc-updown-15m-1791237600 | fee_lookup_await | NO | 1020.311 | 27.000 | 45.875 | 14.733 | 1.657 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:08:59.890+08:00 | 5 | btc-updown-15m-1791237600 | adapter_price_change | YES | 129.653 | 28.000 | 261.544 | 14.098 | 1.657 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:11:46.448+08:00 | 5 | btc-updown-15m-1791237600 | status_timer | NO | 139.791 | 41.000 | 46.706 | 13.982 | 1.657 | SYNCHRONIZED_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:15:51.513+08:00 | 5 | btc-updown-15m-1791238500 | journal_event | NO | 119.199 | 56.000 | 43.291 | 10.998 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:18:32.882+08:00 | 6 | btc-updown-15m-1791238500 | loop_lag | YES | 38940.000 | 38940.000 | UNKNOWN | 0.000 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:19:05.225+08:00 | 6 | btc-updown-15m-1791238500 | fee_lookup_await | NO | 995.539 | 29.000 | 62.904 | 12.465 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:20:03.183+08:00 | 6 | btc-updown-15m-1791238500 | journal_order | NO | 220.303 | 61.000 | 40.356 | 13.900 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:22:55.395+08:00 | 6 | btc-updown-15m-1791238500 | fee_lookup_await | NO | 988.057 | 18.000 | 45.892 | 14.066 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:26:32.770+08:00 | 6 | btc-updown-15m-1791238500 | journal_order | NO | 187.061 | 13.000 | 42.787 | 14.216 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:27:41.695+08:00 | 6 | btc-updown-15m-1791238500 | status_timer | NO | 115.634 | 40.000 | 63.685 | 15.632 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:27:57.754+08:00 | 6 | btc-updown-15m-1791238500 | fee_lookup_await | NO | 1001.476 | 40.000 | 63.685 | 15.632 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:30:00.309+08:00 | 6 | btc-updown-15m-1791238500 | lifecycle_phase | NO | 264.688 | 39.000 | 51.378 | 10.920 | 2.509 | INTERRUPTED | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:30:56.538+08:00 | 6 | btc-updown-15m-1791239400 | data_engine_queue_wait | YES | 161.113 | 100.000 | 161.113 | 13.117 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:31:36.850+08:00 | 6 | btc-updown-15m-1791239400 | fee_lookup_await | NO | 957.220 | 42.000 | 46.057 | 13.655 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:36:38.145+08:00 | 6 | btc-updown-15m-1791239400 | fee_lookup_await | NO | 967.202 | 72.000 | 88.610 | 15.067 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:38:42.796+08:00 | 6 | btc-updown-15m-1791239400 | status_timer | NO | 195.634 | 102.000 | 96.076 | 15.364 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:39:05.388+08:00 | 6 | btc-updown-15m-1791239400 | adapter_raw_decoder | YES | 160.352 | 102.000 | 96.076 | 15.364 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:39:22.825+08:00 | 6 | btc-updown-15m-1791239400 | adapter_price_change | YES | 142.401 | 102.000 | 96.076 | 15.364 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:39:40.232+08:00 | 6 | btc-updown-15m-1791239400 | adapter_quote_generation | YES | 129.841 | 16.000 | 45.576 | 13.433 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:40:54.726+08:00 | 6 | btc-updown-15m-1791239400 | adapter_quote_generation | YES | 148.025 | 50.000 | 63.865 | 14.763 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:41:41.091+08:00 | 6 | btc-updown-15m-1791239400 | fee_lookup_await | NO | 1007.428 | 54.000 | 46.682 | 14.432 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |
| 2026-10-06T06:42:05.089+08:00 | 6 | btc-updown-15m-1791239400 | journal_order | NO | 201.300 | 54.000 | 46.682 | 14.432 | 1.769 | PARTIAL_USABLE | 0 | scheduled only if lifecycle table matches; no attribution to trace |


## Appendix C. Elevated boundary-delay summary windows

Diagnostic buckets >100ms，非新的 trading threshold；與 slow traces可能重複，不能加總當 independent incidents。


| cycle | run_id | slug | window_sec | lag_max | lag_P95 | queue_max | queue_P95 | engine_rate | startup_lag | window end Taipei |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | run_1791220474_2ed1742e | btc-updown-15m-1791220500 | 60.021 | 44765.000 | 2.000 | 57.017 | 4.796 | 11.296 | YES | 2026-10-06T01:16:39.604+08:00 |
| 1 | run_1791220474_2ed1742e | btc-updown-15m-1791220500 | 60.003 | 38.000 | 21.000 | 109.375 | 13.597 | 10.833 | NO | 2026-10-06T01:28:39.719+08:00 |
| 1 | run_1791220474_2ed1742e | btc-updown-15m-1791222300 | 60.000 | 214.000 | 8.000 | 38.497 | 10.960 | 13.867 | NO | 2026-10-06T01:49:40.100+08:00 |
| 1 | run_1791220474_2ed1742e | btc-updown-15m-1791222300 | 60.013 | 74.000 | 15.000 | 127.812 | 19.768 | 14.114 | NO | 2026-10-06T01:57:40.206+08:00 |
| 2 | run_1791224107_573fabab | btc-updown-15m-1791224100 | 60.058 | 44841.000 | 23.000 | 49.384 | 13.639 | 10.390 | YES | 2026-10-06T02:17:09.094+08:00 |
| 2 | run_1791224107_573fabab | btc-updown-15m-1791224100 | 60.007 | 29.000 | 10.000 | 169.237 | 14.435 | 14.315 | NO | 2026-10-06T02:18:09.095+08:00 |
| 2 | run_1791224107_573fabab | btc-updown-15m-1791226800 | 60.015 | 31.000 | 11.000 | 102.083 | 25.780 | 13.613 | NO | 2026-10-06T03:01:09.882+08:00 |
| 3 | run_1791227746_2be3a494 | btc-updown-15m-1791227700 | 60.002 | 42630.000 | 22.000 | 37.356 | 11.023 | 11.566 | YES | 2026-10-06T03:17:50.755+08:00 |
| 3 | run_1791227746_2be3a494 | btc-updown-15m-1791227700 | 60.001 | 28.000 | 15.000 | 141.383 | 13.446 | 14.116 | NO | 2026-10-06T03:22:50.790+08:00 |
| 3 | run_1791227746_2be3a494 | btc-updown-15m-1791228600 | 60.014 | 62.000 | 11.000 | 126.508 | 13.990 | 11.297 | NO | 2026-10-06T03:30:50.947+08:00 |
| 3 | run_1791227746_2be3a494 | btc-updown-15m-1791228600 | 60.001 | 11.000 | 9.000 | 257.606 | 15.764 | 13.950 | NO | 2026-10-06T03:39:50.970+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791231300 | 60.124 | 38066.000 | 3.000 | UNKNOWN | UNKNOWN | 0.000 | YES | 2026-10-06T04:17:22.043+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791231300 | 60.027 | 179.000 | 19.000 | 244.692 | 33.036 | 13.510 | NO | 2026-10-06T04:25:22.204+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791232200 | 60.004 | 50.000 | 9.000 | 135.981 | 12.642 | 13.699 | NO | 2026-10-06T04:35:22.712+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791233100 | 60.003 | 26.000 | 11.000 | 112.451 | 16.081 | 13.733 | NO | 2026-10-06T04:57:22.900+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791234000 | 60.000 | 29.000 | 7.000 | 108.473 | 15.607 | 11.933 | NO | 2026-10-06T05:06:22.876+08:00 |
| 4 | run_1791231380_2435b627 | btc-updown-15m-1791234000 | 60.001 | 38.000 | 19.000 | 135.054 | 15.625 | 11.166 | NO | 2026-10-06T05:09:22.888+08:00 |
| 5 | run_1791235015_758df0b8 | btc-updown-15m-1791234900 | 60.008 | 45061.000 | 26.000 | 71.464 | 9.227 | 10.165 | YES | 2026-10-06T05:18:57.990+08:00 |
| 5 | run_1791235015_758df0b8 | btc-updown-15m-1791236700 | 60.001 | 24.000 | 4.000 | 119.257 | 11.850 | 12.000 | NO | 2026-10-06T05:45:59.322+08:00 |
| 5 | run_1791235015_758df0b8 | btc-updown-15m-1791237600 | 60.008 | 28.000 | 18.000 | 261.544 | 18.507 | 14.098 | NO | 2026-10-06T06:09:59.788+08:00 |
| 6 | run_1791238651_7f90656a | btc-updown-15m-1791238500 | 60.000 | 38940.000 | 10.000 | UNKNOWN | UNKNOWN | 0.000 | YES | 2026-10-06T06:18:32.883+08:00 |
| 6 | run_1791238651_7f90656a | btc-updown-15m-1791239400 | 60.000 | 100.000 | 27.000 | 161.113 | 18.703 | 13.117 | NO | 2026-10-06T06:31:33.149+08:00 |
| 6 | run_1791238651_7f90656a | btc-updown-15m-1791239400 | 60.010 | 102.000 | 15.000 | 96.076 | 34.834 | 15.364 | NO | 2026-10-06T06:39:33.360+08:00 |


## Appendix D. Weekend candidate membership after payload/coverage gate

| slug | canonical_quality | anchors | diagnostic_equivalence |
| --- | --- | --- | --- |
| btc-updown-15m-1790964000 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1790964900 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1790965800 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791005400 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791009900 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791010800 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791015300 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791019800 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791024300 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791027000 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791027900 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791028800 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791029700 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791030600 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791031500 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791032400 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791035100 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791036000 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791037800 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791038700 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791042300 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791043200 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791045000 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791045900 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791048600 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791052200 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791054000 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791054900 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791055800 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791057600 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791061200 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791062100 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791064800 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791065700 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791070200 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791076500 | FULL | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791077400 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791081000 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791081900 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791085500 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791086400 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791089100 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791090000 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791095400 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791102600 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791103500 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791112500 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791113400 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791114300 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791117900 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |
| btc-updown-15m-1791121500 | GOOD | {"300":true,"180":true,"120":true,"60":true,"30":true} | UNAVAILABLE: pre-fix has no equivalent consumer/writer timing telemetry |


## Final summary

```text
POST_FIX_STABILITY=STABILITY_MATERIALLY_IMPROVED_BUT_FAILURES_REMAIN
CLEAN_WEEKDAY_MARKETS=13
TOTAL_WEEKDAY_MARKETS=22 (21 completed + 1 active)
USABLE_RATE=59.09% (13/22); completed-only=61.90% (13/21)
WORST_LOOP_LAG_MS=45061 (startup); steady-state=214
WORST_DATAENGINE_P95_MS=51.557 (recorded quote queue windows)
WATCHDOG_TRIGGERS=0
WATCHDOG_ROLLOVERS=0
SCHEDULED_ROLLOVERS=5
PREDICTION_DROPS=0 observed durable health counters; unobserved shutdown tail not certified
LARGEST_SAME_RUN_GAP_SEC=6.187; cross-run market max=111.607

WEEKDAY_SAMPLE_STATUS=TOO_SMALL
WEEKEND_SAMPLE_N=51 payload/coverage candidates; full runtime quality equivalence UNKNOWN
T5_WEEKDAY_FLIP_RATE=NOT_RUN_STAGE_B_GATE
T5_WEEKEND_FLIP_RATE=NOT_RUN_STAGE_B_GATE
T2_WEEKDAY_FLIP_RATE=NOT_RUN_STAGE_B_GATE
T2_WEEKEND_FLIP_RATE=NOT_RUN_STAGE_B_GATE
REGIME_VERDICT=INSUFFICIENT_CLEAN_WEEKDAY_SAMPLE

FORMAL_RESEARCH_WEEKDAY_READY=NO (sample checkpoint); 13 admitted markets retainable=YES
OLD_DEGRADED_MONDAY_EXCLUDED=YES
STRATEGY_CHANGE_RECOMMENDED=NO
```

Safety：new offline snapshots YES；both quick_check ok YES；source/config edited NO；commit/push NO；bot stopped/restarted/another bot launched NO；active DB mutated by analysis NO；active Parquet mutated NO；strategy/watchdog tuned NO。唯讀讀取既有 Parquet，僅新建分析快照、暫存分析檔與本份 generated report。
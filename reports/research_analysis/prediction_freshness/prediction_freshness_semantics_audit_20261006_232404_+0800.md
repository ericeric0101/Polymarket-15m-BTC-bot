# Prediction freshness semantics audit

## 1. Provenance

稽核 HEAD：548ae5574b56d2618a99534c595854772077c79b，符合要求；branch codex/db-resilience-and-stoploss-priority。Tracked/staged tree clean；既有 research reports 未追蹤。本次無 source/config edit、commit、push、stop/restart、active DB/Parquet mutation。

近期 run：run_1791295980_6fbb687f；觀測範圍 2026-10-06T22:14:26.688567+08:00 至 2026-10-06T23:19:08.808232+08:00。Journal run_manifest.git_commit 明確為548ae5574b56d2618a99534c595854772077c79b，runtime_git_revision=548ae55；manifest git_dirty=true但tracked diff hash為null，不能把未追蹤report造成dirty自動推論為runtime source不同。此次最新run已有manifest，不沿用舊run的legacy/unknown結論。

使用 SQLite online backup API、source mode=ro，新建 data/analysis_snapshots/freshness_audit_20261006_231849_+0800/twap_forward_shadow_snapshot.db。TWAP及journal destination quick_check=ok。Journal以read-only BEGIN固定read snapshot後online backup；首次可移動backup因持續寫入反覆重讀，僅終止本輪分析備份程序後安全重作，未停止bot。Manifest 記錄 source mtime/destination size；沒有 cp live DB、manual WAL checkpoint、migration 或 index creation。兩個 backup 為先後取得，非跨 DB 原子快照。

## 2. Source-of-truth map

Polymarket ws message.timestamp / Binance E,T / Chainlink payload observation time → adapter/local receipt → canonical strategy maps/history → maker quote cycle `_quote_maker_orders` (run_bot.py:3777) 或 forced entry decision (2169)、bot/quote_runtime.py:506 order_submit/dry_run_order_submit capture → `PredictionResearchSnapshotter.capture` → `_settlement_probability_shadow_inputs` → `build_prediction_snapshot` → existing LeadLagDB.enqueue_decision → bounded worker SQL/commit → TWAP SQLite lead_lag_decisions payload_json。

這種 prediction payload 寫入 SQLite，沒有自己的 Parquet writer。BTC 1s Parquet 是另一個 observer，不能把其 write time 當 prediction write time。Maker event-driven callback 經 minimum interval=1s 限流，非保證每秒的獨立定時採樣。

`_emit_metrics` 的 console freshness 是 accepted enqueue 計數，不是 SQLite durable commit 計數。離線 report/store 使用真正 persisted rows。Writer failure 可能使這兩者不同，但不是 joint 的分母差異。

## 3. Timestamp-domain table

| 概念 | 權威變數／函式 | 單位／domain |
|---|---|---|
| sample time | capture.now → snapshot_ts；caller time.time() 或 entry now_ts | epoch sec，local wall；capture 開始固定 |
| p_ex source | p_ex_source_ts；Chainlink `_polymarket_chainlink_price_observation_ts` 或 Binance source ts | epoch sec，source clock；不是 probability computation time |
| p_ex local receipt | spot_pricer `_polymarket_chainlink_price_ts` / `_binance_ws_price_ts` | local epoch sec；snapshot 未持久化這個 receipt |
| p_ex composite age | max(source age, sigma age, TWAP age)，忽略 None | sec；跨 source/local clock subtraction |
| market source | last_quote_source_ts_by_inst → per-side source ts | QuoteTick.ts_event ns → epoch sec；ws timestamp ms |
| market receive | last_quote_received_ts_by_inst → per-side received ts | local time.time() epoch sec；valid 與 rejected heartbeat receipt 都可能更新 |
| QuoteTick creation | ts_init / last_quote_update_ts_by_inst | ns→sec local adapter clock；research payload 未保存 ts_init |
| underlying book | every ws delta/snapshot applied to `_local_books` | event ts ns，local ts_init ns；payload 不保存完整 book clock |
| BTC fast source | history[-1][0] → btc_source_ts | Binance trade T (fallback E) ms→epoch sec |
| BTC receipt | history tuple[2] = PriceTick.received_at_ts | local epoch sec；prediction payload 未保存 |
| TWAP/reference | `_polymarket_chainlink_twap_observation_ts` → twap_source_ts | Chainlink observation epoch sec；receipt 分開保存於 strategy |
| enqueue | decision_epoch_ns=int(now*1e9), last_enqueue_ts=now | sample time surrogate；不是 enqueue completion clock |
| durable write | conn.commit returns | 沒有每列 persisted write timestamp；health last_persist_ts=None |
| elapsed telemetry | perf_counter / monotonic | duration only，未拿來減 epoch |

所有 freshness 時間形式上皆為 epoch seconds；沒有證據顯示 ms/ns unit conversion 錯誤。問題是不同機器/source clock 的 epoch 被視為嚴格同時鐘；source 略領先 local 會被拒絕。另 capture 固定 now 後仍讀 mutable feed state，後到資料可造成負 age；兩者必須區分。

## 4. Exact freshness equations

令 F(a,L) := (a is not None AND 0 <= a <= L)。Missing/negative/over-limit 均 false，沒有排除 denominator。

```
pex_age = max(nonmissing(now - p_ex_source_ts,
                        sigma_ex_market_age_sec, now - twap_source_ts))
p_ex_fresh = finite(raw p_up_ex_market) AND sigma_ex_market_fresh AND F(pex_age,10)
side_fresh = finite bid/ask AND 0 <= bid <= ask <= 1
             AND F(input source age,2) AND F(input receive age,2)
             AND F(now-side_source_ts,2) AND F(now-side_received_ts,2)
market_mid_fresh = up_fresh OR down_fresh
market_quote_fresh = up_fresh AND down_fresh
btc_fresh = F(now-btc_source_ts,10) AND finite btc_spot
twap_fresh = F(now-twap_source_ts,10)
joint_fresh = p_ex_fresh AND market_mid_fresh AND btc_fresh AND twap_fresh
```

Threshold authority：quote_max_delivery_delay_sec ← QUOTE_MAX_DELIVERY_DELAY_SEC default2、minimum0.1；p_ex/TWAP `_RAW_SPOT_FRESHNESS_SEC`=10；BTC SIDE_SIGNAL_BTC_TREND_PRIMARY_STALE_SEC default10、minimum1。檢查 local .env 無上述 market/BTC override；run_manifest.safe_config明確記錄market2sec、BTC10sec；persisted rows以2/10重建一致。Model sigma/raw-path eligibility 另用严格 `<10`；snapshot F 使用 `<=`，不能混同。

## 5. Numerator/denominator

Console 三者皆 `100 * cumulative accepted snapshots with flag true / max(1,cumulative accepted snapshots)`；`written` 實為 enqueue accepted。Counter 同屬一個 snapshotter、node cycle 建立時清零；不 per-slug reset，不含 prewarm 單獨 capture。False/stale/missing 已接受列入分母；rejected enqueue 不入分子分母。

`eligible` 提早在 market start/end guard、exception 前遞增，不是這三個 percentage denominator。No slug/throttled 不入 eligible；periodic future/expired market returns 無 snapshot；forced entry 可跨界；沒有 phase gate 排除 WAITING/SETTLING，若 current slug/時間允許仍可計入。Startup 取得 slug 後的實際 accepted rows 會計入。

`recent_health` 另有最多600項、預設300sec、可 current-slug filter 的 capture denominator；persisted_joint_fresh_pct 實際仍為 accepted subset，不是 DB acknowledgment。Scripts `_quality` / ResearchStore 才用 persisted row counts。

## 6. Why 99% / 34% / 3% is possible

相同分母若 joint 只是 P∩M，P=99%、M=34% 則交集必≥33%，3%不可能。實作卻為 P∩M∩B∩T，因此沒有數學矛盾。

最新 run 全部3038列：p_ex=3013/3038 (99.18%)、market=1356/3038 (44.63%)、BTC=360/3038 (11.85%)、TWAP=3028/3038 (99.67%)、joint=151/3038 (4.97%)。P∩M=1346；其中1195因 BTC false 被排除，TWAP 額外排除0，剩151。這不是拿舊 console 34%硬套新 cohort。

22:15–22:30 完整市場（slug尾1791296100）684列：p_ex99.85%、market28.22%、BTC10.38%、joint2.78%，重現所述數量級。以下每個百分比均附 raw counts。

| market | rows | span sec | max gap sec | p_ex | market | BTC | TWAP | joint |
|---|---:|---:|---:|---|---|---|---|---|
| btc-updown-15m-1791295200 | 15 | 33.11 | 5.70 | 7/15 = 46.67% | 7/15 = 46.67% | 4/15 = 26.67% | 15/15 = 100.00% | 2/15 = 13.33% |
| btc-updown-15m-1791296100 | 684 | 881.48 | 5.89 | 683/684 = 99.85% | 193/684 = 28.22% | 71/684 = 10.38% | 684/684 = 100.00% | 19/684 = 2.78% |
| btc-updown-15m-1791297000 | 717 | 879.74 | 5.96 | 706/717 = 98.47% | 216/717 = 30.13% | 103/717 = 14.37% | 707/717 = 98.61% | 33/717 = 4.60% |
| btc-updown-15m-1791297900 | 683 | 879.43 | 5.81 | 682/683 = 99.85% | 285/683 = 41.73% | 66/683 = 9.66% | 683/683 = 100.00% | 15/683 = 2.20% |
| btc-updown-15m-1791298800 | 750 | 880.80 | 3.21 | 747/750 = 99.60% | 487/750 = 64.93% | 100/750 = 13.33% | 750/750 = 100.00% | 66/750 = 8.80% |
| btc-updown-15m-1791299700 | 189 | 229.77 | 3.86 | 188/189 = 99.47% | 168/189 = 88.89% | 16/189 = 8.47% | 189/189 = 100.00% | 16/189 = 8.47% |

## 7. stale_for vs market_fresh

STALE_FOR_SAME_AUTHORITY=NO。STATUS=`now_ts-last_valid_quote_ts`（run_bot.py:4658）；preferred current-token valid callback 或 handoff prewarm promotion 更新 local receipt。Native ws_snapshot/ws_price_change 接受條件是 adapter ts_init delivery age，而不是 source timestamp age。Research 任一 side midpoint需 source AND receive age 均在[0,2]；因此 fresh execution quote 與 source clock稍領先 local 可並存。STATUS 是瞬時、active token；console freshness 是 cycle累計、UP OR DOWN。

不等同傳輸死掉：3035/3038列至少一側 receipt age在[0,2]（僅 age 判定，非可交易價格驗證）。市場 double-source-negative=1631/3038。Rejected transport heartbeat 可刷新 receipt map但不刷新有效 book/source，不能僅看 receipt 當作 pricing fresh。

## 8. Quote-generation coalescing

COALESCING_CLASSIFICATION=COALESCING_INTERACTS_WITH_EXISTING_FRESHNESS_DESIGN。

request_quote_generation (adapter_overrides.py:477) 逐次覆寫 pending instrument 的 message.timestamp、raw_ws_received_ts、request時 ts_init；flush call_soon讀最新 local book。ts_event 使用最後收到的 message timestamp，ts_init保留 request時間，不冒充 flush時間。這是 latest arrival，不保證 out-of-order message的最大 event time。沒有 first-event timestamp 漏留證據。

每個delta仍先apply；price AND size全部不變時既有 heartbeat5sec才emit，research兩秒window可能在穩定BBO期間失效。Price不變但size變仍立即emit；所以不是單純 last price-change age。Transport heartbeat保留舊ts_event，正常拒絕用作fresh pricing。git show 01c87aa證明5sec suppression与 QuoteTick source semantics既有，該commit加入deferred latestmetadata。未找到coalescing新timestamp bug；不能量化它單獨改變百分比的因果效應，缺乏同cohort controlled comparison。

## 9. Persisted-row reconstruction

p_ex composite age亦獨立由source/sigma/TWAP重建，mismatch=0。3038列，p_ex mismatches=0、market mismatches=0、joint mismatches=0；以獨立 one-off equations 重算，未呼叫 production build function。

限制：payload會清空 stale raw probability/bid/ask/BTC price，故一致性是對 retained values + persisted timestamps/ages/flags 的可觀測判定。可獨立重算所有 per-side source/receive age、BTC/TWAP age和joint conjunction；不能恢復被清空的原始值來證明上游原始價格有效性。p_ex composite不是單純snapshot_ts-p_ex_source_ts，需sigma/TWAP age最大值。

| TTE | p_ex | market | BTC | TWAP | joint |
|---|---|---|---|---|---|
| >10m | 1133/1140 = 99.39% | 548/1140 = 48.07% | 123/1140 = 10.79% | 1140/1140 = 100.00% | 53/1140 = 4.65% |
| 10–5m | 1080/1090 = 99.08% | 412/1090 = 37.80% | 155/1090 = 14.22% | 1080/1090 = 99.08% | 63/1090 = 5.78% |
| 5–2m | 565/565 = 100.00% | 264/565 = 46.73% | 56/565 = 9.91% | 565/565 = 100.00% | 21/565 = 3.72% |
| 2–1m | 127/127 = 100.00% | 76/127 = 59.84% | 12/127 = 9.45% | 127/127 = 100.00% | 9/127 = 7.09% |
| <1m | 108/116 = 93.10% | 56/116 = 48.28% | 14/116 = 12.07% | 116/116 = 100.00% | 5/116 = 4.31% |

| age field | missing | negative | >2sec | >10sec | P10 sec | median sec | P90 sec |
|---|---:|---:|---:|---:|---:|---:|---:|
| btc_age_sec | 0 | 2678 | 0 | 0 | -0.337105 | -0.213473 | 0.022426 |
| market_quote_up_source_age_sec | 0 | 2277 | 28 | 0 | -0.430877 | -0.190725 | 0.283385 |
| market_quote_down_source_age_sec | 1 | 2125 | 41 | 0 | -0.429949 | -0.156458 | 0.409264 |
| market_quote_up_receive_age_sec | 0 | 210 | 8 | 0 | 0.000441 | 0.001429 | 0.359463 |
| market_quote_down_receive_age_sec | 1 | 299 | 20 | 0 | 0.000414 | 0.002570 | 0.419907 |
| p_ex_age_sec | 0 | 0 | 1016 | 10 | 1.217911 | 1.798374 | 2.457155 |
| twap_age_sec | 0 | 0 | 997 | 10 | 1.205745 | 1.790553 | 2.446884 |

BTC2678 false全部是負age（無missing、無>10sec）。負age範圍最低-1.251826sec；不是數百秒stale。UP2277、DOWN2125列負source age；receive age負數UP210、DOWN299，顯示固定snapshot now與晚讀mutable state／較早的caller now並非原子as-of view；負receipt本身不能單獨區分執行期間state更新與caller時間較早。不能把所有source-negative都歸因單一clock offset，需receipt pairing才能分離。

代表列：snapshot1791296067.891818、BTC source1791296068.061，age=-0.169182，BTC false；snapshot1791299948.8082318、DOWN source1791299949.032，age=-0.223768，receipt1791299948.807481 (age0.000751)，DOWN false、UP true。後者source在本機receipt之後，直接顯示source/local時鐘不一致，不能用queue延遲解釋。全run UP source晚於receipt2728/3038、DOWN2722/3037；receipt-source median分別-0.312386與-0.304868sec。Trigger分層：periodic2909列/BTC負2559/UP負receipt178/DOWN257；entry_decision73列/BTC負67/UP負receipt32/DOWN42；dry_run_order_submit56列/BTC負52/負receipt0。

### 獨立 journal 傳輸證據（與prediction相同時間窗口）

1541筆throttled QUOTE_TRANSPORT_TELEMETRY，其中native236筆；來源/有效性計數：{"('ws_price_change', True)": 233, "('transport_heartbeat', False)": 1305, "('ws_snapshot', True)": 3}。Heartbeat占比不能當raw ingress來源分布，因每instrument每5sec限流会優先捕獲heartbeat。

| subset | metric | N | negative | median sec | P95 sec |
|---|---|---:|---:|---:|---:|
| all | quote_age_raw_sec | 1541 | 1185 | -0.265531 | 2.190910 |
| all | adapter_to_strategy_delay_sec | 1541 | 0 | 0.017791 | 0.078181 |
| all | data_engine_delivery_delay_sec | 1541 | 0 | 0.010652 | 0.024502 |
| native | quote_age_raw_sec | 236 | 212 | -0.288922 | 0.792164 |
| native | adapter_to_strategy_delay_sec | 236 | 0 | 0.030138 | 0.253871 |
| native | data_engine_delivery_delay_sec | 236 | 0 | 0.001010 | 0.014573 |

Native execution-valid quote即使raw source age負值仍正常接受：execution使用adapter ts_init，research使用source clock。這是獨立durable證據，非console推測。Journal採樣不能證明每個snapshot的精確raw ingress as-of；不可把稀疏telemetry backfill到所有列。

## 10. Value-age vs transport-age

VALUE_AGE_EQUALS_TRANSPORT_AGE=NO。Market衡量最近被接受 QuoteTick source observation與latest callback receipt，不是raw ws ingress，也不是純price-change時間。五秒unchanged-BBO heartbeat使觀測稀疏；不能將兩秒觀測window失敗當作斷線。主要本cohort是負age，而非值長時間未變；source >2sec僅UP28、DOWN41列。

## 11. Quality-gate consequence

QUALITY_GATE_AFFECTED=YES。ResearchStore.get_market_coverage：無settlement/span<60→UNUSABLE；multi-run或maxgap>15→INTERRUPTED；span<600或joint<25%→PARTIAL；span<840或joint<70%→GOOD；其餘FULL。近期四個完整市場span≈879–881sec、maxgap3.21–5.96sec，joint2.20–8.80%，即使settled皆只能PARTIAL。

現有postfix formal SYNCHRONIZED_USABLE report gate要求FULL/GOOD、joint≥25%、五canonical anchors joint valid；PARTIAL不能納入formal comparison。Source內沒有同名五類enum，應分清report-derived label與canonical coverage分類。Health component fresh<100亦顯示DEGRADED，只是observability，不是BUY gate。

以目前嚴格source-as-of契約排除是忠實執行，但當source clock偏移或capture競態占主要失敗時，不能解讀為collection transport品質差，更不能將不同clock-skew期間作regime effect。仍不得直接放寬gate把這些列當clean no-lookahead。

## 12. Root-cause classification

PRIMARY=TIMESTAMP_DOMAIN_BUG（clock alignment/零負age容忍；不是單位轉換錯誤）。高confidence於負age直接決定false與BTC壓低joint；clock偏移大小/成因的細分confidence中等。Secondary：mutable state不是原子as-of、5secunchangedquote與2secwindow設計、STATUS execution authority與research observation authority分開。

未發現console denominator bug、不同counter reset、prewarm混入或writer backlog能解釋這個交集。真正transport staleness可在少數列共存，不足以解釋主要低比例。

## 13. Minimal future design recommendation — no implementation

保留 source observation、local receipt、capture time各自canonical；不動p_ex maths、sampling、trading/watchdog、coalescing、threshold。未來最小audit-driven修補先限定research capture：固定一致的as-of observation bundle，BTC使用既有 `_btc_returns` 的 <=now選取精神，不讀未過濾history[-1]；保留BTC source+receipt，QuoteTick adapter/request+receipt及原始bid/ask，並顯式紀錄 clock skew / FUTURE_SOURCE / AFTER_CAPTURE 狀態。

不可把負age直接clamp0、不可把本機receipt替代source就宣稱no-lookahead。若要更改clock policy，需以receipt/event對照驗證並version語意；transport_fresh、observation_fresh、synchronized_pair_fresh分開。新增診斷而保留現有判定是最小可review的第一步，尚非批准production fix。

未來最小檔案：bot/prediction_research_snapshot.py capture/build/_emit_metrics/recent_health；bot/research/provenance.py schema version；tests/test_prediction_research_snapshot.py。若擴充adapter metadata才涉及bot/market_runtime.py既有quote telemetry。Downstream語意批准後才調整ResearchStore/analysis；此輪全部未改。

## 14. Weekday/weekend historical compatibility

既有payload version一致不代表clock狀態一致。全DB歷史概覽（不是matched regime cohort）：{"WEEKDAY": {"rows": 95370, "p_ex_fresh": 93414, "market_mid_fresh": 70408, "btc_fresh": 61124, "joint_fresh": 48121, "btc_negative": 34246, "btc_ts_missing": 0}, "WEEKEND": {"rows": 89644, "p_ex_fresh": 88650, "market_mid_fresh": 67907, "btc_fresh": 62411, "joint_fresh": 50330, "btc_negative": 27233, "btc_ts_missing": 0}}。Weekend也有27233負BTC age，因此現有gate可能選擇性排除不同時鐘狀態的市場。

HISTORICAL_WEEKEND_RECOMPUTABLE=PARTIAL。已有source/per-side receipt/ages可重算原判定、負age診斷与joint；不能完整回填被清空的bid/ask/p_ex/BTC價格，不能從prediction payload重建BTC receipt/as-of prior state、adapter ts_init或historical clockoffset。另有journal/Parquet可證實的單列才可補，不能fabricate。對新clock policy的所有週末列完整recompute目前不支持，不能說無需recollection。

原始歷史列仍可用舊語意解讀；任何新語意應標schema/policy版本，並對週末/平日採相同可還原子集。無必要physical DB migration；analysis-only derived columns/backfill應另存離線報告，active DB禁止修改。

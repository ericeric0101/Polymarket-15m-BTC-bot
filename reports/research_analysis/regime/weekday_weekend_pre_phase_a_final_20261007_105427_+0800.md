# FINAL PRE-RESTART WEEKDAY VS WEEKEND — PRE-PHASE-A

本報告為唯讀分析；沒有策略變更建議。主要 sampling unit 是 MARKET；candidate/event rows 非獨立樣本。所有 confidence interval/Fisher 都未做 multiple-comparison adjustment。

## Data provenance / quality gate

固定截止：**2026-10-07T10:49:19+08:00**。最新 collection 窗口從 2026-10-07T07:35:03+08:00 至截止（約 3 小時 14 分鐘）；只納入截止前已完成、已 canonical settled 且通過 gate 的市場。非滑動最後三小時，而是原監控啟動後這一批。
新 online-backup 未完成，已取消且未使用；只移除這次分析產生的未完成副本。來源為 logs/trade_journal.db、data/research/twap_forward_shadow.db、data/research/hyperliquid_lead_lag.db 的 mode=ro 查詢；research/legacy decisions 分批讀取、限定截止。歷史 eligibility 沿用已稽核 historical_freshness_recomputation rows；重現原 corrected cohort 所有 checkpoint flip counts 通過。新資料用同一 classify_row 的 exact BTC endpoint/receipt 規則；不使用未來 tick，不恢復已清空 BBO，不把 remote-source age 當 local transport age。Parquet 僅讀 completed part files，讀取前後 hash/mtime/size 驗證；read failures=0。
Gate：canonical_settlement_side UP/DOWN 且 canonical reference、single run、single non-null cycle、span ≥600s、max gap ≤15s、joint_fresh ≥25%、五個 checkpoints nearest within ±12s、有有效 settlement-state side、exact authoritative opening strike 與 fresh TWAP。Monday 僅逐市場重審，沒有 wholesale rehabilitation。
目前 source HEAD 是 3b50637；原 Python 程序仍執行 8309152 loaded modules。Cycle 2 manifest 採當時磁碟 HEAD，不能當成 code reload 證明：舊 STATUS/FF handoff 仍在，新增 Phase-A execution-disabled manifest 欄位缺席。因此全部最新 execution evidence 仍 PRE-Phase-A。未混入任何已載入 Phase-A 的 runtime。

| Cohort | N | 定義 |
| --- | --- | --- |
| WEEKDAY_PRIMARY | 75 | Oct6/7 clean |
| WEEKEND_PRIMARY | 54 | Oct3/4 corrected clean |
| WEEKDAY_WITH_MONDAY_SENSITIVITY | 81 | Oct5/6/7 individually gated |
| LATEST_3H_ONLY | 11 | 啟動後 clean completed markets |
| BEFORE_LATEST_3H | 64 | 主平日排除最新批次 |
| PRE_PHASE_A_EXECUTION_PATH | 75 | 與主平日相同；舊 FF authority 尚未退休 |

PRE_PHASE_A_EXECUTION_PATH 表示運行政策/權限狀態，不代表每個市場都實際被 FF 接管。完整 slugs/run/config 與 per-market gate evidence 見同名 JSON sidecar 和文末清單。

## 1. Flip / reversal

| Horizon | Weekday | Weekend | Diff pp | Newcombe 95% pp | Fisher two-sided | Latest only |
| --- | --- | --- | --- | --- | --- | --- |
| T-300 | 18/75 (24.00%); Wilson [15.75, 34.78]% | 2/54 (3.70%); Wilson [1.02, 12.54]% | 20.30 | [8.21, 31.4] | 0.00129 | 3/11 (27.27%); Wilson [9.75, 56.56]% |
| T-180 | 13/75 (17.33%); Wilson [10.42, 27.43]% | 1/54 (1.85%); Wilson [0.33, 9.77]% | 15.48 | [4.97, 25.69] | 0.00764 | 2/11 (18.18%); Wilson [5.14, 47.70]% |
| T-120 | 9/75 (12.00%); Wilson [6.44, 21.26]% | 0/54 (0.00%); Wilson [0.00, 6.64]% | 12.00 | [3.34, 21.26] | 0.01020 | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| T-60 | 5/75 (6.67%); Wilson [2.88, 14.68]% | 0/54 (0.00%); Wilson [0.00, 6.64]% | 6.67 | [-0.98, 14.68] | 0.07420 | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| T-30 | 4/75 (5.33%); Wilson [2.09, 12.93]% | 0/54 (0.00%); Wilson [0.00, 6.64]% | 5.33 | [-2.06, 12.93] | 0.13913 | 1/11 (9.09%); Wilson [1.62, 37.74]% |

這是觀測窗口與選擇後 cohort 的 regime association；不是跨多週驗證、因果 weekday 效應或 trading optimization 證據。

## 2. Official TWAP / strike crossing

只用官方 Chainlink 60s TWAP 與 authoritative strike。Crossings 為 persisted fresh/exact observations 的 side transitions；snapshot gaps 內未觀察的 crossing 不可重建，所以是真實 continuous-path 次數的下界。沒有用 shadow settlement 當終局。

| Cohort | Horizon | Market crossing incidence | Observed total crossings | Multiple-crossing markets | First delay from checkpoint sec |
| --- | --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | T-300 | 22/75 (29.33%); Wilson [20.24, 40.44]% | 26 | 4 | N=22; median=180.284; IQR=[52.993, 204.729]; mean=152.555 |
| WEEKDAY_PRIMARY | T-180 | 15/75 (20.00%); Wilson [12.51, 30.41]% | 17 | 2 | N=15; median=68.125; IQR=[59.641, 124.482]; mean=87.605 |
| WEEKDAY_PRIMARY | T-120 | 10/75 (13.33%); Wilson [7.41, 22.83]% | 11 | 1 | N=10; median=39.577; IQR=[11.653, 89.394]; mean=50.951 |
| WEEKDAY_PRIMARY | T-60 | 5/75 (6.67%); Wilson [2.88, 14.68]% | 5 | 0 | N=5; median=35.979; IQR=[33.172, 42.298]; mean=37.311 |
| WEEKDAY_PRIMARY | T-30 | 4/75 (5.33%); Wilson [2.09, 12.93]% | 4 | 0 | N=4; median=10.655; IQR=[5.835, 18.157]; mean=13.338 |
| WEEKEND_PRIMARY | T-300 | 4/54 (7.41%); Wilson [2.92, 17.55]% | 6 | 2 | N=4; median=108.022; IQR=[91.532, 130.681]; mean=114.192 |
| WEEKEND_PRIMARY | T-180 | 2/54 (3.70%); Wilson [1.02, 12.54]% | 3 | 1 | N=2; median=61.594; IQR=[56.109, 67.079]; mean=61.594 |
| WEEKEND_PRIMARY | T-120 | 1/54 (1.85%); Wilson [0.33, 9.77]% | 2 | 1 | N=1; median=12.615; IQR=[12.615, 12.615]; mean=12.615 |
| WEEKEND_PRIMARY | T-60 | 0/54 (0.00%); Wilson [0.00, 6.64]% | 0 | 0 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| WEEKEND_PRIMARY | T-30 | 0/54 (0.00%); Wilson [0.00, 6.64]% | 0 | 0 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| LATEST_3H_ONLY | T-300 | 3/11 (27.27%); Wilson [9.75, 56.56]% | 3 | 0 | N=3; median=121.494; IQR=[91.456, 197.251]; mean=151.973 |
| LATEST_3H_ONLY | T-180 | 2/11 (18.18%); Wilson [5.14, 47.70]% | 2 | 0 | N=2; median=77.407; IQR=[39.256, 115.557]; mean=77.407 |
| LATEST_3H_ONLY | T-120 | 1/11 (9.09%); Wilson [1.62, 37.74]% | 1 | 0 | N=1; median=93.390; IQR=[93.390, 93.390]; mean=93.390 |
| LATEST_3H_ONLY | T-60 | 1/11 (9.09%); Wilson [1.62, 37.74]% | 1 | 0 | N=1; median=33.172; IQR=[33.172, 33.172]; mean=33.172 |
| LATEST_3H_ONLY | T-30 | 1/11 (9.09%); Wilson [1.62, 37.74]% | 1 | 0 | N=1; median=3.589; IQR=[3.589, 3.589]; mean=3.589 |

## 3. Maker intrinsic signal / qualification

**完整 intrinsic qualified opportunity set 無法無條件重建。** 舊 `_evaluate_quote_targets` 的 FF ownership veto 在後續 maker economic gate / ENTRY_DECISION_TRACE 之前；無完整 inputs 的 veto observations 不能 counterfactually replay 所有 gates。以下是可觀察、存活的 maker candidate episodes 與 ALLOW evidence 下界，不宣稱缺資料市場沒有機會，也不以 FF veto 當 intrinsic rejection。Entry fair 是 maker digital fair；p_up_ex_market 是 separate ex-market forecast，二者不可互換。shadow_only 的 gate/label 不當 live ALLOW。

| Cohort | Trace-covered markets | Candidate episodes (per market) | Observed ALLOW episodes | Observed ALLOW markets/N | First ALLOW TTE | ALLOW edge |
| --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | 75/75 | 965 (12.87/market) | 862 | 71/75 | N=71; median=585.845; IQR=[548.425, 597.071]; mean=565.485 | N=1775; median=0.005; IQR=[0.005, 0.005]; mean=0.005 |
| WEEKEND_PRIMARY | 54/54 | 206 (3.81/market) | 145 | 33/54 | N=33; median=530.353; IQR=[408.917, 584.105]; mean=495.706 | N=241; median=0.005; IQR=[0.005, 0.005]; mean=0.005 |
| LATEST_3H_ONLY | 11/11 | 128 (11.64/market) | 112 | 10/11 | N=10; median=593.008; IQR=[583.064, 597.135]; mean=586.299 | N=201; median=0.005; IQR=[0.005, 0.005]; mean=0.005 |

每個市場 candidate IDs、trace/update counts、first ALLOW TTE、median/max edge 在 JSON maker_per_market。稀疏 trace 不能推估每秒機會率。Maker intent/sim fill 可證明曾走到執行路徑，不能反推沒留下 trace 的所有 qualified candidates。

## 4. Actual dry-run entry path

此節獨立於 intrinsic signal。SHADOW_SIM_ENTRY_FILLED 是 maker dry-run simulation fill，不是 venue real fill；ORDER_FAST_FOLLOW_INTENT 是 reservation/intent，不等於 accepted FOK。一次 re-quote 不當新獨立 position。

| Cohort | Intent markets | Filled markets/N | Fill rows | Maker ownership blocks | FF ownership markets | FOK intents | FOK submits | FF confirmed | FF errors | TypeError |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | 71 | 67/75 | 67 | 280 | 2 | 2 | 2 | 134 | 10 | 10 |
| WEEKEND_PRIMARY | 33 | 16/54 | 16 | 0 | 0 | 0 | 0 | 33 | 0 | 0 |
| LATEST_3H_ONLY | 10 | 10/11 | 10 | 0 | 0 | 0 | 0 | 53 | 10 | 10 |

- WEEKDAY_PRIMARY exact order event counts：`{"DEPTH_RISK_SHADOW_CANDIDATE": 3860, "DEPTH_RISK_SHADOW_MARKOUT": 15440, "ENTRY_EDGE_OBSERVATION": 16418, "ORDER_DRY_RUN_CANCELLED": 132, "ORDER_DRY_RUN_SUBMITTED": 201, "ORDER_FAST_FOLLOW_INTENT": 2, "ORDER_OBSERVE_BUY_BLOCKED": 2954, "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE": 3109, "ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE": 2059, "ORDER_SKIP_FAST_FOLLOW_OWNERSHIP": 280, "ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW": 2582, "ORDER_SKIP_LOCKED_SIDE_INVALIDATED": 2323, "ORDER_SKIP_TWAP_REFERENCE_DEGRADED": 29, "SHADOW_SIM_ENTRY_CANCELLED": 131, "SHADOW_SIM_ENTRY_CANDIDATE": 71, "SHADOW_SIM_ENTRY_EXPIRED": 1, "SHADOW_SIM_ENTRY_FILLED": 67, "SHADOW_SIM_ENTRY_REQUOTED": 128, "SHADOW_SIM_MARKOUT": 134, "SHADOW_SIM_SETTLED": 67}`
- WEEKEND_PRIMARY exact order event counts：`{"DEPTH_RISK_SHADOW_CANDIDATE": 630, "DEPTH_RISK_SHADOW_MARKOUT": 2520, "ENTRY_EDGE_OBSERVATION": 11186, "ORDER_DRY_RUN_CANCELLED": 92, "ORDER_DRY_RUN_SUBMITTED": 108, "ORDER_OBSERVE_BUY_BLOCKED": 2451, "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE": 1078, "ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE": 3011, "ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW": 1301, "ORDER_SKIP_LOCKED_SIDE_INVALIDATED": 800, "ORDER_SKIP_TWAP_REFERENCE_DEGRADED": 24, "SHADOW_SIM_ENTRY_CANCELLED": 90, "SHADOW_SIM_ENTRY_CANDIDATE": 33, "SHADOW_SIM_ENTRY_EXPIRED": 2, "SHADOW_SIM_ENTRY_FILLED": 16, "SHADOW_SIM_ENTRY_REQUOTED": 75, "SHADOW_SIM_MARKOUT": 32, "SHADOW_SIM_SETTLED": 16}`
- LATEST_3H_ONLY exact order event counts：`{"DEPTH_RISK_SHADOW_CANDIDATE": 475, "DEPTH_RISK_SHADOW_MARKOUT": 1900, "ENTRY_EDGE_OBSERVATION": 2409, "ORDER_DRY_RUN_CANCELLED": 38, "ORDER_DRY_RUN_SUBMITTED": 48, "ORDER_OBSERVE_BUY_BLOCKED": 399, "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE": 479, "ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE": 306, "ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW": 389, "ORDER_SKIP_LOCKED_SIDE_INVALIDATED": 287, "ORDER_SKIP_TWAP_REFERENCE_DEGRADED": 7, "SHADOW_SIM_ENTRY_CANCELLED": 38, "SHADOW_SIM_ENTRY_CANDIDATE": 10, "SHADOW_SIM_ENTRY_FILLED": 10, "SHADOW_SIM_ENTRY_REQUOTED": 38, "SHADOW_SIM_MARKOUT": 20, "SHADOW_SIM_SETTLED": 10}`

Order counters 提供 no-trade/intent/cancel/requote/fill 精確 event 名稱；未將 confirmed signals 或 quote handoff 當成 FOK attempts。FOK attempts 定義為 ORDER_FAST_FOLLOW_INTENT，以 client_order_id 檢查；submit 計 ORDER_FAST_FOLLOW_SUBMIT 或 FF client 的 ORDER_DRY_RUN_SUBMITTED。

| Cohort | First observed fill TTE | Entry price | Entry fair edge | Fill outcome side available |
| --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | N=67; median=547.681; IQR=[472.567, 574.330]; mean=522.996 | N=67; median=0.750; IQR=[0.675, 0.830]; mean=0.750 | N=67; median=0.005; IQR=[0.005, 0.005]; mean=0.006 | 67 |
| WEEKEND_PRIMARY | N=16; median=410.329; IQR=[364.476, 499.632]; mean=432.413 | N=16; median=0.830; IQR=[0.805, 0.860]; mean=0.828 | N=16; median=0.005; IQR=[0.005, 0.005]; mean=0.006 | 16 |
| LATEST_3H_ONLY | N=10; median=460.661; IQR=[419.780, 575.511]; mean=482.656 | N=10; median=0.770; IQR=[0.742, 0.837]; mean=0.783 | N=10; median=0.005; IQR=[0.005, 0.005]; mean=0.006 | 10 |

## 5. Entry parameter distributions

以下是 persisted normal-maker trace row 的 descriptive distributions，非獨立 N；市場粒度 incidence 用上節。N=0 = unavailable，不能補值。p_ex/sigma/bps 使用 trace 內 persisted snapshot evidence；entry-price/edge 是 candidate plan。

| Parameter | Weekday N/median/IQR/mean | Weekend N/median/IQR/mean |
| --- | --- | --- |
| abs_distance_bps | N=11644; median=5.149; IQR=[2.713, 8.887]; mean=7.568 | N=7540; median=3.359; IQR=[1.995, 5.825]; mean=5.264 |
| abs_distance_usd | N=11644; median=44.109; IQR=[23.230, 76.181]; mean=64.703 | N=7540; median=28.492; IQR=[16.912, 49.382]; mean=44.656 |
| best_ask | N=11666; median=0.790; IQR=[0.660, 0.930]; mean=0.768 | N=7555; median=0.880; IQR=[0.760, 0.970]; mean=0.843 |
| best_bid | N=11666; median=0.780; IQR=[0.650, 0.920]; mean=0.758 | N=7555; median=0.870; IQR=[0.750, 0.960]; mean=0.834 |
| btc_return_10s_bps | N=6528; median=0.000; IQR=[-0.303, 0.273]; mean=-0.038 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| btc_return_30s_bps | N=6554; median=0.000; IQR=[-1.166, 1.165]; mean=-0.062 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| btc_return_5s_bps | N=6455; median=0.000; IQR=[-0.001, 0.001]; mean=-0.022 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| entry_price | N=11666; median=0.780; IQR=[0.650, 0.920]; mean=0.758 | N=7555; median=0.870; IQR=[0.750, 0.960]; mean=0.833 |
| execution_penalty_per_share | N=11664; median=0.025; IQR=[0.025, 0.025]; mean=0.024 | N=7553; median=0.025; IQR=[0.020, 0.025]; mean=0.023 |
| fair_probability | N=11666; median=0.785; IQR=[0.655, 0.925]; mean=0.763 | N=7555; median=0.875; IQR=[0.755, 0.965]; mean=0.838 |
| fee_adjusted_edge | N=11666; median=0.005; IQR=[0.005, 0.005]; mean=0.005 | N=7555; median=0.005; IQR=[0.005, 0.005]; mean=0.005 |
| fee_per_share | N=11666; median=0.000; IQR=[0.000, 0.000]; mean=0.000 | N=7555; median=0.000; IQR=[0.000, 0.000]; mean=0.000 |
| gross_probability_edge_ps | N=11666; median=0.005; IQR=[0.005, 0.005]; mean=0.005 | N=7555; median=0.005; IQR=[0.005, 0.005]; mean=0.005 |
| maker_robust_net_usdc | N=11666; median=-0.202; IQR=[-0.202, -0.165]; mean=-0.161 | N=7555; median=-0.202; IQR=[-0.202, -0.171]; mean=-0.196 |
| nearby_ask_depth | N=11666; median=219.540; IQR=[71.882, 458.692]; mean=428.956 | N=7555; median=179.000; IQR=[51.305, 404.395]; mean=428.165 |
| nearby_bid_depth | N=11666; median=330.195; IQR=[140.000, 613.950]; mean=1157.808 | N=7555; median=381.830; IQR=[176.110, 783.070]; mean=1298.085 |
| p_ex_minus_market | N=4611; median=-0.005; IQR=[-0.032, 0.023]; mean=-0.007 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| p_up_ex_market | N=7637; median=0.453; IQR=[0.176, 0.758]; mean=0.477 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| required_move_bps | N=7628; median=0.689; IQR=[-4.808, 5.984]; mean=1.845 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| required_move_sigma | N=7619; median=0.755; IQR=[0.377, 1.486]; mean=1.811 | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| safety_sigma | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| signal_score | N=11666; median=-0.020; IQR=[-0.234, 0.196]; mean=-0.025 | N=7555; median=0.027; IQR=[-0.261, 0.275]; mean=-0.007 |
| spread | N=11666; median=0.010; IQR=[0.010, 0.010]; mean=0.010 | N=7555; median=0.010; IQR=[0.010, 0.010]; mean=0.009 |
| time_left_sec | N=11666; median=388.948; IQR=[221.211, 563.703]; mean=395.399 | N=7555; median=347.474; IQR=[191.894, 530.934]; mean=364.883 |
| top_ask_size | N=11032; median=239.995; IQR=[93.000, 483.220]; mean=453.153 | N=6916; median=204.290; IQR=[79.430, 440.000]; mean=460.245 |
| top_bid_size | N=11664; median=325.895; IQR=[137.782, 607.548]; mean=1124.926 | N=7552; median=371.360; IQR=[169.000, 763.020]; mean=1266.646 |

Directional threshold、side locking、economic result 用原 trace state/reason；非新增 signal。BTC 5/10/30 只是既有 evidence，不復原 freshness 被清空的 returns。Smart-money、entry-confirmation、L2 timestamp/age 若沒同一 candidate 完整可 join evidence，標 NOT_RECONSTRUCTABLE；不以跨時間 nearest future observation 補值。

## 6. p_ex vs market calibration / sigma / bps

| Cohort | Horizon | N | Market implied flip mean | p_ex flip mean | Market Brier | Analytic Brier | Analytic-market |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | T-300 | 75 | 0.202 | 0.183 | 0.161 | 0.162 | 0.001 |
| WEEKDAY_PRIMARY | T-180 | 75 | 0.148 | 0.142 | 0.109 | 0.110 | 0.002 |
| WEEKDAY_PRIMARY | T-120 | 75 | 0.127 | 0.130 | 0.063 | 0.067 | 0.004 |
| WEEKDAY_PRIMARY | T-60 | 75 | 0.083 | 0.093 | 0.025 | 0.026 | 0.002 |
| WEEKDAY_PRIMARY | T-30 | 75 | 0.053 | 0.052 | 0.002 | 0.004 | 0.001 |
| WEEKEND_PRIMARY | T-300 | 54 | 0.138 | 0.226 | 0.045 | 0.082 | 0.037 |
| WEEKEND_PRIMARY | T-180 | 54 | 0.079 | 0.170 | 0.023 | 0.047 | 0.023 |
| WEEKEND_PRIMARY | T-120 | 54 | 0.075 | 0.142 | 0.027 | 0.040 | 0.013 |
| WEEKEND_PRIMARY | T-60 | 54 | 0.035 | 0.088 | 0.015 | 0.023 | 0.008 |
| WEEKEND_PRIMARY | T-30 | 54 | 0.019 | 0.014 | 0.008 | 0.004 | -0.004 |
| LATEST_3H_ONLY | T-300 | 11 | 0.210 | 0.168 | 0.104 | 0.129 | 0.025 |
| LATEST_3H_ONLY | T-180 | 11 | 0.172 | 0.167 | 0.054 | 0.061 | 0.007 |
| LATEST_3H_ONLY | T-120 | 11 | 0.085 | 0.076 | 0.033 | 0.058 | 0.025 |
| LATEST_3H_ONLY | T-60 | 11 | 0.099 | 0.111 | 0.008 | 0.028 | 0.020 |
| LATEST_3H_ONLY | T-30 | 11 | 0.090 | 0.087 | 0.000 | 0.000 | 0.000 |

Flip probability 相對 checkpoint settlement leader，非 token UP probability 直接比較。Brier 正差是市場較佳；小 N 的符號不證明穩定优势，p_ex 非 lead signal。

### Fixed sigma bins

| Horizon | Bin | Weekday k/N + Wilson | Weekend k/N + Wilson |
| --- | --- | --- | --- |
| T-300 | <0.5 | 10/17 (58.82%); Wilson [36.01, 78.39]% | 1/20 (5.00%); Wilson [0.89, 23.61]% |
| T-300 | 0.5-1 | 1/21 (4.76%); Wilson [0.85, 22.67]% | 1/17 (5.88%); Wilson [1.05, 26.98]% |
| T-300 | 1-2 | 6/23 (26.09%); Wilson [12.55, 46.47]% | 0/12 (0.00%); Wilson [0.00, 24.25]% |
| T-300 | 2-3 | 1/10 (10.00%); Wilson [1.79, 40.42]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-300 | 3-5 | 0/3 (0.00%); Wilson [0.00, 56.15]% | 0/1 (0.00%); Wilson [0.00, 79.35]% |
| T-300 | >5 | 0/1 (0.00%); Wilson [0.00, 79.35]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-300 | missing | missing N=0 | missing N=0 |
| T-180 | <0.5 | 9/18 (50.00%); Wilson [29.03, 70.97]% | 1/10 (10.00%); Wilson [1.79, 40.42]% |
| T-180 | 0.5-1 | 2/10 (20.00%); Wilson [5.67, 50.98]% | 0/23 (0.00%); Wilson [0.00, 14.31]% |
| T-180 | 1-2 | 1/22 (4.55%); Wilson [0.81, 21.80]% | 0/13 (0.00%); Wilson [0.00, 22.81]% |
| T-180 | 2-3 | 0/15 (0.00%); Wilson [-0.00, 20.39]% | 0/5 (0.00%); Wilson [0.00, 43.45]% |
| T-180 | 3-5 | 1/6 (16.67%); Wilson [3.01, 56.35]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-180 | >5 | 0/4 (0.00%); Wilson [0.00, 48.99]% | 0/3 (0.00%); Wilson [0.00, 56.15]% |
| T-180 | missing | missing N=0 | missing N=0 |
| T-120 | <0.5 | 6/15 (40.00%); Wilson [19.82, 64.25]% | 0/9 (0.00%); Wilson [0.00, 29.91]% |
| T-120 | 0.5-1 | 1/6 (16.67%); Wilson [3.01, 56.35]% | 0/20 (0.00%); Wilson [0.00, 16.11]% |
| T-120 | 1-2 | 1/26 (3.85%); Wilson [0.68, 18.89]% | 0/12 (0.00%); Wilson [0.00, 24.25]% |
| T-120 | 2-3 | 1/9 (11.11%); Wilson [1.99, 43.50]% | 0/9 (0.00%); Wilson [0.00, 29.91]% |
| T-120 | 3-5 | 0/14 (0.00%); Wilson [0.00, 21.53]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-120 | >5 | 0/5 (0.00%); Wilson [0.00, 43.45]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-120 | missing | missing N=0 | missing N=0 |
| T-60 | <0.5 | 0/5 (0.00%); Wilson [0.00, 43.45]% | 0/6 (0.00%); Wilson [0.00, 39.03]% |
| T-60 | 0.5-1 | 0/2 (0.00%); Wilson [0.00, 65.76]% | 0/6 (0.00%); Wilson [0.00, 39.03]% |
| T-60 | 1-2 | 1/10 (10.00%); Wilson [1.79, 40.42]% | 0/8 (0.00%); Wilson [0.00, 32.44]% |
| T-60 | 2-3 | 0/8 (0.00%); Wilson [0.00, 32.44]% | 0/5 (0.00%); Wilson [0.00, 43.45]% |
| T-60 | 3-5 | 0/10 (0.00%); Wilson [0.00, 27.75]% | 0/6 (0.00%); Wilson [0.00, 39.03]% |
| T-60 | >5 | 0/7 (0.00%); Wilson [0.00, 35.43]% | 0/1 (0.00%); Wilson [0.00, 79.35]% |
| T-60 | missing | missing N=33 | missing N=22 |
| T-30 | <0.5 | 1/1 (100.00%); Wilson [20.65, 100.00]% | 0/1 (0.00%); Wilson [0.00, 79.35]% |
| T-30 | 0.5-1 | 2/6 (33.33%); Wilson [9.68, 70.00]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-30 | 1-2 | 1/5 (20.00%); Wilson [3.62, 62.45]% | 0/8 (0.00%); Wilson [0.00, 32.44]% |
| T-30 | 2-3 | 0/7 (0.00%); Wilson [0.00, 35.43]% | 0/5 (0.00%); Wilson [0.00, 43.45]% |
| T-30 | 3-5 | 0/14 (0.00%); Wilson [0.00, 21.53]% | 0/17 (0.00%); Wilson [0.00, 18.43]% |
| T-30 | >5 | 0/42 (0.00%); Wilson [0.00, 8.38]% | 0/21 (0.00%); Wilson [0.00, 15.46]% |
| T-30 | missing | missing N=0 | missing N=0 |

### Fixed bps bins

| Horizon | Bin | Weekday k/N + Wilson | Weekend k/N + Wilson |
| --- | --- | --- | --- |
| T-300 | 0-2 | 7/13 (53.85%); Wilson [29.14, 76.79]% | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| T-300 | 2-5 | 3/17 (17.65%); Wilson [6.19, 41.03]% | 1/23 (4.35%); Wilson [0.77, 20.99]% |
| T-300 | 5-10 | 5/25 (20.00%); Wilson [8.86, 39.13]% | 0/13 (0.00%); Wilson [0.00, 22.81]% |
| T-300 | 10-20 | 3/16 (18.75%); Wilson [6.59, 43.01]% | 0/5 (0.00%); Wilson [0.00, 43.45]% |
| T-300 | >20 | 0/4 (0.00%); Wilson [0.00, 48.99]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-300 | zero | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-300 | missing | missing N=0 | missing N=0 |
| T-180 | 0-2 | 6/13 (46.15%); Wilson [23.21, 70.86]% | 1/9 (11.11%); Wilson [1.99, 43.50]% |
| T-180 | 2-5 | 5/18 (27.78%); Wilson [12.50, 50.87]% | 0/24 (0.00%); Wilson [0.00, 13.80]% |
| T-180 | 5-10 | 1/20 (5.00%); Wilson [0.89, 23.61]% | 0/14 (0.00%); Wilson [0.00, 21.53]% |
| T-180 | 10-20 | 1/19 (5.26%); Wilson [0.94, 24.64]% | 0/4 (0.00%); Wilson [0.00, 48.99]% |
| T-180 | >20 | 0/5 (0.00%); Wilson [0.00, 43.45]% | 0/3 (0.00%); Wilson [0.00, 56.15]% |
| T-180 | zero | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-180 | missing | missing N=0 | missing N=0 |
| T-120 | 0-2 | 6/15 (40.00%); Wilson [19.82, 64.25]% | 0/10 (0.00%); Wilson [0.00, 27.75]% |
| T-120 | 2-5 | 1/18 (5.56%); Wilson [0.99, 25.76]% | 0/23 (0.00%); Wilson [0.00, 14.31]% |
| T-120 | 5-10 | 1/20 (5.00%); Wilson [0.89, 23.61]% | 0/15 (0.00%); Wilson [-0.00, 20.39]% |
| T-120 | 10-20 | 1/17 (5.88%); Wilson [1.05, 26.98]% | 0/4 (0.00%); Wilson [0.00, 48.99]% |
| T-120 | >20 | 0/5 (0.00%); Wilson [0.00, 43.45]% | 0/2 (0.00%); Wilson [0.00, 65.76]% |
| T-120 | zero | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-120 | missing | missing N=0 | missing N=0 |
| T-60 | 0-2 | 0/6 (0.00%); Wilson [0.00, 39.03]% | 0/9 (0.00%); Wilson [0.00, 29.91]% |
| T-60 | 2-5 | 1/9 (11.11%); Wilson [1.99, 43.50]% | 0/10 (0.00%); Wilson [0.00, 27.75]% |
| T-60 | 5-10 | 0/13 (0.00%); Wilson [0.00, 22.81]% | 0/10 (0.00%); Wilson [0.00, 27.75]% |
| T-60 | 10-20 | 0/11 (0.00%); Wilson [0.00, 25.88]% | 0/3 (0.00%); Wilson [0.00, 56.15]% |
| T-60 | >20 | 0/3 (0.00%); Wilson [0.00, 56.15]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-60 | zero | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-60 | missing | missing N=33 | missing N=22 |
| T-30 | 0-2 | 3/7 (42.86%); Wilson [15.82, 74.95]% | 0/3 (0.00%); Wilson [0.00, 56.15]% |
| T-30 | 2-5 | 1/7 (14.29%); Wilson [2.57, 51.31]% | 0/10 (0.00%); Wilson [0.00, 27.75]% |
| T-30 | 5-10 | 0/19 (0.00%); Wilson [-0.00, 16.82]% | 0/22 (0.00%); Wilson [0.00, 14.87]% |
| T-30 | 10-20 | 0/22 (0.00%); Wilson [0.00, 14.87]% | 0/14 (0.00%); Wilson [0.00, 21.53]% |
| T-30 | >20 | 0/20 (0.00%); Wilson [0.00, 16.11]% | 0/5 (0.00%); Wilson [0.00, 43.45]% |
| T-30 | zero | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% | 0/0 (NOT_RECONSTRUCTABLE%); Wilson [NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]% |
| T-30 | missing | missing N=0 | missing N=0 |

固定 bins 未變；zero/missing 分開。部分 60s sigma 不可得，保留 actual N。不能因尾端零翻轉推導風險為零。

## 7. Stop / invalidation observations

實際 stop、side invalidation observation、shadow candidate 三者分開。Cycle 1/2 console 明確 STOP_LOSS_ENABLED=0，loss-triggered exits paused；設定中的 catastrophic/absolute thresholds 不證明真正 stop 有觸發。

| Cohort | Invalidation rows/markets | Reconstructable stops | Correct stops | Whipsaws | Actual stop PnL | Canonical hold PnL | Stop-hold delta | Median delta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | 58/35 | 0 | 0 | 0 | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| WEEKEND_PRIMARY | 17/12 | 0 | 0 | 0 | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| LATEST_3H_ONLY | 10/5 | 0 | 0 | 0 | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | NOT_RECONSTRUCTABLE | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |

Stop PnL 只用实际 fill/STOP_SHADOW_POST_STOP_SETTLEMENT 可 join 的 position，最终胜负重算自该市场 canonical TWAP summary；如原 shadow final 不同仍使用 canonical side。缺 qty/entry/fee/actual-stop evidence 不估計 stop benefit。N 小或零 → TOO_SMALL / NOT_RECONSTRUCTABLE，零 recorded 不代表真实 stop 风险为零。MAE、unrealized loss、trigger duration、post-stop reversal timing 未有完整同一 position evidence 時不補算。

## 8. Runtime thresholds / config comparability

完整 exact safe_config exit/risk/side 分 run 保存在 JSON configs。缺 manifest 的週末 run 不反推 current config，故不能宣稱所有 thresholds constant。下面只列有 persisted runtime evidence 的 distinct stop configs，未混合推估 stop efficacy。

### Exit config stratum 1 — 17 runs

Runs：`run_1791220474_2ed1742e`, `run_1791224107_573fabab`, `run_1791227746_2be3a494`, `run_1791231380_2435b627`, `run_1791235015_758df0b8`, `run_1791238651_7f90656a`, `run_1791242284_d2414b9e`, `run_1791245915_384288e1`, `run_1791249551_7300cf99`, `run_1791253399_d0ff0054`, `run_1791257485_084da14f`, `run_1791261985_8f4edaaa`, `run_1791266486_408fbe92`, `run_1791275488_682e8353`, `run_1791279466_720c2e47`, `run_1791304179_b0a8dcd8`, `run_1791329712_9b02abda`

| Runtime parameter | Value |
| --- | --- |
| absolute_max_loss_enabled | True |
| absolute_max_loss_min_hold_sec | 60 |
| absolute_max_loss_usdc | 2.00 |
| catastrophic_stop_loss_confirmations | 2 |
| catastrophic_stop_loss_enabled | True |
| catastrophic_stop_loss_min_score_abs | 0.50 |
| catastrophic_stop_loss_usdc | 0.40 |
| exit_conviction_band_min_price | 0.60 |
| exit_conviction_band_min_score_abs | 0.12 |
| exit_conviction_extra_confirmations | 1 |
| exit_conviction_stop_loss_multiplier | 1.75 |
| exit_hold_band_min_price | 0.68 |
| exit_hold_band_min_score_abs | 0.12 |
| exit_hold_band_release_min_roi | 0.15 |
| exit_hold_band_requires_locked | True |
| exit_stop_loss_hold_on_none_signal | True |
| exit_stop_loss_requires_thesis_weakening | True |
| exit_stop_loss_thesis_min_score_abs | 0.05 |
| hold_to_redeem_enabled | True |
| maker_early_profit_hold_enabled | True |
| maker_early_profit_hold_max_profit_ps | 0.08 |
| maker_early_profit_hold_min_hold_sec | 60 |
| maker_early_profit_hold_min_score_abs | 0.18 |
| maker_profit_run_enabled | True |
| maker_profit_run_min_hold_sec | 20 |
| maker_profit_run_min_profit_ps | 0.05 |
| maker_profit_run_min_score_abs | 0.12 |
| maker_profit_run_trailing_drawdown_ps | 0.06 |
| maker_profit_run_unlock_profit_ps | 0.18 |
| maker_profit_run_unlock_trailing_drawdown_ps | 0.02 |
| maker_urgent_exit_min_loss_usdc | 0.10 |
| market_stop_loss_max_per_market | 1 |
| stop_loss_enabled | False |
| stop_loss_reentry_cooldown_sec | 180 |
| taker_exit_disable_stop_loss_last_sec | 45 |
| taker_exit_max_hold_near_close_sec | 90 |
| taker_exit_max_hold_sec | 120 |
| taker_exit_min_hold_sec | 120 |
| taker_exit_only_after_invalidation | True |
| taker_exit_only_on_profit | False |
| taker_exit_stop_loss_confirmations | 2 |
| taker_exit_stop_loss_max_spread_pct | 0.03 |
| taker_exit_stop_loss_usdc | 0.5 |

### Exit config stratum 2 — 36 runs

Runs：`run_1790964229_ae4dba0a`, `run_1790990130_9fddffb2`, `run_1791000045_df1adc92`, `run_1791003676_3d58af66`, `run_1791009819_4f16006f`, `run_1791014641_12c10290`, `run_1791018273_c80d24ec`, `run_1791023404_d05b32b1`, `run_1791026106_c815ceee`, `run_1791029737_3db8ca59`, `run_1791033368_63fa64b7`, `run_1791036999_39eed4e1`, `run_1791041249_fc1dd8e0`, `run_1791044880_acbd0786`, `run_1791048511_365b424d`, `run_1791052141_fe30de47`, `run_1791053867_a36e4c67`, `run_1791057497_bb44b528`, `run_1791061128_6012b10b`, `run_1791064759_671ece47`, `run_1791070109_cc475056`, `run_1791076424_50193a93`, `run_1791081118_a7cf46b9`, `run_1791084345_41679360`, `run_1791088949_8a69164e`, `run_1791095281_4743bbb1`, `run_1791101455_05564309`, `run_1791107133_3f0342b8`, `run_1791112480_51b3a76a`, `run_1791116112_9db06308`, `run_1791119628_cdb7d7c2`, `run_1791131381_baae0ef5`, `run_1791135014_15577a71`, `run_1791146614_c03299a7`, `run_1791150245_90251ae4`, `run_1791197001_1b6f1dd9`

CONFIG_UNKNOWN：沒有 run manifest，無法證明與其他 strata 相同。

Session PnL guard 與其他 risk configuration 未變更。Config 異質性/legacy missing provenance 是 actual-entry/stop comparisons 的額外 confounder；canonical market flip 是 observational market outcome，仍不等於消除全部 confounder。

## 9. Entry × reversal interaction

| Cohort | Side-resolved fill observations | Post-entry adverse settlement state | Entered side loses final | Interpretation |
| --- | --- | --- | --- | --- |
| WEEKDAY_PRIMARY | 67 | 29 | 20 | descriptive; use unique markets for inference |
| WEEKEND_PRIMARY | 16 | 1 | 1 | descriptive; use unique markets for inference |
| LATEST_3H_ONLY | 10 | 4 | 3 | descriptive; use unique markets for inference |

逐 entry time/side、checkpoint sides 與 canonical final 在 JSON entry_reversal。這證明已記錄 maker entries 後是否出現 adverse state；不能由 population T300 flip rate 直接推論所有 maker 都會受損，也不能由此發明 stop threshold。

## 10. Latest collection incremental effect

| Metric | Before latest | After latest | Latest only |
| --- | --- | --- | --- |
| Clean markets | 64 | 75 | 11 |
| T-300 | 15/64 (23.44%); Wilson [14.75, 35.13]% | 18/75 (24.00%); Wilson [15.75, 34.78]% | 3/11 (27.27%); Wilson [9.75, 56.56]% |
| T-180 | 11/64 (17.19%); Wilson [9.88, 28.21]% | 13/75 (17.33%); Wilson [10.42, 27.43]% | 2/11 (18.18%); Wilson [5.14, 47.70]% |
| T-120 | 8/64 (12.50%); Wilson [6.47, 22.77]% | 9/75 (12.00%); Wilson [6.44, 21.26]% | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| T-60 | 4/64 (6.25%); Wilson [2.46, 15.00]% | 5/75 (6.67%); Wilson [2.88, 14.68]% | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| T-30 | 3/64 (4.69%); Wilson [1.61, 12.90]% | 4/75 (5.33%); Wilson [2.09, 12.93]% | 1/11 (9.09%); Wilson [1.62, 37.74]% |
| candidate_episodes | 837 | 965 | 128 |
| qualified_observed_markets | 61 | 71 | 10 |
| intent_markets | 61 | 71 | 10 |
| actual_fill_markets | 57 | 67 | 10 |
| invalidation_rows | 48 | 58 | 10 |
| stop_count | 0 | 0 | 0 |
| whipsaws | 0 | 0 | 0 |

Latest collection 稀疏小 N；headline difference 是否穩定需要連同 interval 評估。

| Parameter at T300 | Before | After | Latest |
| --- | --- | --- | --- |
| distance_usd | N=64; median=51.235; IQR=[27.735, 88.970]; mean=61.161 | N=75; median=56.047; IQR=[29.449, 93.933]; mean=70.098 | N=11; median=67.550; IQR=[42.031, 120.506]; mean=122.096 |
| distance_bps | N=64; median=5.990; IQR=[3.224, 10.366]; mean=7.133 | N=75; median=6.561; IQR=[3.428, 10.967]; mean=8.195 | N=11; median=7.897; IQR=[4.920, 14.095]; mean=14.377 |
| sigma | N=64; median=0.940; IQR=[0.580, 1.591]; mean=1.160 | N=75; median=0.947; IQR=[0.553, 1.773]; mean=1.266 | N=11; median=1.291; IQR=[0.357, 2.284]; mean=1.880 |
| bps | N=64; median=5.801; IQR=[3.580, 9.812]; mean=7.154 | N=75; median=5.842; IQR=[3.406, 10.947]; mean=8.253 | N=11; median=7.970; IQR=[2.202, 14.077]; mean=14.643 |
| market | N=64; median=0.450; IQR=[0.150, 0.807]; mean=0.477 | N=75; median=0.385; IQR=[0.120, 0.745]; mean=0.457 | N=11; median=0.235; IQR=[0.055, 0.675]; mean=0.340 |
| divergence | N=64; median=-0.001; IQR=[-0.028, 0.027]; mean=-0.005 | N=75; median=-0.004; IQR=[-0.037, 0.022]; mean=-0.015 | N=11; median=-0.042; IQR=[-0.104, -0.011]; mean=-0.069 |
| abs_divergence | N=64; median=0.028; IQR=[0.014, 0.042]; mean=0.036 | N=75; median=0.029; IQR=[0.015, 0.043]; mean=0.042 | N=11; median=0.042; IQR=[0.015, 0.104]; mean=0.075 |

## 11. Taipei-hour matching

沿用 previous deterministic matching：每 hour 兩組 min(N)，slugs chronological selection；非最佳化 outcome matching，不移除 config/date selection bias。Matched N=37 per arm；TOO_SMALL。

| Horizon | Matched weekday | Matched weekend | Diff pp Newcombe | Fisher |
| --- | --- | --- | --- | --- |
| T-300 | 8/37 (21.62%); Wilson [11.39, 37.20]% | 1/37 (2.70%); Wilson [0.48, 13.82]% | [3.804611900984442, 34.650445718697206] | 0.02810 |
| T-180 | 6/37 (16.22%); Wilson [7.65, 31.14]% | 1/37 (2.70%); Wilson [0.48, 13.82]% | [-0.5237293210187843, 28.59862475609199] | 0.10704 |
| T-120 | 3/37 (8.11%); Wilson [2.80, 21.30]% | 0/37 (0.00%); Wilson [0.00, 9.41]% | [-2.6940504091050337, 21.300679349287915] | 0.23973 |

| Group | Observed maker ALLOW incidence (lower bound) |
| --- | --- |
| Matched weekday | 35/37 |
| Matched weekend | 23/37 |

## 12. Pre-Phase-A interference / interpretation

Clean weekday FAST_FOLLOW_EXECUTION_CONTAMINATION=MATERIAL。Actual-entry path 尚非 execution-disabled，因此 ACTUAL_ENTRY_PATH_CLEAN=NO。這是 pre-restart baseline，不宣稱 Phase A 已上線。Intrinsic maker pricing/side diagnostics可局部解讀；完整 qualified opportunity set 則受 veto 前置與 sparse/missing coverage 限制，MAKER_INTRINSIC_SIGNAL_INTERPRETABLE=NO（指完整獨立 opportunity set；可觀察子集仍有描述價值）。
為避免 clean selection 隱藏執行問題，最新所有 completed markets（含品質排除）另列：

| Latest scope | N | Ownership markets | Maker blocks | FOK intents | FOK submits | Confirmed | Errors | TypeErrors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| all completed | 12 | 1 | 1 | 1 | 1 | 55 | 10 | 10 |

FF pending-related exact event counters 在 JSON aggregate.fast_follow；confirmed/expired/handoff/counterfactual events 不等同 order authority acquisition。maker-block counts來自 order journal，沒有與 BUY_PATH duplicate telemetry 重複計數。

## Additional source-grounded feature / invalidation evidence

以下數值為該 clean cohort 的同 run durable observations，並非都與 entry candidate 同一時間。沒有使用 future-nearest joins；不宣稱 market/btc signal 的每個 observation 是獨立樣本。Smart-money 在 persisted payload 標 shadow_only，不能視為新增 trading authority。

### signal_features

| Field | Weekday | Weekend |
| --- | --- | --- |
| btc_trend | N=3834; median=-0.000; IQR=[-0.070, 0.059]; mean=-0.004 | N=2367; median=-0.000; IQR=[-0.000, 0.008]; mean=-0.015 |
| btc_trend_source_age_sec | N=3834; median=0.232; IQR=[0.082, 0.516]; mean=0.388 | N=2367; median=0.242; IQR=[0.090, 0.532]; mean=0.396 |
| composite_score | N=3834; median=-0.006; IQR=[-0.093, 0.074]; mean=-0.014 | N=2367; median=0.000; IQR=[-0.071, 0.056]; mean=-0.012 |
| confidence | N=3834; median=0.083; IQR=[0.027, 0.174]; mean=0.122 | N=2367; median=0.067; IQR=[0.017, 0.148]; mean=0.091 |
| market_consensus | N=3834; median=0.000; IQR=[-0.030, 0.010]; mean=-0.014 | N=2367; median=0.000; IQR=[-0.050, 0.050]; mean=-0.005 |
| market_momentum | N=3834; median=0.000; IQR=[-0.198, 0.000]; mean=-0.040 | N=2367; median=0.000; IQR=[-0.286, 0.203]; mean=-0.022 |
| quote_age_sec | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE | N=0; median=NOT_RECONSTRUCTABLE; IQR=[NOT_RECONSTRUCTABLE, NOT_RECONSTRUCTABLE]; mean=NOT_RECONSTRUCTABLE |
| strike_proximity | N=3834; median=-0.008; IQR=[-0.071, 0.038]; mean=-0.021 | N=2367; median=0.001; IQR=[-0.033, 0.018]; mean=-0.013 |
| time_left_ratio | N=3834; median=0.938; IQR=[0.919, 0.957]; mean=0.938 | N=2367; median=0.936; IQR=[0.918, 0.956]; mean=0.937 |
| w_btc | N=3834; median=0.341; IQR=[0.331, 0.583]; mean=0.446 | N=2367; median=0.338; IQR=[0.330, 0.583]; mean=0.423 |
| w_market | N=3834; median=0.415; IQR=[0.000, 0.432]; mean=0.235 | N=2367; median=0.420; IQR=[0.000, 0.435]; mean=0.274 |
| w_strike | N=3834; median=0.244; IQR=[0.237, 0.417]; mean=0.319 | N=2367; median=0.242; IQR=[0.235, 0.417]; mean=0.303 |

### smart_money

| Field | Weekday | Weekend |
| --- | --- | --- |
| cache_age_sec | N=5085; median=1.686; IQR=[0.810, 2.583]; mean=1.704 | N=3862; median=1.654; IQR=[0.769, 2.577]; mean=1.679 |
| recent_trade_count | N=5084; median=22.000; IQR=[0.000, 52.000]; mean=39.165 | N=3862; median=18.000; IQR=[0.000, 42.000]; mean=26.308 |
| score | N=6316; median=0.662; IQR=[0.000, 0.952]; mean=0.496 | N=4972; median=0.611; IQR=[0.000, 0.961]; mean=0.481 |
| wallet_count | N=5084; median=15.000; IQR=[0.000, 28.000]; mean=17.752 | N=3862; median=12.000; IQR=[0.000, 25.000]; mean=15.159 |

### confirmation

| Field | Weekday | Weekend |
| --- | --- | --- |
| binance_age_sec | N=6316; median=0.228; IQR=[0.074, 0.523]; mean=0.392 | N=4972; median=0.258; IQR=[0.090, 0.589]; mean=0.455 |
| book_vs_fair_ps | N=6315; median=0.000; IQR=[0.000, 0.000]; mean=0.000 | N=4971; median=0.000; IQR=[0.000, 0.000]; mean=0.000 |
| confidence | N=6316; median=1.000; IQR=[1.000, 1.000]; mean=0.971 | N=4972; median=1.000; IQR=[1.000, 1.000]; mean=0.983 |
| spot_minus_strike_bps | N=6316; median=-2.956; IQR=[-9.062, 8.075]; mean=-2.921 | N=4972; median=0.031; IQR=[-7.130, 3.373]; mean=-2.418 |

### invalidation

| Field | Weekday | Weekend |
| --- | --- | --- |
| confirmed_to_clear_sec | N=42; median=11.790; IQR=[6.161, 22.509]; mean=15.563 | N=13; median=30.764; IQR=[19.101, 47.010]; mean=36.956 |
| fair | N=58; median=0.325; IQR=[0.278, 0.345]; mean=0.293 | N=17; median=0.305; IQR=[0.255, 0.345]; mean=0.297 |
| hits | N=58; median=3.000; IQR=[3.000, 3.000]; mean=3.000 | N=17; median=3.000; IQR=[3.000, 3.000]; mean=3.000 |
| inventory_qty | N=58; median=0.000; IQR=[0.000, 0.000]; mean=0.000 | N=17; median=0.000; IQR=[0.000, 0.000]; mean=0.000 |
| right_censored_markets | 16 | 4 |
| signed_distance_usd | N=58; median=-2.605; IQR=[-8.865, 7.806]; mean=-1.895 | N=17; median=-3.574; IQR=[-11.580, 2.098]; mean=-3.783 |
| time_left_sec | N=58; median=316.510; IQR=[137.212, 531.775]; mean=343.766 | N=17; median=412.453; IQR=[142.618, 535.104]; mean=372.667 |

Confirmed-to-clear 是 observed invalidation episode duration；未見 clear 的 episode 是 right-censored，不估作持續到終局。hits 是 persisted consecutive confirmations。inventory=0 的 invalidation 不是 actual stop。Trigger unrealized loss/MAE、L2 freshness age、actual exit/stop price、完整 stopped position 的 post-stop reverse timing 不可重建，不以 fair 或 mid 冒充成交。

### Config-stratified execution observations

| Stratum | Config availability | Weekday N | Weekend N | Observed qualified markets | Sim-filled markets | Reconstructable stops |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PERSISTED | 75 | 0 | 71 | 67 | 0 |
| 2 | UNKNOWN | 6 | 54 | 38 | 19 | 0 |

有 manifest 的 clean runs 觀測到一組共同 exit config；legacy absence 保留 UNKNOWN。不同 strata 不被當成同一 stop policy 的 efficacy sample；無完整 stop samples 也不計算 zero-whipsaw rate。

### Binary observed entry incidence (lower bound)

| Metric | Weekday + Wilson | Weekend + Wilson | Diff pp | Newcombe pp | Fisher |
| --- | --- | --- | --- | --- | --- |
| Observed maker ALLOW (censored) | 71/75 (94.67%); Wilson [87.07, 97.91]% | 33/54 (61.11%); Wilson [47.79, 72.96]% | 33.556 | [19.48435318242194, 47.26667781717077] | 0.000002 |
| Maker simulation fill incidence | 67/75 (89.33%); Wilson [80.34, 94.50]% | 16/54 (29.63%); Wilson [19.14, 42.83]% | 59.704 | [43.73215819409949, 71.39686660288726] | 0.000000 |

這些差異混合 eligibility/config/runtime/policy/fill-model effects；不是 pure maker alpha 或無 FF veto 的完整 counterfactual incidence。

### Incremental crossing / entry / stop evidence

| Horizon | Before crossing incidence | After crossing incidence | Latest only |
| --- | --- | --- | --- |
| T-300 | 19/64 (29.69%); Wilson [19.91, 41.77]% | 22/75 (29.33%); Wilson [20.24, 40.44]% | 3/11 (27.27%); Wilson [9.75, 56.56]% |
| T-180 | 13/64 (20.31%); Wilson [12.27, 31.71]% | 15/75 (20.00%); Wilson [12.51, 30.41]% | 2/11 (18.18%); Wilson [5.14, 47.70]% |
| T-120 | 9/64 (14.06%); Wilson [7.58, 24.62]% | 10/75 (13.33%); Wilson [7.41, 22.83]% | 1/11 (9.09%); Wilson [1.62, 37.74]% |

最新 all-completed scope（跨 rollover 所有 observed runs，含排除市場）FF event counts：`{"completed_markets": 12, "event_counts": {"FAST_FOLLOW_CONFIRMED": 56, "FAST_FOLLOW_ERROR": 10, "FAST_FOLLOW_EXPIRED": 48, "FAST_FOLLOW_QUOTE_HANDOFF": 56}, "typeerror_count": 10}`。這補充 clean-table 的 same coherent run counts，避免把被排除的 rollover市場當成沒有診斷事件。

### Binary crossing / post-entry interaction uncertainty

| Horizon official TWAP crossing | Weekday Wilson | Weekend Wilson | Diff pp Newcombe | Fisher |
| --- | --- | --- | --- | --- |
| T-300 | 22/75 (29.33%); Wilson [20.24, 40.44]% | 4/54 (7.41%); Wilson [2.92, 17.55]% | [8.300880930376605, 33.905965176062665] | 0.001932 |
| T-180 | 15/75 (20.00%); Wilson [12.51, 30.41]% | 2/54 (3.70%); Wilson [1.02, 12.54]% | [4.717744963075171, 27.04749591165091] | 0.007547 |
| T-120 | 10/75 (13.33%); Wilson [7.41, 22.83]% | 1/54 (1.85%); Wilson [0.33, 9.77]% | [1.589758377014501, 21.1028105411176] | 0.024925 |
| T-60 | 5/75 (6.67%); Wilson [2.88, 14.68]% | 0/54 (0.00%); Wilson [0.00, 6.64]% | [-0.9778654523655284, 14.67506005856018] | 0.074198 |
| T-30 | 4/75 (5.33%); Wilson [2.09, 12.93]% | 0/54 (0.00%); Wilson [0.00, 6.64]% | [-2.056198833950656, 12.92598715379828] | 0.139127 |

| Entered-market metric | Weekday Wilson | Weekend Wilson | Diff pp | Newcombe pp | Fisher |
| --- | --- | --- | --- | --- | --- |
| post_entry_adverse_state_observed | 29/67 (43.28%); Wilson [32.10, 55.19]% | 1/16 (6.25%); Wilson [1.11, 28.33]% | 37.03358208955223 | [12.285920711808293, 50.00249083763633] | 0.0074936004214942115 |
| entry_lost | 20/67 (29.85%); Wilson [20.23, 41.66]% | 1/16 (6.25%); Wilson [1.11, 28.33]% | 23.600746268656714 | [-0.4824498342372391, 36.474951863261985] | 0.05964869318735266 |

此 cohort 每個 filled market 恰一個 recorded simulation fill；因此此表 sampling unit 是 entered MARKET。Entry selection、fill simulation、runtime config 與 FF ownership 仍造成 confounding。

## Interpretation / answers

- Reversal：主平日 T300 18/75、週末 2/54；T180 13/75 vs 1/54；T120 9/75 vs 0/54，方向保留。Latest-only 分別 3/11、2/11、1/11，與先前方向一致但 N=11 的 interval 寬。
- Hour matching 37/37 是較小 sensitivity；T300/T180 direction 仍保留，T120 的 sample 不足以支持穩定 matched effect。這不是取消所有 confounders。
- p_ex calibration：平日 market Brier 五個 checkpoints 都較低，但主 cohort 差僅約 0.0011–0.0045；最新批次差較大而 N 小。只能說 point estimates 傾向 market，尚未證明 stable superiority。週末 T30 反而 analytic 較佳。
- Sigma/bps：固定 bins 保留，T300 ordering 仍非乾淨 monotonic；不能由新資料推出新 cutoff。T300 部分 medium sigma bin 也有 flips，需繼續分層驗證。
- Maker-entry interaction：有 side-resolved simulation fill 的平日 67 個市場中 29 個觀察到 post-entry adverse canonical state，20 個 entered side 最終輸；週末 16 中分別 1、1。Typical first maker qualified TTE 約 586s，故後段 reversal 確實可能发生在 entry之後；但沒有 actual stop-vs-hold 資料，不能據此量化 stop benefit。
- FF interference：主平日兩個 market 有 durable FF intent/ownership，280 次 maker blocking；latest clean 11 markets 沒有 takeover，但 10 次 TypeError。最新 all-runs 診斷另外列出。Full intrinsic opportunity set 不可無條件反推；actual execution path 存在 MATERIAL contamination。
- Stop effectiveness：TOO_SMALL / NOT_RECONSTRUCTABLE。所有 matched clean markets 沒有可重建 actual stopped position；whipsaw count=0 是 recorded reconstructable count，並非 whipsaw risk=0。
- 整體：最新資料方向一致、N增加，沒有授權改 live strategy。這份 baseline 將 maker signal evidence 與 PRE-Phase-A execution path 分開；之後 Phase-A reload 應另建 cohort。

## Quality / cohort identities

| Status | Observed markets |
| --- | --- |
| ACTIVE | 1 |
| INSUFFICIENT_COLLECTION_SPAN | 5 |
| INTERRUPTED | 74 |
| LOW_JOINT_FRESHNESS | 20 |
| MISSING_FRESH_CHECKPOINT | 30 |
| NO_CANONICAL_SETTLEMENT | 121 |
| SYNCHRONIZED_USABLE | 135 |

### WEEKDAY_PRIMARY

| Market slug | Run ID | Span sec | Max gap sec | Joint fresh % | Manifest revision |
| --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1791221400 | run_1791220474_2ed1742e | 879.7 | 5.16 | 26.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791225000 | run_1791224107_573fabab | 880.2 | 3.94 | 58.3 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791225900 | run_1791224107_573fabab | 880.1 | 3.23 | 67.7 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791226800 | run_1791224107_573fabab | 880.1 | 5.64 | 80.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791227700 | run_1791227746_2be3a494 | 778.1 | 5.67 | 89.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791228600 | run_1791227746_2be3a494 | 880.2 | 2.90 | 92.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791229500 | run_1791227746_2be3a494 | 879.8 | 2.64 | 89.1 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791230400 | run_1791227746_2be3a494 | 880.6 | 5.99 | 82.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791232200 | run_1791231380_2435b627 | 880.4 | 6.05 | 71.3 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791233100 | run_1791231380_2435b627 | 880.4 | 3.48 | 73.8 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791234000 | run_1791231380_2435b627 | 879.7 | 5.87 | 47.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791235800 | run_1791235015_758df0b8 | 896.2 | 6.19 | 48.2 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791236700 | run_1791235015_758df0b8 | 880.5 | 4.29 | 58.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791237600 | run_1791235015_758df0b8 | 879.7 | 3.20 | 75.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791239400 | run_1791238651_7f90656a | 881.1 | 3.95 | 85.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791240300 | run_1791238651_7f90656a | 880.2 | 4.77 | 84.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791241200 | run_1791238651_7f90656a | 880.0 | 2.83 | 87.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791243000 | run_1791242284_d2414b9e | 879.9 | 6.25 | 94.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791243900 | run_1791242284_d2414b9e | 880.1 | 2.85 | 96.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791244800 | run_1791242284_d2414b9e | 880.0 | 5.19 | 87.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791246600 | run_1791245915_384288e1 | 880.5 | 5.89 | 85.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791247500 | run_1791245915_384288e1 | 881.0 | 5.99 | 76.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791248400 | run_1791245915_384288e1 | 879.7 | 5.59 | 79.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791250200 | run_1791249551_7300cf99 | 880.0 | 5.70 | 84.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791251100 | run_1791249551_7300cf99 | 880.1 | 5.71 | 79.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791252000 | run_1791249551_7300cf99 | 879.7 | 5.84 | 81.8 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791253800 | run_1791253399_d0ff0054 | 880.9 | 2.98 | 86.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791254700 | run_1791253399_d0ff0054 | 879.9 | 5.72 | 82.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791255600 | run_1791253399_d0ff0054 | 880.3 | 3.31 | 83.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791256500 | run_1791253399_d0ff0054 | 880.4 | 5.31 | 79.4 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791257400 | run_1791257485_084da14f | 743.0 | 3.53 | 71.7 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791258300 | run_1791257485_084da14f | 880.5 | 3.40 | 67.1 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791259200 | run_1791257485_084da14f | 879.9 | 6.08 | 74.8 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791260100 | run_1791257485_084da14f | 880.8 | 5.64 | 85.1 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791261000 | run_1791257485_084da14f | 880.3 | 3.07 | 77.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791261900 | run_1791261985_8f4edaaa | 740.8 | 5.10 | 87.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791262800 | run_1791261985_8f4edaaa | 881.2 | 5.52 | 62.4 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791263700 | run_1791261985_8f4edaaa | 880.7 | 3.28 | 51.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791265500 | run_1791261985_8f4edaaa | 879.2 | 4.23 | 57.7 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791266400 | run_1791266486_408fbe92 | 738.7 | 6.14 | 62.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791267300 | run_1791266486_408fbe92 | 880.6 | 5.64 | 67.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791268200 | run_1791266486_408fbe92 | 879.5 | 5.85 | 73.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791269100 | run_1791266486_408fbe92 | 880.8 | 3.42 | 74.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791270000 | run_1791266486_408fbe92 | 880.1 | 3.71 | 76.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791275400 | run_1791275488_682e8353 | 736.8 | 5.90 | 30.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791277200 | run_1791275488_682e8353 | 880.8 | 5.47 | 29.6 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791278100 | run_1791275488_682e8353 | 881.0 | 6.13 | 38.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791279900 | run_1791279466_720c2e47 | 879.3 | 5.71 | 44.9 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791280800 | run_1791279466_720c2e47 | 880.6 | 5.56 | 35.7 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791281700 | run_1791279466_720c2e47 | 879.4 | 6.13 | 40.0 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791282600 | run_1791279466_720c2e47 | 879.5 | 5.03 | 57.7 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791283500 | run_1791279466_720c2e47 | 880.9 | 3.90 | 63.6 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791284400 | run_1791279466_720c2e47 | 879.3 | 4.97 | 62.8 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791285300 | run_1791279466_720c2e47 | 877.8 | 13.46 | 58.1 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791286200 | run_1791279466_720c2e47 | 868.9 | 5.90 | 65.0 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791287100 | run_1791279466_720c2e47 | 880.2 | 4.27 | 75.8 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791304200 | run_1791304179_b0a8dcd8 | 839.8 | 5.94 | 98.4 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791306900 | run_1791304179_b0a8dcd8 | 881.0 | 5.49 | 98.2 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791307800 | run_1791304179_b0a8dcd8 | 881.3 | 5.28 | 99.1 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791308700 | run_1791304179_b0a8dcd8 | 880.1 | 6.22 | 99.5 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791309600 | run_1791304179_b0a8dcd8 | 880.2 | 5.90 | 99.3 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791310500 | run_1791304179_b0a8dcd8 | 880.0 | 6.05 | 99.4 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791311400 | run_1791304179_b0a8dcd8 | 879.7 | 6.32 | 99.8 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791313200 | run_1791304179_b0a8dcd8 | 879.5 | 14.92 | 99.3 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791330300 | run_1791329712_9b02abda | 880.8 | 5.78 | 98.9 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791331200 | run_1791329712_9b02abda | 880.5 | 5.44 | 99.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791332100 | run_1791329712_9b02abda | 880.9 | 5.83 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333000 | run_1791329712_9b02abda | 881.1 | 6.02 | 98.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333900 | run_1791329712_9b02abda | 881.2 | 6.01 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791334800 | run_1791329712_9b02abda | 879.9 | 5.70 | 99.2 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791335700 | run_1791329712_9b02abda | 879.5 | 5.94 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791336600 | run_1791329712_9b02abda | 881.1 | 2.70 | 99.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791337500 | run_1791329712_9b02abda | 879.1 | 5.67 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791338400 | run_1791329712_9b02abda | 880.6 | 5.89 | 99.8 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791339300 | run_1791329712_9b02abda | 879.7 | 5.44 | 97.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |

### WEEKEND_PRIMARY

| Market slug | Run ID | Span sec | Max gap sec | Joint fresh % | Manifest revision |
| --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1790964000 | run_1790964229_ae4dba0a | 611.1 | 5.03 | 95.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1790964900 | run_1790964229_ae4dba0a | 880.2 | 12.32 | 98.1 | LEGACY_UNKNOWN |
| btc-updown-15m-1790965800 | run_1790964229_ae4dba0a | 880.0 | 6.16 | 97.6 | LEGACY_UNKNOWN |
| btc-updown-15m-1790990100 | run_1790990130_9fddffb2 | 795.7 | 5.14 | 39.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791001800 | run_1791000045_df1adc92 | 881.3 | 10.08 | 36.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791005400 | run_1791003676_3d58af66 | 880.7 | 5.23 | 76.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791009900 | run_1791009819_4f16006f | 897.9 | 6.01 | 87.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791010800 | run_1791009819_4f16006f | 881.0 | 5.10 | 76.5 | LEGACY_UNKNOWN |
| btc-updown-15m-1791015300 | run_1791014641_12c10290 | 879.7 | 5.90 | 83.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791019800 | run_1791018273_c80d24ec | 880.5 | 5.78 | 66.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791024300 | run_1791023404_d05b32b1 | 879.0 | 8.77 | 64.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791027000 | run_1791026106_c815ceee | 880.0 | 4.97 | 80.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791027900 | run_1791026106_c815ceee | 880.1 | 5.16 | 71.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791028800 | run_1791026106_c815ceee | 878.9 | 6.05 | 77.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791029700 | run_1791029737_3db8ca59 | 791.9 | 6.27 | 78.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791030600 | run_1791029737_3db8ca59 | 880.2 | 13.22 | 84.8 | LEGACY_UNKNOWN |
| btc-updown-15m-1791031500 | run_1791029737_3db8ca59 | 878.9 | 5.98 | 96.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791032400 | run_1791029737_3db8ca59 | 880.1 | 5.02 | 94.6 | LEGACY_UNKNOWN |
| btc-updown-15m-1791035100 | run_1791033368_63fa64b7 | 880.0 | 4.40 | 99.8 | LEGACY_UNKNOWN |
| btc-updown-15m-1791036000 | run_1791033368_63fa64b7 | 880.1 | 6.15 | 95.6 | LEGACY_UNKNOWN |
| btc-updown-15m-1791037800 | run_1791036999_39eed4e1 | 880.8 | 5.77 | 98.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791038700 | run_1791036999_39eed4e1 | 880.4 | 14.05 | 98.6 | LEGACY_UNKNOWN |
| btc-updown-15m-1791042300 | run_1791041249_fc1dd8e0 | 879.4 | 6.06 | 92.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791043200 | run_1791041249_fc1dd8e0 | 880.4 | 5.58 | 96.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791045000 | run_1791044880_acbd0786 | 881.2 | 5.01 | 99.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791045900 | run_1791044880_acbd0786 | 879.6 | 5.61 | 99.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791048600 | run_1791048511_365b424d | 880.9 | 5.19 | 87.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791052200 | run_1791052141_fe30de47 | 892.3 | 5.84 | 93.0 | LEGACY_UNKNOWN |
| btc-updown-15m-1791054000 | run_1791053867_a36e4c67 | 880.8 | 6.06 | 84.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791054900 | run_1791053867_a36e4c67 | 880.2 | 5.04 | 70.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791055800 | run_1791053867_a36e4c67 | 879.7 | 6.19 | 62.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791057600 | run_1791057497_bb44b528 | 880.9 | 6.19 | 44.8 | LEGACY_UNKNOWN |
| btc-updown-15m-1791061200 | run_1791061128_6012b10b | 898.8 | 5.76 | 72.6 | LEGACY_UNKNOWN |
| btc-updown-15m-1791062100 | run_1791061128_6012b10b | 880.5 | 3.83 | 76.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791064800 | run_1791064759_671ece47 | 874.8 | 11.59 | 67.0 | LEGACY_UNKNOWN |
| btc-updown-15m-1791065700 | run_1791064759_671ece47 | 879.1 | 13.67 | 55.5 | LEGACY_UNKNOWN |
| btc-updown-15m-1791070200 | run_1791070109_cc475056 | 879.5 | 5.96 | 79.1 | LEGACY_UNKNOWN |
| btc-updown-15m-1791076500 | run_1791076424_50193a93 | 897.5 | 6.41 | 89.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791077400 | run_1791076424_50193a93 | 879.4 | 6.29 | 76.8 | LEGACY_UNKNOWN |
| btc-updown-15m-1791081000 | run_1791081118_a7cf46b9 | 713.3 | 8.71 | 55.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791081900 | run_1791081118_a7cf46b9 | 880.6 | 11.02 | 56.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791085500 | run_1791084345_41679360 | 880.0 | 8.35 | 67.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791086400 | run_1791084345_41679360 | 879.2 | 6.18 | 66.0 | LEGACY_UNKNOWN |
| btc-updown-15m-1791089100 | run_1791088949_8a69164e | 881.4 | 9.09 | 66.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791090000 | run_1791088949_8a69164e | 880.1 | 5.79 | 46.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791095400 | run_1791095281_4743bbb1 | 879.5 | 7.17 | 59.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791102600 | run_1791101455_05564309 | 839.1 | 5.69 | 56.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791103500 | run_1791101455_05564309 | 880.9 | 5.18 | 47.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791109800 | run_1791107133_3f0342b8 | 879.6 | 7.07 | 33.5 | LEGACY_UNKNOWN |
| btc-updown-15m-1791112500 | run_1791112480_51b3a76a | 849.9 | 6.27 | 62.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791113400 | run_1791112480_51b3a76a | 878.1 | 6.14 | 63.9 | LEGACY_UNKNOWN |
| btc-updown-15m-1791114300 | run_1791112480_51b3a76a | 880.5 | 5.71 | 72.7 | LEGACY_UNKNOWN |
| btc-updown-15m-1791117900 | run_1791116112_9db06308 | 879.8 | 5.98 | 52.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791121500 | run_1791119628_cdb7d7c2 | 880.3 | 6.24 | 45.3 | LEGACY_UNKNOWN |

### WEEKDAY_WITH_MONDAY_SENSITIVITY

| Market slug | Run ID | Span sec | Max gap sec | Joint fresh % | Manifest revision |
| --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1791131400 | run_1791131381_baae0ef5 | 845.2 | 6.19 | 66.1 | LEGACY_UNKNOWN |
| btc-updown-15m-1791135000 | run_1791135014_15577a71 | 808.2 | 12.32 | 61.8 | LEGACY_UNKNOWN |
| btc-updown-15m-1791146700 | run_1791146614_c03299a7 | 879.1 | 14.34 | 66.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791150300 | run_1791150245_90251ae4 | 882.3 | 6.48 | 71.2 | LEGACY_UNKNOWN |
| btc-updown-15m-1791198000 | run_1791197001_1b6f1dd9 | 880.0 | 6.31 | 54.4 | LEGACY_UNKNOWN |
| btc-updown-15m-1791198900 | run_1791197001_1b6f1dd9 | 878.8 | 5.95 | 58.3 | LEGACY_UNKNOWN |
| btc-updown-15m-1791221400 | run_1791220474_2ed1742e | 879.7 | 5.16 | 26.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791225000 | run_1791224107_573fabab | 880.2 | 3.94 | 58.3 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791225900 | run_1791224107_573fabab | 880.1 | 3.23 | 67.7 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791226800 | run_1791224107_573fabab | 880.1 | 5.64 | 80.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791227700 | run_1791227746_2be3a494 | 778.1 | 5.67 | 89.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791228600 | run_1791227746_2be3a494 | 880.2 | 2.90 | 92.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791229500 | run_1791227746_2be3a494 | 879.8 | 2.64 | 89.1 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791230400 | run_1791227746_2be3a494 | 880.6 | 5.99 | 82.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791232200 | run_1791231380_2435b627 | 880.4 | 6.05 | 71.3 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791233100 | run_1791231380_2435b627 | 880.4 | 3.48 | 73.8 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791234000 | run_1791231380_2435b627 | 879.7 | 5.87 | 47.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791235800 | run_1791235015_758df0b8 | 896.2 | 6.19 | 48.2 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791236700 | run_1791235015_758df0b8 | 880.5 | 4.29 | 58.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791237600 | run_1791235015_758df0b8 | 879.7 | 3.20 | 75.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791239400 | run_1791238651_7f90656a | 881.1 | 3.95 | 85.4 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791240300 | run_1791238651_7f90656a | 880.2 | 4.77 | 84.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791241200 | run_1791238651_7f90656a | 880.0 | 2.83 | 87.9 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791243000 | run_1791242284_d2414b9e | 879.9 | 6.25 | 94.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791243900 | run_1791242284_d2414b9e | 880.1 | 2.85 | 96.6 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791244800 | run_1791242284_d2414b9e | 880.0 | 5.19 | 87.0 | 01c87aadb09092eeb6257a77c9d0e48270ec2e5b |
| btc-updown-15m-1791246600 | run_1791245915_384288e1 | 880.5 | 5.89 | 85.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791247500 | run_1791245915_384288e1 | 881.0 | 5.99 | 76.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791248400 | run_1791245915_384288e1 | 879.7 | 5.59 | 79.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791250200 | run_1791249551_7300cf99 | 880.0 | 5.70 | 84.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791251100 | run_1791249551_7300cf99 | 880.1 | 5.71 | 79.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791252000 | run_1791249551_7300cf99 | 879.7 | 5.84 | 81.8 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791253800 | run_1791253399_d0ff0054 | 880.9 | 2.98 | 86.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791254700 | run_1791253399_d0ff0054 | 879.9 | 5.72 | 82.2 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791255600 | run_1791253399_d0ff0054 | 880.3 | 3.31 | 83.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791256500 | run_1791253399_d0ff0054 | 880.4 | 5.31 | 79.4 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791257400 | run_1791257485_084da14f | 743.0 | 3.53 | 71.7 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791258300 | run_1791257485_084da14f | 880.5 | 3.40 | 67.1 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791259200 | run_1791257485_084da14f | 879.9 | 6.08 | 74.8 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791260100 | run_1791257485_084da14f | 880.8 | 5.64 | 85.1 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791261000 | run_1791257485_084da14f | 880.3 | 3.07 | 77.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791261900 | run_1791261985_8f4edaaa | 740.8 | 5.10 | 87.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791262800 | run_1791261985_8f4edaaa | 881.2 | 5.52 | 62.4 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791263700 | run_1791261985_8f4edaaa | 880.7 | 3.28 | 51.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791265500 | run_1791261985_8f4edaaa | 879.2 | 4.23 | 57.7 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791266400 | run_1791266486_408fbe92 | 738.7 | 6.14 | 62.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791267300 | run_1791266486_408fbe92 | 880.6 | 5.64 | 67.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791268200 | run_1791266486_408fbe92 | 879.5 | 5.85 | 73.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791269100 | run_1791266486_408fbe92 | 880.8 | 3.42 | 74.3 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791270000 | run_1791266486_408fbe92 | 880.1 | 3.71 | 76.9 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791275400 | run_1791275488_682e8353 | 736.8 | 5.90 | 30.0 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791277200 | run_1791275488_682e8353 | 880.8 | 5.47 | 29.6 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791278100 | run_1791275488_682e8353 | 881.0 | 6.13 | 38.5 | 49aa9cad9963b596138302221f06831a86f1e470 |
| btc-updown-15m-1791279900 | run_1791279466_720c2e47 | 879.3 | 5.71 | 44.9 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791280800 | run_1791279466_720c2e47 | 880.6 | 5.56 | 35.7 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791281700 | run_1791279466_720c2e47 | 879.4 | 6.13 | 40.0 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791282600 | run_1791279466_720c2e47 | 879.5 | 5.03 | 57.7 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791283500 | run_1791279466_720c2e47 | 880.9 | 3.90 | 63.6 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791284400 | run_1791279466_720c2e47 | 879.3 | 4.97 | 62.8 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791285300 | run_1791279466_720c2e47 | 877.8 | 13.46 | 58.1 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791286200 | run_1791279466_720c2e47 | 868.9 | 5.90 | 65.0 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791287100 | run_1791279466_720c2e47 | 880.2 | 4.27 | 75.8 | ad7cc22e0175f0ded77ab48e4bc945e9b5f7e508 |
| btc-updown-15m-1791304200 | run_1791304179_b0a8dcd8 | 839.8 | 5.94 | 98.4 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791306900 | run_1791304179_b0a8dcd8 | 881.0 | 5.49 | 98.2 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791307800 | run_1791304179_b0a8dcd8 | 881.3 | 5.28 | 99.1 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791308700 | run_1791304179_b0a8dcd8 | 880.1 | 6.22 | 99.5 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791309600 | run_1791304179_b0a8dcd8 | 880.2 | 5.90 | 99.3 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791310500 | run_1791304179_b0a8dcd8 | 880.0 | 6.05 | 99.4 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791311400 | run_1791304179_b0a8dcd8 | 879.7 | 6.32 | 99.8 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791313200 | run_1791304179_b0a8dcd8 | 879.5 | 14.92 | 99.3 | 9f60f63b747528d264e62c06b780cd56b8c08cff |
| btc-updown-15m-1791330300 | run_1791329712_9b02abda | 880.8 | 5.78 | 98.9 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791331200 | run_1791329712_9b02abda | 880.5 | 5.44 | 99.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791332100 | run_1791329712_9b02abda | 880.9 | 5.83 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333000 | run_1791329712_9b02abda | 881.1 | 6.02 | 98.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333900 | run_1791329712_9b02abda | 881.2 | 6.01 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791334800 | run_1791329712_9b02abda | 879.9 | 5.70 | 99.2 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791335700 | run_1791329712_9b02abda | 879.5 | 5.94 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791336600 | run_1791329712_9b02abda | 881.1 | 2.70 | 99.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791337500 | run_1791329712_9b02abda | 879.1 | 5.67 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791338400 | run_1791329712_9b02abda | 880.6 | 5.89 | 99.8 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791339300 | run_1791329712_9b02abda | 879.7 | 5.44 | 97.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |

### LATEST_3H_ONLY

| Market slug | Run ID | Span sec | Max gap sec | Joint fresh % | Manifest revision |
| --- | --- | --- | --- | --- | --- |
| btc-updown-15m-1791330300 | run_1791329712_9b02abda | 880.8 | 5.78 | 98.9 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791331200 | run_1791329712_9b02abda | 880.5 | 5.44 | 99.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791332100 | run_1791329712_9b02abda | 880.9 | 5.83 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333000 | run_1791329712_9b02abda | 881.1 | 6.02 | 98.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791333900 | run_1791329712_9b02abda | 881.2 | 6.01 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791334800 | run_1791329712_9b02abda | 879.9 | 5.70 | 99.2 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791335700 | run_1791329712_9b02abda | 879.5 | 5.94 | 99.4 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791336600 | run_1791329712_9b02abda | 881.1 | 2.70 | 99.3 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791337500 | run_1791329712_9b02abda | 879.1 | 5.67 | 99.7 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791338400 | run_1791329712_9b02abda | 880.6 | 5.89 | 99.8 | 830915257df988d8dc9343d7f3f8df6c44702f15 |
| btc-updown-15m-1791339300 | run_1791329712_9b02abda | 879.7 | 5.44 | 97.5 | 830915257df988d8dc9343d7f3f8df6c44702f15 |

## Final summary

```text
PRE_PHASE_A_WEEKDAY_WEEKEND_FINAL
CUTOFF=2026-10-07T10:49:19+08:00
WEEKDAY_PRIMARY_N=75
WEEKEND_PRIMARY_N=54
WEEKDAY_MONDAY_SENSITIVITY_N=81
LATEST_3H_COMPLETE_MARKETS=11
T300_WEEKDAY=18/75
T300_WEEKEND=2/54
T300_DIFF_PP=20.29630
T180_WEEKDAY=13/75
T180_WEEKEND=1/54
T180_DIFF_PP=15.48148
T120_WEEKDAY=9/75
T120_WEEKEND=0/54
T120_DIFF_PP=12.00000
WEEKDAY_MAKER_QUALIFIED_MARKETS=71/75 OBSERVED_LOWER_BOUND
WEEKEND_MAKER_QUALIFIED_MARKETS=33/54 OBSERVED_LOWER_BOUND
WEEKDAY_ACTUAL_ENTRY_MARKETS=67/75
WEEKEND_ACTUAL_ENTRY_MARKETS=16/54
WEEKDAY_STOP_COUNT=0
WEEKEND_STOP_COUNT=0
WEEKDAY_WHIPSAW_COUNT=0
WEEKEND_WHIPSAW_COUNT=0
WEEKDAY_STOP_VS_HOLD_DELTA=NOT_RECONSTRUCTABLE
WEEKEND_STOP_VS_HOLD_DELTA=NOT_RECONSTRUCTABLE
FAST_FOLLOW_BLOCKED_MAKER_COUNT=280
FAST_FOLLOW_OWNERSHIP_MARKETS=2
FAST_FOLLOW_FOK_ATTEMPTS=2
FAST_FOLLOW_ERRORS=10
FAST_FOLLOW_EXECUTION_CONTAMINATION=MATERIAL
MAKER_INTRINSIC_SIGNAL_INTERPRETABLE=NO
ACTUAL_ENTRY_PATH_CLEAN=NO
LATEST_3H_EFFECT=DIRECTIONALLY_CONSISTENT
REGIME_RESULT_STABILITY=DIRECTIONALLY_STABLE_BUT_MAGNITUDE_CHANGED
CODE_CHANGED=NO
ACTIVE_DATA_MUTATED=NO
BOT_RESTARTED=NO
```
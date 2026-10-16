# WEEKDAY VS WEEKEND MID-CHECK — CLEAN COHORT ONLY

資料截止：**2026-10-06T20:44:17.342+08:00**。本輪固定重算既有盤點截止點，不把分析進行期間新增的市場混入；報告生成時間另見檔名。N 全部以獨立 market_slug 計算，同一市場的五個 checkpoint 不是五個獨立樣本。

主要結果：星期二 T-300 13/53，週末 2/51；T-120 8/53，週末 0/51。全樣本呈現翻轉／晚期 crossing 差異，但精確小時配對僅各 29 個市場，且平日僅一個主要觀察日。結論為 **WEEKDAY_WEEKEND_DIFFERENCE_WEAK**，研究決策 **REGIME_SIGNAL_WORTH_FORMAL_VALIDATION**；不建議任何 live strategy 變更。

## 1. Cohort 定義與樣本帳

| Cohort | Markets | 台北日期／N | Runtime／N | Unique runs | Unique cycles | Canonical settlement | Fresh checkpoints | Verified exact strike |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 53 | {'2026-10-06': 53} | {'01c87aa': 19, '49aa9ca': 26, 'ad7cc22': 8} | 14 | 14 known (53/53 markets) | 53/53 | 5/5 × 53 | 53/53 |
| WEEKDAY_SENSITIVITY | 58 | {'2026-10-06': 53, '2026-10-05': 5} | {'01c87aa': 19, '49aa9ca': 26, 'ad7cc22': 8, 'LEGACY_UNKNOWN': 5} | 19 | 14 known (53/58 markets) | 58/58 | 5/5 × 58 | 58/58 |
| WEEKDAY_COMPLETE_SIGMA_BPS | 36 | {'2026-10-06': 32, '2026-10-05': 4} | {'01c87aa': 10, '49aa9ca': 17, 'ad7cc22': 5, 'LEGACY_UNKNOWN': 4} | 17 | 13 known (32/36 markets) | 36/36 | 5/5 × 36 | 36/36 |
| LATEST_RUNTIME_ONLY | 8 | {'2026-10-06': 8} | {'ad7cc22': 8} | 1 | 1 known (8/8 markets) | 8/8 | 5/5 × 8 | 8/8 |
| WEEKEND_PRIMARY | 51 | {'2026-10-03': 21, '2026-10-04': 30} | {'LEGACY_UNKNOWN': 51} | 28 | UNKNOWN | 51/51 | 5/5 × 51 | 51/51 |
| WEEKEND_COMPLETE_SIGMA_BPS | 30 | {'2026-10-03': 12, '2026-10-04': 18} | {'LEGACY_UNKNOWN': 30} | 19 | UNKNOWN | 30/30 | 5/5 × 30 | 30/30 |

- PRIMARY_WEEKDAY：市場開盤範圍 2026-10-06T02:30:00.000+08:00 至 2026-10-06T19:45:00.000+08:00；weekday composition 由台北日期決定。
- WEEKDAY_SENSITIVITY：市場開盤範圍 2026-10-05T00:30:00.000+08:00 至 2026-10-06T19:45:00.000+08:00；weekday composition 由台北日期決定。
- WEEKDAY_COMPLETE_SIGMA_BPS：市場開盤範圍 2026-10-05T00:30:00.000+08:00 至 2026-10-06T19:30:00.000+08:00；weekday composition 由台北日期決定。
- LATEST_RUNTIME_ONLY：市場開盤範圍 2026-10-06T17:45:00.000+08:00 至 2026-10-06T19:45:00.000+08:00；weekday composition 由台北日期決定。
- WEEKEND_PRIMARY：市場開盤範圍 2026-10-03T02:00:00.000+08:00 至 2026-10-04T21:45:00.000+08:00；weekday composition 由台北日期決定。

OLD_DEGRADED_MONDAY 全部排除。星期一只有 5 個逐市場合格者進入 sensitivity；不是把整個星期一納入。53 個星期二 = 19 個 01c87aa、26 個 49aa9ca、8 個 ad7cc22。cycle 不明不能當成 0 cycles；週末 28 個 run_id 是實際 run 數，不是 snapshot-row 數。

## 2. 完全相同的正式品質門檻

重新套用 canonical gate：市場已完成；MARKET_TWAP_SUMMARY 的 canonical_settlement_side 為 UP/DOWN 且 settlement_reference_is_canonical=true；coverage class GOOD/FULL；單一 run_id；全市場相鄰 snapshot 最大間隔 ≤15 秒；joint-fresh 可用市場機率；T-300/180/120/60/30 各有最近觀察且絕對時間差 ≤12 秒、有效 settlement_state_side。

Coverage class 原門檻：span<60 秒或無 summary 為 UNUSABLE；跨 run／gap>15 秒為 INTERRUPTED；span<600 秒或 joint_fresh<25% 為 PARTIAL；否則 span<840 秒或 joint_fresh<70% 為 GOOD，其餘 FULL。coverage_ratio=observed span/900。GOOD 不代表 70% joint freshness；fresh 五 checkpoints 是另行必須通過的門檻。

| 日期 | Observed markets（非理論 slots） | 互斥分類／N |
| --- | --- | --- |
| 2026-10-03 | 74 | {'NO_CANONICAL_SETTLEMENT': 13, 'PARTIAL': 13, 'INTERRUPTED': 25, 'SYNCHRONIZED_USABLE': 21, 'MISSING_FRESH_CHECKPOINT': 2} |
| 2026-10-04 | 96 | {'SYNCHRONIZED_USABLE': 30, 'INTERRUPTED': 41, 'NO_CANONICAL_SETTLEMENT': 13, 'MISSING_FRESH_CHECKPOINT': 7, 'PARTIAL': 5} |
| 2026-10-05 | 92 | {'INTERRUPTED': 60, 'NO_CANONICAL_SETTLEMENT': 20, 'SYNCHRONIZED_USABLE': 5, 'MISSING_FRESH_CHECKPOINT': 3, 'PARTIAL': 4} |
| 2026-10-06 | 83 | {'NO_CANONICAL_SETTLEMENT': 4, 'INTERRUPTED': 11, 'PARTIAL': 8, 'SYNCHRONIZED_USABLE': 53, 'MISSING_FRESH_CHECKPOINT': 6, 'ACTIVE': 1} |

WEEKDAY_ADMITTED_N=53；WEEKEND_ADMITTED_N=51。週末可用相同門檻比較，不使用舊 124 個 T-300／170 個 observed markets 等較寬樣本作為本輪分母。排除率不同可能帶來 collection-selection bias；相同 gate 降低測量差異，不能證明完全移除選樣偏差。

| Cohort | Median coverage | P10 coverage | Median max gap s | P90 max gap s | Median joint fresh % | P10 joint fresh % | Median snapshots |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 97.8% | 97.6% | 5.312 | 6.120 | 69.032 | 40.760 | 732 |
| WEEKEND_PRIMARY | 97.8% | 97.2% | 6.010 | 11.020 | 68.643 | 40.657 | 614 |

## 3. Checkpoint／payload／provenance 語義

| 欄位 | 星期二 | 週末 |
| --- | --- | --- |
| research_schema_version | 1 | None |
| prediction_schema_version | 1 | None |
| probability_model_version | existing_twap_average_approx_v1 | existing_twap_average_approx_v1 |
| lifecycle_schema_version | None | None |
| trace_schema_version | None | None |

SEMANTICALLY_COMPATIBLE（本輪核心市場測量）。實際 payload signature：星期二 98 keys、週末 96 keys，各只有一種 signature；共同 96 keys 一致，差別只在星期二增加 research_schema_version / prediction_schema_version。週末缺顯式版本標記，不能自動判為 schema incompatible。兩者 probability_model_version 均為 existing_twap_average_approx_v1。payload rows：星期二 37,769，週末 30,345；這些是 ROWS，不是 markets／runs。

Canonical terminal flip：checkpoint 的官方 TWAP settlement_state_side 與 MARKET_TWAP_SUMMARY.canonical_settlement_side 不同。不能用交易 active_side、BTC spot 的正負或 market p>0.5 取代 TWAP leader。Canonical crossing：fresh 官方 TWAP 與 verified strike 的相鄰側別改變；本輪以 crossing_count(include_left_context=False) 計算各 horizon 後 observed crossing。

市場路徑／divergence 使用既有 _up_mid：fresh UP mid 優先，否則 fresh DOWN mid 的 1-p；所以 basic path N=53/51。Canonical calibration helper checkpoint_flip_rows 則只接受 fresh UP mid，並要求 sigma_ex_market_fresh 才使用 p_ex；其有效 N 另列，不能拿不同 N 的兩個 Brier 直接比較。

台北時區分類沿用 market_context。週末 run-level provenance manifests absent；星期二 manifest 存在，同一 safe_config hash b86082ac5c66ec1fc1fb59edcb89ccd261591f25aa2cf7924aac2a83a3b49d81。這只涵蓋 allowlisted config，不能證明週末的 shadow 策略／fill 程式與當前完全相同。官方 TWAP label 是本地已保存 canonical reference，不是另向 Polymarket 查核的最終 adjudication。

## 4. Primary T-5m / T-2m flip analysis

| Horizon | Weekday | Wilson 95% | Weekend | Wilson 95% | 差 pp | Difference 95% | Risk ratio | Cohen h | Fisher two-sided p |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-300 | 13/53 = 24.5% | [14.9%, 37.6%] | 2/51 = 3.9% | [1.1%, 13.2%] | 20.61 | [7.2%, 34.0%] | 6.25 | 0.638 | 0.00404 |
| T-120 | 8/53 = 15.1% | [7.9%, 27.1%] | 0/51 = 0.0% | [0.0%, 7.0%] | 15.09 | [5.0%, 27.1%] | UNDEFINED (weekend 0) | 0.798 | 0.00591 |

差異 CI 使用 Newcombe independent Wilson 方法；Fisher exact 為雙尾，未做多重比較校正。T-300 相對率 +525.5%；T-120 週末零事件，RR 不定義，不能宣稱無限風險。週末 0/51 的 Wilson 上限仍約 7.0%。這些是本樣本的市場單位名義不確定區間；鄰接市場可能相關，且一天星期二不足以代表重複的 weekday regime，未估計跨日群聚不確定性。

## 5. 五個 horizon reversal／persistence

| Horizon | Weekday reversal | Persistence | Weekday directions | Weekend reversal | Persistence | Weekend directions | 差 pp | Difference 95% | Cohen h |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T-300 | 13/53 = 24.5% | 75.5% | UP leader 25; UP final 26 | 2/51 = 3.9% | 96.1% | UP leader 28; UP final 28 | 20.61 | [7.2%, 34.0%] | 0.638 |
| T-180 | 10/53 = 18.9% | 81.1% | UP leader 26; UP final 26 | 1/51 = 2.0% | 98.0% | UP leader 27; UP final 28 | 16.91 | [5.2%, 29.5%] | 0.618 |
| T-120 | 8/53 = 15.1% | 84.9% | UP leader 26; UP final 26 | 0/51 = 0.0% | 100.0% | UP leader 28; UP final 28 | 15.09 | [5.0%, 27.1%] | 0.798 |
| T-60 | 4/53 = 7.5% | 92.5% | UP leader 30; UP final 26 | 0/51 = 0.0% | 100.0% | UP leader 28; UP final 28 | 7.55 | [-0.8%, 17.9%] | 0.557 |
| T-30 | 3/53 = 5.7% | 94.3% | UP leader 29; UP final 26 | 0/51 = 0.0% | 100.0% | UP leader 28; UP final 28 | 5.66 | [-2.3%, 15.4%] | 0.480 |

最大 separation 在 **T-300：+20.61 pp**。T-60/T-30 的差異 CI 包含零；不把最後一分鐘的 4/53、3/53 當作已確認 regime effect。

## 6. Exact strike／official TWAP path

Exact strike：星期二 53/53；週末 51/51；全部五 checkpoints 的 strike 與同 run、as-of 已鎖定的 authoritative=true、strike_status=verified、strike_source=polymarket_crypto_price_twap_open 數值相同，且 twap_fresh=true。未使用 provisional raw-open 或任何 Binance proxy。

下表為 absolute 官方 TWAP-to-strike USD distance；bps 使用既有 twap_minus_strike_bps 定義 |TWAP−strike|/strike×10000；它與 required_move_bps 不同，也不是 required_move_sigma 的替代。格式為 N；median；[P25,P75]；P10/P90。

| Horizon | Weekday USD | Weekend USD | Weekday bps | Weekend bps |
| --- | --- | --- | --- | --- |
| T-300 | N=53; 46.483; [26.499, 81.592]; 9.384/107.200 | N=51; 34.542; [20.822, 60.717]; 14.088/89.563 | N=53; 5.422; [3.092, 9.509]; 1.096/12.445 | N=51; 4.070; [2.454, 7.149]; 1.661/10.569 |
| T-180 | N=53; 40.594; [23.937, 82.252]; 16.563/118.461 | N=51; 32.549; [20.411, 66.615]; 15.698/95.654 | N=53; 4.710; [2.796, 9.573]; 1.934/13.747 | N=51; 3.848; [2.407, 7.856]; 1.850/11.233 |
| T-120 | N=53; 44.843; [16.847, 74.952]; 4.344/103.273 | N=51; 33.240; [21.645, 69.788]; 16.340/94.837 | N=53; 5.220; [1.955, 8.737]; 0.507/12.018 | N=51; 3.897; [2.555, 8.225]; 1.927/11.137 |
| T-60 | N=53; 40.107; [23.881, 79.396]; 10.008/114.964 | N=51; 35.700; [21.436, 63.625]; 12.004/87.975 | N=53; 4.679; [2.779, 9.255]; 1.166/13.446 | N=51; 4.209; [2.523, 7.487]; 1.415/10.325 |
| T-30 | N=53; 41.599; [23.601, 77.989]; 9.602/122.318 | N=51; 35.965; [22.416, 64.688]; 11.462/82.479 | N=53; 4.856; [2.767, 9.049]; 1.122/14.307 | N=51; 4.217; [2.645, 7.632]; 1.354/9.726 |

| Window | Weekday ≥1 crossing | Wilson | Crossings/market distribution | Weekend ≥1 crossing | Wilson | Crossings/market distribution | 差 pp | Difference 95% |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| After T-300 | 17/53 = 32.1% | [21.1%, 45.5%] | N=53; 0.000; [0.000, 1.000]; 0.000/1.000 | 3/51 = 5.9% | [2.0%, 15.9%] | N=51; 0.000; [0.000, 0.000]; 0.000/0.000 | 26.19 | [11.3%, 40.1%] |
| After T-120 | 9/53 = 17.0% | [9.2%, 29.2%] | N=53; 0.000; [0.000, 0.000]; 0.000/1.000 | 0/51 = 0.0% | [0.0%, 7.0%] | N=51; 0.000; [0.000, 0.000]; 0.000/0.000 | 16.98 | [6.5%, 29.2%] |
| After T-60 | 4/53 = 7.5% | [3.0%, 17.9%] | N=53; 0.000; [0.000, 0.000]; 0.000/0.000 | 0/51 = 0.0% | [0.0%, 7.0%] | N=51; 0.000; [0.000, 0.000]; 0.000/0.000 | 7.55 | [-0.8%, 17.9%] |
| After T-30 | 3/53 = 5.7% | [1.9%, 15.4%] | N=53; 0.000; [0.000, 0.000]; 0.000/0.000 | 0/51 = 0.0% | [0.0%, 7.0%] | N=51; 0.000; [0.000, 0.000]; 0.000/0.000 | 5.66 | [-2.3%, 15.4%] |

Crossing 是可觀察離散樣本的 lower bound；不能補造兩筆 fresh observation 之間沒記錄的 crossings。一次市場可能 cross 後回到原側，因此 crossing≥1 與 terminal reversal 不相同，且 crossings 次數不是獨立 N。

正式 winner margin at settlement = NOT_MEASURABLE：canonical summary 保存 side/source/age，未保存 final reference price；不可把 journal 的 spot 欄位當作官方 TWAP final price。可另描述最後一筆 fresh official TWAP 的距離，但不升格為 final margin：

| 描述量 | Weekday | Weekend |
| --- | --- | --- |
| Last observed TWAP distance USD; NOT final settlement margin | N=53; 48.784; [27.508, 76.745]; 8.316/122.048 | N=51; 39.444; [24.825, 65.904]; 12.253/81.226 |

## 7. Market probability path

機率均為 UP 機率，而不是 leader probability；absolute move to settlement=|resolved UP indicator−checkpoint p_up|，為 terminal outcome distance，不代表實際成交價移動。Δ 是後一 checkpoint 減前一 checkpoint；T-300 無更早 canonical checkpoint，故 N/A。

| Cohort | Horizon | Market p_up | Absolute move to outcome | Δ from previous checkpoint |
| --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | T-300 | N=53; 0.565; [0.215, 0.835]; 0.069/0.955 | N=53; 0.265; [0.085, 0.415]; 0.045/0.849 | N=0; N/A; [N/A, N/A]; N/A/N/A |
| WEEKEND_PRIMARY | T-300 | N=51; 0.645; [0.055, 0.895]; 0.025/0.945 | N=51; 0.095; [0.045, 0.190]; 0.025/0.335 | N=0; N/A; [N/A, N/A]; N/A/N/A |
| PRIMARY_WEEKDAY | T-180 | N=53; 0.425; [0.155, 0.935]; 0.029/0.973 | N=53; 0.135; [0.035, 0.405]; 0.015/0.691 | N=53; 0.010; [-0.050, 0.080]; -0.168/0.186 |
| WEEKEND_PRIMARY | T-180 | N=51; 0.775; [0.035, 0.945]; 0.005/0.975 | N=51; 0.055; [0.015, 0.105]; 0.005/0.175 | N=51; 0.010; [-0.025, 0.050]; -0.070/0.120 |
| PRIMARY_WEEKDAY | T-120 | N=53; 0.565; [0.075, 0.925]; 0.011/0.994 | N=53; 0.075; [0.015, 0.175]; 0.005/0.535 | N=53; -0.003; [-0.060, 0.026]; -0.158/0.206 |
| WEEKEND_PRIMARY | T-120 | N=51; 0.775; [0.020, 0.975]; 0.005/0.995 | N=51; 0.025; [0.007, 0.060]; 0.004/0.155 | N=51; 0.010; [-0.006, 0.030]; -0.076/0.060 |
| PRIMARY_WEEKDAY | T-60 | N=53; 0.535; [0.005, 0.985]; 0.005/0.996 | N=53; 0.005; [0.005, 0.095]; 0.002/0.233 | N=53; 0.000; [-0.020, 0.033]; -0.136/0.148 |
| WEEKEND_PRIMARY | T-60 | N=51; 0.925; [0.005, 0.995]; 0.002/0.996 | N=51; 0.005; [0.003, 0.007]; 0.001/0.055 | N=51; 0.000; [-0.009, 0.033]; -0.033/0.080 |
| PRIMARY_WEEKDAY | T-30 | N=53; 0.395; [0.005, 0.995]; 0.001/0.999 | N=53; 0.005; [0.001, 0.005]; 0.001/0.023 | N=53; 0.000; [-0.004, 0.004]; -0.098/0.098 |
| WEEKEND_PRIMARY | T-30 | N=51; 0.995; [0.001, 0.995]; 0.001/0.999 | N=51; 0.001; [0.001, 0.005]; 0.001/0.005 | N=51; 0.000; [-0.001, 0.004]; -0.004/0.020 |

| Cohort | Canonical observed UP-mid max−min N | Median | IQR |
| --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 53 | 0.660 | [0.524, 0.784] |
| WEEKEND_PRIMARY | 51 | 0.494 | [0.459, 0.594] |

路徑波動只列既有 summary min/max market-mid 的觀察 range；未建新 realized-volatility 模型。range 不是年化波動率，且不同 sampling cadence／freshness 可能影響觀察 extrema。

## 8. p_ex vs market：divergence／calibration

p_ex 為既有 structural/fair-value reference，不是經證實 alpha／lead predictor。Signed divergence = p_up_ex_market − canonical fresh market UP probability。

| Cohort | Horizon | N | Signed mean | Signed median/IQR/P10/P90 | Mean absolute divergence | Absolute median/IQR/P10/P90 | Direction agreement |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | T-300 | 53 | -0.006 | N=53; -0.001; [-0.029, 0.030]; -0.060/0.041 | 0.037 | N=53; 0.029; [0.014, 0.041]; 0.004/0.084 | 52/53 = 98.1% |
| WEEKEND_PRIMARY | T-300 | 51 | -0.024 | N=51; -0.011; [-0.141, 0.071]; -0.175/0.142 | 0.106 | N=51; 0.115; [0.048, 0.151]; 0.010/0.190 | 49/51 = 96.1% |
| PRIMARY_WEEKDAY | T-180 | 53 | -0.002 | N=53; -0.004; [-0.022, 0.025]; -0.052/0.045 | 0.031 | N=53; 0.024; [0.009, 0.043]; 0.004/0.066 | 53/53 = 100.0% |
| WEEKEND_PRIMARY | T-180 | 51 | -0.023 | N=51; -0.005; [-0.135, 0.046]; -0.182/0.136 | 0.100 | N=51; 0.114; [0.018, 0.169]; 0.005/0.206 | 50/51 = 98.0% |
| PRIMARY_WEEKDAY | T-120 | 53 | -0.011 | N=53; -0.001; [-0.020, 0.011]; -0.077/0.048 | 0.040 | N=53; 0.016; [0.005, 0.060]; 0.002/0.097 | 52/53 = 98.1% |
| WEEKEND_PRIMARY | T-120 | 51 | -0.021 | N=51; -0.002; [-0.114, 0.036]; -0.152/0.144 | 0.088 | N=51; 0.095; [0.006, 0.147]; 0.002/0.175 | 50/51 = 98.0% |
| PRIMARY_WEEKDAY | T-60 | 53 | -0.003 | N=53; 0.001; [-0.012, 0.009]; -0.085/0.088 | 0.044 | N=53; 0.010; [0.004, 0.072]; 0.001/0.139 | 52/53 = 98.1% |
| WEEKEND_PRIMARY | T-60 | 51 | -0.031 | N=51; -0.001; [-0.068, 0.003]; -0.181/0.078 | 0.064 | N=51; 0.019; [0.002, 0.123]; 0.001/0.181 | 50/51 = 98.0% |
| PRIMARY_WEEKDAY | T-30 | 53 | -0.000 | N=53; 0.001; [-0.005, 0.005]; -0.005/0.005 | 0.010 | N=53; 0.005; [0.001, 0.005]; 0.001/0.024 | 53/53 = 100.0% |
| WEEKEND_PRIMARY | T-30 | 51 | -0.020 | N=51; -0.001; [-0.001, 0.001]; -0.005/0.005 | 0.022 | N=51; 0.001; [0.001, 0.005]; 0.001/0.005 | 50/51 = 98.0% |

| Horizon | Signed mean diff weekday−weekend | Market bootstrap 95% | Absolute mean diff | Market bootstrap 95% | Hour-matched abs diff | Bootstrap 95% |
| --- | --- | --- | --- | --- | --- | --- |
| T-300 | 0.018 | [-0.019, 0.055] | -0.069 | [-0.090, -0.047] | -0.093 | [-0.120, -0.066] |
| T-180 | 0.021 | [-0.014, 0.056] | -0.069 | [-0.092, -0.047] | -0.086 | [-0.114, -0.058] |
| T-120 | 0.010 | [-0.027, 0.047] | -0.048 | [-0.073, -0.023] | -0.065 | [-0.100, -0.030] |
| T-60 | 0.028 | [-0.005, 0.060] | -0.019 | [-0.046, 0.007] | -0.018 | [-0.053, 0.015] |
| T-30 | 0.019 | [-0.001, 0.048] | -0.013 | [-0.040, 0.008] | -0.030 | [-0.081, 0.002] |

4,000 market-level bootstrap draws，固定 seeds 20261006／60261006；每市場每 horizon 一筆，沒有 snapshot-row bootstrap。T-300/180/120 的 absolute divergence 平日較小，在小時配對仍同向；signed divergence 差異的 CI 全含零。結論：PEX_REGIME_DIFFERENCE=WEAK（差異主要是幅度，不是固定方向）；前段差異值得複驗，不可當交易訊號。

| Cohort | Horizon | Observed flips | Market N | p_ex N | Mean market flip p | Mean p_ex flip p | Market Brier | p_ex Brier | Paired N | Paired analytic−market Brier | Paired bootstrap 95% |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | T-300 | 13/53 = 24.5% | 31 | 53 | 0.214 | 0.193 | 0.198 | 0.178 | 31 | -0.015 | [-0.033, 0.002] |
| WEEKEND_PRIMARY | T-300 | 2/51 = 3.9% | 36 | 51 | 0.158 | 0.229 | 0.049 | 0.081 | 36 | 0.036 | [0.016, 0.053] |
| PRIMARY_WEEKDAY | T-180 | 10/53 = 18.9% | 31 | 53 | 0.146 | 0.154 | 0.170 | 0.136 | 31 | 0.006 | [-0.007, 0.018] |
| WEEKEND_PRIMARY | T-180 | 1/51 = 2.0% | 31 | 51 | 0.065 | 0.170 | 0.026 | 0.046 | 31 | 0.014 | [-0.012, 0.034] |
| PRIMARY_WEEKDAY | T-120 | 8/53 = 15.1% | 38 | 53 | 0.179 | 0.163 | 0.060 | 0.082 | 38 | 0.002 | [-0.015, 0.017] |
| WEEKEND_PRIMARY | T-120 | 0/51 = 0.0% | 35 | 51 | 0.079 | 0.137 | 0.027 | 0.036 | 35 | 0.014 | [-0.008, 0.030] |
| PRIMARY_WEEKDAY | T-60 | 4/53 = 7.5% | 33 | 53 | 0.109 | 0.108 | 0.048 | 0.031 | 33 | -0.009 | [-0.037, 0.013] |
| WEEKEND_PRIMARY | T-60 | 0/51 = 0.0% | 32 | 51 | 0.051 | 0.088 | 0.026 | 0.024 | 32 | 0.005 | [-0.013, 0.021] |
| PRIMARY_WEEKDAY | T-30 | 3/53 = 5.7% | 31 | 53 | 0.059 | 0.055 | 0.005 | 0.005 | 31 | 0.002 | [0.000, 0.006] |
| WEEKEND_PRIMARY | T-30 | 0/51 = 0.0% | 30 | 51 | 0.032 | 0.015 | 0.015 | 0.005 | 30 | -0.007 | [-0.041, 0.017] |

Weekday T-300 paired Brier diff −0.01454，CI 包含零；T-180/T-120/T-60 同樣不足以穩定區分；T-30 雖有很小正差，屬 checkpoint-specific exploratory 結果，非整體 outperform 證明。週末 T-300 market calibration 較佳，但不能推定平日已複現；本輪平日評價 MIXED / INSUFFICIENT_N。

## 9. Sigma／bps 完整 subset

只使用 58 個合格平日中的 36 個 all-five non-null 市場（星期二 32、星期一 4），以及 51 個合格週末中的 30 個等價 complete 市場。其餘 22 個平日／21 個週末仍保留在 basic flip/path cohort。缺失全部在最近 T-60：模式 FINAL_WINDOW_RAW_UNAVAILABLE / required_move_mode=UNAVAILABLE / remaining_final_window_sec=60.0，原因是最後窗 raw integral history 尚不可用，不是把 null 補成零。

required_move_bps 依既有 entry-stop 報告採 absolute 值；payload 原本有正負，不能只分箱正值。sigma bins 既有 <0.5 / 0.5–1 / 1–2 / 2–3 / 3–5 / >5；bps bins 0–2 / 2–5 / 5–10 / 10–20 / >20，zero、missing 另列。以既有 lower-inclusive/upper-exclusive 分界（0 單獨）；最後標記 >5/>20 延用 canonical 名稱，包含分界值。未最佳化任何門檻。

| Complete cohort | Horizon | Sigma distribution | Absolute bps distribution |
| --- | --- | --- | --- |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | N=36; 1.069; [0.702, 1.811]; 0.437/2.629 | N=36; 6.585; [4.325, 11.180]; 2.698/16.264 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | N=30; 0.661; [0.401, 1.286]; 0.286/1.492 | N=30; 4.077; [2.477, 7.931]; 1.755/9.190 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | N=36; 1.513; [1.005, 2.259]; 0.391/3.442 | N=36; 7.235; [4.790, 10.818]; 1.868/16.421 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | N=30; 0.857; [0.466, 1.738]; 0.302/2.294 | N=30; 4.095; [2.226, 8.354]; 1.442/10.971 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | N=36; 1.824; [1.018, 2.789]; 0.347/3.731 | N=36; 7.105; [3.974, 11.036]; 1.354/14.605 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | N=30; 0.953; [0.567, 1.824]; 0.334/2.826 | N=30; 3.718; [2.206, 7.120]; 1.300/11.057 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | N=36; 2.500; [1.299, 4.090]; 0.481/6.160 | N=36; 6.974; [3.593, 11.594]; 1.332/17.119 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | N=30; 1.659; [0.703, 2.164]; 0.467/3.455 | N=30; 4.612; [1.942, 6.054]; 1.300/9.662 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | N=36; 6.683; [3.854, 10.553]; 1.530/18.914 | N=36; 13.804; [7.701, 21.005]; 3.090/36.659 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | N=30; 4.732; [2.109, 7.690]; 1.418/9.086 | N=30; 9.180; [4.129, 14.932]; 2.815/17.923 |

### 固定 sigma bins

| Cohort | Horizon | Bin | Flips/N | Wilson 95% | Mean p_ex−market | Divergence N |
| --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | <0.5 | 2/4 = 50.0% | [15.0%, 85.0%] | -0.035 | 4 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 0.5-1 | 2/12 = 16.7% | [4.7%, 44.8%] | 0.002 | 12 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 1-2 | 2/13 = 15.4% | [4.3%, 42.2%] | 0.004 | 13 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 2-3 | 1/5 = 20.0% | [3.6%, 62.4%] | 0.026 | 5 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 3-5 | 0/2 = 0.0% | [0.0%, 65.8%] | -0.022 | 2 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | >5 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | <0.5 | 1/10 = 10.0% | [1.8%, 40.4%] | -0.028 | 10 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 0.5-1 | 0/10 = 0.0% | [0.0%, 27.8%] | -0.072 | 10 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 1-2 | 0/8 = 0.0% | [0.0%, 32.4%] | 0.024 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 2-3 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.011 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 3-5 | 0/1 = 0.0% | [0.0%, 79.3%] | 0.006 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | >5 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | <0.5 | 3/6 = 50.0% | [18.8%, 81.2%] | 0.010 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 0.5-1 | 0/3 = 0.0% | [0.0%, 56.1%] | 0.001 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 1-2 | 2/14 = 14.3% | [4.0%, 39.9%] | -0.014 | 14 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 2-3 | 0/9 = 0.0% | [0.0%, 29.9%] | 0.011 | 9 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 3-5 | 1/3 = 33.3% | [6.1%, 79.2%] | 0.013 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | >5 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.004 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | <0.5 | 1/8 = 12.5% | [2.2%, 47.1%] | -0.018 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 0.5-1 | 0/11 = 0.0% | [0.0%, 25.9%] | -0.054 | 11 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 1-2 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.004 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 2-3 | 0/3 = 0.0% | [0.0%, 56.1%] | -0.007 | 3 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 3-5 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | >5 | 0/1 = 0.0% | [0.0%, 79.3%] | 0.002 | 1 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | <0.5 | 2/6 = 33.3% | [9.7%, 70.0%] | -0.007 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 0.5-1 | 0/3 = 0.0% | [0.0%, 56.1%] | 0.027 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 1-2 | 2/12 = 16.7% | [4.7%, 44.8%] | -0.002 | 12 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 2-3 | 1/7 = 14.3% | [2.6%, 51.3%] | -0.017 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 3-5 | 0/6 = 0.0% | [0.0%, 39.0%] | 0.001 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | >5 | 0/2 = 0.0% | [0.0%, 65.8%] | -0.001 | 2 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | <0.5 | 0/7 = 0.0% | [0.0%, 35.4%] | -0.073 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 0.5-1 | 0/9 = 0.0% | [0.0%, 29.9%] | -0.036 | 9 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 1-2 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.012 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 2-3 | 0/5 = 0.0% | [0.0%, 43.4%] | -0.002 | 5 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 3-5 | 0/2 = 0.0% | [0.0%, 65.8%] | -0.004 | 2 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | >5 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | <0.5 | 0/5 = 0.0% | [0.0%, 43.4%] | 0.011 | 5 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 0.5-1 | 1/3 = 33.3% | [6.1%, 79.2%] | 0.032 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 1-2 | 1/6 = 16.7% | [3.0%, 56.4%] | -0.012 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 2-3 | 0/7 = 0.0% | [0.0%, 35.4%] | -0.001 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 3-5 | 0/9 = 0.0% | [0.0%, 29.9%] | 0.001 | 9 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | >5 | 0/6 = 0.0% | [0.0%, 39.0%] | -0.002 | 6 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | <0.5 | 0/5 = 0.0% | [0.0%, 43.4%] | -0.136 | 5 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 0.5-1 | 0/6 = 0.0% | [0.0%, 39.0%] | -0.030 | 6 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 1-2 | 0/8 = 0.0% | [0.0%, 32.4%] | -0.002 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 2-3 | 0/4 = 0.0% | [0.0%, 49.0%] | -0.001 | 4 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 3-5 | 0/6 = 0.0% | [0.0%, 39.0%] | -0.001 | 6 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | >5 | 0/1 = 0.0% | [0.0%, 79.3%] | 0.001 | 1 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | <0.5 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 0.5-1 | 1/3 = 33.3% | [6.1%, 79.2%] | -0.081 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 1-2 | 0/2 = 0.0% | [0.0%, 65.8%] | 0.002 | 2 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 2-3 | 0/2 = 0.0% | [0.0%, 65.8%] | 0.005 | 2 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 3-5 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.001 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | >5 | 0/22 = 0.0% | [0.0%, 14.9%] | -0.000 | 22 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | <0.5 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.261 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 0.5-1 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.139 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 1-2 | 0/5 = 0.0% | [0.0%, 43.4%] | -0.002 | 5 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 2-3 | 0/2 = 0.0% | [0.0%, 65.8%] | 0.005 | 2 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 3-5 | 0/8 = 0.0% | [0.0%, 32.4%] | 0.001 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | >5 | 0/13 = 0.0% | [0.0%, 22.8%] | -0.001 | 13 |

### 固定 bps bins

| Cohort | Horizon | Bin | Flips/N | Wilson 95% | Mean p_ex−market | Divergence N |
| --- | --- | --- | --- | --- | --- | --- |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 0-2 | 1/3 = 33.3% | [6.1%, 79.2%] | -0.060 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 2-5 | 1/8 = 12.5% | [2.2%, 47.1%] | 0.003 | 8 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 5-10 | 3/15 = 20.0% | [7.0%, 45.2%] | 0.006 | 15 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | 10-20 | 2/10 = 20.0% | [5.7%, 51.0%] | 0.009 | 10 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-300 | >20 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 0-2 | 1/6 = 16.7% | [3.0%, 56.4%] | -0.015 | 6 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 2-5 | 0/13 = 0.0% | [0.0%, 22.8%] | -0.079 | 13 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 5-10 | 0/8 = 0.0% | [0.0%, 32.4%] | 0.043 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | 10-20 | 0/3 = 0.0% | [0.0%, 56.1%] | -0.012 | 3 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-300 | >20 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 0-2 | 1/4 = 25.0% | [4.6%, 69.9%] | 0.022 | 4 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 2-5 | 3/7 = 42.9% | [15.8%, 75.0%] | 0.005 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 5-10 | 1/12 = 8.3% | [1.5%, 35.4%] | -0.021 | 12 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | 10-20 | 1/12 = 8.3% | [1.5%, 35.4%] | 0.011 | 12 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-180 | >20 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.004 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 0-2 | 1/7 = 14.3% | [2.6%, 51.3%] | -0.040 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 2-5 | 0/12 = 0.0% | [0.0%, 24.2%] | -0.038 | 12 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 5-10 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.004 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | 10-20 | 0/3 = 0.0% | [0.0%, 56.1%] | -0.007 | 3 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-180 | >20 | 0/1 = 0.0% | [0.0%, 79.3%] | 0.002 | 1 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 0-2 | 2/6 = 33.3% | [9.7%, 70.0%] | -0.007 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 2-5 | 0/6 = 0.0% | [0.0%, 39.0%] | 0.014 | 6 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 5-10 | 2/13 = 15.4% | [4.3%, 42.2%] | -0.001 | 13 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | 10-20 | 1/9 = 11.1% | [2.0%, 43.5%] | -0.014 | 9 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-120 | >20 | 0/2 = 0.0% | [0.0%, 65.8%] | -0.001 | 2 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 0-2 | 0/7 = 0.0% | [0.0%, 35.4%] | -0.073 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 2-5 | 0/12 = 0.0% | [0.0%, 24.2%] | -0.022 | 12 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 5-10 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.001 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | 10-20 | 0/4 = 0.0% | [0.0%, 49.0%] | -0.001 | 4 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-120 | >20 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 0-2 | 1/7 = 14.3% | [2.6%, 51.3%] | 0.027 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 2-5 | 1/7 = 14.3% | [2.6%, 51.3%] | -0.016 | 7 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 5-10 | 0/10 = 0.0% | [0.0%, 27.8%] | 0.001 | 10 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | 10-20 | 0/11 = 0.0% | [0.0%, 25.9%] | -0.001 | 11 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-60 | >20 | 0/1 = 0.0% | [0.0%, 79.3%] | -0.001 | 1 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 0-2 | 0/8 = 0.0% | [0.0%, 32.4%] | -0.105 | 8 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 2-5 | 0/9 = 0.0% | [0.0%, 29.9%] | -0.003 | 9 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 5-10 | 0/10 = 0.0% | [0.0%, 27.8%] | -0.001 | 10 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | 10-20 | 0/3 = 0.0% | [0.0%, 56.1%] | -0.001 | 3 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-60 | >20 | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 0-2 | 1/3 = 33.3% | [6.1%, 79.2%] | -0.081 | 3 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 2-5 | 0/2 = 0.0% | [0.0%, 65.8%] | 0.002 | 2 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 5-10 | 0/9 = 0.0% | [0.0%, 29.9%] | 0.002 | 9 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | 10-20 | 0/12 = 0.0% | [0.0%, 24.2%] | -0.000 | 12 |
| WEEKDAY_COMPLETE_SIGMA_BPS | T-30 | >20 | 0/10 = 0.0% | [0.0%, 27.8%] | -0.001 | 10 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | zero | 0/0 = N/A | N/A | N/A | 0 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 0-2 | 0/2 = 0.0% | [0.0%, 65.8%] | -0.200 | 2 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 2-5 | 0/7 = 0.0% | [0.0%, 35.4%] | 0.000 | 7 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 5-10 | 0/9 = 0.0% | [0.0%, 29.9%] | 0.000 | 9 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | 10-20 | 0/10 = 0.0% | [0.0%, 27.8%] | -0.000 | 10 |
| WEEKEND_COMPLETE_SIGMA_BPS | T-30 | >20 | 0/2 = 0.0% | [0.0%, 65.8%] | 0.000 | 2 |

T-300 平日 <0.5σ 為 2/4、0.5–1 為 2/12、1–2 為 2/13；週末相對為 1/10、0/10、0/8。低 σ 區點估計較高，但平日最低 bin 僅 4 個，不能稱 SUPPORTED。3–5/>5 是稀少描述性 bins，不要求等待其 N=10。all-five complete 條件選樣依賴 T-60 availability，並非隨機缺失；不能把此 subset 的分箱結果直接概括全 53/58 市場。

## 10. 時段 confounding：Taipei 與 ET

| Cohort | Taipei hour:markets | ET hour:markets | ET calendar-date composition |
| --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 02:2, 03:4, 04:3, 05:3, 06:3, 07:3, 08:3, 09:3, 10:3, 11:4, 12:4, 13:3, 14:4, 15:1, 17:3, 18:3, 19:4 | 00:4, 01:3, 02:4, 03:1, 05:3, 06:3, 07:4, 14:2, 15:4, 16:3, 17:3, 18:3, 19:3, 20:3, 21:3, 22:3, 23:4 | {'2026-10-05 Monday': 31, '2026-10-06 Tuesday': 22} |
| WEEKDAY_SENSITIVITY | 00:1, 01:1, 02:2, 03:4, 04:4, 05:4, 06:3, 07:3, 08:3, 09:3, 10:3, 11:4, 12:4, 13:3, 14:4, 15:1, 17:3, 18:3, 19:5 | 00:4, 01:3, 02:4, 03:1, 05:3, 06:3, 07:5, 12:1, 13:1, 14:2, 15:4, 16:4, 17:4, 18:3, 19:3, 20:3, 21:3, 22:3, 23:4 | {'2026-10-05 Monday': 32, '2026-10-06 Tuesday': 22, '2026-10-04 Sunday': 4} |
| WEEKDAY_COMPLETE_SIGMA_BPS | 00:1, 01:1, 03:2, 04:1, 05:4, 06:1, 07:2, 08:3, 09:2, 10:3, 11:2, 12:2, 13:1, 14:3, 15:1, 17:2, 18:2, 19:3 | 00:2, 01:1, 02:3, 03:1, 05:2, 06:2, 07:3, 12:1, 13:1, 15:2, 16:1, 17:4, 18:1, 19:2, 20:3, 21:2, 22:3, 23:2 | {'2026-10-05 Monday': 20, '2026-10-06 Tuesday': 13, '2026-10-04 Sunday': 3} |
| LATEST_RUNTIME_ONLY | 17:1, 18:3, 19:4 | 05:1, 06:3, 07:4 | {'2026-10-06 Tuesday': 8} |
| WEEKEND_PRIMARY | 00:3, 01:1, 02:4, 03:3, 04:1, 05:2, 06:2, 07:1, 09:2, 10:2, 11:1, 12:2, 13:2, 14:2, 15:1, 16:3, 17:1, 18:1, 19:5, 20:5, 21:3, 22:3, 23:1 | 00:2, 01:2, 02:2, 03:1, 04:3, 05:1, 06:1, 07:5, 08:5, 09:3, 10:3, 11:1, 12:3, 13:1, 14:4, 15:3, 16:1, 17:2, 18:2, 19:1, 21:2, 22:2, 23:1 | {'2026-10-02 Friday': 3, '2026-10-03 Saturday': 37, '2026-10-04 Sunday': 11} |

使用 ZoneInfo(Asia/Taipei) 與 ZoneInfo(America/New_York)；當期 ET=EDT UTC−4，與台北相差 12 小時。原 regime 標籤維持台北日期：週末 cohort 中 3 個市場在 ET 仍是星期五；不偷偷改為美東 weekend 定義。

Exact hour-count matching：每台北 hour 取雙方可用數較小值，先按 slug/開盤時間排序、再取相同數量；選樣規則在看 outcome 前固定，不依翻轉結果挑樣本。配對結果 weekday=29、weekend=29，hour 分布完全相同：02:2, 03:3, 04:1, 05:2, 06:2, 07:1, 09:2, 10:2, 11:1, 12:2, 13:2, 14:2, 15:1, 17:1, 18:1, 19:4。N<30=TOO_SMALL，不能以配對丟失顯著性證明沒有 regime 差異。

| Horizon | Matched weekday | Wilson | Matched weekend | Wilson | 差 pp | Difference 95% | Fisher p |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-300 | 7/29 = 24.1% | [12.2%, 42.1%] | 1/29 = 3.4% | [0.6%, 17.2%] | 20.69 | [2.5%, 38.9%] | 0.05171 |
| T-180 | 5/29 = 17.2% | [7.6%, 34.5%] | 1/29 = 3.4% | [0.6%, 17.2%] | 13.79 | [-3.0%, 31.3%] | 0.19364 |
| T-120 | 3/29 = 10.3% | [3.6%, 26.4%] | 0/29 = 0.0% | [0.0%, 11.7%] | 10.34 | [-3.2%, 26.4%] | 0.23684 |
| T-60 | 2/29 = 6.9% | [1.9%, 22.0%] | 0/29 = 0.0% | [0.0%, 11.7%] | 6.90 | [-5.8%, 22.0%] | 0.49123 |
| T-30 | 1/29 = 3.4% | [0.6%, 17.2%] | 0/29 = 0.0% | [0.0%, 11.7%] | 3.45 | [-8.6%, 17.2%] | 1.00000 |

T-300 matched=7/29 vs 1/29，Newcombe CI 略高於零，但 discrete Fisher p=0.05171；不同方法在邊界附近不一致，不宣稱配對後獲得強確認。T-120=3/29 vs 0/29，差異 CI 包含零。After T-300 crossing=9/29 vs 2/29；After T-120=4/29 vs 0/29，後者 CI 亦含零。TIME_MATCHED_RESULT=PARTIALLY。

| Horizon | Common-hour weekday N/rate | Hour-restricted weekend N | Weekday-hour-weighted weekend rate |
| --- | --- | --- | --- |
| T-300 | 50 / 26.0% | 32 | 3.0% |
| T-180 | 50 / 20.0% | 32 | 4.0% |
| T-120 | 50 / 16.0% | 32 | 0.0% |
| T-60 | 50 / 8.0% | 32 | 0.0% |
| T-30 | 50 / 6.0% | 32 | 0.0% |

Hour-weighted 僅描述性補充：50 個 weekday、32 個 weekend 位於 overlap hours；按 weekday hour frequency 加權 weekend hour rates，T-300=26.0% vs weighted 3.0%。不是 50 個獨立 weekend 市場，不替加權值虛構 Wilson N。小時配對只能處理時段，不能控制波動環境／選樣／日間群聚／runtime。

## 11. 既有 volatility／starting-state confounders

| 既有指標／每市場一個摘要 | Weekday | Weekend |
| --- | --- | --- |
| btc_abs_return_5s_bps_median | N=53; 0.001; [0.001, 0.001]; 0.001/0.036 | N=51; 0.001; [0.001, 0.001]; 0.000/0.001 |
| btc_abs_return_10s_bps_median | N=53; 0.215; [0.001, 0.364]; 0.001/0.691 | N=51; 0.001; [0.001, 0.001]; 0.001/0.001 |
| btc_abs_return_30s_bps_median | N=53; 1.089; [0.693, 1.319]; 0.394/1.755 | N=51; 0.001; [0.001, 0.174]; 0.001/0.710 |
| start_market | N=53; 0.475; [0.395, 0.525]; 0.345/0.563 | N=51; 0.515; [0.460, 0.565]; 0.425/0.635 |
| start_pex | N=53; 0.465; [0.413, 0.528]; 0.343/0.583 | N=51; 0.503; [0.493, 0.516]; 0.449/0.565 |
| start_distance | N=53; 5.056; [1.360, 8.822]; 0.633/17.512 | N=51; 1.960; [0.271, 4.573]; 0.069/8.328 |
| start_elapsed | N=53; 22.505; [21.555, 25.371]; 21.337/29.744 | N=51; 24.832; [21.981, 27.791]; 20.992/38.900 |
| median_spread | N=53; 0.010; [0.010, 0.010]; 0.010/0.010 | N=51; 0.010; [0.010, 0.010]; 0.010/0.010 |
| median_top_size | N=0; N/A; [N/A, N/A]; N/A/N/A | N=0; N/A; [N/A, N/A]; N/A/N/A |

| Cohort | First fresh observation opening direction |
| --- | --- |
| PRIMARY_WEEKDAY | {'DOWN': 31, 'UP': 22} |
| WEEKEND_PRIMARY | {'UP': 28, 'DOWN': 23} |

Starting state 是每市場 first joint-fresh row，不宣稱嚴格 T=900；elapsed seconds 另列。既有 BTC absolute returns 是行情變動 proxy，非 realized-volatility estimator；保存資料的更新 cadence／近乎一個 tick 的 floor 亦可能影響。星期二 30s abs-return median 約 1.089 bps、週末約 0.00118 bps，starting market p／distance 也不同，應標記 **BASELINE_CONFOUNDING_PRESENT**。Spread median 同為 0.01；top depth-size 在 payload 無值，因此 N=0，不把 spread 相同當作 liquidity 完全相同。無外部 price feed／新指標。

## 12. Shadow／simulated strategy evidence：正式績效比較省略

| Cohort | Unique SHADOW_SIM_ENTRY_CANDIDATE simulation IDs | Dedup settled entries | Unique entry markets | Markets with >1 entry | Shadow vs canonical label conflicts |
| --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 51 | 47 | 47 | 0 | 5 |
| WEEKEND_PRIMARY | 31 | 16 | 16 | 0 | 0 |
| WEEKDAY_SENSITIVITY | 55 | 49 | 49 | 0 | 5 |
| LATEST_RUNTIME_ONLY | 8 | 8 | 8 | 0 | 0 |

Dedup 沿既有 _read_shadow_settlements 的 simulation_id／fallback 規則，保留最後 event id，並限制同 market 的 admitted run 與 cutoff。不是將 MAIN_SIGNAL_CANDIDATE_LIVE／頻繁 requote 當作獨立 entries。

**MEASUREMENT_SEMANTICS_NOT_CONFIRMED：不做正式 win rate / gross EV / net EV / profit factor / loss-tail / time-to-TP / stop-counterfactual 比較。** 星期二 settled shadow 的 size_multiplier 包含 1.0=17、0.55=30；週末 16 個均為 0.55。TTL=25s、fresh quote threshold=2s、fee/rebate=0 雖相同，不能證明 sizing／fill authority 版本完全相同；週末 manifests 缺失。

更直接的測量問題：_record_market_settlement 使用 general-purpose external spot 先結算 shadow；canonical research summary 用 direct official 60s TWAP cache。星期二 5/53 市場、其中 5/47 shadow entries 的 journal/shadow outcome 與 canonical label 不同，週末目前 0/51、0/16。此差異不是用另一套標籤重算交易輸贏的授權；本輪只識別並省略不可比績效。

| 台北市場開盤 | Market slug | Journal／shadow side | Canonical TWAP side |
| --- | --- | --- | --- |
| 2026-10-06T03:30:00.000+08:00 | btc-updown-15m-1791228600 | UP | DOWN |
| 2026-10-06T03:45:00.000+08:00 | btc-updown-15m-1791229500 | DOWN | UP |
| 2026-10-06T04:45:00.000+08:00 | btc-updown-15m-1791233100 | UP | DOWN |
| 2026-10-06T07:00:00.000+08:00 | btc-updown-15m-1791241200 | UP | DOWN |
| 2026-10-06T14:45:00.000+08:00 | btc-updown-15m-1791269100 | UP | DOWN |

部分 journal reference_source 字串也標為 TWAP，但 spot/outcome 並未引用研究 summary 的 direct-TWAP label；僅看來源字串不足以確認 measurement semantics。市場 flip/path 全程使用 canonical research summary，沒有混入這些 journal/shadow labels。精確 final margin 同樣不能從 journal spot 升格。此為獨立研究語義追蹤點；本輪未修程式、未改 writer／live settlement。

## 13. Runtime-version sensitivity

| Horizon | All clean Tuesday | ad7cc22-only | Subset Wilson | All distance USD | Subset distance USD |
| --- | --- | --- | --- | --- | --- |
| T-300 | 13/53 = 24.5% | 2/8 = 25.0% | [7.1%, 59.1%] | N=53; 46.483; [26.499, 81.592]; 9.384/107.200 | N=8; 54.527; [23.559, 97.025]; 8.149/118.108 |
| T-180 | 10/53 = 18.9% | 2/8 = 25.0% | [7.1%, 59.1%] | N=53; 40.594; [23.937, 82.252]; 16.563/118.461 | N=8; 36.096; [25.213, 116.498]; 15.080/128.992 |
| T-120 | 8/53 = 15.1% | 1/8 = 12.5% | [2.2%, 47.1%] | N=53; 44.843; [16.847, 74.952]; 4.344/103.273 | N=8; 46.755; [12.904, 69.866]; 0.804/89.454 |
| T-60 | 4/53 = 7.5% | 0/8 = 0.0% | [0.0%, 32.4%] | N=53; 40.107; [23.881, 79.396]; 10.008/114.964 | N=8; 44.600; [36.117, 55.654]; 27.929/70.163 |
| T-30 | 3/53 = 5.7% | 0/8 = 0.0% | [0.0%, 32.4%] | N=53; 41.599; [23.601, 77.989]; 9.602/122.318 | N=8; 52.311; [43.519, 64.625]; 33.408/83.489 |

ad7cc22=8 個 clean 市場，T-300 2/8=25.0%，接近全星期二 24.5%；T-120 1/8=12.5%，接近全星期二 15.1%。T-60/30 在 subset 為零，N 太小，不構成 runtime 改變市場行為證據。RUNTIME_VERSION_SENSITIVITY=INSUFFICIENT_N（未確認 sensitivity present）；最新 subset 僅涵蓋台北 17–19 時，也不能與全天 runtime revision 脫鉤。它是主樣本子集，不能當作兩個獨立 cohort 做差異檢定。

## 14. Qualified Monday sensitivity

| Horizon | Tuesday only | Monday+Tuesday | Sensitivity−weekend pp | Difference 95% | Fisher p |
| --- | --- | --- | --- | --- | --- |
| T-300 | 13/53 = 24.5% | 14/58 = 24.1% | 20.22 | [7.2%, 32.9%] | 0.00283 |
| T-180 | 10/53 = 18.9% | 11/58 = 19.0% | 17.00 | [5.4%, 29.0%] | 0.00492 |
| T-120 | 8/53 = 15.1% | 9/58 = 15.5% | 15.52 | [5.5%, 26.9%] | 0.00321 |
| T-60 | 4/53 = 7.5% | 5/58 = 8.6% | 8.62 | [0.1%, 18.6%] | 0.05933 |
| T-30 | 3/53 = 5.7% | 4/58 = 6.9% | 6.90 | [-1.3%, 16.4%] | 0.12118 |

T-300：13/53→14/58，與週末差 +20.61→+20.22 pp；T-120：8/53→9/58，差 +15.09→+15.52 pp。主要方向、幅度不翻轉，PRIMARY 結論由星期二主導。T-60 的 Newcombe CI 邊界與 Fisher p=0.05933 不一致，不用方法切換製造顯著性。MONDAY_SENSITIVITY_RESULT=YES。

## 15. Sample size／power

PRIMARY_WEEKDAY_N=53 → **MID_CHECK_READY**。Sensitivity 58 不替代 primary；complete structural 36=EARLY_DIRECTIONAL；latest 8=TOO_SMALL；exact matched 29=TOO_SMALL。市場數達 mid-check 不代表窄 bins／跨日 regime power 足夠。繼續收集到至少 **100 個同門檻 clean weekday markets**，再作 formal checkpoint；至少還需 47 個，且應跨更多完整平日而非只增加同一天 snapshot rows。

## 16. Final regime verdict

**WEEKDAY_WEEKEND_DIFFERENCE_WEAK**。核心市場 schema 相容、same-gate cohort 可比；全樣本有明顯前中段 flip/crossing 與 absolute p_ex-market divergence 差異。但時段精確配對 N=29、僅一個主要平日、baseline volatility／起始狀態不同、collection selection 與 legacy provenance 仍限制因果及外推。不是 NO DIFFERENCE，也不是已證實一個可交易 regime。

| 問題 | 答案 | 證據／限制 |
| --- | --- | --- |
| T-5m flip meaningfully different? | WEAK | 13/53 vs 2/51；全樣本 CI 排除零，hour-matched Fisher 約0.052、N29 |
| T-2m flip meaningfully different? | WEAK | 8/53 vs 0/51；exact hour matching 3/29 vs 0/29，CI 含零 |
| Late strike crossing different? | WEAK | After T-300/T-120 差異較明顯；T-60/T-30 尚不足 |
| p_ex divergence differ by regime? | WEAK | absolute divergence 前三個 horizons較小，signed mean differences不確定 |
| Results survive time-of-day matching? | PARTIALLY | 方向保留、absolute divergence保留；T-120 flip仍underpowered |
| Results survive Tuesday vs Monday+Tuesday? | YES | T-300/T-120 direction and magnitude stable |

## 17. Research decision

**REGIME_SIGNAL_WORTH_FORMAL_VALIDATION**。維持現行策略與收集，先累積 100 個同 gate、跨更多完整 weekday 的市場，再重新 gate 週末並複驗。另追蹤 canonical TWAP vs shadow/journal label 語義差異，不把它混成市場 regime effect；如要修改程式需另行授權，這份 read-only analysis 不實施修補。STRATEGY_CHANGE_RECOMMENDED=NO。

## 18. Reproducibility／output／safety

Source authorities：scripts/research_analysis.py 的 market_context、_checkpoint_row、checkpoint_flip_rows、_read_shadow_settlements、entry-stop absolute-bps／fixed bins；scripts/four_market_prediction_forensics.py 的 _up_mid／_joint_rows；scripts/strike_flip_risk_analysis.py 的 crossing_count；bot/lifecycle_runtime.py 的 _canonical_twap_shadow_label／_record_market_settlement；bot/spot_pricer.py 的 FINAL_WINDOW_RAW_UNAVAILABLE。分析只呼叫純計算 helper，不啟動策略。

讀取方式：原盤點 JSON 的固定 cutoff/gate，加上 sqlite3 mode=ro、PRAGMA query_only=ON、BEGIN 的只讀 query，讀取既有 research payload／journal 並限制 timestamps≤cutoff。未 cp DB、未執行 VACUUM／migration／index／WAL checkpoint；不是新建 offline SQLite snapshot，亦不假稱用了新 snapshot。兩個 source DB 的 enrich 交易非跨 DB 原子；固定 cutoff與合格 run/slug约束減少錯配，晚到的歷史時間列理論上仍可能存在，截止点 N/gate已對照原固定盘点完全一致。

唯一 repo 新輸出為本 Markdown；one-off scripts/JSON 僅在 /private/tmp，未新增 repo source 檔或 CSV。驗證 N=53/58/36/51/8、每個 admitted market 五 checkpoints、所有固定分箱 N 合計等於 complete cohort、core payload signature差異僅兩個metadata keys。

| Safety | 結果 |
| --- | --- |
| Source/config modified? | NO |
| Strategy/parameters modified? | NO |
| Commit/push? | NO/NO |
| Bot stopped/restarted/another launched? | NO/NO/NO |
| Active DB mutated by analysis? | NO |
| Active Parquet mutated? | NO |
| External price data / Binance proxy strike? | NO |
| Generated report untracked? | YES |

## Final compact summary

```text
PRIMARY_WEEKDAY_N=53
WEEKDAY_SENSITIVITY_N=58
WEEKDAY_COMPLETE_SIGMA_BPS_N=36
WEEKEND_PRIMARY_N=51
LATEST_RUNTIME_ONLY_N=8
SAMPLE_STATUS=MID_CHECK_READY
T5_WEEKDAY_RATE=13/53 = 24.5%
T5_WEEKEND_RATE=2/51 = 3.9%
T5_DIFF_PP=20.61
T5_EFFECT=RR=6.255; Cohen_h=0.638; provisional
T2_WEEKDAY_RATE=8/53 = 15.1%
T2_WEEKEND_RATE=0/51 = 0.0%
T2_DIFF_PP=15.09
T2_EFFECT=RR=UNDEFINED; Cohen_h=0.798; provisional
LARGEST_REVERSAL_DIFFERENCE_HORIZON=T-300
EXACT_STRIKE_WEEKDAY_N=53
EXACT_STRIKE_WEEKEND_N=51
PEX_REGIME_DIFFERENCE=WEAK
TIME_MATCHED_RESULT=PARTIALLY
MONDAY_SENSITIVITY_RESULT=YES
RUNTIME_VERSION_SENSITIVITY=INSUFFICIENT_N
REGIME_VERDICT=WEEKDAY_WEEKEND_DIFFERENCE_WEAK
RESEARCH_DECISION=REGIME_SIGNAL_WORTH_FORMAL_VALIDATION
STRATEGY_CHANGE_RECOMMENDED=NO
```

## Appendix：admitted market audit

| Cohort | Taipei open | Slug | Coverage class | Run ID | Revision | All-five sigma/bps |
| --- | --- | --- | --- | --- | --- | --- |
| PRIMARY_WEEKDAY | 10-06T02:30 | btc-updown-15m-1791225000 | GOOD | run_1791224107_573fabab | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T02:45 | btc-updown-15m-1791225900 | GOOD | run_1791224107_573fabab | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T03:00 | btc-updown-15m-1791226800 | FULL | run_1791224107_573fabab | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T03:15 | btc-updown-15m-1791227700 | GOOD | run_1791227746_2be3a494 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T03:30 | btc-updown-15m-1791228600 | FULL | run_1791227746_2be3a494 | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T03:45 | btc-updown-15m-1791229500 | FULL | run_1791227746_2be3a494 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T04:00 | btc-updown-15m-1791230400 | FULL | run_1791227746_2be3a494 | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T04:30 | btc-updown-15m-1791232200 | GOOD | run_1791231380_2435b627 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T04:45 | btc-updown-15m-1791233100 | GOOD | run_1791231380_2435b627 | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T05:00 | btc-updown-15m-1791234000 | GOOD | run_1791231380_2435b627 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T05:30 | btc-updown-15m-1791235800 | GOOD | run_1791235015_758df0b8 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T05:45 | btc-updown-15m-1791236700 | GOOD | run_1791235015_758df0b8 | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T06:00 | btc-updown-15m-1791237600 | GOOD | run_1791235015_758df0b8 | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T06:30 | btc-updown-15m-1791239400 | FULL | run_1791238651_7f90656a | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T06:45 | btc-updown-15m-1791240300 | FULL | run_1791238651_7f90656a | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T07:00 | btc-updown-15m-1791241200 | FULL | run_1791238651_7f90656a | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T07:30 | btc-updown-15m-1791243000 | FULL | run_1791242284_d2414b9e | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T07:45 | btc-updown-15m-1791243900 | FULL | run_1791242284_d2414b9e | 01c87aa | NO |
| PRIMARY_WEEKDAY | 10-06T08:00 | btc-updown-15m-1791244800 | FULL | run_1791242284_d2414b9e | 01c87aa | YES |
| PRIMARY_WEEKDAY | 10-06T08:30 | btc-updown-15m-1791246600 | FULL | run_1791245915_384288e1 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T08:45 | btc-updown-15m-1791247500 | FULL | run_1791245915_384288e1 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T09:00 | btc-updown-15m-1791248400 | FULL | run_1791245915_384288e1 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T09:30 | btc-updown-15m-1791250200 | FULL | run_1791249551_7300cf99 | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T09:45 | btc-updown-15m-1791251100 | FULL | run_1791249551_7300cf99 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T10:00 | btc-updown-15m-1791252000 | FULL | run_1791249551_7300cf99 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T10:30 | btc-updown-15m-1791253800 | FULL | run_1791253399_d0ff0054 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T10:45 | btc-updown-15m-1791254700 | FULL | run_1791253399_d0ff0054 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T11:00 | btc-updown-15m-1791255600 | FULL | run_1791253399_d0ff0054 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T11:15 | btc-updown-15m-1791256500 | FULL | run_1791253399_d0ff0054 | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T11:30 | btc-updown-15m-1791257400 | GOOD | run_1791257485_084da14f | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T11:45 | btc-updown-15m-1791258300 | GOOD | run_1791257485_084da14f | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T12:00 | btc-updown-15m-1791259200 | GOOD | run_1791257485_084da14f | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T12:15 | btc-updown-15m-1791260100 | FULL | run_1791257485_084da14f | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T12:30 | btc-updown-15m-1791261000 | FULL | run_1791257485_084da14f | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T12:45 | btc-updown-15m-1791261900 | GOOD | run_1791261985_8f4edaaa | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T13:00 | btc-updown-15m-1791262800 | GOOD | run_1791261985_8f4edaaa | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T13:15 | btc-updown-15m-1791263700 | GOOD | run_1791261985_8f4edaaa | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T13:45 | btc-updown-15m-1791265500 | GOOD | run_1791261985_8f4edaaa | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T14:00 | btc-updown-15m-1791266400 | GOOD | run_1791266486_408fbe92 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T14:15 | btc-updown-15m-1791267300 | GOOD | run_1791266486_408fbe92 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T14:30 | btc-updown-15m-1791268200 | GOOD | run_1791266486_408fbe92 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T14:45 | btc-updown-15m-1791269100 | GOOD | run_1791266486_408fbe92 | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T15:00 | btc-updown-15m-1791270000 | GOOD | run_1791266486_408fbe92 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T17:00 | btc-updown-15m-1791277200 | GOOD | run_1791275488_682e8353 | 49aa9ca | NO |
| PRIMARY_WEEKDAY | 10-06T17:15 | btc-updown-15m-1791278100 | GOOD | run_1791275488_682e8353 | 49aa9ca | YES |
| PRIMARY_WEEKDAY | 10-06T17:45 | btc-updown-15m-1791279900 | GOOD | run_1791279466_720c2e47 | ad7cc22 | YES |
| PRIMARY_WEEKDAY | 10-06T18:15 | btc-updown-15m-1791281700 | GOOD | run_1791279466_720c2e47 | ad7cc22 | YES |
| PRIMARY_WEEKDAY | 10-06T18:30 | btc-updown-15m-1791282600 | GOOD | run_1791279466_720c2e47 | ad7cc22 | NO |
| PRIMARY_WEEKDAY | 10-06T18:45 | btc-updown-15m-1791283500 | GOOD | run_1791279466_720c2e47 | ad7cc22 | YES |
| PRIMARY_WEEKDAY | 10-06T19:00 | btc-updown-15m-1791284400 | GOOD | run_1791279466_720c2e47 | ad7cc22 | YES |
| PRIMARY_WEEKDAY | 10-06T19:15 | btc-updown-15m-1791285300 | GOOD | run_1791279466_720c2e47 | ad7cc22 | NO |
| PRIMARY_WEEKDAY | 10-06T19:30 | btc-updown-15m-1791286200 | GOOD | run_1791279466_720c2e47 | ad7cc22 | YES |
| PRIMARY_WEEKDAY | 10-06T19:45 | btc-updown-15m-1791287100 | FULL | run_1791279466_720c2e47 | ad7cc22 | NO |
| WEEKEND_PRIMARY | 10-03T02:00 | btc-updown-15m-1790964000 | GOOD | run_1790964229_ae4dba0a | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T02:15 | btc-updown-15m-1790964900 | FULL | run_1790964229_ae4dba0a | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T02:30 | btc-updown-15m-1790965800 | FULL | run_1790964229_ae4dba0a | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T13:30 | btc-updown-15m-1791005400 | GOOD | run_1791003676_3d58af66 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T14:45 | btc-updown-15m-1791009900 | FULL | run_1791009819_4f16006f | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T15:00 | btc-updown-15m-1791010800 | FULL | run_1791009819_4f16006f | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T16:15 | btc-updown-15m-1791015300 | FULL | run_1791014641_12c10290 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T17:30 | btc-updown-15m-1791019800 | GOOD | run_1791018273_c80d24ec | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T18:45 | btc-updown-15m-1791024300 | GOOD | run_1791023404_d05b32b1 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T19:30 | btc-updown-15m-1791027000 | FULL | run_1791026106_c815ceee | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T19:45 | btc-updown-15m-1791027900 | GOOD | run_1791026106_c815ceee | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T20:00 | btc-updown-15m-1791028800 | GOOD | run_1791026106_c815ceee | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T20:15 | btc-updown-15m-1791029700 | GOOD | run_1791029737_3db8ca59 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T20:30 | btc-updown-15m-1791030600 | FULL | run_1791029737_3db8ca59 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T20:45 | btc-updown-15m-1791031500 | FULL | run_1791029737_3db8ca59 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T21:00 | btc-updown-15m-1791032400 | FULL | run_1791029737_3db8ca59 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T21:45 | btc-updown-15m-1791035100 | FULL | run_1791033368_63fa64b7 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T22:00 | btc-updown-15m-1791036000 | FULL | run_1791033368_63fa64b7 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-03T22:30 | btc-updown-15m-1791037800 | FULL | run_1791036999_39eed4e1 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T22:45 | btc-updown-15m-1791038700 | FULL | run_1791036999_39eed4e1 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-03T23:45 | btc-updown-15m-1791042300 | FULL | run_1791041249_fc1dd8e0 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T00:00 | btc-updown-15m-1791043200 | FULL | run_1791041249_fc1dd8e0 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T00:30 | btc-updown-15m-1791045000 | FULL | run_1791044880_acbd0786 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T00:45 | btc-updown-15m-1791045900 | FULL | run_1791044880_acbd0786 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T01:30 | btc-updown-15m-1791048600 | FULL | run_1791048511_365b424d | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T02:30 | btc-updown-15m-1791052200 | FULL | run_1791052141_fe30de47 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T03:00 | btc-updown-15m-1791054000 | FULL | run_1791053867_a36e4c67 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T03:15 | btc-updown-15m-1791054900 | GOOD | run_1791053867_a36e4c67 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T03:30 | btc-updown-15m-1791055800 | GOOD | run_1791053867_a36e4c67 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T04:00 | btc-updown-15m-1791057600 | GOOD | run_1791057497_bb44b528 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T05:00 | btc-updown-15m-1791061200 | GOOD | run_1791061128_6012b10b | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T05:15 | btc-updown-15m-1791062100 | FULL | run_1791061128_6012b10b | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T06:00 | btc-updown-15m-1791064800 | GOOD | run_1791064759_671ece47 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T06:15 | btc-updown-15m-1791065700 | GOOD | run_1791064759_671ece47 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T07:30 | btc-updown-15m-1791070200 | GOOD | run_1791070109_cc475056 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T09:15 | btc-updown-15m-1791076500 | FULL | run_1791076424_50193a93 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T09:30 | btc-updown-15m-1791077400 | GOOD | run_1791076424_50193a93 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T10:30 | btc-updown-15m-1791081000 | GOOD | run_1791081118_a7cf46b9 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T10:45 | btc-updown-15m-1791081900 | GOOD | run_1791081118_a7cf46b9 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T11:45 | btc-updown-15m-1791085500 | GOOD | run_1791084345_41679360 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T12:00 | btc-updown-15m-1791086400 | GOOD | run_1791084345_41679360 | LEGACY_UNKNOWN | NO |
| WEEKEND_PRIMARY | 10-04T12:45 | btc-updown-15m-1791089100 | GOOD | run_1791088949_8a69164e | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T13:00 | btc-updown-15m-1791090000 | GOOD | run_1791088949_8a69164e | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T14:30 | btc-updown-15m-1791095400 | GOOD | run_1791095281_4743bbb1 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T16:30 | btc-updown-15m-1791102600 | GOOD | run_1791101455_05564309 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T16:45 | btc-updown-15m-1791103500 | GOOD | run_1791101455_05564309 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T19:15 | btc-updown-15m-1791112500 | GOOD | run_1791112480_51b3a76a | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T19:30 | btc-updown-15m-1791113400 | GOOD | run_1791112480_51b3a76a | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T19:45 | btc-updown-15m-1791114300 | GOOD | run_1791112480_51b3a76a | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T20:45 | btc-updown-15m-1791117900 | GOOD | run_1791116112_9db06308 | LEGACY_UNKNOWN | YES |
| WEEKEND_PRIMARY | 10-04T21:45 | btc-updown-15m-1791121500 | GOOD | run_1791119628_cdb7d7c2 | LEGACY_UNKNOWN | NO |
| WEEKDAY_SENSITIVITY | 10-05T00:30 | btc-updown-15m-1791131400 | GOOD | run_1791131381_baae0ef5 | LEGACY_UNKNOWN | YES |
| WEEKDAY_SENSITIVITY | 10-05T01:30 | btc-updown-15m-1791135000 | GOOD | run_1791135014_15577a71 | LEGACY_UNKNOWN | YES |
| WEEKDAY_SENSITIVITY | 10-05T04:45 | btc-updown-15m-1791146700 | GOOD | run_1791146614_c03299a7 | LEGACY_UNKNOWN | NO |
| WEEKDAY_SENSITIVITY | 10-05T05:45 | btc-updown-15m-1791150300 | GOOD | run_1791150245_90251ae4 | LEGACY_UNKNOWN | YES |
| WEEKDAY_SENSITIVITY | 10-05T19:00 | btc-updown-15m-1791198000 | GOOD | run_1791197001_1b6f1dd9 | LEGACY_UNKNOWN | YES |


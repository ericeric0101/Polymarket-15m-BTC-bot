# 事後唯讀軌跡研究稽核 — 2026-10-10

## 1. 三行結論

官方三筆持有方向全部獲勝；17:15 breaker 將可恢復的下跌鎖定成約 −3.09 USDC，首要問題是分類，不能只歸因於慢。
First／second DD 都在贏家誤報；hysteresis 減少分段卻沒有消除錯誤持倉警報，單靠第二段無法辨識不可恢復。
恢復幅度、第二段與連續 Binance 背景值得更大 prospective shadow；沒有任何 production SELL 或停損修改依據。

## 2. 官方結果驗證與方法邊界

三個市場均用既有 `scripts/fetch_official_resolutions.py::fetch_one`，公開 Gamma GET，HTTP 200、closed=true、uma_resolution_status=resolved、one-hot outcomePrices。取回時間 2026-10-10 18:25:32 台北，原始結構化證據見 `official_resolution_verification.json`。無密鑰、無交易端點、未載入 `.env`。官方結果優先於 runtime。

- OFFICIAL_OUTCOME_1645=UP
- OFFICIAL_OUTCOME_1715=DOWN
- OFFICIAL_OUTCOME_1730=DOWN
- RUNTIME_VS_OFFICIAL_MATCH=YES（各市場最後 MARKET_SETTLEMENT）；17:15 原 STOP_TIMING_POSITION_SETTLEMENT=UNKNOWN，為 c6f454e 之前的既有標籤缺陷，**不改歷史列**。後續 canonical_twap_deferred_relabel 已是 DOWN。near-tie runtime 數字不是官方勝負依據。

Run `run_1791621743_36d99e96`；LIVE HEAD `7a597551bd596e9e734cdcd8ef14a6b75d91e8c9`；本地分析 HEAD `94f1c47` 包含使用者列出的四個修正。主 cohort 3 個 actual LIVE positions、1 個台北日；16:45 為啟動後第一完整市場，保留暖機標記。17:00 無成交，僅背景，未計入 positions。

SQLite 皆 `mode=ro`，未用 immutable 忽略可能存在的 WAL。只寫本報告目錄與 `/tmp` 腳本。未啟動 bot／DRY-RUN，未下單／撤單、未改 env／runtime／門檻、未 commit／push。程序列表開始與結尾檢查，無交易程序。報告讀取不需額外策略權限。

重用 canonical `research/early_warning_timeline.py` 的 Decimal DD、PRIMARY、2 ticks + ≥5s HYSTERESIS。DD 是 entry executable bid 減 held executable bid；不使用 midpoint。BOOK_EMPTY 別列 EXIT_UNAVAILABLE，不設數字 DD；本次持倉內沒有觀察到 BOOK_EMPTY。>3s gap 或 unknown 皆刪截，不插值。實際 cadence 中位約 1.16–1.27s，**並非精確 1s 或 ≤1s**；<2s 的先後視為 SIMULTANEOUS_APPROX。持倉期 gap 次數 16:45=5、17:15=1、17:30=3。

Actual exposure 以第一 SELL fill 截止，後續尾單／殘量另列，出場後行情標 `POST_EXIT_HYPOTHETICAL_FULL_QTY`，不得算成仍持有全倉的警報或成交證據。CSV 保留原始最佳可得 cadence，這是三筆特別時間線所需的緊湊資料，沒有複製全 run raw ticks。17:15 17:27:30–17:30:00 區間完整涵蓋。

1s snapshot 沒有 engine net／exit-size L2。net 明確分 recorded engine 與依凍結 run 公式重建；`q*(bid*0.998-entry)-fee`，fee=`0.072*q*p²*(1-p)`，p clamped [0.01,0.99]。全部 STOP_TIMING recorded net 可用同公式重現，並非新 engine evaluation。Shadow 用 **exit VWAP**、估計 taker fee、可售股數與官方殘量 payout，不用 slip-buffer bid 假裝成交價；公式差異有意保留。實際 realized SELL 與官方殘量結算另列，不以 runtime PnL 代替 fill 重算。

## 3. 三筆 LIVE position 總表

| 市場 | 持有 | 股數 | 成交／baseline | 官方 | SELL realized | 含殘量官方 payout | 全倉 hold | 出場類型 |
|---|---|---|---|---|---|---|---|---|
| 16:45（暖機） | UP | 10.0 | 0.67 | UP | +2.995732 | +3.000427 | +3.30 | MAKER_TP |
| 17:15 | DOWN | 5.5 | 0.8 | DOWN | -3.094238 | -3.093138 | +1.10 | PROTECTIVE_TAKER |
| 17:30 | DOWN | 7.5 | 0.7 | DOWN | +2.022975 | +2.025225 | +2.25 | MAKER_TP |

16:45=A（乾淨 TP winner）；17:30=C（反覆惡化 TP winner）；17:15=E（breaker 出場後官方獲勝）。**沒有 D/F 官方輸家**，所以 loser coverage、不可逆分類成功率皆 INSUFFICIENT。總實際含殘量約 +1.932514，總 hold +6.65；不可把全部 hold 優勢解讀成一般持有政策有效。

16:45 實際進場16:51:27.232，TP 首填16:56:29.918、第二填16:56:30.769，殘量0.014226；17:15 進場17:25:57.866，breaker17:28:41.009，殘量0.0055；17:30 進場17:37:23.557，TP17:43:46.858，殘量0.0075。17:30 實填 **7.5股**，不是將 sizing 先驗套成10股。maker SELL 一律 MAKER_TP／T_TP_EXIT_FILL。

## 4. First DD → recovery → second DD

| 市場 | DD | first | first recovery | second | first max DD | second max DD |
|---|---|---|---|---|---|---|
| 16:45 | 0.05 | — | 未恢復／無訊號 | — | — | — |
| 16:45 | 0.10 | — | 未恢復／無訊號 | — | — | — |
| 16:45 | 0.15 | — | 未恢復／無訊號 | — | — | — |
| 16:45 | 0.20 | — | 未恢復／無訊號 | — | — | — |
| 17:15 | 0.05 | 17:27:40.179 | 17:27:51.225 | 17:27:57.042 | 0.13 | 0.59 |
| 17:15 | 0.10 | 17:27:42.122 | 17:27:43.348 | 17:27:44.696 | 0.12 | 0.13 |
| 17:15 | 0.15 | 17:28:13.596 | 未恢復／無訊號 | — | 0.59 | — |
| 17:15 | 0.20 | 17:28:13.596 | 未恢復／無訊號 | — | 0.59 | — |
| 17:30 | 0.05 | 17:37:25.818 | 17:37:33.397 | 17:37:48.811 | 0.05 | 0.05 |
| 17:30 | 0.10 | 17:38:41.334 | 17:39:58.816 | 17:40:05.598 | 0.16 | 0.12 |
| 17:30 | 0.15 | 17:39:04.697 | 17:39:10.153 | 17:39:38.037 | 0.16 | 0.16 |
| 17:30 | 0.20 | — | 未恢復／無訊號 | — | — | — |

17:15 DD0.10 在1.23s就恢復至 DD<0.10，但 bid 只由0.68→0.71，未回 entry0.80，更未回前30s局部高0.91；second 很快再出現。之後 primary 共3段，第三段17:28:13.597才是真正大幅collapse。Hysteresis 合併前兩段，first recovery start17:27:49.086、confirmation17:27:54.733，再於17:28:13.597出現 second；所以「second」的語意依 filtering 改變。

17:30 DD0.15 first6秒內恢復、second 再跌仍最終 TP；DD0.10 first要77.48s才恢復，同樣贏。所有 threshold 的 5/15/30/60/120s、回entry、回前30s局部高、first/second context 與 censor 標記見 `episode_table.csv`。17:15 DD0.05 first11.05s恢復；0.15/0.20直到實際退出前沒有恢復。17:30 DD0.05 first7.58s、0.15 first5.46s恢復；0.10直到120s窗口內才恢復。可觀察恢復不是連續無風險證據。

**B=混合**：速度本身沒有穩定區分力，17:15 甚至更快。恢復幅度／連續性更值得描述；**C=不足**：second0.15反而只擊中17:30贏家，17:15在0.15沒有second。不能把second次數當loser辨識。

## 5. Hysteresis 是否真的有幫助

| 市場 | DD | PRIMARY段數 | HYSTERESIS段數 | 減少分段數／flap |
|---|---|---|---|---|
| 16:45 | 0.05 | 0 | 0 | 0 |
| 16:45 | 0.10 | 0 | 0 | 0 |
| 16:45 | 0.15 | 0 | 0 | 0 |
| 16:45 | 0.20 | 0 | 0 | 0 |
| 17:15 | 0.05 | 2 | 1 | 1 |
| 17:15 | 0.10 | 3 | 2 | 1 |
| 17:15 | 0.15 | 1 | 1 | 0 |
| 17:15 | 0.20 | 1 | 1 | 0 |
| 17:30 | 0.05 | 7 | 1 | 6 |
| 17:30 | 0.10 | 3 | 3 | 0 |
| 17:30 | 0.15 | 5 | 3 | 2 |
| 17:30 | 0.20 | 0 | 0 | 0 |

17:30 合計 primary15段→hysteresis7段（跨threshold描述，**不是15個獨立樣本**）；0.05 7→1、0.10 3→3、0.15 5→3。17:15 7→5。first trigger 完全不變，所以不丟first early warning，但 recovery confirmation 至少等5s，某些第二段被合併／延後。減少的只是分段數，不能命名為已證明「假警報數減少」。17:30 hysteresis DD0.15 second仍17:41:28.936、maxDD0.19，屬false-positive control，故**D=有分段幫助，無分類改善證據**。

## 6. Binance 的角色

符號：正值表示持有方向不利，相對進場Binance價格，不是strike cross。每個事件／恢復／second／PnL／breaker／fill數值見 `event_context_table.csv` 與 `episode_table.csv`。

17:15 ≥1bps在17:27:38.972（1.77bps），DD0.05在17:27:40.180，差1.21s，SIMULTANEOUS_APPROX；≥2和≥3bps同在17:28:14.652（4.18bps），DD0.15在17:28:13.597，差1.05s亦同格。≥5未觸發。不能宣稱本次Binance總是領先token。first／recovery／second0.10時均約1.77bps，collapse後維持4.18bps。

16:45 最大不利約0bps；17:30 最大只有0.484bps，**沒有1bps以上警報，而非絕對沒有任何正向不利數字**。17:30 primary second0.15時仍約+0.484bps，因此把CF6的adverse定成任何>0的小值仍會誤報；不偷偷新加1/3bps門檻替它過濾。

**E=CONCURRENT_CONTEXT（本run）／MIXED（與歷史）**。連續幅度能區分價格壓力，但17:15在4.18bps也正式贏，因此不是不可恢復或可授權stop的證據。

## 7. Chainlink cross vs TWAP

17:15 Chainlink adverse cross17:28:17.350，TWAP cross17:29:05.253，前者早 **47.90s**；TWAP在breaker fill後24.24s才出現。Chainlink→breaker20.75s；TWAP→breaker為−27.15s（即TWAP晚27.15s）。DD0.10→Chainlink35.23s、→TWAP83.13s；DD0.15→Chainlink3.75s、→TWAP51.66s。TWAP cross 不在全倉持倉期間，必須標出場後假想hold context。

17:15 Chainlink favorable recross17:29:33.572；TWAP favorable recross17:29:57.486。17:30 spot/TWAP一開始就ADVERSE_AT_ENTRY，不是進場後新cross；spot於17:40:00.975回有利、TWAP17:40:32.398回有利，spot約早31.42s。16:45無adverse cross。

**F=較早（只有一個可配對新cross）**；**G=沒有更可採取交易行動的證據**。17:30 adverse entry和17:15後續recross都推翻「cross=不可逆」。Chainlink在17:15還晚於token/DD和−2，沒證明增量SELL價值；**H=仍作慢確認／結算狀態描述**。spot不是官方resolved outcome本身。

## 8. DD vs −$2

17:15 engine17:28:13.328直接記錄net−2.510，故首次≤−1/−1.5/−2/−2.5在同列；≤−3首次17:28:19.526（snapshot公式）。DD0.05早33.15s，DD0.10早 **31.21s**；DD0.15/0.20 snapshot晚0.27s，SIMULTANEOUS_APPROX。不是精確知道連續價格何時逐一跨越四個PnL門檻。

16:45未到−1；17:30最低重建約−1.501，觸及−1/−1.5但沒有−2/−2.5/−3。`lead_time_table.csv`逐position×DD×所有PnL門檻列出，缺事件不補0。**I=0.10中位31.21s、0.15同格，兩者n=1**，不是3筆中位。DD股數正規化、PnL受qty影響；兩者bid同源，不獨立。

## 9. DD vs breaker

17:15 first0.10→eligible **55.98s**，first0.15→eligible **24.51s**；primary second0.10→eligible53.41s，hysteresis second0.10→eligible24.51s。−2→eligible24.78s；eligible→submit0.13s、submit→fill2.77s、eligible→fill2.90s。

first0.10 snapshot net約−0.726，first recovery約−0.561，primary second約−0.726；−2第一次engine列實際已−2.510；eligible−3.261；fill實際SELL realized−3.094（尾量官方結算後−3.093）。經濟惡化主要是淺recover之後的collapse以及−2→trend eligible段；−2→eligible recorded再惡化 **0.751**，執行時成交0.24反比eligible bid0.21好。不能把這2.9秒當主要損失來源。

**J**只適用這一個breaker；16:45/17:30沒有可配對eligible，lead=NOT_COMPARABLE。

## 10. 早期訊號的實際可成交性

`executability_table.csv`列所有first/second、primary/hysteresis訊號：價格、sellable、depth covers、VWAP、fresh與nearest-after估計分開。訊號當下要求≤1s以前、fresh且價格一致；CF只取訊號後≤5s、實際第一fill以前的fresh支持quote。不把較晚找到深度回填成訊號當下YES；**capture freshness不保證FOK真的成交**。

17:15 DD0.10 snapshot bid0.68，0.256s前BBO size941.62足夠全5.5股；0.003s後STOP_TIMING bid/VWAP0.70、top35、sellable5.4945，fresh，能估shadow退出。DD0.05後1.69s有足夠BBO深度，但訊號當下未知。

17:15 DD0.15/0.20當下UNKNOWN：前0.269s的STOP row bestbid0.35但VWAP0.66，雖raw L2 age標fresh仍**同列價格不相容**，不能用0.66做假出場。5s內下一BBO top4.57不足5.5，而且沒有fresh可驗證full VWAP，故CF2/CF3=UNKNOWN；這不是證明市場無深度。17:30 DD0.10/0.15沒有5s內可靠full-depth，全部UNKNOWN；0.05後2.89s才有支持深度。16:45沒有DD事件，N/A。

負L2 age四列約−0.77..−0.88s，依ef5c45d固定容忍[−1,0]離線修正為0/FRESH，原始欄位保留；不改DB。另一種VWAP高於bid不一致不能被這個age修正掩蓋。DECISION_POINT_L2 source_ts缺失只作未驗證旁證；SHADOW_BBO只在fresh quote top size覆蓋全qty時支持top-price退出，不用累計近價depth猜VWAP。

**K=部分**，不是全部YES。Full entry qty／sellable haircut／unsold payout分列；BBO counterfactual的sellable=qty×0.999為凍結run既有buffer的推定，未查帳戶；不是0.001新閾值。殘量payout不是shadow當下可賣的現金。

## 11. 17:15 深入分析

| 事件 | 台北 | TTE | bid | net¹ | Binance bps | Chainlink | TWAP | legacy sigma | diffusion z |
|---|---|---|---|---|---|---|---|---|---|
| T_TOKEN_005_FIRST | 17:27:40.179 | 139.8 | 0.74 | -0.395 | 1.77 | FAVORABLE | FAVORABLE | 1.025 | 5.312 |
| T_TOKEN_010_FIRST | 17:27:42.122 | 137.9 | 0.68 | -0.726 | 1.77 | FAVORABLE | FAVORABLE | 0.501 | 0.975 |
| T_TOKEN_010_RECOVERY | 17:27:43.348 | 136.7 | 0.71 | -0.561 | 1.77 | FAVORABLE | FAVORABLE | 0.510 | 0.993 |
| T_TOKEN_010_SECOND | 17:27:44.696 | 135.3 | 0.68 | -0.726 | 1.77 | FAVORABLE | FAVORABLE | 0.474 | 0.919 |
| NET_LE_2.00 | 17:28:13.327 | 106.7 | 0.35 | -2.510 | 1.77 | FAVORABLE | FAVORABLE | 0.524 | 1.073 |
| T_TOKEN_015_FIRST | 17:28:13.596 | 106.4 | 0.30 | -2.778 | 1.77 | FAVORABLE | FAVORABLE | 0.525 | 1.076 |
| T_CHAINLINK_SPOT_CROSS | 17:28:17.350 | 102.6 | 0.28 | -2.885 | 4.18 | ADVERSE | FAVORABLE | 0.087 | 0.142 |
| T_BREAKER_ELIGIBLE | 17:28:38.105 | 81.9 | 0.21 | -3.261 | 4.18 | ADVERSE | FAVORABLE | 0.106 | 0.187 |
| T_PROTECTIVE_EXIT_SUBMIT | 17:28:38.235 | 81.8 | 0.21 | -3.261 | 4.18 | ADVERSE | FAVORABLE | 0.106 | 0.187 |
| EXIT_FILL | 17:28:41.009 | 79.0 | 0.24 | -3.100 | 4.18 | ADVERSE | FAVORABLE | 0.108 | 0.193 |

¹ snapshot事件使用同期bid公式重建；exact STOP_TIMING事件使用recorded engine net。Snapshot/engine不同callback可能在數毫秒內報不同bid，CSV保留source與兩種時鐘；不可將前一秒snapshot bid配在新的engine loss列假裝一致。詳細best-available網格見 `position_timeline.csv`，未插值成≤1秒。

A 第一可信小惡化是17:27:40 DD0.05，0.10於42秒確認；B first0.10恢復只是0.68→0.71的門檻穿越，沒有回成本／局部高，不能稱有力恢復。
C primary有second0.10；hysteresis的second0.10是後面的collapse；0.15首次collapse後實際退出前沒有second。
D 1.77bps與小DD近同時，4.18bps與collapse近同時；E Chainlink17:28:17才cross；F engine−2在17:28:13。
G **確切最後阻擋條件是abs_adverse_trend_confirmed**：hold在17:26:59成立，TTE≤120在17:28:00成立；loss/votes17:28:13、persistence17:28:28成立；17:28:38 locked_side_invalidated／adverse_trend首次成立才eligible。TTE分支已成立，不是等120秒，亦不是還等persistence15s。component first-true只證首次時序，沒有假裝所有布林之後永不重置；eligible那一列則全布林verified。
H/I 可觀察full-size支持：17:27:41.867 @0.68、42.126 @0.70、47.139 @0.69、52.307 @0.76、57.311 @0.74、17:28:00.470 sellable VWAP0.72；之後collapse區間是否每刻可賣未知。17:28:28.622、38.105有fresh L2足sellable；不能把其中較佳價格挑成新counterfactual。
J exit後17:29:32.379 bid回0.82、終局0.99；Chainlink/TWAP也回有利，官方DOWN贏，所以這次collapse在完整市場路徑上可恢復。不能由已知未來推出當下總能正確預測恢復。
K breaker含官方尾量−3.093 vs hold+1.100，**delta−4.193 USDC**。經濟有害vs這次官方hold，並不是全市場或所有breaker的一般結論。

## 12. 17:30 winner false-positive control

A bid相對0.70 entry反覆跨固定門檻，maxDD0.19；PRIMARY0.15有5段，部分很短，threshold自身會產生flap。沒有逐筆quote因果證據可把全部稱純quote noise。
B Binance最大0.484bps，相對17:15持續1.77→4.18較弱，可幫助描述壓力；但任何>0的CF6仍會對second0.15誤報。
C 恢復結構有用作路徑描述：0.15 first5.46s恢復，later回entry0.70以上17:41:46.673；0.10 first恢復卻要77.48s，故「fast recovery識別many winners」未證明。
D primary/hysteresis都有second／third，第二段邏輯仍假警報。
E Chainlink與TWAP adverse entry、低sigma仍TP，cross沒有獨立可交易性。
F 最清楚差異是**Binance連續不利幅度及token下跌幅度／經濟深度**：本筆DD≤0.19、net最低約−1.50、沒有−2／breaker；17:15 DD0.59、net−3.26、Binance4.18。但它們只能區分當下壓力，不是官方輸贏，兩筆最終持有方向都贏。

## 13. 16:45 clean winner control

暖機第一完整市場UP，baseline0.67；maxDD0.03、無≥0.05episode、無≥1bps不利Binance、無Chainlink/TWAP adverse、無−1與breaker。兩筆MAKER_TP fills均0.97，actual含殘量+3.000427。乾淨winner展示「不需先跌再恢復」路徑，不能當成所有winner必備樣貌。

## 14. breaker 是 timing 還是 classification 問題

**L 主因classification**：唯一breaker是官方winner，failure enum=EARLY_FALSE_POSITIVE（對官方hold的錯誤提前退出；不是相對DD觸發早）。相對已選擇退出的actual政策，則次要CONFIRMATION_TOO_SLOW：−2後24.78s等trend，loss多約0.751。Execution latency2.77s不是主因，且fill價格改善。

COUNTERFACTUAL_HOLD_PNL=+1.100000；ACTUAL_BREAKER_PNL（SELL realized）=−3.094238；ACTUAL_BREAKER_WITH_OFFICIAL_DUST=−3.093138；BREAKER_DELTA_VS_HOLD=−4.193138。即使早0.10退出可相對actual省2.487，也相對hold損失1.707；「晚」和「本來不該以不可恢復看待」必須分開。

## 15. counterfactual exit comparison

事前八組完全不優化：CF1 first0.10、CF2 first0.15、CF3 firstHYST0.15、CF4 secondPRIMARY0.10、CF5 secondPRIMARY0.15、CF6 secondPRIMARY0.15且Binance連續仍不利、CF7actual breaker／其他市場actual TP、CF8official hold。

| 方案 | 16:45 | 17:15 | 17:30 |
|---|---|---|---|
| CF1 | +3.000（未觸發，實際） | -0.607 | UNKNOWN |
| CF2 | +3.000（未觸發，實際） | UNKNOWN | UNKNOWN |
| CF3 | +3.000（未觸發，實際） | UNKNOWN | UNKNOWN |
| CF4 | +3.000（未觸發，實際） | -0.662 | UNKNOWN |
| CF5 | +3.000（未觸發，實際） | -3.093（未觸發，實際） | UNKNOWN |
| CF6 | +3.000（未觸發，實際） | -3.093（未觸發，實際） | UNKNOWN |
| CF7 | +3.000 | -3.093 | +2.025 |
| CF8 | +3.300 | +1.100 | +2.250 |

CF3與CF2 first時刻相同，canonical hysteresis只改恢復定義，沒有另加觸發等待。未觸發=新增shadow exit不介入，保留actual TP/breaker，不擅自改成hold。UNKNOWN不能補actual、不能排除後算組合總收益。17:15 CF1 −0.607，vsactual+2.487、vshold−1.707；CF4 −0.662，vsactual+2.431、vshold−1.762。17:30 first/second0.10/0.15雖能觀察價格，depth不足以產生可靠經濟值，不能用bid當full VWAP。CF6 binance>0是固定符號語意，沒有新調門檻；17:30 gap使持續adverse條件UNKNOWN。

## 16. 與 historical audit 是否一致

| 歷史觀察 | 本次判定 | 依據 |
|---|---|---|
| DD0.10/0.15 回顧經濟為正 | MIXED | 17:15 CF1 比actual省2.49，但比hold少1.71；17:30 would cut winner，CF1/2深度未知，不能算合計 |
| Binance 常早且吵 | MIXED | 本次1bps與0.05同格；3bps與0.15同格；4.18bps仍官方贏，不能證明一般早領先 |
| TWAP 晚、作確認 | SUPPORT | 17:15 17:29:05才cross，比exit fill晚24.24s |
| 贏家經歷meaningful DD | SUPPORT | 17:30 DD0.19後TP；17:15 DD0.59後官方贏 |
| 固定−2不足辨識doomed | SUPPORT | 17:15跨−2卻官方DOWN贏 |
| breaker 在−2後等待 | SUPPORT | 本次24.78s等待trend；成交另2.77s |

參照 `reports/historical_early_warning_audit/`：9月125 LIVE positions／13台北日、bid/net約21.3s網格、舊sizing/多breaker版本、無1s exit-depth；早上10月3筆為v1另cohort，當時官方皆DOWN且有真losers。不能把這次3筆混進舊128當同schema sample。

Optional episode盤點：另外只有3筆STOP_TIMING LIVE position可join retained snapshot，各690/653/678列但**全部v1**，native-v2-quality額外LIVE position=0。本次v2=3；所以不做兼容缺陷被隱藏的pooled episode重建，停止在counts。歷史正經濟只供描述，不等於本次positive shadow economics；無隨機train/test／tick樣本獨立化。

## 17. feature family ranking

| 特徵族 | 評等 | 本次支持 |
|---|---|---|
| First token deterioration | PROMISING | MIXED |
| Recovery time | PROMISING | MIXED |
| Second-leg deterioration | INSUFFICIENT | MIXED |
| Hysteresis episode filtering | PROMISING | SUPPORT_FRAGMENTATION_ONLY |
| Binance adverse path | PROMISING | MIXED |
| Chainlink spot cross | WEAK | SUPPORT_EARLIER_NOT_ACTIONABLE |
| TWAP cross | WEAK | SUPPORT_CONFIRMATION_ONLY |
| TTE | WEAK | INSUFFICIENT |
| sigma / diffusion-z | INSUFFICIENT | INSUFFICIENT |
| fixed PnL thresholds | WEAK | MIXED |
| existing breaker | WEAK | CONTRADICT_CORRECT_LOSER_CLASSIFICATION |

完整earliness／loser coverage／winner false positives／executability／recovery discrimination／data quality七面向見 `feature_family_assessment.csv`。本次沒有任何STRONG，n=3一天、全部官方贏；尤其sigma/z沒有獨立增量證據，legacy sigma不是機率／diffusion-z。

## 18. 下一輪 prospective shadow 建議

**N=可以準備研究型shadow；本次沒有啟動任何run，也沒改telemetry。**

- SHADOW_CANDIDATE_1：first→恢復幅度／確認持續→second的完整狀態；primary與既有hysteresis並列，不新增／最佳化DD門檻。
- SHADOW_CANDIDATE_2：同一軌跡下連續Binance相對entry路徑，保留小正噪音與持續強逆向的描述，不讓任一bps授權SELL。
- SHADOW_CANDIDATE_3：token狀態與Chainlink/TWAP recross、TTE、exit-size freshness／price consistency联合證據，特別看collapse後官方winner。

先固定缺失／gap／full-depth規則與五秒shadowquote窗口；需要能在每個研究signal保留held bid、top qty、可售qty、完整exit VWAP、L2來源時間和bid一致性，不能僅依age=FRESH。官方resolution全數驗證。多個台北日／market為cluster，不把tick／episode當獨立持倉。事前凍結endpoint，觀察actual losses與official winners兩種單位，不由這次三筆挑贏的門檻。不承諾樣本數即可達到統計能力。

## 19. 明確列出不能下的結論

A first alone太吵：案例支持；B fast recovery能識別many winners：未證明（MIXED）；C second改善loser區分：INSUFFICIENT；D hysteresis減分段：YES但不是減false-position；E Binance concurrent/noisy context而非自動stop；F Chainlink較TWAP早：單cross case支持；G more actionable：NO證據；H TWAP confirmation：描述支持；I/J只有一筆lead；K depth部分UNKNOWN；L分類錯誤主因；M差異是當下壓力而非最終命運；N僅更大shadow；O production change=NO。

不能說DD0.15有early normalized優勢於−2、second0.15抓到本次loser、fast recovery保證winner、Chainlink cross獨立可交易、hysteresis解決false-positive、Binance≥3意味着必敗、book depth標fresh保證成交、任何CF是實際可得到PnL、historical net能跨版本套用、hold永遠較好、tiny-run有顯著性或勝率估計。本次不更改STOP_LOSS_ENABLED／Flip Stop／breaker／persistence／TTE，也不授予任何production SELL。


```text
OFFICIAL_OUTCOMES_VERIFIED=YES
OFFICIAL_OUTCOME_1645=UP
OFFICIAL_OUTCOME_1715=DOWN
OFFICIAL_OUTCOME_1730=DOWN
RUNTIME_VS_OFFICIAL_MATCH=YES_LAST_RUNTIME_LABELS; 17:15 STOP_TIMING settlement row remains UNKNOWN legacy artifact
FIRST_DD_TOO_NOISY=YES_CASE_SUPPORT
RECOVERY_USEFUL=MIXED; amplitude more useful than speed in these cases
SECOND_DD_PROMISING=INSUFFICIENT_STANDALONE; CONTEXT_COMBINATION_SHADOW_ONLY
HYSTERESIS_USEFUL=YES_FRAGMENTATION_ONLY
BINANCE_ROLE=CONCURRENT_CONTEXT; MIXED_HISTORICALLY
CHAINLINK_VS_TWAP=EARLIER_47.90_SEC_SINGLE_CROSS_CASE_NOT_ACTIONABLE
TWAP_CONFIRMATION_ONLY=YES_DESCRIPTIVE
DD_010_MEDIAN_LEAD_VS_MINUS2=31.21_SEC_N1
DD_015_MEDIAN_LEAD_VS_MINUS2=SIMULTANEOUS_APPROX_RAW_MINUS_0.27_SEC_N1
DD_010_MEDIAN_LEAD_VS_BREAKER=55.98_SEC_N1
DD_015_MEDIAN_LEAD_VS_BREAKER=24.51_SEC_N1
EXECUTABLE_DEPTH_AT_EARLY_DD=PARTIAL; 17:15 DD0.10_SUPPORTED; DD0.15_INCONSISTENT_L2; 17:30_UNKNOWN
BREAKER_PRIMARY_FAILURE_MODE=EARLY_FALSE_POSITIVE_CLASSIFICATION; SECONDARY_CONFIRMATION_TOO_SLOW_RELATIVE_TO_ACTUAL_EXIT
RECOVERY_SECOND_LEG_READY_FOR_LARGER_SHADOW=YES_RESEARCH_ONLY
PRODUCTION_STOP_CHANGE_JUSTIFIED=NO
BOT_STOPPED_AT_END=YES
NO_PUSH=YES
```

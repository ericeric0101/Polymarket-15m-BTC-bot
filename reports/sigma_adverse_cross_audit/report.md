# Adverse cross 後 sigma 稽核：唯讀 retrospective research

稽核日期：2026-10-09（Asia/Taipei）。實際 HEAD：`76cdfa9b756bda4528bb0b329d828bf6eec238d2`；使用者預期 HEAD：`14ec69f`。保留現況，未 checkout／reset。兩個 HEAD 之間 `spot_pricer.py`、`live_entry_research.py`、`prediction_research_snapshot.py`、`forecast_state.py` 沒有差異。

**結論：legacy sigma 有弱描述性關係，但尚未證明獨立於 TTE、final-window 目標切換及 reference 差異的資訊。diffusion z 的首次 cross 樣本只有 1 個。不能據此設定或啟用 LIVE Flip Stop threshold。**

全程 bot stopped、無網路／交易／runtime 修改／stop-loss 修改／schema 修改／新增 shadow events／production telemetry／push。只新增一支 `scripts/sigma_adverse_cross_audit.py` 與本目錄六份小型研究輸出。

## 1. 欄位與公式稽核

`summary.json.field_inventory` 逐欄位列出 FIELD、FORMULA、SOURCE_FILE_OR_EVENT、UNITS、USES_TTE、USES_VOLATILITY、USES_DECAY、FLOOR_OR_CEILING、REFERENCE_PRICE_SOURCE、STRIKE_SOURCE、KNOWN_SOURCE_SWITCHING、FRESHNESS_GUARANTEE、TWAP_OR_TERMINAL_ASSUMPTION、HISTORICAL_COVERAGE、NUMBER_OF_MARKETS、NUMBER_OF_DAYS。以下公式與 coverage 對照為其摘要。Coverage 的非空數值不等於可用 adverse-cross 樣本；多個來源中的重複 rows 只用來盤點欄位，不作推論。

令 S 為 path spot、K 為 strike、K* 為目前 required-move 目標、W=60秒、T為TTE、τ為剩餘 final-window 秒數。

| 欄位／群組 | 公式與語義 | TTE／vol／decay／界限 | 來源 |
|---|---|---|---|
| required_move_sigma | `abs(S-K*) / (S*σ_decay*sqrt(H/(365.25*86400)))`；窗外 K*=strike、H=T；窗內 K*=(W*K−已觀測積分)/τ、H=max(1,τ) | 使用 TTE、vol 與 decay；σ 經 scale、floor／ceiling，沒有 sigma 值本身的 cap。**不是標準常態 z-score** | bot/live_entry_research.py:build_safety_sigma；bot/spot_pricer.py |
| required_move_z_diffusion | `abs(log(K*/S))/(σ_raw*sqrt(h/(365.25*86400)))`；窗外 h=T−W+W/3；窗內 h=τ/3 | 使用 TTE／raw vol；無 decay、scale、vol floor、implied floor；缺 partial integral 或正值輸入時 NA | bot/live_entry_research.py:build_diffusion_flip_z |
| sigma_ex_market／forecast_sigma_raw_realized | 過去 log returns 的 sample std(ddof=1) × sqrt(365*86400／平均正 dt) | 過去資料 window；無 TTE decay／floor。Research vol 只用 Chainlink raw history；forecast 使用各自 reference history | bot/market_data.py:estimate_external_spot_sigma_annualized；bot/spot_pricer.py |
| forecast_sigma_default／after_scale／after_bounds／time_decay_factor／after_time_decay | default 或 realized → ×scale → clip(floor,ceiling) → max(floor,bounded×round(clip(T/ref,decay_min,1),4)) | decay 可停用；當前程式 defaults：floor=.20、ceiling=2.00、scale=1、ref=600s、decay_min=.30；不能假定所有歷史 run 相同 | bot/forecast_state.py |
| sigma_final／forecast_sigma_final／sigma／implied_sigma／implied_sigma_floor | implied sigma 由 digital probability 與 market mid 反解；floor=.6×implied sigma；可上調 decayed sigma | implied solver .05–5、最多20 iterations、容差1e-4；final floor／ceiling；舊 sigma 可為 default | bot/forecast_state.py；execution/maker_engine.py |
| strike_z | `[log(spot/strike)−.5*σ²*T_year]/(σ*sqrt(T_year))`，T_year=T/(365*86400)；有符號 digital endpoint term | sigma input 可能已 transformed；無效輸入舊 helper 回傳0，0不能當有效 z | bot/shadow_signal.py:_strike_z |
| required_move_usd／required_move_bps | K*−S；(K*−S)/S×10000 | 無 vol normalize；final-window 會切換 K*，故間接受 TTE 影響 | bot/spot_pricer.py |
| twap_minus_strike_bps／spot_minus_strike_bps／spot_minus_strike／entry_signed_spot_distance | (reference−strike)/strike×10000，或 USD 差；TWAP 與 spot／entry reference 分開 | 不用 vol／decay；不是 sigma；market min/max 為全市場 extrema，不能作 first-cross 值 | bot/twap_forward_shadow.py；bot/shadow_signal.py；FILL_MARKOUT |
| projected_settlement_twap_flat/trend_bps_vs_strike | `(projected−strike)/strike*10000`；projected=TWAP+(spot_or_capped_trend−TWAP)*min(1,T/60) | 用 TTE，trend movement 有 cap；是 pressure proxy | bot/twap_forward_shadow.py；非官方 settlement 權威 |
| public_proxy.safety_sigma／abs_distance_bps | abs(Binance spot−strike)/(spot×過去分鐘returns std×sqrt(T/60))；距離為abs bps | 過去30／60分鐘、至少15 returns；無 decay／floor；1分鐘 closed-bar proxy | scripts/strike_flip_risk_analysis.py；reports/strike_flip_risk/safety_sigma.csv |

來源切換：required-move 的 S 優先 Chainlink raw spot，可退回 Binance WS；其 raw vol 卻仍取 Chainlink history。K* 在 final window 由 strike 切至剩餘平均值邊界。官方 TWAP、raw path spot、Binance proxy、market midpoint及真正官方 winner均分開保留。Public proxy study 的200市場中194個 strike 為 Binance boundary proxy，只有6個 local verified strike，不能與 runtime sigma 視為同語義。

Freshness：native v2 用 local receipt／同來源時鐘與 component flags；historical FRESH_ORIGINAL 是保留原 strict flag，不是獨立修復時鐘。canonical path gate 延用現有 scripts 的 FRESH_* 分類與 MAX_GAP=10秒。本稽核沒有修補 cleared sigma 或將 legacy 升格為 native。Forecast／signal 事件本身沒有统一的 native-v2 freshness 保證。已驗證 manifest 的 native Parquet 將版本號序列化為 double 2.0；只在分析讀取端精確轉回2，52,097 rows，runtime gate未改。

```text
LEGACY_SIGMA_RUNTIME_EQUIVALENT=PARTIAL
DIFFUSION_Z_RUNTIME_EQUIVALENT=YES
```

PARTIAL 指 required_move_sigma 的核心算式與當前 helper 相同，但歷史參數、clock semantics／source context 無法全面視為相同；先前 canonical stage-4 使用這個 legacy heuristic。更早 public safety_sigma study 則是另一套 minute-vol proxy，runtime equivalent=NO。Diffusion YES 僅指已保留的非空數值來自相同 current helper／模型；不是足夠樣本或校準成功。

### 欄位 coverage（各欄位 unique markets／台北日）

| FIELD | 市場 | 日數 | retained coverage（台北） |
|---|---:|---:|---|
| abs_distance_bps | 200 | 57 | 2026-07-27T15:00:00+08:00 ～ 2026-09-21T07:30:00+08:00 |
| entry_signed_spot_distance | 125 | 13 | 2026-09-10T20:20:54.671134+08:00 ～ 2026-09-30T23:50:39.129747+08:00 |
| forecast_implied_sigma | 661 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:24:20.414685+08:00 |
| forecast_implied_sigma_floor | 661 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:24:20.414685+08:00 |
| forecast_sigma_after_bounds | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_after_scale | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_after_time_decay | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_default | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_final | 682 | 14 | 2026-09-25T08:02:28.474991+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_raw_realized | 674 | 14 | 2026-09-25T08:03:57.179605+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| forecast_sigma_time_decay_factor | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| implied_sigma | 661 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:24:20.414685+08:00 |
| implied_sigma_floor | 661 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:24:20.414685+08:00 |
| max_required_move_sigma_to_flip | 423 | 10 | 2026-09-30T23:45:00.166557+08:00 ～ 2026-10-09T01:15:00.356298+08:00 |
| max_twap_minus_strike_bps | 469 | 12 | 2026-09-28T19:45:00.151047+08:00 ～ 2026-10-09T01:15:00.356298+08:00 |
| min_required_move_sigma_to_flip | 423 | 10 | 2026-09-30T23:45:00.166557+08:00 ～ 2026-10-09T01:15:00.356298+08:00 |
| min_twap_minus_strike_bps | 469 | 12 | 2026-09-28T19:45:00.151047+08:00 ～ 2026-10-09T01:15:00.356298+08:00 |
| projected_settlement_twap_flat_bps_vs_strike | 299 | 10 | 2026-09-28T19:32:13.900967+08:00 ～ 2026-10-09T01:28:00.740143+08:00 |
| projected_settlement_twap_trend_bps_vs_strike | 299 | 10 | 2026-09-28T19:32:13.900967+08:00 ～ 2026-10-09T01:28:00.740143+08:00 |
| public_proxy.safety_sigma | 200 | 57 | 2026-07-27T15:00:00+08:00 ～ 2026-09-21T07:30:00+08:00 |
| required_move_bps | 515 | 10 | 2026-09-30T23:30:41.347908+08:00 ～ 2026-10-09T16:26:34.266438+08:00 |
| required_move_sigma | 514 | 10 | 2026-09-30T23:30:51.352800+08:00 ～ 2026-10-09T16:26:34.266438+08:00 |
| required_move_usd | 265 | 8 | 2026-09-30T23:30:41.347908+08:00 ～ 2026-10-09T16:26:34.266438+08:00 |
| required_move_z_diffusion | 12 | 2 | 2026-10-08T23:01:08.284080+08:00 ～ 2026-10-09T16:26:34.266438+08:00 |
| sigma | 696 | 14 | 2026-09-25T08:00:18.649332+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| sigma_after_time_decay_ex_market | 261 | 8 | 2026-09-30T23:30:51.352800+08:00 ～ 2026-10-09T01:28:00.740143+08:00 |
| sigma_before_implied_floor | 680 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| sigma_ex_market | 514 | 10 | 2026-09-30T23:30:51.352800+08:00 ～ 2026-10-09T16:26:34.266438+08:00 |
| sigma_final | 261 | 8 | 2026-09-30T23:30:41.347908+08:00 ～ 2026-10-09T01:28:00.740143+08:00 |
| spot_minus_strike | 677 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| spot_minus_strike_bps | 677 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| strike_z | 677 | 14 | 2026-09-25T08:02:56.825002+08:00 ～ 2026-10-09T16:26:20.713017+08:00 |
| twap_minus_strike_bps | 299 | 10 | 2026-09-28T19:32:13.900967+08:00 ～ 2026-10-09T01:28:00.740143+08:00 |

## 2. Research event 與品質

主 cohort 是305個已記錄 DRY-RUN shadow fill市場；方向取每市場第一次fill，不以事後winner決定。另一份LIVE appendix有125個BUY市場，方向取BUY FILL_MARKOUT，strike取entry context。每個市場最多一列，沒有以episode／tick／不同checkpoint擴增樣本。

UP adverse 為TWAP由strict above strike轉strict below；DOWN則相反。等於strike標成touch，不直接算方向cross。若填單時已adverse，須先觀測favorable，再觀測adverse才有cross；不把「已在不利側」當cross。從entry前10秒內anchor開始，任一 stale／missing／gap>10秒／strike變動即中止 first-cross 重建，不跳過缺口挑後來episode。

cross timestamp為第一個不利側snapshot，保留前一sample時間與interval width；沒有插值或宣稱抓到真正觸碰strike瞬間。全部都是 **OBSERVED_GRID_APPROX**，**VERIFIED_CONTINUOUS=0**。

| cohort／provenance | 首次cross市場 | 日數 | legacy sigma | diffusion z |
|---|---:|---:|---:|---:|
| DRY-RUN／HISTORICAL_PRE_V2 | 33 | 4 | 33 | 0 |
| DRY-RUN／NATIVE_V2 | 14 | 2 | 14 | 1 |
| LIVE／LEGACY_REFERENCE_ORIGINAL_AGE_ONLY | 31 | 11 | 0 | 0 |

主sigma分析合計47市場／6日（10/3–10/8台北）；LIVE appendix另列，合計可辨識observed-grid cross為78市場／17日。430個原始market rows中，352沒有可用first-cross事件：包括121 no observed cross、179被path censor、50沒有accepted same-run path、2缺anchor；這些不能作negative-trigger probability分母。

## 3. 主legacy bucket結果

以下只用cross當刻記錄值，沒有挑後來最大sigma。Revert分母只含可辨識first favorable re-cross或完整觀測到settlement且沒有revert的市場；其餘unknown。官方winner給final settle count，兩種分母分開。所有primary bucket均為observed-grid、DRY-RUN。

| provenance | sigma | 市場／日 | 最終有利／不利 | revert／no-revert／未知 | revert rate | 最終不利率 | revert median［P25,P75］秒 | cross TTE median秒 | observed adverse duration median秒 | raw TWAP距離median bps |
|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|
| HISTORICAL_PRE_V2 | <0.5 | 25/4 | 11/14 | 11/9/5 | 55.0% | 56.0% | 45.4 [38.8,78.3] | 314.4 | 104.8 | 0.0314 |
| HISTORICAL_PRE_V2 | 0.5–<1.0 | 4/2 | 0/4 | 0/3/1 | 0.0% | 100.0% | NA [NA,NA] | 152.8 | 117.6 | 0.0583 |
| HISTORICAL_PRE_V2 | 1.0–<2.0 | 3/1 | 0/3 | 0/3/0 | 0.0% | 100.0% | NA [NA,NA] | 29.6 | 29.6 | 0.0804 |
| HISTORICAL_PRE_V2 | >=2.0 | 1/1 | 0/1 | 0/1/0 | 0.0% | 100.0% | NA [NA,NA] | 2.0 | 2.0 | 0.0058 |
| NATIVE_V2 | <0.5 | 10/1 | 5/5 | 1/0/9 | 100.0% | 50.0% | 73.1 [73.1,73.1] | 420.7 | 73.1 | 0.0333 |
| NATIVE_V2 | 0.5–<1.0 | 3/2 | 0/3 | 0/1/2 | 0.0% | 100.0% | NA [NA,NA] | 312.4 | 66.5 | 0.1665 |
| NATIVE_V2 | 1.0–<2.0 | 1/1 | 1/0 | 0/0/1 | NA | 0.0% | NA [NA,NA] | 277.1 | NA | 0.0515 |
| NATIVE_V2 | >=2.0 | 0/0 | 0/0 | 0/0/0 | NA | NA | NA | NA | NA | NA |

Native <.5 的100% revert只是1/1 identifiable market，另外9個revert outcome未知；不能說10個都revert。Adverse duration統計含「到settlement仍未revert」的right-censored觀測時長，是描述性lower-bound，不是完整生存時間median。P25/P75使用線性分位數；revert時間只對可辨識first favorable re-cross者計算。

主cohort有3個市場先revert favorable再final adverse。TOUCH/CROSS、REVERT、FINAL FAVORABLE、FINAL ADVERSE是不同endpoints；任何表中的率都不是未限定的「flip probability」。

歷史bucket看似高sigma較危險，但 >=1 的4個市場全在final-window（TTE≤60秒）；target及horizon已改變。Native的1–<2只有1個市場，最終favorable，沒有一致單調關係。不能將higher legacy sigma直接轉為校準機率。

在47個cross中，43個為PRE_FINAL_STRIKE_PROXY，4個為EXACT_FINAL_WINDOW_BOUNDARY。有5個cross的TWAP已adverse，但由同一row的required_move_bps反推，其sigma numerator raw spot在held-favorable側。這是 **INFERRED 幾何對齊檢查**，不是新增price observation。Raw TWAP distance與required_move_bps／sigma不是同一分子。

### Diffusion z（完全獨立分析）

只有1個native cross有z，落在<.5、1市場／1日；final adverse=1/1，revert未知。其餘bucket=0，沒有可比較的排序。雖全retained資料有12市場／2日的非空z，但只有上述1個符合first-filled-cross cohort。**DIFFUSION_Z_SIGNAL=INSUFFICIENT**。

## 4. 固定 persistence sensitivity

checkpoint固定0/5/15/30秒；sigma bucket仍取first cross，沒有於checkpoint重新分bucket。要求觀測grid上同一adverse episode持續存在，未把revert後再adverse視為存活。目標時刻用前後fresh samples bracket，最大gap10秒；兩端方向不同則unknown，沒有精確秒級插值。這是retrospective grid判定，不是runtime timer。

| provenance | sigma | checkpoint秒 | first-cross市場 | 可辨識checkpoint | 仍adverse市場／日 | 最終不利率 | later re-cross率（可辨識分母） |
|---|---|---:|---:|---:|---:|---:|---:|
| HISTORICAL_PRE_V2 | <0.5 | 0 | 25 | 25 | 25/4 | 56.0% | 55.0% (N=20) |
| HISTORICAL_PRE_V2 | <0.5 | 5 | 25 | 24 | 24/4 | 54.2% | 55.0% (N=20) |
| HISTORICAL_PRE_V2 | <0.5 | 15 | 25 | 24 | 24/4 | 54.2% | 55.0% (N=20) |
| HISTORICAL_PRE_V2 | <0.5 | 30 | 25 | 23 | 21/4 | 61.9% | 50.0% (N=18) |
| HISTORICAL_PRE_V2 | 0.5–<1.0 | 0 | 4 | 4 | 4/2 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 0.5–<1.0 | 5 | 4 | 4 | 4/2 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 0.5–<1.0 | 15 | 4 | 4 | 4/2 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 0.5–<1.0 | 30 | 4 | 4 | 4/2 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 1.0–<2.0 | 0 | 3 | 3 | 3/1 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 1.0–<2.0 | 5 | 3 | 3 | 3/1 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 1.0–<2.0 | 15 | 3 | 3 | 3/1 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | 1.0–<2.0 | 30 | 3 | 1 | 1/1 | 100.0% | 0.0% (N=1) |
| HISTORICAL_PRE_V2 | >=2.0 | 0 | 1 | 1 | 1/1 | 100.0% | 0.0% (N=1) |
| HISTORICAL_PRE_V2 | >=2.0 | 5 | 1 | 0 | 0/0 | NA | NA (N=0) |
| HISTORICAL_PRE_V2 | >=2.0 | 15 | 1 | 0 | 0/0 | NA | NA (N=0) |
| HISTORICAL_PRE_V2 | >=2.0 | 30 | 1 | 0 | 0/0 | NA | NA (N=0) |
| NATIVE_V2 | <0.5 | 0 | 10 | 10 | 10/1 | 50.0% | 100.0% (N=1) |
| NATIVE_V2 | <0.5 | 5 | 10 | 10 | 10/1 | 50.0% | 100.0% (N=1) |
| NATIVE_V2 | <0.5 | 15 | 10 | 9 | 9/1 | 55.6% | 100.0% (N=1) |
| NATIVE_V2 | <0.5 | 30 | 10 | 7 | 7/1 | 57.1% | 100.0% (N=1) |
| NATIVE_V2 | 0.5–<1.0 | 0 | 3 | 3 | 3/2 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | 0.5–<1.0 | 5 | 3 | 3 | 3/2 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | 0.5–<1.0 | 15 | 3 | 3 | 3/2 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | 0.5–<1.0 | 30 | 3 | 3 | 3/2 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | 1.0–<2.0 | 0 | 1 | 1 | 1/1 | 0.0% | NA (N=0) |
| NATIVE_V2 | 1.0–<2.0 | 5 | 1 | 1 | 1/1 | 0.0% | NA (N=0) |
| NATIVE_V2 | 1.0–<2.0 | 15 | 1 | 1 | 1/1 | 0.0% | NA (N=0) |
| NATIVE_V2 | 1.0–<2.0 | 30 | 1 | 0 | 0/0 | NA | NA (N=0) |

歷史<.5的存活數25→24→24→21；final adverse約56%→54%→54%→62%，later re-cross約55%→55%→55%→50%。5／15秒沒有清楚區分；30秒變化同時涉及存活／censor分母。高sigma cells很小，且>=2的單一市場2秒後即到期，不能用其5/15/30秒零樣本作不revert證據。沒有選best persistence。

## 5. TTE stratification

邊界採 (>300)、(120,300]、(60,120]、≤60，避免重複計120／60秒。同樣sigma buckets，沒有為漂亮趨勢合併cells。

| provenance | TTE秒 | sigma | 市場／日 | 最終不利率 | revert rate（可辨識N） |
|---|---|---|---:|---:|---:|
| HISTORICAL_PRE_V2 | >300 | <0.5 | 14/3 | 57.1% | 66.7% (N=12) |
| HISTORICAL_PRE_V2 | 120–300 | <0.5 | 7/2 | 57.1% | 40.0% (N=5) |
| HISTORICAL_PRE_V2 | 60–120 | <0.5 | 4/2 | 50.0% | 33.3% (N=3) |
| HISTORICAL_PRE_V2 | >300 | 0.5–<1.0 | 1/1 | 100.0% | 0.0% (N=1) |
| HISTORICAL_PRE_V2 | 120–300 | 0.5–<1.0 | 1/1 | 100.0% | NA (N=0) |
| HISTORICAL_PRE_V2 | 60–120 | 0.5–<1.0 | 2/1 | 100.0% | 0.0% (N=2) |
| HISTORICAL_PRE_V2 | <=60 | 1.0–<2.0 | 3/1 | 100.0% | 0.0% (N=3) |
| HISTORICAL_PRE_V2 | <=60 | >=2.0 | 1/1 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | >300 | <0.5 | 9/1 | 44.4% | 100.0% (N=1) |
| NATIVE_V2 | 120–300 | <0.5 | 1/1 | 100.0% | NA (N=0) |
| NATIVE_V2 | >300 | 0.5–<1.0 | 2/2 | 100.0% | NA (N=0) |
| NATIVE_V2 | 60–120 | 0.5–<1.0 | 1/1 | 100.0% | 0.0% (N=1) |
| NATIVE_V2 | 120–300 | 1.0–<2.0 | 1/1 | 0.0% | NA (N=0) |

各TTE×sigma非空cell只有1–14個市場，全為UNDERPOWERED（事前描述標記：N<20或日數<3）。歷史>=1只有≤60秒cell，同一TTE沒有低sigma對照；不能分離TTE／final-window模式與sigma效應。Native cells也沒有足夠日數。沒有tick-level統計檢定或bootstrap significance claim。

## 6. LIVE −$2 region窄問題

31個LIVE observed-grid cross沿用原legacy TWAP age∈[0,10]秒，strike取held-entry context，標記original-age-only；沒有升格為native或加入主sigma表。在這31個cross中，4個找到cross前1秒內、原quote_age≤2秒、same instrument的recorded bid；14個UP位置有當時保留的raw midpoint。Midpoint的freshness不足以保證fill，DOWN沒有以complement price補值。各bid／mark見market_level.csv。

直接PnL要求same run/slug/instrument、positive cost/qty、EXIT_POLICY_DECISION的net_if_exit在cross前1秒內；沒有合格值。沒有把較遠mark或entry price×別的時間點mid組成假PnL，也沒有做full breaker replay。

```text
CROSS_BEFORE_MINUS2_IDENTIFIABLE_MARKETS=0
CROSS_ALREADY_BELOW_MINUS2=0
CROSS_STILL_ABOVE_MINUS2=0
UNKNOWN=125
```

UNKNOWN=125以全部LIVE BUY市場為audit分母；其中已有可用observed-grid cross的31個，PnL亦全未知。其餘94個沒有可用first-cross事件。0/0不是0% early-warning，無法回答是否「常在−$2之前」；即使有point-in-time值，也不能自動證明之前從未進過−$2區域。

## 7. 決策與證據層級

```text
LEGACY_SIGMA_SIGNAL=WEAK
DIFFUSION_Z_SIGNAL=INSUFFICIENT
SIGMA_HAS_POTENTIAL_EARLY_WARNING=INSUFFICIENT
LIVE_THRESHOLD_JUSTIFIED=NO
SIGMA_USEFUL_AS_FEATURE=INSUFFICIENT
NEW_RUNTIME_SHADOW_REQUIRED=NO
```

**VERIFIED**：程式公式、retained檔案／manifest hashes、官方outcome provenance、snapshot數值、fill方向、market/day counts、各表分母。官方cache與sidecar、outcome CSV與summary checksum均一致；canonical P manifests逐檔驗hash。

**INFERRED／描述性**：相鄰sample之間的cross、first observed re-cross timing、sampled persistence、從signed required_move反推的raw-price geometry、legacy bucket與結果的弱關聯。沒有VERIFIED_CONTINUOUS market。

**INSUFFICIENT**：sigma相對TTE／distance的獨立增量價值、diffusion-z排序、LIVE −$2前的覆蓋率、production threshold。Selection受entry gates、先前path censor、revert後tail censor與只有6個主cohort日期限制，不能泛化為全部市場。

是否有用：legacy目前只支持研究候選，不能稱為已驗證的risk feature。若研究Flip Stop V1，可保留raw distance、TTE、raw sigma、diffusion z、source／target mode及方向對齊一起比較；本次沒有將它們加入runtime或制定threshold。需要獨立時間區段／事前固定規則的驗證，不能同資料選最佳規則再自稱validated。

本稽核不需新增專屬runtime shadow，現有retained path足夠完成上述受限描述；這不表示現有LIVE telemetry足夠驗證early-warning。日後若要解決LIVE問題，仍需合格同步position/PnL證據與更多完整native、不同日期樣本，是否新增capture應另行決定。Bot保持停止，沒有啟動採集。

## 8. 重用、來源與重跑

沿用ResearchStore的唯讀access、canonical native-v2 provenance、research_flip_analysis.MAX_GAP、canonical P export與retained official-resolution hash檢查；現有工具沒有first-filled-adverse-cross×sigma×persistence輸出，故只新增一支窄範圍research script。Public safety_sigma只作定義／coverage inventory，沒有加入主樣本。

兩份legacy DB沿用本對話前一audit已驗SHA256／quick_check=ok的/private/tmp staging copy：logs版6c76e140abdf981a99cebf4a7bc3589a3ea2d4aa6c8ed8512a2c4499fc8e8934；phase-B版778d85d13a53301e26b68aaf8a1ec46b45cd84d0862ba00b077262ebe4670439。未覆寫runtime DB，未更動cold archive。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/sigma_adverse_cross_audit.py \
  --legacy-db /private/tmp/flip-retained-audit/logs/hyperliquid_lead_lag.db \
  --legacy-db /private/tmp/flip-retained-audit/phase-b/hyperliquid_lead_lag.db
```

輸出：report.md、summary.json、market_level.csv、bucket_summary.csv、persistence_summary.csv、tte_summary.csv。NA／null保持缺失；沒有冗餘tick-level intermediate CSV。Market unit與settlement/revert/checkpoint denominators已核對，source tree tracked diff為空。Assessment是本報告對固定結果的研究判斷；重跑script產生數值表，不會自動將參數或判斷升格為production建議。

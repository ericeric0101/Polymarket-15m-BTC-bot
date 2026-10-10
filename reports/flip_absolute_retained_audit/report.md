# Flip Stop 與 conditional absolute breaker：retained-data audit

日期：2026-10-09（Asia/Taipei）。Source 全程唯讀；bot stopped。只新增 research script 與本報告，未修改 runtime、live/shadow events、DB schema，未 push。

```
EXISTING_DATA_SUFFICIENT=NO
NEW_RUNTIME_SHADOW_REQUIRED=NO
NEW_READONLY_ANALYSIS_SCRIPT_REQUIRED=YES
```

NO 的範圍是「目前 conditional breaker 與假想 Flip rule 的完整、可識別 LIVE 成對比較」。這不表示保留資料沒有研究價值，也不表示 Flip 沒有效果。NEW_RUNTIME_SHADOW_REQUIRED=NO 表示本次尚無理由要求新 Flip-specific runtime/shadow；新增 shadow 無法補回缺失的歷史狀態。本報告也不宣稱既有 telemetry 已足夠做未來完整驗證。

## 重用與證據

重用 `scripts/final_research_analysis.py:live_trades` 的 LIVE／official settlement cohort，與 `scripts/research_stop_analysis.py:load_paths` 的 canonical 路徑 freshness 篩選。現有 stop replay 的 226 positions 是 DRY-RUN shadow，HARD_LOSS_EQUIVALENT 也不是目前 conditional breaker；不能將其結果移植為本次 LIVE overlap。

新腳本 `scripts/flip_absolute_retained_audit.py` 的 DB 連線只用 mode=ro、query_only。冷封存只解壓到 `/private/tmp/flip-retained-audit`，未覆寫 runtime 路徑。兩份 cold DB 的 SHA256 與原 manifest 相符，獨立 PRAGMA quick_check 均為 ok：

| 封存 DB | SHA256 |
|---|---|
| legacy logs/hyperliquid_lead_lag.db | 6c76e140abdf981a99cebf4a7bc3589a3ea2d4aa6c8ed8512a2c4499fc8e8934 |
| legacy phase-B hyperliquid_lead_lag.db | 778d85d13a53301e26b68aaf8a1ec46b45cd84d0862ba00b077262ebe4670439 |

Official resolution cache 與 sidecar、outcome-provenance CSV 與 summary checksum 均已驗證。結果不以 journal spot outcome 當作最終權威。

## VERIFIED 與缺口

| 欄位／證據 | 狀態與範圍 |
|---|---|
| LIVE BUY fills、journal fill timestamp、成交價與數量 | VERIFIED recorded：136 rows，125 unique markets；不是 136 個獨立樣本 |
| 持倉 UP/DOWN | 125/125 可由 BUY FILL_MARKOUT identity 取得 |
| strike | 125/125 有 MARKET_STRIKE_LOCKED 記錄；數值存在不代表每個 reference snapshot 均有同期 strike/freshness join |
| final_resolution | 125/125 官方 winner；可得各持倉 WIN/LOSS |
| TTE | 可由 slug 的市場起點 +900 秒及 journal/snapshot timestamp 重建；是觀測時鐘，不是 venue 觸發時鐘 |
| legacy reference snapshots | 125 市場、16,754 rows 同 run/slug；TWAP、reference 與 age 欄位存在，但不具有 canonical native-v2 historical freshness 證明 |
| canonical accepted paths | 與這 125 LIVE 市場交集 0；不能用 10 月 shadow 路徑填補 9 月 LIVE 路徑 |
| 稀疏 LIVE PnL marks | 113 市場、2,169 positive-cost EXIT_POLICY_DECISION rows；VERIFIED 當時記錄值，非完整逐次 evaluation 路徑 |
| 同 instrument research BBO | 19 市場、23,888 rows；可作局部 quote 證據，尚不構成完整 exposure/cost/reference/state 的聯合路徑 |
| SHADOW_POSITION_MARK | 同 LIVE run/slug 不等於 LIVE position identity；候選模擬量不能取代實際持倉量 |
| stop-shadow candidates | 8 市場、33 rows；為 episode/多個 persistence candidates，不能各自算獨立交易，也不是完整 Flip crossing census |
| 歷史 absolute breaker observed submits | 18 市場、21 次 submit、4 台北交易日；VERIFIED decision_reason、送單時間及預估 net_if_exit。未證明每一筆都是目前版本／設定，未觸發紀錄也不能當成 current-rule negative |
| absolute_breaker_trigger_time | 僅 18 市場可填最早「已記錄送單時間」；不是第一次滿足條件的精確時間。CSV 明確標示此限制 |
| pnl_at_absolute_trigger | 上述 18 市場可填當時送單的 net estimate；不是 guaranteed fill PnL |
| flip_trigger_time、pnl_at_flip_trigger | 完整 VERIFIED 重建不可得；不把稀疏 episode 起點自動命名為 Flip trigger |
| current absolute first eligibility / nontrigger labels | 不可完整重建：缺少逐次 confirmed invalidation、signal locked/score、fair-at-entry／fair deterioration、two-vote components、components 變動重置與 persistence 狀態 |

目前 breaker 要求：啟用、最低持有時間、價格逆行與負 gross、net loss 門檻、confirmed adverse trend，以及 TTE≤120 秒或持續≥15秒且 available/weakening votes 均≥2。Components 改變會重置 persistence。只有 TWAP adverse cross 或 net≤−2 不能代替此判定。

另有一筆 STOP_SHADOW_ACTUAL_STOP（9/29 台北 22:26:22，market 1790691300），記錄 −5.10865623 USDC 的實際 stop PnL；它是 execution timestamp，不是 trigger timestamp。送單事件還存在較早嘗試，因此不能直接拿這個成交時間計 lead。其 SELL journal instrument token 與 stop payload identity 也有差異，完整 exposure reconciliation 應保持獨立稽核狀態。

## 十項結果

完整成對比較的 **eligible markets=0、eligible days=0**。NA 不等於零，亦不表示兩種 stop 不重疊。

| 要求 | 結果 |
|---|---|
| 1. overlap % | NA；主分母為完整 eligible markets，另應報 both/union；目前兩者均不可識別 |
| 2. Flip-only % | NA；不能將 absolute 未記錄當作未觸發 |
| 3. Absolute-only % | NA；不能將 Flip 未記錄當作未觸發 |
| 4. Flip lead median / mean | NA；需同市場 first eligible trigger pair，lead=t_absolute−t_flip，保留負值 |
| 5. Flip 時 PnL | NA |
| 6. Absolute 時 PnL | 成對/current-rule 結果 NA。僅歷史 descriptive observed-submit estimate：18 markets，mean −4.269019、median −3.940032 USDC；版本混合，不是目前 breaker 驗證 |
| 7. 理論 saved loss | NA；相對 absolute 應用同一 exposure 的 net_exit_flip−net_exit_absolute，另報 losers-only loss saved 與 winners opportunity cost |
| 8. Flip 後最後 WIN whipsaw | NA；分母需有官方 resolution 的 Flip-trigger markets，不能用已停止 absolute 市場的 WIN rate 替代 |
| 9. stop-vs-hold counterfactual net PnL | NA；需同期可售量、成本／entry fees、bid depth、slippage／exit fees、官方 payout。BBO/mid 和成交保證不同；fill ledger 的實際 exits-vs-hold 也不是 Flip policy |
| 10. 樣本数 | 原始 125 unique markets / 13 台北日 / 136 BUY fill rows；完整成對 0 markets / 0 days。逐市場、逐日見 CSV |

日分組以第一次 LIVE BUY fill 的 Asia/Taipei 日期為準，避免 UTC 跨日混淆。多 fills、episode、不同 thresholds 與 checkpoints 不增加獨立 market 數。沒有足夠的成對資料，因此未做 bootstrap、未掃 persistence／distance／TTE，也未挑最佳參數或宣稱驗證。

## 判斷

**Flip Stop 相對目前 conditional absolute breaker 的獨立 early-warning value：尚未證實、也未被否定。** 現有資料不能區分「同一 adverse confirmation 較早跨價」、「独立訊號」與「會 whipsaw 的提前出場」。目前不能據此支持新增 runtime Flip Stop 或設定 Flip V1 threshold。

如後續做探索性近似研究，應另行標示 observed-grid triggers、歷史版本 cohort、exposure censorship、freshness uncertainty 與 execution assumptions；不得將其升格為本報告要求的 VERIFIED 比較。任何參數探索都需要另一時間區段或事前固定規則的留出驗證。

重跑（需先依 archive manifests 還原並驗 hash，保持 bot stopped）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/flip_absolute_retained_audit.py \
  --cold-db /private/tmp/flip-retained-audit/logs/hyperliquid_lead_lag.db \
  --cold-db /private/tmp/flip-retained-audit/phase-b/hyperliquid_lead_lag.db
```

完整機器可讀結果：audit.json；逐市場：market_comparison.csv；逐日：day_counts.csv。所有 unknown trigger/PnL 保留 null／空白，不以零填補。

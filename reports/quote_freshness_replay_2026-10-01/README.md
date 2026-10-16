# Quote freshness 與兩場市場離線回放

日期：2026-10-01（Asia/Taipei）  
用途：研究資料與描述性回放；不構成交易績效或因果結論。

## 範圍與可重現輸出

- `twap_forward/`：由 `scripts/twap_forward_report.py` 產生的 TWAP、機率、資料品質與 lead/lag 報表。
- `empirical_probability/`：由 `scripts/empirical_probability_research.py` 產生的 checkpoint、校準、paired comparison 與 regime 報表。
- 使用本機 `data/research/twap_forward_shadow.db` 唯讀產生；原始 DB 與 Parquet 不在此提交。
- 回放涉及的近期 dry-run run：`run_1790853042_273606e6`，市場 `btc-updown-15m-1790853300`（結算 DOWN）與 `btc-updown-15m-1790854200`（結算 UP）。

## 主要觀察

1. 兩場的 T−120 `p_up_ex_market` 都與最後結算方向一致；但 Polymarket mid 當時也已朝結算方向定價。兩個市場只能作案例檢視，不能證明模型優於市場或具有可交易優勢。
2. 第二場中，`p_up_ex_market` 約在市場開始後 129.3 秒跌破 0.5；當時舊記錄的 UP mid 約 0.575，下一個可用 BBO 子影子樣本約在 130.9 秒、mid 約 0.475。這看似約 1.6 秒的先行，但兩者時間戳品質不同，且中間取樣稀疏，不能確認為可靠領先訊號。較大的 repricing 在此之前已開始：約 121.4 秒時 UP mid 已約 0.575，而開場決策參考約 0.695。
3. 同一段 BTC 1 秒歷史在該門檻前 30 秒、10 秒、5 秒報酬約為 −5.55、−1.48、−0.044 bps；前 30 秒成交量約 4.175 BTC、301 筆。這顯示短線價格走弱，但不能單憑這些資料判定是大額主動賣出、撤單，或證明它早於市場報價反應。
4. 這兩場可支持「以 BTC/結算參考獨立計算的機率可能與市場快速重估同步，個別時點或稍早」這個研究方向；目前尚未證明穩定領先，也沒有足夠證據可接入 live entry、止損或拒單決策。

## 時間戳與新鮮度限制

- 新的方向訊號路徑已要求驗證 quote source timestamp/age，過期 quote 不再更新 market consensus/EMA；本目錄中的舊市場 mid/BBO 記錄則沒有完整的 source-age 與 receive-age 證據。缺少 provenance 的列會被排除於 freshness-strict market-mid 校準，而非直接宣稱它們必定 stale。
- TWAP 報表中 freshness-proven market-mid checkpoint 為 0；因此不能據此比較模型與市場的 Brier，也不能把約 1.6 秒差值當作已驗證 lead。
- 兩場 dry-run 的市場開始早期及 rollover 附近存在資料捕捉缺口／稀疏 BBO-L2 配對；價格序列可描述局部變化，不能還原完整訂單簿撤單與成交因果。

## 歷史機率模型結果

56 日、只用評估時間之前資料的 1 分鐘 Binance OHLCV 回放，選出 259 個 checkpoint rows，但只有 11 筆可用 empirical estimates；可用樣本集中於 T−120 的 `PRE_FINAL_PROXY`。此處：analytic Brier = 0.0014、unconditional empirical Brier = 0.0081、vol-conditioned Brier = 0.0007；樣本僅 11 個市場，vol-conditioned 與 analytic 的 market-cluster bootstrap 95% CI 為 [−0.0020, 0.0001]，不足以確認改善。64 筆市場 mid 因缺少明確 source/receive age 被排除；這不是「市場一定 stale」的判定。

1 分鐘資料無法可靠估算 5/10/15/30 秒 forward return；exact final-window boundary 也不由 minute-close empirical model 評分。mid 是非可成交參考，以上都是機率評估，不是 PnL 或 edge。

## 結論與使用邊界

- 目前看到值得追蹤的候選現象：BTC 短線報酬／成交活動可能和機率及 mid 重估同時變化；但本次不能證明它可提前識別反轉或訂單簿撤單。
- 下一步應以 source/receive 時戳一致的 1 秒 BTC、fresh BBO/L2 與 `p_up_ex_market` 對齊，在更多獨立市場上比較變化時間；並把「訊號出現時可成交的 ask/bid 與深度」納入評估。
- `p_up_ex_market`、TWAP projection 及本回放均維持 shadow/research-only；沒有新增 live authority，也沒有用本報告調整任何策略參數。

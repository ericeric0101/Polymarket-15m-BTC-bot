# 近平手（near-tie）TWAP 結算標籤防護

## 問題

Runtime 結算唯一依據是 canonical Chainlink 60s TWAP 標籤
（`bot/lifecycle_runtime.py::_canonical_twap_shadow_label` → `canonical_settlement_outcome`，
commit bbbdc09）。結算在進入 SETTLING 時執行（約 market end + 0.4 s），此時手上最新的
TWAP tick 通常是 end − 1 s 或 end − 2 s 蓋章的值，**不是**官方結算用的 end 時刻 60s 平均。
少了的 k 秒樣本會讓最終 TWAP 再移動約 `k/60 ×（新價 − 被移出價）`，當 TWAP 與 strike
距離很小時，這點移動就足以翻轉方向。

## 證據

來源：`data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv`
（官方快取 sha256 `5356cf95…`）。437 個市場同時有官方結果與 canonical TWAP，
其中 2 個衝突（`HIGH_OFFICIAL_TWAP_CONFLICT`），兩者都是近平手、且最後一個 tick 在 end 之前：

| market | 最後 TWAP | strike | margin | tick − end | tick age @結算 | runtime | 官方 |
|---|---|---|---|---|---|---|---|
| btc-updown-15m-1791039600 | 84828.4032 | 84828.5775 | −0.17 USD / **−0.021 bps** | −4 s | 4.38 s | DOWN | UP |
| btc-updown-15m-1791144000 | 85406.9064 | 85412.0000 | −5.09 USD / **−0.596 bps** | −2 s | 2.50 s | DOWN | UP |

TWAP 值／strike 取自 `logs/trade_journal.db` 的 `MARKET_SETTLEMENT`（`reference_source =
polymarket_chainlink_twap_60s_ws` 時 `spot` 欄即 TWAP 值）；tick 時刻取自
`data/research_export/P_paths/*/canonical_events.parquet` 的 `MARKET_TWAP_SUMMARY`
（`summary_ts − settlement_reference_age_sec`）。

437 個市場最後 tick 相對 end 的位移分布（433 個有時序）：0 s：5、±1 s：164、±2 s：253、
±3 s：5、±4 s：4、7 s：1、9 s：1。也就是說，結算當下幾乎從來沒有「剛好在 end 蓋章」的 tick。

## 規則

```
margin_bps = (TWAP − strike) / strike × 1e4
offset     = |tick source_ts − market_end_ts|      （market end 未知時改用 tick age）
band_bps   = 0.5 × offset                           （NEAR_TIE_BPS_PER_OFFSET_SEC = 0.5）
|margin_bps| < band_bps  →  標籤非 canonical → UNKNOWN
```

- tick 剛好在 end 蓋章（offset 0）時 band = 0，一律照常判定；TWAP == strike 仍判 UP（`>=`）。
- end 之後的 tick 也算位移（含了 window 外的價格）。
- 超出 band 的市場行為完全不變；既有的 stale／缺 TWAP／錯 window → UNKNOWN 規則不變。
- UNKNOWN 沿用既有路徑：寫 `MARKET_SETTLEMENT{outcome: UNKNOWN, settlement_pending: true}`，
  不寫 `MARKET_CYCLE_PNL`、不動 session PnL／regime guard，由啟動時 Gamma reconciliation 依官方結果補齊。
- 新增 provenance 欄位：`settlement_reference_margin_bps`、`settlement_reference_end_offset_sec`、
  `settlement_near_tie_band_bps`、`settlement_near_tie_unresolved`。
- 未改任何交易門檻。

係數 0.5 bps/s 的依據：第二個衝突需要 > 0.298 bps/s 才能攔下（0.596 bps / 2 s），
0.5 約 1.7 倍餘裕；物理上相當於「新進樣本與被移出樣本平均相差 30 bps」才會在 1 s 內移動 0.5 bps。

## 為何不做「短暫等待 end tick」

`_update_market_phase()` → `_transition_market_phase(SETTLING)` → `_record_market_settlement()`
會在 quote 事件迴圈（`bot/quote_runtime.py:154`）上被呼叫，也會在 lifecycle timer thread 上被呼叫。
在事件迴圈上阻塞等待數秒會凍結報價／成交處理，不安全。改為非阻塞的延遲重標（由 timer 在
bounded 時間內重試）需處理 rollover 時 slug／inventory 重置的競態，屬後續工作。目前採「要求」版：
近平手且 tick 不在 end → UNKNOWN。

## 影響量化（437 個比較市場）

| 類別 | 市場數 | 移至 UNKNOWN |
|---|---|---|
| journal 有 TWAP 值、可精確計算 margin | 389 | **27**（6.9%），含 **2/2 個衝突** |
| journal 無 TWAP 值，以 end 前最後一筆 path 事件近似 | 26 | 約 4 |
| 完全無 margin 資料 | 22 | 未知（依 6.9% 推估約 1–2） |
| **合計** | **437** | **27 確定，估計約 31–33（≈7%）** |

被移到 UNKNOWN 的 27 個中 25 個 runtime 標籤本來就與官方一致（代價：這些週期的 PnL 延後到下次啟動
reconciliation 才入帳），2 個是原本標錯的衝突市場（收益：不再寫入錯誤的 cycle PnL／guard 輸入）。
對照：固定 1.0 bps band 會移動 37 個、0.3 bps/s 會移動 13 個（剛好攔下兩個衝突、無餘裕）。

注意：journal `spot` 與 export summary 的 tick 並非總是同一筆（在上述 27 個近平手市場中，
journal 與 summary 標籤有 5 例不一致），這本身也顯示近平手標籤在 1 s 內就會翻轉；margin 數值僅作估計。

## 測試

`tests/test_near_tie_settlement_guard.py`：兩個衝突市場重播 → UNKNOWN（修正前為 DOWN，已以
`git archive HEAD` 至 `/tmp` 驗證失敗）；同值但 tick 在 end → 照常判定；band 隨位移縮放；
end 未知時用 age；helper 欄位。`tests/test_settlement_authority.py` 的 Host 預設改為 tick 在 end 蓋章，
維持原測試意圖（精確平手、rounding 語意）。

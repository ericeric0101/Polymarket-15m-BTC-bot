# canonical_events `fast_spot` 來源修正與舊資料還原 — 2026-10-15

## 程式修正（branch `fix/shadow-fast-spot-provenance`）

- `bot/spot_pricer.py`：settlement diagnostics 不再輸出 `fast_spot`；路徑 spot 只放在既有的 `path_spot`／`path_spot_source`。
- `bot/twap_forward_shadow.py`：diagnostics 不得覆寫觀察器自己的欄位（`fast_spot`、`fast_spot_source`、`chainlink_spot`、`binance_minus_chainlink_bps`）。新增兩個只供研究的欄位：
  - `chainlink_spot`：只有 `path_spot_source == polymarket_chainlink_spot` 時才填。Binance 備援的 path spot 和 `fast_spot` 是同一個來源，不能算價差。
  - `binance_minus_chainlink_bps`。
- 完整 payload 會寫入 canonical events，所以 export 不用改就會帶出新欄位。
- 測試：新增 3 個（覆寫回歸、價差欄位只來自 Chainlink、diagnostics 不輸出 `fast_spot`），並把 3 處舊斷言改為 `path_spot`。全套測試 1813 passed。
- 沒有改任何交易決策、門檻或設定。

## 舊資料還原（只讀，輸出為衍生檔）

`binance_spot_reconstructed = official_current_twap × (1 + spot_minus_twap_bps / 1e4)`。`spot_minus_twap_bps` 是在被覆寫之前，用原本的 Binance 參數算出來的。

| 驗證（與 P_paths `btc_spot` 在 1 秒內配對，7,037 對） | bps |
|---|---:|
| 還原值誤差中位數（絕對值） | ≈ 1e-12 |
| 原 `fast_spot` 與 Binance 的差（中位數） | −1.70 |

輸出：`canonical_events_spot_provenance_corrected.parquet`（29,389 列，含 `binance_spot_reconstructed`、`chainlink_spot`、`binance_minus_chainlink_bps`、`fast_spot_was_overwritten`）。凍結的 export 沒有被修改（`inputs_unchanged = true`）。

## Binance − Chainlink 價差會漂移

| export 日 | Chainlink 列數 | 價差中位數（bps） |
|---|---:|---:|
| 09-30 | 317 | 6.75 |
| 10-01 | 1,410 | 5.53 |
| 10-02 | 5,819 | 3.13 |
| 10-03 | 5,772 | 1.85 |
| 10-04 | 4,676 | 1.63 |
| 10-08 | 1,204 | 7.88 |
| 10-09 | 3,161 | 9.11 |

價差不是常數，一週內就在 1.6–9.1 bps 之間變動。任何價差修正都必須用決策前的滾動估計，不能用固定值。09-25～09-29 和 10-05～10-07 的 export 沒有 Chainlink spot，無法計算價差。

## 重現

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python reports/fast_spot_provenance_backfill_20261015/backfill.py
```

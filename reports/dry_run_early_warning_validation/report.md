# DRY-RUN Early-Warning Validation：1 warm-up + 4 個完整市場

日期：2026-10-10（+08）。這是觀測與訊號時序實驗，**不是獲利測試**。

- 模式：DRY-RUN（`./.venv/bin/python run_bot.py`，沒有 `--live`）。只產生 `ORDER_DRY_RUN_*`，沒有任何 wallet 訂單。
- 沒有改任何 threshold、sizing、breaker、guard、settlement、backup 或 rollover 設定，也沒有 push。
- 所有 exit 經濟數字一律標為 SHADOW／COUNTERFACTUAL。本報告沒有計算 exit 經濟，因為 measured 市場沒有 shadow 持倉。

## 1. Run 概況

| 項目 | 值 |
|---|---|
| HEAD | `e921152`（tree clean） |
| DRY_RUN_ID | `run_1791602735_398a83d0`（`TEST_DRY_RUN`） |
| config hash | `087867b1…`（啟動前計算 == manifest） |
| 開始／SIGINT／process 結束（UTC） | 03:25:17／04:46:00／04:47:14 |
| 總時長 | **81 分 57 秒**（90 分硬上限未觸及） |
| 啟動時進行中的市場 | `1791602100`：排除（只觀測到 TTE 224 s 之後）；有 1 筆 production-path shadow fill |
| Warm-up | `1791603000`（11:30 +08），不計入 cohort |
| Measured 1–4 | `1791603900`、`1791604800`、`1791605700`、`1791606600`（11:45–12:45 +08） |
| 狀態 | M1–M3 = VALID_WITH_GAPS（只有 token quote freshness 缺口），M4 = VALID |
| measured 期間的 shadow 進場 | **0**。17 次 dry-run submit，16 次 cancel，1 筆 fill（在 startup 市場） |
| 官方結果 | warm-up DOWN；M1 UP、M2 UP、M3 DOWN；M4 **PENDING**（尚未 closed；價格 0.9995/0.0005，runtime 標籤 UP） |

結束流程乾淨：forced backup 4.6 s 完成；graceful-exit journal retention exit 0；process 數 = 0。

**重要限制：** 4 個 measured 市場都沒有進場。因此每個市場的訊號追蹤都是 **REFERENCE_ONLY**：以 TTE≈580 s 時的市場熱門方為「假想持倉側」，用它當下的 executable bid 當 baseline。這**不是持倉**，也不是 production 會做的進場。

## 2. 各市場時間線（REFERENCE_ONLY；1 s snapshot 網格，本地接收時鐘 → APPROX_SAME_SECOND）

| 市場 | 熱門方 @TTE580（bid） | 觀察到的順序 | 結果 |
|---|---|---|---|
| M1 1791603900 | UP 0.75 | ENTRY → TOKEN_005（TTE 520）→ TOKEN_010（TTE 487） | UP（official）→ **假警報**；0.10 在 34 s 後回復，之後回到進場 bid |
| M2 1791604800 | UP 0.89 | ENTRY → BINANCE_3（TTE 236） | UP → **假警報**；26 s 回復；token DD 始終 ≤0 |
| M3 1791605700 | DOWN 0.76 | ENTRY → SPOT_CROSS（**進場時就已成立，basis 造成**） | DOWN → 無真實警報 |
| M4 1791606600 | DOWN 0.67 | ENTRY → SPOT_CROSS（basis，進場時）→ TOKEN_005≈010≈015≈020（TTE 531）→ BINANCE_3（TTE 247）→ TWAP_CROSS（TTE 234） | runtime UP（official PENDING）→ 熱門方最後輸，**真實惡化** |

M4 的細節（1 s 路徑）：
- 04:36:08→09，DOWN bid 在 1 秒內從 0.64 掉到 0.46。同一秒 Binance 跳了約 +1.7 bps（未達 3 bps 門檻）。
- 約 80 秒後 bid 回到 0.64（token_005 首次回復 79.7 s）。之後又下跌：TTE 189 時 0.28，TTE 50 時 0.02。
- Binance ≥3 bps 在 TTE 247 才觸發，比 TWAP cross 早 12.5 s。
- 成對 lead：TOKEN_010→TWAP_CROSS = 296 s；BINANCE_3→TWAP_CROSS = 12.5 s；BINANCE_3→TOKEN_xxx = −284 s（token 先）。

## 3. 各訊號觀察（measured n=4，只做描述）

| 訊號 | 觸發市場數 | TTE | 持續 5/15/30/60 s | 回復 |
|---|---|---|---|---|
| BINANCE_3 | 2（M2、M4） | 236、247 | M2: Y/Y/N/N；M4: Y/Y/Y/Y | M2 26 s 回復（贏家） |
| BINANCE_5 | 0（最大 3.46 bps） | — | — | — |
| TOKEN_005 | 2（M1、M4） | 520、531 | M1: Y/Y/UNKNOWN/UNKNOWN；M4: Y/Y/Y/Y | M1 117 s、M4 80 s 回復 |
| TOKEN_010 | 2（M1、M4） | 487、531 | M1: Y/Y/Y/N；M4: Y/Y/Y/N | 34 s／45 s 回復，之後 M4 再惡化 |
| TOKEN_015 | 1（M4） | 531 | Y/Y/Y/N | 44 s 回復，之後再惡化 |
| TOKEN_020 | 1（M4） | 531 | Y/Y/Y/N | 44 s 回復，之後再惡化 |
| SPOT_CROSS（Binance） | 2（M3、M4，皆為**進場時已成立**） | 580、581 | — | basis 汙染，見下 |
| TWAP_CROSS | 1（M4） | 234 | Y/Y/Y/Y | 不回復 |

- startup 市場（不計入）：shadow fill UP @0.68 於 TTE 220。TOKEN_005／010／015 在 16 s 內觸發，Binance 完全沒動，0.15 在 4.7 s 回復，最後 UP 贏。這是 token 單獨的假警報。
- TTE 分桶：token 訊號都在 >300（487–531）；Binance／TWAP 在 180–300（234–247）。n 太小，不推門檻。
- **sigma／diffusion-z：** 覆蓋率 98–99.8%（VERIFIED）。警報當下的 legacy sigma 為：
  - M4 token：0.25（<0.5，最後輸）
  - M1 token：0.32–0.42（<0.5，回復）
  - M2 Binance：1.6（1–<2，回復）

  樣本太小 → **INSUFFICIENT**。另外，legacy sigma 在接近到期時會暴衝（最大 78–842），這是公式設計使然，不是資料錯誤。

## 4. 資料品質

| 項目 | 結果 |
|---|---|
| Snapshot 缺口 >10 s | 0（所有市場；中位間隔 1.2 s） |
| Binance | 新鮮度 100%，無缺口 |
| TWAP | 新鮮度 98.8–100%；1 次 silent stall + 1 次 WS disconnect + 1 次 TWAP_REFERENCE_DEGRADED（M3），沒有 >10 s 的缺口 |
| Token quote（`market_quote_fresh`，需**兩個** token 都新鮮） | 共 9 段 >10 s 的 stale：warm-up 18 s、M1 13 s、M2 15/66/20/15 s、M3 11/11 s。多數集中在最後 2–3 分鐘，屬於一側 book 清空（INFERRED，非 feed 中斷）。這些期間的 token 訊號記為 UNKNOWN |
| L2 新鮮度、exit-size depth | DRY-RUN 不跑 protective 路徑 → **UNAVAILABLE** |
| PROTECTIVE_EVAL_GAP、STOP_TIMING_*（含新的 E1/E3/E4 欄位） | 0 筆。兩者都是 LIVE 專用路徑，DRY-RUN 無法驗證 |
| SLOW_CONSUMER_CALLBACK | 17 次 |

**Binance strike cross 的問題：** Binance 相對 Chainlink 有約 9–10 bps 的 basis（各市場 bnD − twD = 9.0／9.4／10.2／9.0 bps）。因此用 Binance 對 Chainlink-based strike 判斷 cross 會有系統性偏差：熱門方是 DOWN 時，進場就已「adverse」。snapshot 沒有 Chainlink spot 欄位 → 乾淨的 spot cross **UNAVAILABLE**。

## 5. Storage

| 時點 | journal (B) | research (B) | 可用 GiB | events (strategy/order) |
|---|---|---|---|---|
| T0 | 2,417,172,480 | 158,466,048 | 14.08 | — |
| T1（warm-up 後） | 2,422,386,688 | 163,586,048 | 13.64 | 1152/688 |
| T2 | 2,424,885,248 | 168,910,848 | 13.44 | 1773/1067 |
| T3 | 2,427,199,488 | 173,998,080 | 13.28 | 2425/1267 |
| T4 | 2,430,038,016 | 179,343,360 | 13.23 | 3083/1636 |
| T5 | 2,432,196,608 | 183,275,520 | 13.18 | 3565/1876 |
| T6（關機後） | 2,432,270,336 | 183,357,440 | 17.50（retention 清掉約 4.3 GiB） | 3581/1876 |

- journal：DB_GROWTH 14.40 MiB；10.5 MiB/h；約 2.3 MiB／完整市場。
- research DB：23.7 MiB；17.4 MiB/h。
- 沒有 event explosion，沒有 VACUUM。

## 6. 主要問題回答（描述性，不宣稱顯著）

1. **Binance 3 bps 會先於 token 惡化嗎？** 這次沒有。唯一能比較的 M4 是 token 先（但它和一次 1.7 bps 的 Binance 跳動在同一秒）。M2 是 Binance 觸發、token 完全沒動 → INSUFFICIENT。
2. **Binance 5 bps 能減少假警報嗎？** 這次沒有觸發（最大 3.46 bps）→ INSUFFICIENT。
3. **token DD 各門檻相隔多久？** M4 四個門檻在同一秒觸發（跳空）；M1 的 0.05→0.10 相隔 34 s。
4. **spot cross 先於 TWAP cross 嗎？** Binance spot cross 被 basis 汙染，無法回答。
5. **TWAP 落後多少？** M4：比 token DD 晚 296 s，比 Binance 3 bps 晚 12.5 s。
6. **哪些訊號能持續 5／15／30／60 秒？** M4 的 BINANCE_3、TOKEN_005、TWAP_CROSS 全部 Y/Y/Y/Y；token 0.10/0.15/0.20 撐到 30 s，60 s 時中途回復，之後才真正崩跌。
7. **哪些很快回復？** startup 市場 token 0.15（4.7 s）、M2 Binance 3（26 s）、M1 token 0.10（34 s）——都是最後贏的熱門方。
8. **TTE 影響？** token 警報出現在 TTE 487–531，Binance／TWAP 在 234–247。描述上 token 警報較早，但 n 不足以下結論。
9. **sigma 與持續／回復的關係？** INSUFFICIENT。
10. **token ≥0.10／0.15 比 Binance 穩定嗎？** MIXED：M4 是真實且早的警報；M1（0.10）與 startup 市場（0.15）是回復的假警報；M2 中 Binance 單獨誤報時 token 沒動。
11. **沒進場的市場也有很多警報嗎？** 有。4 個 measured 都沒進場，其中 3 個出現警報（M1、M2 是假警報，M4 是真實的）。
12. **有缺口妨礙時序比較嗎？** Binance／TWAP 沒有；token 在收盤前的一側 book 清空時有缺口；spot cross 定義需要修正。

## 7. 與歷史 audit 比較 → HISTORICAL_AUDIT_CONSISTENCY = **INSUFFICIENT**

| 歷史觀察 | 這次 |
|---|---|
| Binance 早但雜 | MIXED：雜（M2 假警報）；早則被反駁（M4 比 token 晚 284 s） |
| token DD 0.10／0.15 最有希望 | INSUFFICIENT：M4 真實且比 TWAP 早 296 s；M1、startup 市場是假警報 |
| TWAP 晚、只能當確認 | SUPPORT |
| sigma INSUFFICIENT | 仍是 INSUFFICIENT（但覆蓋率已到 98–99%） |
| 9 月 21 s 網格無法解析 token persistence | SUPPORT：1 s 網格已能解析 5／15／30／60 s |

## 8. READY_FOR_12_PLUS_LIVE_SHADOW = **CONDITIONAL**

資料管線本身可用：Binance、兩側 executable bid、官方 TWAP、strike、TTE、sigma、z 都有約 1 s 解析度。下一輪 LIVE shadow 前要處理或接受：

1. Spot strike cross 需要不受 basis 影響的定義（或把 Chainlink spot 加進 snapshot）。這是 telemetry／研究定義問題，不是交易邏輯。
2. LIVE 專用的 STOP_TIMING E1/E3/E4 欄位和 PROTECTIVE_EVAL_GAP 沒有在 DRY-RUN 驗證，要在第一個 LIVE 市場確認有寫入。
3. Token quote freshness 是「兩側都新鮮」的判定，收盤前會頻繁 stale。研究端應改用持有側的 quote freshness。
4. 這次 measured 市場沒有任何 shadow 進場，只有 REFERENCE 追蹤，不能評估持倉經濟。

本次不據此調任何門檻，也不產生任何 SELL 權限。

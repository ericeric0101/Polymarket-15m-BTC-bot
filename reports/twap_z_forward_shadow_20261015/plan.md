# TWAP 難度 z 候選策略：forward shadow 事前登記規格

寫定時間：2026-10-15（Asia/Taipei），在任何評估資料產生之前。commit 之後，本檔的規則、門檻與判讀不得修改；若必須修改，另開新版本檔並把舊版視為已作廢，已收集的市場不得重用於新版本的確認性判讀。

## 0. 背景與假說

探索性研究（`twap_opportunity_tail_risk_20261014`、`twap_spot_basis_mechanism_20261014`）在 Oct 2–6 DEV 資料上發現：最後 60 秒、market mid 仍未極端、但「結算 TWAP 翻盤難度」z 已偏向一側時，買 z 支持的一側在價差修正後為 12 勝 1 負、費後 +1.48（每股、記錄 ask）。樣本小、資料已被研究過，必須在新市場上驗證。

**H1：** 在新的市場上，依本規格選出的交易，持有到官方結算，費後每市場平均損益 > 0。

本 shadow 不下單、不改 bot 任何行為，只用 forward 實驗本來就產生的資料離線評估。

## 1. 資料

| 用途 | 來源 | 規則 |
|---|---|---|
| 盤口、strike、sigma、`required_future_avg_to_flip` | forward checkout 的 `data/research_export/P_paths/<day>/paths_native_v2.parquet` | 只讀；只用 manifest `complete_day = true` 的日期 |
| Chainlink／Binance／Coinbase 每秒價格 | `data/price_sources_1s/`（獨立記錄器） | 只讀 |
| 結果 | Polymarket Gamma 官方結算 | 只用 `official_outcome ∈ {UP, DOWN}` |

**評估母體：** `market_start_ts ≥ 2026-10-15T00:15:00Z` 的 `btc-updown-15m-*` 市場。開始日期在記錄器啟動之後（00:01 UTC），保留一個市場的緩衝。

## 2. 決策網格與資料品質（與探索研究相同）

- 每個市場在 T−55, T−50, …, T−5 秒做決策，`decision_ts = market_start + 900 − h`。
- 盤口快照：同市場、`snapshot_ts ≤ decision_ts` 且相差 ≤ 3 秒的最近一筆。
- 合格列：`required_move_mode == EXACT_FINAL_WINDOW_BOUNDARY`、`twap_fresh`、`sigma_ex_market_fresh` 且 > 0、`required_future_avg_to_flip > 0`、`remaining_final_window_sec > 0`、`market_mid_fresh`，且至少一側盤口有效（`0 ≤ bid ≤ ask`，`0.001 ≤ ask ≤ 1`）。
- `mid` = UP 側有效時取 UP 的 (bid+ask)/2，否則取 1 − DOWN 側的 mid。

## 3. z 定義

分母共用：`S × σ × sqrt(remaining_final_window_sec / (3 × 365 × 86400))`，σ 為 P_paths 的 `sigma_ex_market`，S 為該變體使用的 spot。

| 變體 | spot S | 角色 |
|---|---|---|
| **ZC（主要）** | 記錄器 `chainlink_spot`：`last_received_ms ≤ decision_ts` 且相差 ≤ 3 秒的最近一筆 | 確認性判讀只看這個 |
| ZB | 記錄器 `binance_btcusdt` 同樣 as-of，乘以 (1 − b̂/1e4)；b̂ = 決策前 3600 秒內，同一秒同時有 Binance 與 Chainlink spot 的配對中位數，需 ≥ 30 對 | 次要，對照 |
| ZK | 記錄器 `coinbase_btcusd` 同樣 as-of，不修正 | 探索，對照 |

`z = (S − required_future_avg_to_flip) / 分母`。變體 spot 不可用時，該列在該變體下不合格。

## 4. 交易規則（固定）

1. 合格列中 `0.10 ≤ mid ≤ 0.90` 且 `|z| ≥ 2`。
2. 買 z 指向的一側（z > 0 買 UP，z < 0 買 DOWN），且該側盤口有效。
3. 每個市場只取時間上第一筆符合的列。
4. 假設以該列記錄的 ask 買 1 股，持有到官方結算。
5. 費用：`0.07 × ask × (1 − ask)`；每市場費後損益 `net = win − ask − fee`。

敏感度（只描述、不參與判讀）：mid 範圍 [0.05, 0.95]。

## 5. 樣本量與停止規則

- **主要分析時點：** ZC 主規則選出的市場數 N 首次 ≥ 50 時。
- 期間只允許描述性的中途查看（市場數、資料覆蓋率），**不看損益**、不提前下結論。
- 若記錄器或 forward 匯出長期中斷，從評估開始起滿 21 天仍未達 N = 50，就以當時樣本做判讀，並標明「樣本不足」。
- INCONCLUSIVE 時可延長一次到 N = 100，之後一律結案。

## 6. 判讀（寫定）

令 `m` = ZC 主規則的每市場平均費後損益，`LB` = 市場層級 bootstrap（10,000 次、seed 20261015）的單側 95% 下界。

**PASS**（值得進入「可成交性」驗證，例如只下最小單位的實盤試驗提案）必須同時滿足：
1. `m > 0` 且 `LB > 0`；
2. 拿掉獲利最大的一個市場後，合計費後損益仍 > 0；
3. 任何單一 UTC 日的費後損益不超過全部合計的 50%（合計 > 0 時才計算）；
4. 買 UP 與買 DOWN 的比例都 ≥ 25%；
5. 延遲 1 秒的報價檢查：在有 +1 秒同側 ask 的市場上，以該 ask 重算的平均費後損益 > 0。

**FAIL**（候選結案）：`m ≤ 0`。

**INCONCLUSIVE**：其他情況。

ZB、ZK 與敏感度範圍的結果只作描述，不改變以上判讀。

## 7. 一併報告（描述性）

- 每日資料覆蓋：市場數、合格列數、各來源 spot 可用率、因記錄器缺資料而不合格的列數。缺資料只計數，不補值、不算輸。
- 每市場明細：決策時點、mid、z（三種）、選邊、ask、結果、費後損益。
- 延遲 +1 秒、+3 秒的同側 ask（不代表當時可成交；缺值不外推）。

## 8. 限制（事先承認）

- 用記錄的 ask，沒有深度、排隊與成交模型，PASS 也不代表實盤可賺。
- forward 實驗的 bot 版本若改變匯出欄位或 freshness 語意，需在報告中註明受影響的日期。
- 記錄器與 bot 是不同程序、不同連線，收到 Chainlink 的時間可能和 bot 不同。

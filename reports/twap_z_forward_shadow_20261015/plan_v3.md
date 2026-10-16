# TWAP-z 多市場 forward shadow：事前登記規格 v3

**寫定時間：** 2026-10-16 約 10:20（Asia/Taipei）。此時還沒有任何 v3 市場的資料被收集或分析。commit 之後，本檔的規則、門檻與判讀都不得修改。

**和 v2 的關係：**
- `plan_v2.md` 繼續只管 BTC 15m，不受本檔影響；BTC 15m 的市場**不計入** v3。
- v3 把同一條規則套到其他以 Chainlink 60 秒 TWAP 結算的 Up/Down 市場。
- **目的：** 增加可交易頻率，並且獨立驗證 H1：結算窗口的機制，讓最後一分鐘的 z 比市價更準。

## 1. 範圍

| 串流 | 市場長度 | Gamma slug |
|---|---|---|
| BTC | 5m | `btc-updown-5m-<start>` |
| ETH | 15m、5m | `eth-updown-15m-<start>`、`eth-updown-5m-<start>` |
| SOL | 15m、5m | `sol-updown-15m-<start>`、`sol-updown-5m-<start>` |
| XRP | 15m、5m | `xrp-updown-15m-<start>`、`xrp-updown-5m-<start>` |

**為什麼這 7 個串流：** 2026-10-16 在 Gamma 確認過，全部的規則文字都是「Chainlink TWAP，結束時 ≥ 開始時則為 Up」，`resolutionSource` 是各自的 `*-usd-twap-60s-streams`。

**評估母體：**
- 條件：`market_start ≥ 2026-10-16T03:00:00Z`，而且市場結束已超過 10 分鐘。
- 起點的由來：v3 記錄器在這個時間之前啟動。

## 2. 資料（全部唯讀）

| 用途 | 來源 |
|---|---|
| ETH/SOL/XRP 的 Chainlink spot 和 60 秒 TWAP（依 Chainlink 來源秒分組） | `data/price_sources_1s/`：`source = chainlink_spot:<asset>`、`chainlink_twap60:<asset>` |
| BTC 的 Chainlink spot 和 TWAP | 沿用 v2 的 `chainlink_spot`、`chainlink_twap60` |
| 各市場 Up/Down 的最佳買賣價，每秒一筆 | `data/polymarket_book_1s/` |
| 官方結果 | Polymarket Gamma（只用 `UP`／`DOWN`） |

## 3. 結算機制的驗證（評估之前，只看機制，不看結果和損益）

- **做法：** 對每個新幣種，用母體開始後最早的 2 小時資料，照 v2 第 1.2 節的方法，驗證「TWAP 戳記在第 S 秒＝第 S+offset 到 S+offset+59 秒的 spot 平均」。
- **結果的使用：** 誤差中位數最小的 offset，寫入 `mechanics_v3.json` 並 commit，之後不再改。
- **排除條件：** 某幣種的最佳 offset 重建誤差 p99 > 0.5 bps，或者 offset 不一致，該幣種整個排除，並在報告中說明。
- 5m 和 15m 共用同一個幣種的 offset，因為它們是同一條 TWAP 串流。

## 4. 規則（與 v2 第 2–4 節相同，只換資料）

- **決策時點：** 每個市場在 T−55、T−50 … T−5 秒（T 是市場結束時間）。5m 和 15m 相同，因為結算窗口都是最後 60 秒。
- **計算：** strike、`required_future_avg_to_flip`、`remaining`、σ（最近 300 秒、至少 240 筆報酬）、盤口有效性和 `mid`，定義全部和 v2 第 1.3 節相同；只換成該幣種的 Chainlink 序列。
- **z：** 只用 ZC（Chainlink spot），公式與 v2 第 3 節相同。v3 沒有 ZB／ZK 變體。
- **交易規則：**
  1. 合格列中 `0.10 ≤ mid ≤ 0.90` 且 `|z| ≥ 2`；
  2. 買 z 指向的一側，且該側盤口有效；
  3. 每個市場只取時間上第一筆符合的列；
  4. 以該列的 ask 買 1 股，持有到官方結算；
  5. `net = win − ask − 0.07 × ask × (1 − ask)`。

## 5. 樣本量與停止規則

- **主要分析時點：** 7 個串流合計、規則選出、且有官方結果的市場數**首次 ≥ 150**。
- **在那之前：** 只看資料覆蓋率、機制驗證和重建結算方向的一致率，**不看損益**。評估程式會強制隱藏損益。
- **期限：** 從母體開始滿 21 天仍未達 150，就以當時樣本判讀，並標明「樣本不足」。
- **延長：** INCONCLUSIVE 時可以延長一次到 N = 300，之後一律結案。

**為什麼是 150：** 每個市場的標準差約 0.44（v2 的 null）。N = 150 時，MDE 約 0.10／股（80% power），和 DEV 估計的每市場約 +0.11 同一量級。

## 6. 判讀

令 `m` = 每市場平均費後損益，`LB` = 單側 95% 下界。

**LB 的計算：** 用 **cluster bootstrap**，10,000 次，seed 20261016。cluster 是**市場結束時間**：同一秒結束的不同幣種、不同長度的市場算同一群，因為它們的價格同向波動，不能當成獨立樣本。

**PASS** 必須同時滿足：
1. `m > 0` 且 `LB > 0`；
2. 拿掉獲利最大的一個 cluster 之後，合計仍 > 0；
3. 任一 UTC 日的損益不超過合計的 50%；
4. 買 Up、買 Down 的比例都 ≥ 25%；
5. 延遲 1 秒的同側 ask 重算後，平均費後損益 > 0；
6. **任一串流的損益不超過合計的 50%。**

**FAIL：** `m ≤ 0`，v3 結案。
**INCONCLUSIVE：** 其他情況。

**一併報告（只描述）：**
- 每個串流的 N、m、勝率、平均 ask；
- 5m 對 15m 的比較；
- 決策時 `ask_size ≥ 5.5` 的比例，也就是可執行性；
- 每日覆蓋率；
- 重建結算方向和官方結果的一致率。

## 7. 和執行器、小額 live 的關係

- **執行器的 DRY-RUN：** `scripts/twap_z_executor.py` 對全部 8 個串流做 DRY-RUN，規則和本檔第 4 節相同。
- **一個串流加入 P2 小額 live 的條件：**
  - 第 3 節的機制驗證通過；
  - 執行器和記錄器 shadow 在該串流的觸發一致率 ≥ 90%；
  - 這兩項都只看覆蓋率，不看損益。
- **P2 的金額上限：** 每筆 5.5 股、每日 $15、累積 $50，由所有串流**共用**，不是每個串流各自一份。
- **live 的定位：** 小額 live 是量測，不是判讀；**放大部位只能在 v2 或 v3 判讀為 PASS 之後**。
- **評估程式：** `evaluate_v3.py` 必須在合計樣本達到 75 之前 commit，而且寫的時候不得看任何損益。

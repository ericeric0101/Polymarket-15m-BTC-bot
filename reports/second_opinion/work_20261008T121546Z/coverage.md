# Coverage ledger (final, TS=20261008T121546Z)

## 2. 覆蓋範圍與限制

| 區域 | 狀態 | 說明 |
|---|---|---|
| Pass 0：HEAD／status／ps／df／大小／schema | EXAMINED_FULLY | `evidence/00_*` |
| Ph1 架構 | SAMPLED | 從程式碼重建；`market_runtime` 的 prewarm 部分未逐行讀 |
| Ph2 Git 歷史 | EXAMINED_FULLY（關鍵 10 個 commit）；SAMPLED（其他） | 對 8 個 commit 做了修補前測試 |
| Ph3 進場 | SAMPLED | 細讀 `run_bot.py:2096-2560` 和 `3525-3600`；`maker_engine` 全讀；`quote_service` 抽樣；`side_decision.py` 未讀 |
| Ph4 停損 | SAMPLED | gating 全部追過；`exit_engine` 門檻數學未逐行讀 |
| Ph5／Ph6 研究 | EXAMINED_FULLY（市場層 T-k 翻轉、z 分箱、選擇偏誤） | 未建立 hazard model；未做小時配對 |
| Ph7 新鮮度 | SAMPLED | snapshot writer 全讀；`spot_pricer` 部分讀；研究腳本只用 grep |
| Ph8 rollover／L2 | SAMPLED | L2 retry 生命週期與測試全讀；handoff／prewarm 抽樣 |
| Ph9 儲存 | EXAMINED_FULLY（journal 備份、TWAP guard、成長量測）；SAMPLED（retention 腳本） | |
| Ph10 runtime 效率 | SAMPLED | 只用日誌（10/7 07:34–23:28 +08）；沒做 profiling |
| Ph11 復原 | SAMPLED | 只讀程式碼；沒做當機模擬 |
| Ph12 實單安全 | EXAMINED_FULLY（4 個 submit 點、模式旗標）；SAMPLED（reconciliation） | 不連網路，無法驗證交易所端 |
| Ph13 測試品質 | SAMPLED | 跑了 140 個目標測試；完整測試套件未逐一確認 hermetic，所以沒跑 |
| Ph14 耦合 | EXAMINED_FULLY（Outcome／FF 殘留引用 grep） | |

**限制：**
- 不能連網路，所以無法驗證交易所的實際持倉和掛單。
- 日誌只涵蓋 10/7 一天；沒有出現錯誤，不代表其他日子也沒有。
- 以 `mode=ro` 開啟 WAL DB 時，會更新 `-shm` 的 mtime（內容沒變）。
- Tier 2 的 provenance 用了先前的重算 CSV 作為輸入，但我做了一致性處理：要求每一列的 TWAP 分類都是 FRESH_*。


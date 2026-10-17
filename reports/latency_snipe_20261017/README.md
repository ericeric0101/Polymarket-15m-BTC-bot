# 舊報價狙擊（snipe）可行性研究：雲端 VM 錄製與分析（2026-10-17 寫定）

**目的：** 在快速的雲端 VM 上回答一個問題：Binance 價格跳動後，Polymarket 喜好方的舊 ask 能撐多久？
- 誰會去吃它、多快吃掉？
- 如果我們的單在 L ms 後抵達撮合引擎，還吃得到嗎？
- 扣掉 taker 手續費之後，還有正的價值嗎？

**範圍：** 只錄資料、只做分析，**不下任何單**。
**分支：** `research/latency-snipe`，worktree 為 `../Polymarket-BTC-latency-snipe`。

## 程式

| 檔案 | 用途 |
|---|---|
| `scripts/clob_rtt_probe.py` | VM 到 CLOB 的往返時間（不帶認證，`POST /order` 回 401） |
| `scripts/ms_recorder.py` | 毫秒級錄製，只需 Python 3.10 以上和 `websockets`。錄 Binance bookTicker 與 aggTrade、Coinbase ticker、Polymarket 8 個串流的盤口和逐筆成交，時間戳用 VM 本機時鐘 |
| `reports/latency_snipe_20261017/analyze_snipe.py` | 分析（本機執行，需要 pandas）。定義寫在檔頭 docstring |
| `tests/test_analyze_snipe.py` | 用合成資料驗證：盤口重播、跳動偵測、舊報價持續時間、狙擊者、捕獲判斷 |

## VM 設定（AWS eu-west-2 倫敦，Ubuntu 24.04，t3.small，磁碟 30 GB）

```
sudo apt update && sudo apt install -y python3-venv chrony tmux
python3 -m venv ~/venv && ~/venv/bin/pip install websockets
chronyc tracking                      # 確認時鐘已同步（AWS 預設使用 Amazon Time Sync）
~/venv/bin/python clob_rtt_probe.py --rounds 100 --out rtt_start.json
tmux new -d -s bn  '~/venv/bin/python ms_recorder.py --out tape --sources binance'
tmux new -d -s cb  '~/venv/bin/python ms_recorder.py --out tape --sources coinbase'
tmux new -d -s pm  '~/venv/bin/python ms_recorder.py --out tape --sources pm'
# 24 小時後
~/venv/bin/python clob_rtt_probe.py --rounds 100 --out rtt_end.json
tmux kill-server; tar czf tape.tgz tape rtt_*.json
```

- 三個來源分成三個程序執行，避免互相排隊，影響收到資料的時間戳。
- **容量：** 每天約 3–5 GB。
- **注意：** `stream.binance.com` 會擋美國 IP。所以 us-east-1 只能當延遲的對照組，只跑 `clob_rtt_probe.py`。

## 分析

```
python3 analyze_snipe.py tape --rtt-ms <rtt_start.json 的 post_order_401 p50> --fetch-outcomes --out result.json
```

## 判讀（在看到任何 VM 資料之前寫定）

**參數：**
- 主要門檻：Binance 中間價在 W = 1000 ms 內移動 **J = 3 bps**。其他 J（2/5/8 bps）只做描述。
- 我們的延遲：L = RTT/2 + 2 ms，向上取到格點 {0, 5, 10, 25, 50, 100, 200, 370} ms。
- Polymarket 的單程延遲 d_pm 取「收到時間 − 伺服器時間戳」的中位數。若時鐘不可信（中位數 < 0 或 > RTT），改用 RTT/2。

**判讀：**

| 判讀 | 條件 | 後續 |
|---|---|---|
| **CLOSE（結案）** | 捕獲到的事件 60 s markout 平均 ≤ 0，**或**狙擊者首筆反應時間的中位數 < L | 速度路線結案 |
| **WRITE_PREREG** | 每天捕獲 ≥ 30 次，**且** 60 s markout 的單側 95% 下界 > 0（以市場分群 bootstrap） | 另寫事前登記，跑狙擊 dry-run，仍不下真單 |
| **INCONCLUSIVE** | 其他情況 | 再錄 1–2 天，或加開東京 VM 做比較 |

**捕獲判斷偏保守：**
- 我們排在所有更早出手的狙擊者後面；
- 舊報價只算「我們收到 Binance 跳動當下」掛在簿上的 ask 數量；
- 手續費以 0.07·p·(1−p) 計算。

## 本機試錄（Mac，經挪威 VPN，5 分鐘；只用來驗證程式能跑）

- 錄製和分析都能正常執行。
- 本機時鐘不準：PM 單程延遲量到 −258 ms，Binance 量到 −121 ms。所以 d_pm 改用 RTT/2。
- **結論必須用 VM 的資料，不能用這次試錄。**
- 試錄中，狙擊者首筆反應時間的中位數是 −194 ms：多數吃單發生在我們收到 Binance 跳動之前。這和 VPN 延遲 300 ms 的情況一致，但樣本太小，只能當參考。

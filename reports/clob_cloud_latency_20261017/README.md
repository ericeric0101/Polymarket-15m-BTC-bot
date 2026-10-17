# CLOB 網路延遲：本機基準與雲端測試方法（2026-10-17）

量測程式：`scripts/clob_rtt_probe.py`。只用 Python 標準函式庫，不需要帳號或金鑰，**不下任何單**。
`POST /order` 送空內容、不帶認證，伺服器回 401，只用來量下單路徑的往返時間。

## 本機基準（home_mac.json）

| 項目 | p50 |
|---|---|
| TCP 握手（到 Cloudflare 節點的網路往返） | **303 ms** |
| GET /time | 362 ms |
| POST /order（401） | 366 ms |
| 節點到後端加處理（366 − 303） | 約 63 ms |

**Cloudflare 節點是 OSL（奧斯陸）。** 從台灣連線卻落在奧斯陸，代表網路路徑經過北歐，例如 VPN 出口。
所以先前量到的約 0.37 秒延遲中，約 300 ms 是「本機到奧斯陸」這一段，交易所本身只占約 60 ms。

## 雲端測試步驟

1. 在候選區域開一台最小的 Linux VM。
   - 優先測：AWS eu-west-2（倫敦）、eu-west-1（愛爾蘭）、eu-central-1（法蘭克福）。
   - 對照組：us-east-1。
2. 把 `scripts/clob_rtt_probe.py` 複製到 VM，執行：
   `python3 clob_rtt_probe.py --rounds 100 --out <region>.json`
3. 把結果放回這個資料夾。

**判讀（寫在量測之前）：**
- 若 POST /order 的 p50 ≤ 50 ms，速度路線可以重新評估：要另寫事前登記，並重跑 TWAP-z 的「領先」分析。
- 若所有區域都 > 50 ms，速度路線結案。

**注意：** 在 VM 上用真錢交易前，必須先確認該區域不在 Polymarket 的限制地區之內。這個量測本身不涉及交易。

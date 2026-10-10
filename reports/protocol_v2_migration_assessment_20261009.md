# Polymarket Protocol V2 migration assessment

分析日期：2026-10-09（Asia/Taipei）。本報告加入官方 `Polymarket/polymarket-v2-external` 的合約原始碼與 repo 文件，對照本地 bot。僅分析；未修改交易程式、升級套件、重啟 bot、提交授權或發送鏈上交易。遠端參考為本次讀取的 main；未驗證鏈上 proxy 的當前 implementation 或目前 BTC 市場版本。

## 結論

必須新增 Protocol V2 相容路徑，才能繼續交易切換後的 BTC 15 分鐘市場。Data API V2 的主要改造已存在，與本次合約協議遷移是兩件事。`py-clob-client-v2==1.0.0` 的「v2」指既有 CTFExchangeV2，不能代表已支援新 PositionManager 系統。

## 本地缺口與官方證據

| 範圍 | 本地證據 | 需要的改造 |
| --- | --- | --- |
| 市場識別 | `bot/market_discovery.py:147` 僅讀 CTF token 欄位；未依 market.version 選 ID | V1 選 clobTokenIds；V2 選 positionIds；版本與 outcome mapping 必須通過驗證。將版本帶入 instrument metadata、訂單、庫存與 journal |
| 簽名 | requirements 固定 py-clob-client-v2 1.0.0；本機 builder 支援 order versions 1/2，config 沒有新 Exchange；typed-data domain 固定 2 | 保留 CTF domain 2，新增新 Exchange proxy 與 domain 3；不能以 SDK 的全域 /version 取代逐市場 protocol version |
| Nautilus adapter | 現有交易 adapter 透過本地 py_clob_client shim 呼叫舊 builder，options 主要是 neg_risk | SDK 更新與 adapter/shim 相容改造必須一起驗證；升級套件本身不會補齊版本傳遞 |
| 餘額／授權 | `bot/wallet_ops.py:142`、merge balance refresh 使用 CONDITIONAL；allowance 腳本仍以 CTF 為核心 | V2 使用 CONDITIONAL-V2；pUSD 授權 Exchange，PositionManager 授權 Exchange/Router；保留 CTF 路徑 |
| Merge／redeem | `bot/merge_ops.py:104` 固定 CTF；`scripts/check_positions_and_redeem.py:371` 使用 redeemPositions | 新增 Router.merge/redeem，明確處理 amount、outcome index 與 bytes31 condition ABI；資產持有人必須是操作 caller 或透過其錢包執行 |
| 啟動結算對帳 | `bot/recovery.py:51` 的 winner parser 使用 closed/outcomePrices/clobTokenIds | V2 加入 resolutionStatus 與 positionIds；依已 resolved 的 payout 證據對帳，不以 closed 當充分的結算證據 |
| 費用／庫存 | `bot/fill_ledger.py:70` 的 taker BUY 估計 fee shares；`bot/inventory.py:43` 從買入 qty 扣 fee shares | V2 BUY 手續費以 collateral 支出處理，SELL 從 collateral 收入扣費；成交數量與費用分開對帳，核對實際 API/adapter commission 以避免重複扣除 |
| Data API | `bot/polymarket_data_api.py`、smart money、positions/redeem 查詢與研究工具已使用 /v2、data envelope、snake_case | 主要 migration 已存在；smart-money polling 與 activity backfill 仍為限量第一頁，若要完整歷史需補 cursor，這是完整性範圍而非仍使用 v1 |

合約來源：

- [Exchange.sol](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/exchange/Exchange.sol)：`_domainNameAndVersion()` 為 Polymarket CTF Exchange / 3；BUY 費用透過 collateral transfer 收取，SELL payout 扣 collateral 費用。
- [OrderStructs.sol](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/exchange/OrderStructs.sol)：tokenId 的 wire type 仍是 uint256；signed fields 與既有 CTFExchangeV2 結構相容，但新的 domain 需要重新簽名。
- [Router.sol](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/routers/Router.sol)：split/merge/redeem；merge 轉入兩個 outcomes，redeem 有明確 outcome index 與 amount。
- [PositionManager.sol](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/positionManager/PositionManager.sol)：新持倉帳本，getPayout(positionId, amount) 依 module 求 payout。
- [Position IDs](https://github.com/Polymarket/polymarket-v2-external/blob/main/docs/position-ids.md)：ConditionId 底層 bytes31，PositionId 底層 uint256。舊 bytes32 condition 不可直接當新 condition 使用。

## 新增的兩項重要澄清

### 舊持倉不會自動轉換，但有條件式轉換功能

官方 [migration.md](https://github.com/Polymarket/polymarket-v2-external/blob/main/docs/migration.md) 與 [BaseMigrationMixin.sol](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/modules/migration/BaseMigrationMixin.sol) 包含 migratePositions。這是另一種 migration：把已登記支援的 legacy condition 持倉轉入新帳本，需要對應映射、授權、amount 與排序。它不是更新 SDK 後自動發生的行為，也不是 bot 支援新市場的必要第一步。是否要轉換既有持倉應另行確認市場支援和實際營運需求，保留舊 CTF 贖回能力。

### 官方 repo 的文字文件有版本不一致

`docs/exchange.md` 寫 domain 為 Polymarket Exchange / 1；實際 Exchange.sol 為 Polymarket CTF Exchange / 3，後者與公開 API migration guide 相同。實作須核對原始碼、ABI、官方部署 proxy 與鏈上 domainSeparator，不能只抄 repo 的文字說明。這裡不是已確認部署合約錯誤；本次未做鏈上 implementation 驗證。

## 地址與既有 collateral

官方 [README 部署表](https://github.com/Polymarket/polymarket-v2-external#deployed-contracts) 列出的 Polygon proxy：

| Contract | Proxy address |
| --- | --- |
| PositionManager | 0x006F54F7f9A22e0000CC2AB60031000000ae9fEF |
| Exchange | 0xe3333700cA9d93003F00f0F71f8515005F6c00Aa |
| Router | 0x12121212006e4CD160D18e3f00711DA5c3372600 |

README 的 pUSD/onramp 地址與本地 `bot/collateral_tokens.py` 一致，因此已存在 collateral wrapping 改造可以沿用；仍需更新各 spender/operator approvals。正式操作前核對官方部署與链上狀態，使用 proxy 而非 implementation 地址。

## 實作順序與驗收

1. 加入逐市場版本辨識與未支援版本的明確拒絕，保留 V1。
2. 完成 Binary V2 的 discovery → book/subscription → BUY/SELL signing → balance/approval；SDK 可採官方 unified Python SDK，需同步處理 Nautilus adapter。
3. 完成 Router merge/redeem、restart inventory recovery、resolved payout 與 journal/PnL 對帳。
4. 用固定 fixtures 驗證兩版本，特別測 BUY collateral fee、SELL net proceeds、bytes31 編碼、未知版本、錯誤 outcome mapping、部分成交與重啟恢復。Canary 真實交易另需在明確授權與實際市場確認後驗證。

目前策略限於 BTC 二元市場，優先支援 Binary V2 足夠；neg-risk/combinatorial/bridge/scalar 不列為第一階段必要功能。定價訊號本身不因新持倉合約而必然需要重寫，但 inventory、fees、balance guards 與結算證據必須更新。

## 驗證與期限

前一輪執行既有 `test_polymarket_data_api.py`、`test_redeem_script.py`、`test_wallet_ops.py`、`test_instrument_admission.py`：31 passed。這些覆蓋既有行為，不構成新協議端到端驗證。本輪加入合約靜態對照，沒有修改程式，沒有重複跑相同測試。

官方 [Data API migration](https://docs.polymarket.com/migrate/data-api-v1-to-v2) 確認 v1 於 2026-10-24 退役。使用者提供公告的 2026-11-02 新市場切換是暫定日期；本次未確認特定 BTC 市場的切換時間。

其他整合依據：[API migration](https://docs.polymarket.com/migrate/polymarket-v2/api-integrations)、[SDK migration](https://docs.polymarket.com/migrate/polymarket-v2/sdk-integrations)、[Contract migration](https://docs.polymarket.com/migrate/polymarket-v2/contract-integrations)。

---

## 實作狀態（2026-10-09 已遷移，Binary V2）

| 範圍 | 實作 | 位置 |
| --- | --- | --- |
| 市場版本與 ID | 依 `version` 選 `clobTokenIds`（v1）或 `positionIds`（v2）；缺少或未知版本、非十進位 ID、position ID 不符合 `conditionId<<8 \| outcomeIndex`、非 Binary module（moduleId≠1）、Gamma conditionId 與 ID 不一致，一律拒絕不交易 | `bot/protocol_v2.py`、`bot/market_discovery.py` |
| Nautilus 載入 instrument | Gamma 正規化改由 `positionIds` 建立 V2 tokens；未知版本不建立 instrument | `bot/protocol_v2_runtime.py` |
| 下單簽名 | V2 asset 一律以 ExchangeV3 簽名（domain `Polymarket CTF Exchange`／`3`）；V1 維持原 SDK 行為；neg-risk V2 拒絕。安裝點在 launcher，不受 compatibility patch 開關影響 | `bot/protocol_v2_runtime.py`、`bot/launcher.py` |
| 餘額 | V2 使用 `CONDITIONAL-V2` | `bot/wallet_ops.py`、`bot/merge_ops.py` |
| 手續費與庫存 | V2 taker BUY 手續費記為 collateral（收到完整股數）；結算成本納入未分攤的 BUY 手續費（V1 該值恆為 0，不變） | `bot/fill_ledger.py`、`bot/order_events.py`、`bot/post_trade.py` |
| 啟動結算對帳 | V2 需 `resolutionStatus == "resolved"`；ID 依版本選取；未知版本不視為已結算 | `bot/recovery.py` |
| Merge | V2 走 `Router.merge(bytes31, amount)`，先確認 Router 已被授權，不自動授權 | `bot/merge_ops.py` |
| Redeem | 依 Gamma 版本分流；V2 對持有的 outcome 呼叫 `Router.redeem(bytes31, outcomeIndex, balance)`；輸出 `routerRedeem ... protocol=v2`，`REDEEM_EXECUTED` 照常記錄 | `scripts/check_positions_and_redeem.py`、`bot/ops.py` |
| 授權 | `--v2-status` 唯讀檢查四項 V2 授權；`--apply --setup-v2-approvals` 只補缺少的項目 | `scripts/check_allowance.py` |

### 已驗證

- 測試：新增 `tests/test_protocol_v2.py` 21 項，包括 V2 簽名可在 domain 3／ExchangeV3 下還原簽名者，而在 domain 2 下不行。完整測試 1313 passed。
- 兩個舊測試的 Gamma fixture 補上真實欄位（`version`、`conditionId`、十進位 ID），斷言不變。
- 鏈上唯讀驗證（沒有送出交易）：
  - ExchangeV3 的 `domainSeparator()` 與本實作計算值相同，`eip712Domain()` 回傳 `Polymarket CTF Exchange`／`3`／137／`0xe333…00aa`。repo 文件中 `Polymarket Exchange`／`1` 的寫法已確認不正確。
  - Router 的 `POSITION_MANAGER` 和 `COLLATERAL_TOKEN` 分別是本實作的 PositionManager 和 pUSD 地址。
- 目前 BTC 15 分鐘市場仍是 `version: "v1"`，新的探索程式在真實 Gamma 上照常運作；本次掃描到的開放市場中沒有 v2 市場。

### 尚未驗證（需要真實 V2 市場或明確授權）

- CLOB 是否接受 V2 訂單、`CONDITIONAL-V2` 餘額 API 的實際回應、市場與使用者 websocket 對 V2 ID 的實際行為。
- 實際的 merge、redeem 與授權交易。這些會發送鏈上交易，本次沒有執行。

### 營運步驟（上線前）

1. `scripts/check_allowance.py --v2-status` 確認授權狀態。
2. 經你同意後執行 `--apply --setup-v2-approvals`，由持有資產的錢包授權。
3. 第一個 V2 市場出現時，先用 dry-run 觀察探索、訂單簿與簽名。

### 限制

- 鏈上 merge、redeem、授權沿用既有前提：只支援 EOA（`POLYMARKET_SIGNATURE_TYPE=0`）。
- neg-risk、combinatorial、bridge 都不支援，遇到時會明確拒絕。
- 舊 CTF 持倉不會轉換，V1 贖回流程保留。
- 研究用腳本（如 `record_polymarket_l2.py`、`analyze_weekend_liquidity.py`）仍只讀 `clobTokenIds`，不影響交易。

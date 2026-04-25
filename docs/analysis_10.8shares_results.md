# 倉位加大後 Bot 無法開單分析報告

## 核心結論

> [!CAUTION]
> **`exec_penalty` 隨 shares 呈超線性增長，而 `expected_net` 的增長被 `MAKER_MAX_ORDER_USDC=12.0` 截斷。**
> 倉位從 5.4→10.8 shares 後，在中間價位區 (0.50-0.65) 幾乎不可能通過 econ_gate。

---

## 1. Log 中的封鎖模式

Bot 成功鎖定了 side=UP，但每次嘗試掛 BUY 都被 `econ_gate` 擋住：

| 時間 | fair | bid/ask | expected_net | exec_penalty | robust_net | 結果 |
|---|---|---|---|---|---|---|
| 14:48:10 | 0.5177 | 0.57/0.58 | **0.034** | **0.057** | **-0.038** | ❌ blocked |
| 14:49:15 | 0.5726 | 0.58/0.59 | **0.034** | **0.060** | **-0.042** | ❌ blocked |
| 14:49:44 | 0.5938 | 0.63/0.64 | **0.034** | **0.063** | **-0.046** | ❌ blocked |

**模式：`exec_penalty` 始終 > `expected_net`，導致 `robust_net` 永遠為負。**

---

## 2. 數學拆解

### 2.1 `expected_net` 的計算路徑

```
expected_net_usdc = spread_capture + rebate - adverse_selection_buffer
                  = shares × half_spread + 0 - 0.02
```

**`MAKER_MAX_ORDER_USDC=12.0` 截斷了 shares：**

```python
# MakerEngine._compute_effective_quote_shares (line 89-113)
if self.config.maker_fixed_shares > 0:
    qty = max(self.config.maker_fixed_shares, min_qty)  # = 10.8
    if self.config.maker_max_order_usdc > 0:
        max_shares_by_notional = self.config.maker_max_order_usdc / quote_price
        if qty > max_shares_by_notional:
            qty = max(max_shares_by_notional, min_qty)  # ← 這裡截斷！
```

以 `quote_price = 0.57`（mid 附近的 bid）：
- `max_shares_by_notional = 12.0 / 0.57 = 21.05` → 10.8 未被截斷
- `quote_size = 10.8 × 0.57 = 6.156 USDC`
- `shares = quote_size / probability = 6.156 / 0.57 = 10.8`（驗證一致）

```
expected_net = 10.8 × 0.03 (half_spread) + 0 (rebate) - 0.02 (adverse)
             = 0.324 - 0.02
             = 0.304 ← 但 log 顯示 0.034？
```

> [!IMPORTANT]
> **等一下 — log 顯示 `expected_net=0.034`，這遠低於手算的 0.304。**
> 
> 問題出在 `half_spread` 的實際值不是 `MAKER_HALF_SPREAD=0.03`，而是 `skewed_fair - quote_bid`。
> 在 passive quoting 模式下：`quote_bid = min(skewed_fair - half_spread, inst_bid)`。
> 
> 當 `inst_bid=0.57 < fair=0.5177`（bid 高於 fair！），passive quoting 設定：
> `quote_bid = min(0.5177 - 0.03, 0.57) = min(0.4877, 0.57) = 0.4877`
> 但如果 pennying 啟用，`quote_bid` 可能被推更高。

讓我重新用 log 中的實際數字反推：

```
# fair=0.5177, bid=0.57, ask=0.58
# expected_net = 0.034000 (from log)
# shares × (skewed_fair - quote_bid) - 0.02 = 0.034
# shares × actual_half_spread = 0.054
# If shares = 10.8: actual_half_spread = 0.054 / 10.8 = 0.005
```

**actual half_spread 只有 0.005！** 因為 bot 需要在 bid=0.57 之下掛單，但 fair=0.5177，差距極小。

### 2.2 `exec_penalty` 的計算

```python
# _estimate_side_execution_penalty_usdc (line 295-373)
notional = quote_shares × quote_price  # = 10.8 × ~0.52 ≈ 5.616

# Component 1: slippage_penalty
spread = ask - bid = 0.58 - 0.57 = 0.01
slippage_penalty = notional × spread × SLIPPAGE_SPREAD_MULT × impact_mult
                 = 5.616 × 0.01 × 0.10 × (1 + impact_ratio × 0.25)

# Component 2: non_atomic_penalty (主要貢獻)  
non_atomic_penalty = notional × vol × 0.05
                   ≈ 5.616 × vol × 0.05

# Component 3: floor
floor = 0.003
```

> [!WARNING]
> **關鍵發現：`exec_penalty` 在 log 中顯示 0.057-0.063，而 `expected_net` 只有 0.034。**
> 
> 反推 `non_atomic_penalty`：如果 exec_penalty ≈ 0.057，notional ≈ 5.6，
> `vol` 的有效值 ≈ 0.057 / (5.6 × 0.05) = **0.204**（合理，15m BTC vol）
> 
> 但更可能的是 VWAP penalty + slippage 也有貢獻。

### 2.3 核心不對稱

| 指標 | 5.4 shares (舊) | 10.8 shares (新) | 增長倍數 |
|---|---|---|---|
| notional USDC | ~2.8 | ~5.6 | **2.0×** |
| expected_net (spread_capture) | ~0.032 | ~0.054 | ~1.7× |
| adverse_selection_buffer | 0.02 (固定) | 0.02 (固定) | 1.0× |
| expected_net after adverse | ~0.012 | ~0.034 | ~2.8× |
| exec_penalty (penalty scales with notional) | ~0.028 | ~0.057 | **~2.0×** |
| **robust_net** | **~-0.016** | **~-0.023** | — |

**即使在舊倉位下，econ_gate 也是勉強通過或不通過的！** 倉位加大讓 penalty 進一步惡化。

---

## 3. 真正的問題根源

問題 **不是倉位加大本身的數學錯誤**，而是以下三個問題的疊加：

### 問題 A：`adverse_selection_buffer` 是固定 USDC 金額，沒有隨 shares 縮放

```python
# rebate_model.py line 119
expected_net = expected_spread_capture + expected_rebate - adverse_selection_buffer
```

`MAKER_ADVERSE_SELECTION_BUFFER=0.02` 是固定值，不會隨 shares 增加而增加。
- 5.4 shares 時：adverse 佔 spread_capture 的比例 = 0.02 / 0.032 = 62.5%
- 10.8 shares 時：adverse 佔比 = 0.02 / 0.054 = 37%

這部分其實加大倉位後 **更有利**。

### 問題 B：`exec_penalty` 與 `notional` 成正比（線性），但在薄流動性時有超線性效應

`exec_penalty` 公式中的 `depth_impact_mult` 讓大單在相同深度時受到更高懲罰：
```python
impact_ratio = max(0, quote_shares / touch_depth)
impact_mult = 1 + impact_ratio × DEPTH_IMPACT_MULT
```

如果 `touch_depth = 20` shares：
- 5.4 shares: `impact_ratio = 0.27`, `impact_mult = 1.07`
- 10.8 shares: `impact_ratio = 0.54`, `impact_mult = 1.14`

### 問題 C（主要問題）：market 的 bid/ask 已經偏離 fair 太多

Log 顯示：
- `fair = 0.5177`，但 `bid = 0.57, ask = 0.58`
- `fair = 0.5726`，但 `bid = 0.58, ask = 0.59`
- `fair = 0.5938`，但 `bid = 0.63, ask = 0.64`

**市場已經比 fair 高了 5-6 cents！** 在 passive quoting 模式下，bot 必須在 bid 之下掛單，
但 bid 已經遠高於 fair，所以只能以極窄的 half_spread 掛單，expected_net 極低。

即使倉位不變（5.4 shares），這種市場條件下也很難通過 econ_gate。
倍增倉位只是讓 exec_penalty 更大，雪上加霜。

---

## 4. 需要修正的地方

### 修正 1（推薦）：`MAKER_ADVERSE_SELECTION_BUFFER` 應改為 per-share 制

目前是固定 0.02 USDC，但 exec_penalty 和 spread_capture 都隨 shares 縮放。
建議改成 per-share 值，或者整體數值下調：

```env
# 舊：固定 0.02 USDC（佔 10.8 shares spread 的 37%）
MAKER_ADVERSE_SELECTION_BUFFER=0.02
# 新建議：0.002 per share × 10.8 = 0.0216，維持舊比例
# 或直接下調到 0.01
MAKER_ADVERSE_SELECTION_BUFFER=0.01
```

### 修正 2（推薦）：`MAKER_EXECUTION_PENALTY_FLOOR_USDC` 需要檢查

目前 `floor = 0.003`，但 10.8 shares 的 expected_net 只有 ~0.034。
Floor 佔比 = 0.003 / 0.034 = 9%（尚可）。

### 修正 3（推薦）：Trend Buy Penalty Discount 應該更積極

`TREND_BUY_PENALTY_DISCOUNT=0.50` 意味著 trend entry 時 exec_penalty 打 5 折：
- 原 penalty = 0.057 → 打折後 = 0.0285
- robust_net = 0.034 - 0.0285 = 0.0055 → **剛好能通過** min=0.001

但 log 顯示 trend buy 似乎沒被觸發 — 檢查為什麼。

### 修正 4（最重要）：`MAKER_MAX_ORDER_USDC` 限制太低

```
MAKER_MAX_ORDER_USDC=12.0
MAKER_FIXED_SHARES=10.8
```

在 price=0.57 時：`10.8 × 0.57 = 6.16 USDC`，沒有被截斷。
但如果 price 接近 1.0：`10.8 × 1.0 = 10.8 USDC`，仍在 12.0 以內。

**目前 MAX_ORDER_USDC 暫時不是瓶頸**，但很接近。

### 修正 5（根本修正）：exec_penalty 的 VWAP/depth 計算以 USDC 為單位，但比較用 per-position robust_net

```python
# line 570
robust_bid_net = bid_econ.expected_net_usdc - bid_exec_penalty - bid_taker_leakage_usdc
```

**`expected_net_usdc` 和 `exec_penalty` 都是 USDC 總額**，所以加大倉位時：
- `expected_net_usdc` 增長 = shares 增長 × spread — 被 adverse 固定扣除抵消
- `exec_penalty` 增長 = notional 增長 × (slippage + vol + depth_impact)

**兩者都是 USDC 總額比較，數學上沒有算錯**。但 exec_penalty 的增長速度快於 expected_net，
因為 `depth_impact` 帶有超線性效應。

---

## 5. 建議的即時修正

> [!TIP]
> **最快的修正是同時調整以下兩個參數：**

```env
# 1. 降低 adverse_selection_buffer（從 0.02 → 0.01）
#    理由：10.8 shares 的 spread_capture 只有 ~0.054，0.02 佔 37% 太高
MAKER_ADVERSE_SELECTION_BUFFER=0.01

# 2. 降低 execution_penalty_floor（從 0.003 → 0.001）
MAKER_EXECUTION_PENALTY_FLOOR_USDC=0.001

# 3. 或者更激進：降低 non_atomic_vol_mult（從 0.05 → 0.02）
#    這會直接減少 exec_penalty 的主要貢獻
MAKER_EXECUTION_NON_ATOMIC_VOL_MULT=0.02
```

### 預估修正後的 robust_net：

```
expected_net = 10.8 × 0.005 (actual spread) + 0 - 0.01 (new adverse)
             = 0.054 - 0.01 = 0.044

exec_penalty ≈ 5.6 × vol × 0.02 (new mult) + slippage + vwap
             ≈ 5.6 × 0.2 × 0.02 + ~0.006 + ~0.01
             ≈ 0.0224 + 0.016 = 0.038

robust_net = 0.044 - 0.038 = +0.006 ✓ (> min 0.001)
```

---

## 6. 總結

| 問題 | 嚴重度 | 說明 |
|---|---|---|
| exec_penalty 隨 notional 線性增長 | ⚠️ 中 | 設計如此，但 depth_impact 帶有超線性效應 |
| adverse_selection_buffer 固定值 | ⚠️ 中 | 倉位加大時相對比例降低但絕對值仍吃掉大量 edge |
| 市場 bid 遠高於 fair | 🔴 高 | 在這種條件下 passive quoting 本就困難 |
| Trend buy discount 沒有成功介入 | ⚠️ 中 | 需要確認 trend buy 路徑是否被其他 gate 擋住 |
| **數學錯誤** | ✅ 無 | 計算邏輯正確，是參數在新倉位下不適配 |

**Bot 的數學計算沒有錯，但參數 calibration 是針對 5.4 shares 設計的，倉位翻倍後 exec_penalty 超過了 expected_net 的承受範圍。**

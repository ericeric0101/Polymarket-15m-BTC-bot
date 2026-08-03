"""
Reconstruct trade outcomes from order_events DB.
For each BUY fill, determine if it was a WIN (redeemed at 1.0) or LOSS (redeemed at 0).
Then simulate which trades would be blocked by stricter penalty parameters.
"""
import sqlite3
from decimal import Decimal
from collections import defaultdict

DB_PATH = "logs/trade_journal.db"
conn = sqlite3.connect(DB_PATH)

# ─── Step 1: Get all BUY fills ───
buy_fills = conn.execute("""
    SELECT 
        ts, price, qty, expected_net_usdc,
        json_extract(payload_json, '$.slug') as slug,
        json_extract(payload_json, '$.instrument_id') as inst_id
    FROM order_events
    WHERE side='BUY' AND event_type='ORDER_FILLED'
        AND ts BETWEEN '2026-04-13T00:00:00' AND '2026-04-16T00:00:00'
    ORDER BY ts
""").fetchall()

# ─── Step 2: Get all SELL fills (these are exits, not redeem) ───
sell_fills = conn.execute("""
    SELECT 
        ts, price, qty,
        json_extract(payload_json, '$.slug') as slug,
        json_extract(payload_json, '$.instrument_id') as inst_id
    FROM order_events
    WHERE side='SELL' AND event_type='ORDER_FILLED'
        AND ts BETWEEN '2026-04-13T00:00:00' AND '2026-04-16T00:00:00'
    ORDER BY ts
""").fetchall()

# Group sells by slug
sells_by_slug = defaultdict(list)
for s in sell_fills:
    sells_by_slug[s[3]].append({
        'ts': s[0], 'price': s[1], 'qty': s[2], 'inst_id': s[4]
    })

# ─── Step 3: Determine UP vs DOWN from instrument ID ───
# The instrument_id contains a condition token ID. 
# For btc-updown-15m markets, we can infer direction from the entry price:
# UP tokens trade near p_up, DOWN tokens trade near 1-p_up
# Since both sides sum to 1.0, if entry_price > 0.5, it's likely the "winning direction" token

# Actually, we need to check if the SELL happened and at what price.
# If there was a SELL fill for the same slug with a DIFFERENT instrument_id, 
# the bot sold via regular exit.
# If no sell, the position was held to settlement.
# If held to settlement: price settled at 1.0 (win) or 0.0 (loss).

# Better approach: check if there are sells matching the same instrument_id
# If no sell → held to settlement → need to determine if it settled 1.0 or 0.0

# For BTC 15m: 
# - If bot bought UP token at 0.65 → needs BTC > strike → token settles 1.0 (win) or 0.0 (loss)
# - If bot bought DOWN token at 0.35 → needs BTC < strike → token settles 1.0 (win) or 0.0 (loss)

# Without external data, we can infer from the pattern:
# In the 560adcd stable period (V1, HOLD_TO_REDEEM=0), the bot did active exits.
# We have sell records that tell us the exit price.

# ─── Step 4: Build per-market P&L ───
markets = defaultdict(lambda: {'buys': [], 'sells': [], 'buy_inst_ids': set(), 'sell_inst_ids': set()})

for b in buy_fills:
    slug = b[4]
    markets[slug]['buys'].append({
        'ts': b[0], 'price': float(b[1]), 'qty': float(b[2]), 
        'exp_net': float(b[3]), 'inst_id': b[5]
    })
    markets[slug]['buy_inst_ids'].add(b[5])

for s in sell_fills:
    slug = s[3]
    markets[slug]['sells'].append({
        'ts': s[0], 'price': float(s[1]), 'qty': float(s[2]), 'inst_id': s[4]
    })
    markets[slug]['sell_inst_ids'].add(s[4])

# ─── Step 5: Calculate PnL per market ───
trade_outcomes = []

for slug in sorted(markets.keys()):
    m = markets[slug]
    total_buy_cost = sum(b['price'] * b['qty'] for b in m['buys'])
    total_buy_qty = sum(b['qty'] for b in m['buys'])
    avg_buy_price = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0
    
    # Check if there were sells on the SAME instrument
    same_inst_sells = [s for s in m['sells'] if s['inst_id'] in m['buy_inst_ids']]
    # Check if there were sells on DIFFERENT instruments (these would be the other side)
    diff_inst_sells = [s for s in m['sells'] if s['inst_id'] not in m['buy_inst_ids']]
    
    total_sell_revenue = sum(s['price'] * s['qty'] for s in same_inst_sells)
    total_sell_qty = sum(s['qty'] for s in same_inst_sells)
    
    # Remaining qty after sells
    remaining_qty = total_buy_qty - total_sell_qty
    
    if total_sell_qty > 0:
        avg_sell_price = total_sell_revenue / total_sell_qty
        # If sold, PnL = sell_revenue - buy_cost (proportional)
        sell_cost_portion = (total_sell_qty / total_buy_qty) * total_buy_cost
        sell_pnl = total_sell_revenue - sell_cost_portion
        outcome = "SOLD"
    else:
        avg_sell_price = 0
        sell_pnl = 0
        outcome = "HELD"
    
    # For remaining qty: settled at 1.0 (win) or 0.0 (loss)
    # We can infer: if the market settled in the bought token's favor
    # Since we don't have settlement data, we'll use a heuristic:
    # - If price was high (>0.5) and bot held → likely picked the favored side
    # - But this doesn't tell us if it actually won
    
    # Better: the 560adcd era was NOT hold_to_redeem, so most positions were sold before settlement
    # Let's check what fraction was sold vs held
    
    trade_outcomes.append({
        'slug': slug,
        'avg_buy_price': avg_buy_price,
        'total_buy_cost': total_buy_cost,
        'total_buy_qty': total_buy_qty,
        'total_sell_qty': total_sell_qty,
        'total_sell_revenue': total_sell_revenue,
        'remaining_qty': remaining_qty,
        'sell_pnl': sell_pnl,
        'outcome': outcome,
        'avg_sell_price': avg_sell_price,
        'buys': m['buys'],
        'exp_net': m['buys'][0]['exp_net'] if m['buys'] else 0,
    })

# ─── Step 6: Classify wins vs losses ───
# If SOLD: win if sell_price > buy_price, loss otherwise
# If HELD to settlement: we need to determine settlement outcome
# Since 560adcd was NOT hold_to_redeem, almost all should have exits

print("=" * 120)
print("TRADE OUTCOME RECONSTRUCTION")
print("=" * 120)
print(f"{'Slug':40s} {'Outcome':8s} {'AvgBuy':>7s} {'AvgSell':>7s} {'BuyQty':>7s} {'SellQty':>7s} {'RemainQty':>9s} {'PnL':>9s} {'ExpNet':>8s}")
print("-" * 120)

wins = []
losses = []
held = []

for t in trade_outcomes:
    if t['outcome'] == 'SOLD':
        pnl = t['sell_pnl']
        # For remaining qty, assume settlement at either 1.0 or 0.0
        # If sold at profit and some remained, likely the same outcome
        if t['remaining_qty'] > 0:
            # Estimate remaining PnL: if sold at profit, likely redeem at 1.0
            if t['avg_sell_price'] > t['avg_buy_price']:
                remain_pnl = t['remaining_qty'] * (1.0 - t['avg_buy_price'])
            else:
                remain_pnl = t['remaining_qty'] * (0.0 - t['avg_buy_price'])
            pnl += remain_pnl
            t['total_pnl'] = pnl
            t['settle_estimate'] = 'win_redeem' if t['avg_sell_price'] > t['avg_buy_price'] else 'loss_redeem'
        else:
            t['total_pnl'] = pnl
            t['settle_estimate'] = 'fully_sold'
    else:
        # HELD to settlement - no sell at all
        # We'll need to estimate from context
        t['total_pnl'] = None  # Unknown
        t['settle_estimate'] = 'unknown_held'
    
    result = "?"
    if t['total_pnl'] is not None:
        if t['total_pnl'] > 0:
            result = "WIN"
            wins.append(t)
        else:
            result = "LOSS"
            losses.append(t)
    else:
        held.append(t)
    t['result'] = result
    
    pnl_str = 'N/A' if t['total_pnl'] is None else f"${t['total_pnl']:+.3f}"
    print(f"{t['slug']:40s} {t['outcome']:8s} {t['avg_buy_price']:7.2f} {t['avg_sell_price']:7.2f} "
          f"{t['total_buy_qty']:7.1f} {t['total_sell_qty']:7.1f} {t['remaining_qty']:9.1f} "
          f"{pnl_str:>9s} {t['exp_net']:8.3f}")

print("-" * 120)
print(f"WIN:  {len(wins)}  |  LOSS: {len(losses)}  |  UNKNOWN (held): {len(held)}")
print(f"Total PnL from sold trades: ${sum(t['total_pnl'] for t in wins + losses):.3f}")

# ─── Step 7: Now simulate penalty scenarios on wins vs losses ───
print()
print("=" * 120)
print("PENALTY SELECTIVITY ANALYSIS: Can we block LOSSES but keep WINS?")
print("=" * 120)

def sim_penalty(entry_price, shares, spread, vol, touch_depth,
                slippage_mult, depth_mult, vwap_mult, non_atomic_mult,
                floor_usdc, adverse, half_spread_ps):
    p = Decimal(str(entry_price))
    notional = Decimal(str(shares)) * p
    sp = Decimal(str(spread))
    v = Decimal(str(vol))
    impact_ratio = Decimal(str(shares)) / Decimal(str(touch_depth))
    impact_mult = Decimal("1") + impact_ratio * Decimal(str(depth_mult))
    slippage_pen = notional * sp * Decimal(str(slippage_mult)) * impact_mult
    non_atomic_pen = notional * v * Decimal(str(non_atomic_mult))
    vwap_pen = notional * sp * Decimal(str(vwap_mult)) * Decimal("0.5")
    total_pen = max(Decimal(str(floor_usdc)), slippage_pen + vwap_pen + non_atomic_pen)
    return float(total_pen)

# For each trade, compute whether it would pass under different scenarios
scenarios = {
    'Current (loose)': dict(slippage_mult=0.02, depth_mult=0.05, vwap_mult=0.02, non_atomic_mult=0.01, half_spread_ps=0.03),
    'Moderate': dict(slippage_mult=0.05, depth_mult=0.25, vwap_mult=0.10, non_atomic_mult=0.05, half_spread_ps=0.03),
    'Moderate+': dict(slippage_mult=0.08, depth_mult=0.35, vwap_mult=0.15, non_atomic_mult=0.08, half_spread_ps=0.03),
    'Half-560': dict(slippage_mult=0.10, depth_mult=0.50, vwap_mult=0.25, non_atomic_mult=0.10, half_spread_ps=0.03),
}

for scenario_name, params in scenarios.items():
    print(f"\n--- {scenario_name} ---")
    wins_pass = 0
    wins_block = 0
    losses_pass = 0
    losses_block = 0
    held_pass = 0
    held_block = 0
    
    for t in trade_outcomes:
        for buy in t['buys']:
            entry_price = buy['price']
            exp_net = buy['exp_net']
            qty = buy['qty']
            
            # Scale expected_net from 5.4 to 10.8 shares
            scale = 10.8 / 5.4
            exp_net_scaled = exp_net * scale
            
            penalty = sim_penalty(
                entry_price, 10.8, 0.01, 0.20, 20,
                params['slippage_mult'], params['depth_mult'],
                params['vwap_mult'], params['non_atomic_mult'],
                0.001, 0.01, params['half_spread_ps']
            )
            
            robust_net = exp_net_scaled - penalty - 0.01  # adverse selection
            passed = robust_net >= 0.001
            
            if t['result'] == 'WIN':
                if passed: wins_pass += 1
                else: wins_block += 1
            elif t['result'] == 'LOSS':
                if passed: losses_pass += 1
                else: losses_block += 1
            else:
                if passed: held_pass += 1
                else: held_block += 1
    
    total_wins = wins_pass + wins_block
    total_losses = losses_pass + losses_block
    total_held = held_pass + held_block
    
    print(f"  WINS:   {wins_pass:3d} pass / {wins_block:3d} blocked  (of {total_wins})  →  {100*wins_pass/max(1,total_wins):.0f}% win retention")
    print(f"  LOSSES: {losses_pass:3d} pass / {losses_block:3d} blocked  (of {total_losses})  →  {100*losses_block/max(1,total_losses):.0f}% loss rejection")
    print(f"  HELD:   {held_pass:3d} pass / {held_block:3d} blocked  (of {total_held})")
    if total_losses > 0:
        selectivity = (losses_block/total_losses - wins_block/max(1,total_wins))
        print(f"  ★ Selectivity score = loss_reject_rate - win_reject_rate = {selectivity:+.2%}")

print()
print("=" * 120)
print("DETAILED: WINS that get blocked (should NOT be blocked)")
print("=" * 120)
for scenario_name, params in scenarios.items():
    print(f"\n--- {scenario_name} ---")
    for t in trade_outcomes:
        if t['result'] != 'WIN':
            continue
        for buy in t['buys']:
            penalty = sim_penalty(
                buy['price'], 10.8, 0.01, 0.20, 20,
                params['slippage_mult'], params['depth_mult'],
                params['vwap_mult'], params['non_atomic_mult'],
                0.001, 0.01, params['half_spread_ps']
            )
            robust_net = (buy['exp_net'] * 2) - penalty - 0.01
            if robust_net < 0.001:
                print(f"  BLOCKED WIN: {t['slug']}  entry={buy['price']:.2f}  exp_net=${buy['exp_net']*2:.3f}  penalty=${penalty:.3f}  robust=${robust_net:+.4f}  PnL=${t['total_pnl']:+.3f}")

print()
print("=" * 120)
print("DETAILED: LOSSES that still pass (should be blocked)")
print("=" * 120)
for scenario_name, params in scenarios.items():
    print(f"\n--- {scenario_name} ---")
    for t in trade_outcomes:
        if t['result'] != 'LOSS':
            continue
        for buy in t['buys']:
            penalty = sim_penalty(
                buy['price'], 10.8, 0.01, 0.20, 20,
                params['slippage_mult'], params['depth_mult'],
                params['vwap_mult'], params['non_atomic_mult'],
                0.001, 0.01, params['half_spread_ps']
            )
            robust_net = (buy['exp_net'] * 2) - penalty - 0.01
            if robust_net >= 0.001:
                print(f"  PASSED LOSS: {t['slug']}  entry={buy['price']:.2f}  exp_net=${buy['exp_net']*2:.3f}  penalty=${penalty:.3f}  robust=${robust_net:+.4f}  PnL=${t['total_pnl']:+.3f}")

conn.close()

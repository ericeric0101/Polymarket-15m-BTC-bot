"""
Simulate execution penalty under different parameter regimes using actual fill data.
Tests whether proposed "A+B+C" parameters would kill all entries.
"""
from decimal import Decimal
import statistics

# ─── Actual fill data from 560adcd stable era (5.4 shares, Apr 13-15) ───
# entry_price, expected_net_usdc
fills = [
    (0.69, 0.282), (0.51, 0.035), (0.61, 0.060), (0.69, 0.269),
    (0.60, 0.310), (0.75, 0.036), (0.70, 0.052), (0.60, 0.267),
    (0.49, 0.042), (0.62, 0.023), (0.55, 0.370), (0.72, 0.186),
    (0.62, 0.175), (0.56, 0.159), (0.56, 0.027), (0.54, 0.251),
    (0.48, 0.049), (0.68, 0.512), (0.65, 0.351), (0.57, 0.170),
    (0.73, 0.207), (0.74, 0.249), (0.59, 0.144), (0.67, 0.254),
    (0.77, 0.065), (0.69, 0.044), (0.69, 0.139), (0.76, 0.113),
    (0.69, 0.208), (0.70, 0.122), (0.50, 0.234), (0.71, 0.222),
    (0.59, 0.137), (0.52, 0.125), (0.70, 0.306), (0.53, 0.116),
    (0.61, 0.048), (0.46, 0.283), (0.53, 0.024), (0.61, 0.344),
    (0.53, 0.084), (0.46, 0.150), (0.60, 0.033), (0.74, 0.109),
    (0.39, 0.434), (0.69, 0.304), (0.60, 0.309), (0.67, 0.188),
    (0.55, 0.145), (0.73, 0.193), (0.75, 0.173), (0.67, 0.497),
    (0.65, 0.384), (0.75, 0.075), (0.65, 0.097), (0.73, 0.267),
    (0.73, 0.167), (0.67, 0.439), (0.69, 0.285), (0.69, 0.516),
    (0.68, 0.073), (0.63, 0.288), (0.52, 0.033), (0.72, 0.188),
    (0.59, 0.276), (0.71, 0.091), (0.67, 0.493), (0.64, 0.280),
    (0.69, 0.058), (0.66, 0.098), (0.64, 0.047), (0.55, 0.142),
    (0.63, 0.160), (0.57, 0.073),
]

def simulate_penalty(shares, spread, vol, touch_depth,
                     slippage_mult, depth_mult, vwap_mult, non_atomic_mult,
                     floor_usdc, adverse_sel_buffer,
                     half_spread_ps):
    """Simulate the exec penalty and robust_net for given params."""
    # For each fill, compute what penalty would have been
    results = []
    for entry_price, orig_expected_net in fills:
        p = Decimal(str(entry_price))
        # Simulated fair = entry_price + half_spread (bot buys below fair)
        fair_approx = p + Decimal(str(half_spread_ps))
        
        notional = Decimal(str(shares)) * p
        sp = Decimal(str(spread))
        v = Decimal(str(vol))
        
        # slippage
        impact_ratio = Decimal(str(shares)) / Decimal(str(touch_depth))
        impact_mult = Decimal("1") + impact_ratio * Decimal(str(depth_mult))
        slippage_pen = notional * sp * Decimal(str(slippage_mult)) * impact_mult
        
        # non-atomic
        non_atomic_pen = notional * v * Decimal(str(non_atomic_mult))
        
        # vwap (simplified)
        vwap_pen = notional * sp * Decimal(str(vwap_mult)) * Decimal("0.5")
        
        total_pen = max(Decimal(str(floor_usdc)), slippage_pen + vwap_pen + non_atomic_pen)
        
        # expected_net = shares * actual_half_spread + rebate - adverse
        # Scale from 5.4 to shares
        scale = Decimal(str(shares)) / Decimal("5.4")
        expected_net = Decimal(str(orig_expected_net)) * scale
        
        # adverse selection
        adv = Decimal(str(adverse_sel_buffer))
        
        robust_net = expected_net - total_pen - adv
        
        # directional_edge_ps 
        # = fair - entry_price - fee_ps - exec_penalty_ps - adverse_ps
        fee_ps = Decimal("0")  # maker fee = 0
        exec_pen_ps = total_pen / Decimal(str(shares))
        adverse_ps = adv / Decimal(str(shares))
        dir_edge = fair_approx - p - fee_ps - exec_pen_ps - adverse_ps
        
        results.append({
            'entry_price': float(p),
            'expected_net': float(expected_net),
            'penalty': float(total_pen),
            'robust_net': float(robust_net),
            'dir_edge': float(dir_edge),
            'passed_econ': float(robust_net) >= 0.001,
            'passed_edge_001': float(dir_edge) >= 0.01,
            'passed_edge_002': float(dir_edge) >= 0.02,
            'edge_positive': float(dir_edge) > 0,
        })
    return results


print("=" * 90)
print("SCENARIO 1: Current params (too loose) - 10.8 shares")
print("=" * 90)
r1 = simulate_penalty(
    shares=10.8, spread=0.01, vol=0.20, touch_depth=20,
    slippage_mult=0.02, depth_mult=0.05, vwap_mult=0.02, non_atomic_mult=0.01,
    floor_usdc=0.001, adverse_sel_buffer=0.01, half_spread_ps=0.03
)
passed1 = sum(1 for x in r1 if x['passed_econ'])
print(f"Entries passing econ_gate: {passed1}/{len(r1)} ({100*passed1/len(r1):.0f}%)")
print(f"Avg penalty: ${statistics.mean(x['penalty'] for x in r1):.4f}")
print(f"Avg robust_net: ${statistics.mean(x['robust_net'] for x in r1):.4f}")
print(f"Avg dir_edge: {statistics.mean(x['dir_edge'] for x in r1):.4f}")
print(f"Dir_edge > 0: {sum(1 for x in r1 if x['edge_positive'])}/{len(r1)}")
print(f"Dir_edge >= 0.01: {sum(1 for x in r1 if x['passed_edge_001'])}/{len(r1)}")

print()
print("=" * 90)
print("SCENARIO 2: Proposed A (half of 560adcd) - 10.8 shares")
print("=" * 90)
r2 = simulate_penalty(
    shares=10.8, spread=0.01, vol=0.20, touch_depth=20,
    slippage_mult=0.10, depth_mult=0.50, vwap_mult=0.25, non_atomic_mult=0.10,
    floor_usdc=0.001, adverse_sel_buffer=0.01, half_spread_ps=0.03
)
passed2 = sum(1 for x in r2 if x['passed_econ'])
print(f"Entries passing econ_gate: {passed2}/{len(r2)} ({100*passed2/len(r2):.0f}%)")
print(f"Avg penalty: ${statistics.mean(x['penalty'] for x in r2):.4f}")
print(f"Avg robust_net: ${statistics.mean(x['robust_net'] for x in r2):.4f}")
print(f"Avg dir_edge: {statistics.mean(x['dir_edge'] for x in r2):.4f}")
print(f"Dir_edge > 0: {sum(1 for x in r2 if x['edge_positive'])}/{len(r2)}")
print(f"Dir_edge >= 0.01: {sum(1 for x in r2 if x['passed_edge_001'])}/{len(r2)}")
blocked2 = [x for x in r2 if not x['passed_econ']]
if blocked2:
    print(f"\n  BLOCKED entries ({len(blocked2)}):")
    for b in blocked2[:10]:
        print(f"    price={b['entry_price']:.2f}  exp_net=${b['expected_net']:.4f}  penalty=${b['penalty']:.4f}  robust=${b['robust_net']:.4f}  edge={b['dir_edge']:.4f}")

print()
print("=" * 90)
print("SCENARIO 3: Moderate rebalance - 10.8 shares")
print("  (slippage=0.05, depth=0.25, vwap=0.10, non_atomic=0.05)")
print("=" * 90)
r3 = simulate_penalty(
    shares=10.8, spread=0.01, vol=0.20, touch_depth=20,
    slippage_mult=0.05, depth_mult=0.25, vwap_mult=0.10, non_atomic_mult=0.05,
    floor_usdc=0.001, adverse_sel_buffer=0.01, half_spread_ps=0.03
)
passed3 = sum(1 for x in r3 if x['passed_econ'])
print(f"Entries passing econ_gate: {passed3}/{len(r3)} ({100*passed3/len(r3):.0f}%)")
print(f"Avg penalty: ${statistics.mean(x['penalty'] for x in r3):.4f}")
print(f"Avg robust_net: ${statistics.mean(x['robust_net'] for x in r3):.4f}")
print(f"Avg dir_edge: {statistics.mean(x['dir_edge'] for x in r3):.4f}")
print(f"Dir_edge > 0: {sum(1 for x in r3 if x['edge_positive'])}/{len(r3)}")
print(f"Dir_edge >= 0.01: {sum(1 for x in r3 if x['passed_edge_001'])}/{len(r3)}")
blocked3 = [x for x in r3 if not x['passed_econ']]
if blocked3:
    print(f"\n  BLOCKED entries ({len(blocked3)}):")
    for b in blocked3[:10]:
        print(f"    price={b['entry_price']:.2f}  exp_net=${b['expected_net']:.4f}  penalty=${b['penalty']:.4f}  robust=${b['robust_net']:.4f}  edge={b['dir_edge']:.4f}")

print()
print("=" * 90)
print("SCENARIO 4: 560adcd original (very heavy) - 10.8 shares")
print("=" * 90)
r4 = simulate_penalty(
    shares=10.8, spread=0.01, vol=0.20, touch_depth=20,
    slippage_mult=0.15, depth_mult=1.0, vwap_mult=0.50, non_atomic_mult=0.20,
    floor_usdc=0.001, adverse_sel_buffer=0.001, half_spread_ps=0.012
)
passed4 = sum(1 for x in r4 if x['passed_econ'])
print(f"Entries passing econ_gate: {passed4}/{len(r4)} ({100*passed4/len(r4):.0f}%)")
print(f"Avg penalty: ${statistics.mean(x['penalty'] for x in r4):.4f}")
print(f"Avg robust_net: ${statistics.mean(x['robust_net'] for x in r4):.4f}")
print(f"Avg dir_edge: {statistics.mean(x['dir_edge'] for x in r4):.4f}")
print(f"Dir_edge > 0: {sum(1 for x in r4 if x['edge_positive'])}/{len(r4)}")
print(f"Dir_edge >= 0.01: {sum(1 for x in r4 if x['passed_edge_001'])}/{len(r4)}")
print(f"Dir_edge >= 0.03: {sum(1 for x in r4 if x['passed_edge_002'])}/{len(r4)}")

print()
print("=" * 90)
print("DIRECTIONAL_EDGE_PS <= 0 ANALYSIS")
print("=" * 90)
for scenario_name, results in [("Current (loose)", r1), ("Half-560adcd (A)", r2), ("Moderate", r3), ("Full-560adcd", r4)]:
    neg = sum(1 for x in results if x['dir_edge'] < 0)
    zero = sum(1 for x in results if x['dir_edge'] == 0)
    low = sum(1 for x in results if 0 < x['dir_edge'] < 0.01)
    ok = sum(1 for x in results if x['dir_edge'] >= 0.01)
    print(f"  {scenario_name:22s}: edge<0={neg:2d}  edge=0={zero:2d}  0<edge<0.01={low:2d}  edge>=0.01={ok:2d}")
    if neg > 0:
        neg_entries = [x for x in results if x['dir_edge'] < 0]
        avg_neg_price = statistics.mean(x['entry_price'] for x in neg_entries)
        avg_neg_edge = statistics.mean(x['dir_edge'] for x in neg_entries)
        print(f"    → Negative edge avg price={avg_neg_price:.2f}, avg edge={avg_neg_edge:.4f}")

print()
print("=" * 90)
print("ENTRY PRICE DISTRIBUTION OF NEGATIVE-EDGE ENTRIES (Moderate scenario)")
print("=" * 90)
neg_r3 = [x for x in r3 if x['dir_edge'] < 0]
if neg_r3:
    for x in sorted(neg_r3, key=lambda x: x['dir_edge']):
        print(f"  price={x['entry_price']:.2f}  edge={x['dir_edge']:.4f}  exp_net=${x['expected_net']:.4f}  penalty=${x['penalty']:.4f}")
else:
    print("  No negative-edge entries in Moderate scenario")


# Phase 0 Baseline: `a0541b7`

Captured before the five-layer decision-chain implementation. This document is
a comparison anchor only; no settings were changed while it was collected.

## Commands

```bash
./.venv/bin/python scripts/replay_journal_signals.py --hours 168 --mode dry-run-fill
./.venv/bin/python scripts/replay_journal_signals.py --hours 168 --mode signal-baseline --shares 6
./.venv/bin/python scripts/execution_penalty_report.py --hours 168
```

## Results

| Measure | Value |
| --- | ---: |
| Dry-run simulated passive fills | 63 candidates / 63 selected markets |
| Dry-run settled samples | 56 settled / 7 unscored |
| Dry-run settlement outcome | 37 wins / 19 losses (66.07%) |
| Dry-run gross settlement-only PnL | +8.8400 USDC |
| Signal-baseline outcome | 131 wins / 53 losses (71.20%), +52.1400 USDC for 6 shares |
| Execution-penalty observations | 77,542 |
| Average model execution penalty | 0.547032 USDC |

The dry-run row uses live entry/submission gates, sizing, and a simulated
passive fill. It excludes fees and exits. `signal-baseline` ignores live gates
and fill probability, so it must not be compared to dry-run performance.

## Gate distribution in the same 168-hour window

| Event | Count |
| --- | ---: |
| `ORDER_SKIP_LOCKED_SIDE_INVALIDATED` | 25,270 |
| `ORDER_SKIP_ENTRY_FAIR_EDGE_GATE` | 24,232 |
| `ORDER_OBSERVE_BUY_BLOCKED` | 22,279 |
| `ORDER_SKIP_DIRECTIONAL_ENTRY_GATE` | 11,860 |
| `ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW` | 6,590 |
| `SHADOW_SIM_ENTRY_CANDIDATE` / `FILLED` / `SETTLED` | 76 / 63 / 56 |

## Phase 2 lifecycle verification

The dry-run entry path in this baseline already uses the same local maker-order
state, target version, re-quote, cancellation, and TTL machinery as live. Its
unit coverage is in `tests/test_shadow_simulation.py`, including re-quote
replacement, cancellation preventing a stale simulated fill, and local dry-run
submit/cancel lifecycle. It still does not claim to simulate wallet execution,
real venue fills, inventory, sell orders, or exits.

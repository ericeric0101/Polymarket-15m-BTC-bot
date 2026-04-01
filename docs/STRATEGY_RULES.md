# Strategy Rules

This file consolidates the current BTC 15-minute live strategy into a single place.
It is intentionally operational, not aspirational.

## Live Path

Current live path:

- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/run_bot.py`
- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/bot/`
- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/execution/`

The rules below describe the live maker-mode behavior, not the old `core/strategy_brain`
stack.

## Entry

The bot enters only after all of these are true:

- market phase is tradable
- strike is known or locked via fallback
- `active_side` is set and, for directional action, normally `locked`
- quote economics pass `econ_gate`
- balance and inventory guards allow the order
- trend protection / reduce-only / stop-loss penalties do not block the side

Entry is quote-driven, not taker-driven.

## Hold

The bot can hold inventory while:

- the position still matches the current confirmed side thesis
- exit policy stays in `HOLD_IN_BAND` or `HOLD_TO_REDEEM`
- high-cost cooldown or other protections defer active exit

Inventory is tracked by local fill ledger (`live_inventory_cost`) and not just by
external balances.

## Exit

There are three main exit families:

1. Normal maker exit
   - inventory is sold by ordinary maker quote logic

2. Maker urgent exit
   - only after confirmed off-side state
   - requires `active_side_locked == True`
   - requires consecutive confirmations
   - requires unrealized loss threshold
   - now includes replacement grace so an urgent exit is not rapidly repriced lower

3. Taker exit
   - emergency path
   - used only when explicitly enabled and when exit policy decides it is necessary

## Restart / Recovery

Mid-market restarts are supported only because startup recovery rehydrates:

- open inventory quantity from position cache
- cost basis from recent fill history

Recovered startup inventory is forced into sell-only mode until flattened.

## Current Strategic Constraints

These are deliberate current biases, based on recent live behavior:

- avoid carrying wrong-side inventory to settlement
- prefer reducing panic exit chasing over forcing immediate full stop-out
- avoid enabling broad taker-exit behavior
- treat `UP` and `DOWN` as potentially asymmetric in practice

## Known Weaknesses

- `run_bot.py` still owns too many responsibilities
- many guards still interact in ways that are hard to attribute after the fact
- side asymmetry (`UP` vs `DOWN`) is observed but not yet fully parameterized
- urgent exit timing still needs live validation after recent tightening

## What To Change Carefully

High-risk knobs and flows:

- side confirmation / lock rules
- urgent exit confirmation and replacement behavior
- buy-count and re-entry rules
- startup rehydrate and sell-only recovery path
- any code touching Nautilus adapter behavior or runtime patch scripts

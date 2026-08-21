# BTC 15-Minute Runtime Specification

`project_overview.md` at the repository root is the current decision authority.
This document is a concise operational runtime reference; historical V1 and
early-V2 design notes are not operating instructions.

## Scope

- Market: Polymarket `btc-updown-15m-*` binary markets.
- Sides: the strategy may select `UP`, `DOWN`, or `NONE`; it does not trade a
  side when the decision is `NONE`.
- Mode: maker-first entries, controlled recovery exits, and optional hold to
  redeem.
- Pricing: Chainlink 60-second TWAP is preferred. A fresh external spot feed
  may be used only through the configured degraded-feed policy.

## Lifecycle

`WAITING -> ACTIVE -> REDUCE_ONLY -> SETTLING -> WAITING` is the market
lifecycle. `REDUCE_ONLY` blocks new risk. `SETTLING` cancels outstanding maker
orders, journals the result, and starts discovery for the next market.

## Entry Decision Chain

Every candidate is evaluated in this fixed order:

1. **Hard safety:** valid, fresh market data; usable TWAP/reference spot; no
   configured external/book conflict; lifecycle permits new risk.
2. **Direction:** one `UP`, `DOWN`, or `NONE` side decision. The first entry
   requires `max(FIRST_ENTRY_SCORE_MIN, ENTRY_SCORE_MIN)`; later entries use
   `ENTRY_SCORE_MIN`.
3. **Model consistency:** strike/spot sanity, calibrated fair probability, and
   high-price risk/reward limits.
4. **Economics:** one `robust_net` calculation using expected value, empirical
   execution cost, and fees. Negative fair-edge bands remain shadow research,
   not an implicit live exception.
5. **Execution:** passive order construction, TTL/requote rules, and inventory
   limits. Normal target is 10 shares, reduced to 5 above the high-price
   threshold; projected per-market exposure cannot exceed 10 shares.

The journal records a final reason for every rejected candidate so a replay
can distinguish safety, direction, model, economics, and execution blocks.

## Exits and Redemption

- `HOLD_TO_REDEEM=1` permits eligible winning inventory to settle and redeem.
- Recovery exits are restricted by the configured TWAP confirmation, minimum
  hold, and remaining-time controls.
- Normal maker exits reduce confirmed inventory only. On restart, recovered
  inventory is sell-only until reconciled.
- Auto-redeem is operationally separate from order placement and requires
  working Polygon allowances and gas.

## Modes

- `./.venv/bin/python run_bot.py`: dry run. It executes the live decision and
  simulated order lifecycle but does not submit wallet orders.
- `./.venv/bin/python run_bot.py --live`: real wallet orders after the live
  confirmation prompt.
- `./.venv/bin/python run_bot.py --preflight-only`: validates startup inputs
  without running the strategy.

## Authority

Use [configuration.md](configuration.md) for configuration precedence,
[STRATEGY_RULES.md](STRATEGY_RULES.md) for operational rules, and
[readme_ZH.md](readme_ZH.md) for the Traditional Chinese runbook.

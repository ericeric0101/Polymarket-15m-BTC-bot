# BOT Runtime Spec

## Core identity

- Strategy type: `7-phase` market lifecycle bot
- Trading style: `UP-only`, `maker-first`, `risk-first`
- Primary goal: buy `UP` below fair value, sell `UP` inventory above cost, and only hold to settlement when confidence is high enough

## What the bot trades

- Only the `UP` outcome of the current BTC 15-minute market
- `MAKER_QUOTE_SIDES=both` means:
  - rest `BUY` quotes on `UP`
  - rest `SELL` quotes to reduce `UP` inventory
- `DOWN` is never traded directly
- `DOWN` pressure is only expressed indirectly through:
  - fair price deterioration
  - directional edge gate
  - regime guard
  - cooldown / reduce-only behavior

## Market lifecycle

1. `WAITING`
   - no valid current market
   - search next BTC 15m market
2. `ACTIVE`
   - normal maker quoting
   - may place `BUY` and `SELL` quotes on `UP`
3. `REDUCE_ONLY`
   - near market end
   - no fresh `BUY` risk
   - prioritize inventory reduction
4. `SETTLING`
   - market finished
   - cancel maker quotes
   - compute settlement PnL
   - wait grace period before next search

## Entry rules

- Default action is to quote `BUY` on `UP`
- Bot will refuse entry when any of these hold:
  - expected net edge is too small
  - directional edge gate is not met
  - post-fill buy cooldown is active
  - momentum filter says market is falling too fast
  - regime guard is active
  - reduce-only mode is active
  - projected inventory exceeds cap

## Exit rules

- First preference: maker `SELL` on `UP` inventory
- If enough profit is available, bot may use taker exit to lock profit quickly
- Taker exit is also allowed for controlled stop-loss / max-hold / near-close handling
- Bot may suppress sells when:
  - hold-to-settlement gate says inventory is worth carrying
  - high-cost exit cooldown is active
  - sell price is below protected cost threshold

## Hold to settlement

- Hold is allowed only when all are true:
  - inventory is small enough
  - avg entry is high enough to justify redeem path
  - enough time remains
- Hold is not the default behavior
- Hold is a selective override for small, high-confidence `UP` inventory

## Cooldown rules

- Post-fill buy cooldown:
  - after a `BUY` fill, temporarily stop new `BUY` quotes
- Consecutive loss cooldown:
  - after repeated realized losses, pause all quoting
- Regime guard cooldown:
  - after repeated bad market cycles, switch to conservative / paused behavior
- High-cost fill cooldown:
  - after expensive `BUY` fills, avoid bad active exits below cost
- Taker reject cooldown:
  - after taker exit reject, delay repeated taker exit attempts

## Risk controls

- `UP` inventory cap
- projected inventory guard before submit
- conditional token balance guard before sell
- cancel/requote throttling
- quote health watchdog
- orderbook missing pause
- balance / allowance pause
- reduce-only tail guard
- automatic reset of per-market state on rollover

## Fair price model

- Recommended mode: `digital`
- Inputs:
  - external BTC spot
  - parsed strike
  - time to expiry
  - estimated short-horizon sigma
- If strike is unavailable, bot falls back temporarily until opening strike can be locked

## Recommended operational stance

- Use `test_mode` / dry run first after parameter changes
- Keep inventory cap small
- Keep directional edge gate enabled
- Keep hold-to-settlement inventory cap small
- Keep auto-redeem disabled until live behavior is stable

## Intent of the current defaults

- Trade less, but with cleaner `UP` selection
- Reduce unnecessary averaging into weak `UP`
- Allow fast taker profit capture when edge is already won
- Use settlement hold only as an exception, not as the baseline exit path

# Directional Market Maker Refactor Plan

## Goal

Shift the strategy from:

- direction-trading with heavy exit management

to:

- locked-side directional market making with minimal forced exits

The intended edge is:

- side selection is usually correct
- current PnL damage comes from overactive stop-loss / de-risk / profit-taking logic
- inventory should be recycled on the locked side instead of repeatedly trying to predict the perfect exit

## Baseline Before Refactor

Before changing behavior, establish a baseline for the current strategy.

Required metrics:

- `correct_side_stopout_rate`
- `average_hold_duration_sec`
- `exit_then_recovery_rate`
- `profitable_peak_capture_ratio`
- `stale_inventory_near_expiry_rate`
- `intramarket_side_flip_count`

Definitions:

- `correct_side_stopout_rate`: fraction of realized loss exits where the market later returned to the held side before expiry
- `average_hold_duration_sec`: mean hold time of realized positions
- `exit_then_recovery_rate`: fraction of exits after which best bid later recovered by at least `0.05`
- `profitable_peak_capture_ratio`: realized exit price divided by post-entry peak bid for profitable trades
- `stale_inventory_near_expiry_rate`: fraction of positions still open inside the final forced-exit window
- `intramarket_side_flip_count`: number of `UP <-> DOWN` side changes within the same market

This baseline is required so each phase can be evaluated against the current system instead of by anecdote.

## Current Strategy Shape

Today the bot behaves like:

1. choose side
2. enter inventory
3. run many exit authorities and vetoes
4. reprice or flatten based on changing confidence

Key current sell influences:

- `ExitPolicyEngine` classification
- `decision_state.phase` / `decision_regime`
- `sell_cost_protect`
- `min_profit_floor`
- recycle drawdown protection
- stop-loss pending / regime armed
- reduce-only tail logic

This creates correct-side churn:

- thesis remains valid
- confidence softens
- bot exits anyway
- market later resolves in the original direction

## Target Strategy Shape

The refactored strategy should behave like:

1. lock a single side for the current market: `UP`, `DOWN`, or `NONE`
2. quote only that side
3. recycle inventory on that side
4. use forced exits only when thesis is genuinely invalidated

This is not a symmetric pure market maker.
It is a directional market maker.

## Core Principles

### 1. Side lock is the main decision

The main brain output should be:

- `locked_side`
- `locked_side_confidence`
- `side_invalidation_state`

The brain should not continuously micromanage every sell.

### 2. Inventory recycle beats thesis micromanagement

Once side is locked:

- buy locked-side inventory at acceptable passive prices
- sell locked-side inventory at acceptable passive prices
- repeat

Do not treat every confidence dip as a reason to flatten.

### 3. Forced exits must be rare

Forced exits should require one of:

- spot/strike invalidation sustained for N cycles
- explicit hard stop-loss pending
- catastrophic adverse move
- final time-window safety exit

General `DE_RISK` should not be a routine permission to loss-sell.

### 4. Flip side must be strict

Side flips should be infrequent and explicit.

Do not allow:

- `UP -> DOWN -> UP` due to short confidence swings

Do allow:

- sustained spot/strike invalidation
- sustained fair inversion with configurable minimum gap
- strong opposite-side confirmation

### 5. `NONE` must be explicit

`locked_side = NONE` is a real operating mode, not a placeholder.

In `NONE` mode:

- no new directional inventory is opened
- no new maker buys are quoted
- existing inventory may still be recycled or force-exited
- the bot only observes, updates market state, and waits for side lock criteria

## Proposed Architecture

### Brain Layer

Owns:

- side selection
- side lock / side unlock
- side invalidation
- fair calculation
- hard exit permission

Outputs:

- `locked_side`
- `quote_mode`
- `hard_exit_allowed`
- `side_invalidation_hits`

### Inventory Layer

Owns:

- inventory by instrument
- average entry
- peak bid / fair tracking
- passive sell floor memory

Outputs:

- inventory state
- inventory sellability
- recycle readiness

### Quote Layer

Owns:

- passive buy price for locked side
- passive sell price for locked-side inventory
- tick alignment
- TTL / requote

Must not invent new strategy decisions.

Required pricing formulas:

- recycle buy:
  - `buy_price = min(passive_best_bid_or_step, fair - acquire_discount)`
- recycle sell:
  - `sell_price = max(avg_entry + min_profit_floor, fair - recycle_discount, passive_best_ask_or_step)`

The exact discounts should be configurable.
The quote layer should not improvise new profit-taking logic outside these formulas.

### Forced Exit Layer

Owns only:

- stop-loss pending
- hard exit when thesis is invalid
- expiry safety exit
- catastrophic exit

## Required Configuration Surface

The refactor should make these controls explicit and configurable:

- `MAKER_SIDE_INVALIDATION_CONFIRM_CYCLES`
- `MAKER_SIDE_INVALIDATION_SPOT_BUFFER_BPS`
- `MAKER_SIDE_INVALIDATION_FAIR_FLIP_MIN`
- `MAKER_SIDE_FORCE_UNLOCK_LAST_SEC`
- `BI_SIDE_FLIP_FAIR_INVERSION_MIN_PS`
- `MAKER_RECYCLE_BUY_DISCOUNT_PS`
- `MAKER_RECYCLE_SELL_DISCOUNT_PS`
- `MAX_LOCKED_SIDE_POSITION`
- `INVENTORY_FULL_BEHAVIOR`

Suggested semantics:

- `MAKER_SIDE_INVALIDATION_CONFIRM_CYCLES`
  - number of consecutive cycles required before ordinary side invalidation is accepted
- `MAKER_SIDE_INVALIDATION_SPOT_BUFFER_BPS`
  - minimum spot/strike penetration before counting invalidation
- `MAKER_SIDE_INVALIDATION_FAIR_FLIP_MIN`
  - opposite-side fair threshold needed to assist a side flip
- `MAKER_SIDE_FORCE_UNLOCK_LAST_SEC`
  - final time window where side lock can be force-cleared for expiry handling
- `MAKER_RECYCLE_BUY_DISCOUNT_PS`
  - passive discount below fair for locked-side inventory acquisition
- `MAKER_RECYCLE_SELL_DISCOUNT_PS`
  - passive discount below fair used for recycle sells
- `MAX_LOCKED_SIDE_POSITION`
  - maximum inventory allowed on the locked side
- `INVENTORY_FULL_BEHAVIOR`
  - one of `STOP_BUY` or `WIDEN_SPREAD`

## What Changes Relative to Current Code

### Keep

- `side_decision` machinery
- strike locking
- fair computation
- inventory ledger
- maker order lifecycle
- reduce-only tail safety

### Downgrade or remove

- ordinary `DE_RISK` as a loss-sell authority
- most confidence-only loss exits
- overlapping profitable exit heuristics
- repeated sell repricing driven by confidence softness

### Simplify

- profitable exit logic into one locked-side recycle policy
- stop-loss logic into one hard-exit policy
- side flip logic into one side invalidation policy

## Inventory Guardrails

Directional MM only works if inventory growth is bounded.

Required rules:

- locked-side inventory must never exceed `MAX_LOCKED_SIDE_POSITION`
- when inventory is full, the bot must not continue quoting the same buy price
- full inventory behavior must be explicit:
  - `STOP_BUY`: disable new buys until inventory is reduced
  - `WIDEN_SPREAD`: continue buying only at materially better prices

Default recommendation:

- start with `STOP_BUY`

This is simpler, safer, and easier to validate before experimenting with spread widening.

## Proposed Phased Refactor

### Phase 1: Stop-loss authority reduction

Goal:

- preserve current strategy
- remove avoidable self-harm

Changes:

- require spot/strike invalidation for ordinary `DE_RISK` loss-sell
- use locked-side invalidation as the single buy-blocking authority for same-market re-entry
- keep `EXIT`, pending stop-loss, catastrophic, and final-window exits
- keep `MAKER_LOSS_SELL_MIN_HOLD_SEC` elevated

Success criteria:

- fewer correct-side stop-outs
- no increase in crashy stale inventory near expiry

Rollback criteria:

- `stale_inventory_near_expiry_rate` increases by more than 25% from baseline
- `average_loss_per_forced_exit` worsens by more than 20%

### Phase 2: Profit exit collapse

Goal:

- replace multiple soft profit exits with one recycle policy

Changes:

- keep recycle drawdown only as a protective recycle rule
- collapse legacy profit-hold / veto logic into one recycle helper
- reduce `run_bot.py` sell branching

Success criteria:

- fewer “sell high confidence winner too early” cases
- fewer “veto then reprice too low” cases

Rollback criteria:

- `profitable_peak_capture_ratio` does not improve
- `exit_then_recovery_rate` improves, but realized PnL per winner worsens materially

### Phase 3: Side flip hardening

Goal:

- stop frequent thesis churn

Changes:

- only flip side when spot/strike invalidation persists for `MAKER_SIDE_INVALIDATION_CONFIRM_CYCLES`
- require fair inversion or strong opposite-side confirmation
- keep current side through low-confidence chop when spot still supports it

Success criteria:

- lower intramarket side churn
- fewer flip-then-regret cases

Rollback criteria:

- `intramarket_side_flip_count` does not decrease
- wrong-side stale inventory increases materially

### Phase 4: Locked-side MM mode

Goal:

- make inventory recycle the default behavior

Changes:

- introduce explicit quote mode:
  - `ACQUIRE_LOCKED_SIDE`
  - `RECYCLE_LOCKED_SIDE`
  - `HARD_EXIT`
- treat sells as recycle by default, not as trade thesis exits
- reserve hard exits for explicit invalidation paths

Success criteria:

- PnL depends more on side-lock correctness and spread capture
- less dependence on soft exit heuristics

Rollback criteria:

- recycle sells fail to clear inventory often enough
- inventory saturation becomes common even when thesis remains valid

## Example: Current vs Refactored

Market:

- strike = `71491`
- current side locked = `UP`
- spot = `71520`
- fair = `0.72`
- orderbook = `0.69 / 0.70`

### Current behavior

1. bot buys `UP @ 0.69`
2. market dips to `0.64-0.66`
3. confidence weakens, `DE_RISK` appears
4. bot starts considering exit
5. bot may flatten near `0.62-0.65`
6. market later returns to `0.80+`

### Refactored behavior

1. bot buys `UP @ 0.69`
2. market dips to `0.64-0.66`
3. spot is still above strike, so side remains valid
4. ordinary loss exit is not allowed
5. inventory is held for recycle
6. recycle sell is posted using the recycle pricing rule, not a confidence-triggered flatten
7. bot sells later when orderbook recovers

## Example: Recycle Pricing

Assume:

- `avg_entry = 0.69`
- `fair = 0.74`
- `best_bid / ask = 0.71 / 0.72`
- `min_profit_floor = 0.02`
- `recycle_sell_discount = 0.01`

Then:

- cost floor = `0.71`
- fair-based recycle price = `0.73`
- passive best ask anchor = `0.72`
- recycle sell = `max(0.71, 0.73, 0.72) = 0.73`

This avoids both:

- panic exits below cost
- arbitrary “winner” logic that reprices down purely because confidence softened

## Implementation Notes

### Current tactical patches that can stay temporarily

- `MAKER_LOSS_SELL_MIN_HOLD_SEC`
- `spot_still_supports_position`

These are tactical stabilizers, not the final architecture. The previous
`profitable_exit_fair_veto` / `profitable_exit_veto_floor` path has already been
removed in favor of recycle pricing plus stricter forced-exit gating.

### Final target

The final target should have only three sell intents:

- `RECYCLE_PROFIT`
- `FORCED_EXIT`
- `TAIL_EXIT`

Everything else should be diagnostics, not execution authority.

## Immediate Next Refactor After Current Stabilization

1. move all profitable exit guards into one helper
2. make `DE_RISK` non-executable unless spot/strike invalidation is confirmed
3. isolate side-flip logic from ordinary inventory recycle logic
4. add baseline metric logging for stop-out and recovery analysis

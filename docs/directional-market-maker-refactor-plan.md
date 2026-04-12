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
- `winner_retrace`
- `profitable_hold_simple`
- `fair veto`
- `veto floor`
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
- sustained fair inversion
- strong opposite-side confirmation

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

### Forced Exit Layer

Owns only:

- stop-loss pending
- hard exit when thesis is invalid
- expiry safety exit
- catastrophic exit

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

## Proposed Phased Refactor

### Phase 1: Stop-loss authority reduction

Goal:

- preserve current strategy
- remove avoidable self-harm

Changes:

- require spot/strike invalidation for ordinary `DE_RISK` loss-sell
- keep `EXIT`, pending stop-loss, catastrophic, and final-window exits
- keep `MAKER_LOSS_SELL_MIN_HOLD_SEC` elevated

Success criteria:

- fewer correct-side stop-outs
- no increase in crashy stale inventory near expiry

### Phase 2: Profit exit collapse

Goal:

- replace multiple soft profit exits with one recycle policy

Changes:

- keep `winner_retrace` only as a protective recycle rule
- collapse `profitable_hold_simple`, `fair veto`, and `veto floor` into one profit-recycle helper
- reduce `run_bot.py` sell branching

Success criteria:

- fewer “sell high confidence winner too early” cases
- fewer “veto then reprice too low” cases

### Phase 3: Side flip hardening

Goal:

- stop frequent thesis churn

Changes:

- only flip side when spot/strike invalidation persists for N cycles
- require fair inversion or strong opposite-side confirmation
- keep current side through low-confidence chop when spot still supports it

Success criteria:

- lower intramarket side churn
- fewer flip-then-regret cases

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
6. bot sells later when orderbook recovers

## Implementation Notes

### Current tactical patches that can stay temporarily

- `MAKER_LOSS_SELL_MIN_HOLD_SEC`
- `spot_still_supports_position`
- `profitable_exit_fair_veto`
- `profitable_exit_veto_floor`

These are tactical stabilizers, not the final architecture.

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


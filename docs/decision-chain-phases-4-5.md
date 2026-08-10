# Decision Chain: Phase 4 and Phase 5

## Phase 4: one economics rule for every new BUY

`TREND_BUY_MIN_NET_USDC` and `TREND_BUY_PENALTY_DISCOUNT` were removed.
Trend remains a directional classification and may still select its configured
size multiplier, but it can no longer lower the economics threshold or
recompute `robust_net` with a discounted execution penalty. Every new BUY must
pass the same model-produced `robust_net >= MAKER_MIN_EXPECTED_NET_USDC` gate.

This deliberately does **not** allow negative `robust_net`. The six raw
`fair - entry` shadow buckets remain research-only until the passive-fill,
cost-adjusted report has enough settled samples.

## Phase 5: two-tier entry size

The operational `.env` now sets a standard target of 10 shares, with a 5-share
risk tier when either weak `p_fair` or high entry price applies:

| Setting | Value |
| --- | ---: |
| `MAKER_FIXED_SHARES` | 10 |
| `MAKER_MIN_SHARES` / venue minimum | 5 |
| `MAKER_MAX_INVENTORY_SHARES` | 10 |
| `MAX_LOCKED_SIDE_POSITION` | 10 |
| Weak/high-price multiplier | 0.5 |

Risk reductions now compose as caps rather than multiplying. If both weak
`p_fair` and high-price protections apply, the result remains 0.5x (5 shares),
not 0.25x (2.5 shares), which would be below the venue minimum and skipped.

## Validation

`python -m pytest -q` passed with 216 tests. The pre-change 168-hour replay
remains `63` simulated fills, `56` settled, `37/19`, and `+8.8400` settlement-
only PnL because it replays already-recorded historical fills. It does not
predict the post-change live result; use future post-restart samples and the
same report window for the next comparison.

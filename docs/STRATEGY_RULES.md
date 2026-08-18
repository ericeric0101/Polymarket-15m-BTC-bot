# Strategy Rules

This is the operational source of truth for the current BTC 15-minute
strategy. It describes deployed behavior, not research ideas or superseded
V1/V2 plans.

## Objective

Trade a selected side of a BTC 15-minute binary market with passive maker
orders while constraining tail loss, stale-data risk, and total market
exposure. A high win rate alone is not an objective: every allowed entry must
also clear the configured live economics model.

## Market Data and Direction

- The preferred reference is Polymarket/Chainlink 60-second TWAP.
- A degraded external reference is allowed only under its configured policy.
- The direction engine selects one side: `UP`, `DOWN`, or `NONE`.
- A locked direction can be invalidated when the supporting evidence reverses;
  invalidation blocks new risk rather than silently switching an existing
  position to the other side.

## Entry Contract

The order of checks is fixed. A failure emits one final journal reason from the
first failing layer.

| Layer | Requirement | Result on failure |
| --- | --- | --- |
| Hard safety | Fresh valid quote/reference data, valid market phase, no configured external/book conflict | No candidate order |
| Direction | Locked side and adequate score | No candidate order |
| Model consistency | Valid strike/spot/fair estimate and high-price risk control | No candidate order |
| Economics | `robust_net` clears `ENTRY_MIN_ROBUST_NET_USDC` | No candidate order |
| Execution | Passive limit is valid; TTL, hysteresis, balance, and projected inventory cap pass | Submit or simulate an order |

`robust_net` is deliberately singular:

```text
robust_net = expected_value - empirical_execution_cost - fees
```

Do not add a trend-specific penalty discount or hidden negative-net exception.
The negative `fair - entry` bands are recorded in shadow telemetry only. A
future live policy requires a separately documented, cost-inclusive,
out-of-sample result and an explicit bounded range.

The empirical execution cost is the observed 10-second adverse markout of
real maker BUY fills, scaled to the submitted quantity. If the journal has
fewer than `EXECUTION_COST_MIN_SAMPLES` observations in
`EXECUTION_COST_LOOKBACK_HOURS`, new BUY entries are blocked; the bot does not
fall back to VWAP, depth, spread, volatility, or taker-leakage proxies.

## Score and Time Rules

- First filled entry of a market: `max(FIRST_ENTRY_SCORE_MIN, ENTRY_SCORE_MIN)`.
- Later entry: `ENTRY_SCORE_MIN`.
- First-entry timing uses `FIRST_ENTRY_MAX_TIME_LEFT_SEC`.
- `ENTRY_MIN_TIME_LEFT_SEC` blocks only new tail-end risk.
- `REDUCE_ONLY` always blocks new buys regardless of score or economics.

## Size and Exposure

- `MARKET_TARGET_SHARES` is the normal market target: 10 shares.
- Above `HIGH_PRICE_THRESHOLD`, target is `HIGH_PRICE_TARGET_SHARES` (currently 5.5 shares in the local deployment profile).
- `MARKET_MAX_POSITION_SHARES` is a hard projected cap: 10 shares.
- Each market has one successful BUY entry slot. Once any UP or DOWN BUY fills,
  all later BUYs in that market are blocked, including after a side flip or
  restart. Partial fills of that same order remain valid inventory.
- A remaining quantity below the venue minimum is skipped; it is not rounded
  up beyond the cap.

## Order Lifecycle

- New entries are passive maker limits according to `ORDER_POST_ONLY`.
- Requotes use `ORDER_REQUOTE_MIN_AGE_SEC` and
  `ORDER_REQUOTE_HYSTERESIS_TICKS` to avoid churn.
- Orders expire at `ORDER_TTL_SEC`; cancellation state is reconciled before a
  replacement can increase exposure.
- Dry run uses the same target, TTL, hysteresis, cancellation, and
  submit-time controls as live. The only deliberate difference is that it
  does not submit a wallet order.

## Exit and Settlement

- Confirmed inventory is eligible for normal passive reduction.
- `HOLD_TO_REDEEM` may carry qualifying inventory through settlement.
- Recovery exits require the configured TWAP confirmation and timing guards;
  they are not a high-frequency reaction to small noise.
- The settlement journal records both traded markets and outcome-only markets
  so replay can distinguish skipped opportunities from trade outcomes.

## Change Discipline

For each strategy change: preserve a baseline, make one causal change, run the
full test suite, compare a replay over the same interval, and then observe
dry-run or limited live behavior. Do not combine threshold, cost, sizing, and
exit changes in one experiment.

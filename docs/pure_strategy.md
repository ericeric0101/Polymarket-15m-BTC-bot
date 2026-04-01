# Pure 15-Min Strategy Spec

## Goal

Design a minimal BTC 15-minute Polymarket strategy around only four live inputs:

- `S`: current BTC spot price
- `K`: market `priceToBeat`
- `tau_sec`: time remaining to settlement, in seconds
- orderbook prices for `UP` and `DOWN`

The strategy does not try to predict long-horizon direction. It only estimates the fair binary probability at the current moment, then looks for orderbook prices that are materially mispriced versus that estimate.

## Core Market Model

This market is treated as a short-dated binary option:

- `UP` settles to `1` if final BTC price is greater than `K`
- `DOWN` settles to `1` if final BTC price is less than or equal to `K`

Fair value is therefore:

- `p_up = P(final_price > K | S, tau_sec, sigma)`
- `p_down = 1 - p_up`

## Fair Price Formula

### Preferred form

Use seconds as the external time unit everywhere in the strategy.

The implementation can still convert seconds internally when combining with annualized volatility:

- `t_years = tau_sec / (365 * 24 * 3600)`
- `d2 = (ln(S / K) - 0.5 * sigma^2 * t_years) / (sigma * sqrt(t_years))`
- `p_up = N(d2)`
- `p_down = 1 - p_up`

Where:

- `N(.)` is the standard normal CDF
- `sigma` is short-horizon annualized realized volatility estimated from recent BTC spot returns
- `tau_sec` stays in seconds in config, logs, DB payloads, and user-facing docs
- only the internal pricing step converts `tau_sec -> t_years` because `sigma` is annualized

### Simplified intuition form

For intuition, the same idea can be read as a standardized distance:

- `z ~= (S - K) / (sigma_sec * S * sqrt(tau_sec))`
- `p_up = Phi(z)`

where:

- `sigma_sec = sigma_annual / sqrt(365 * 24 * 3600)`

This is not the implementation target when the log form is available, but it is the right conceptual model:

- larger `S - K` increases `p_up`
- smaller `tau_sec` pushes probabilities closer to `0` or `1`
- larger `sigma` pulls probabilities back toward `0.5`

## Trading Philosophy

The strategy should only trade temporary mispricing, not conviction.

Examples:

- Buy `UP` only when `ask_up` is sufficiently below `p_up`
- Buy `DOWN` only when `ask_down` is sufficiently below `p_down`

The strategy should avoid carrying meaningful wrong-side inventory into settlement. Inventory and time rules matter more than directional cleverness.

## Signal Definitions

### Fair Values

- `fair_up = clamp(p_up, 0.01, 0.99)`
- `fair_down = clamp(1 - p_up, 0.01, 0.99)`

### Orderbook Inputs

- `bid_up`, `ask_up`
- `bid_down`, `ask_down`

### Entry Edges

- `edge_buy_up = fair_up - ask_up`
- `edge_buy_down = fair_down - ask_down`

### Minimal Candidate Rules

Candidate `BUY_UP`:

- `edge_buy_up >= min_edge`
- `tau_sec >= min_entry_sec`
- `fair_up` is not too extreme

Candidate `BUY_DOWN`:

- `edge_buy_down >= min_edge`
- `tau_sec >= min_entry_sec`
- `fair_down` is not too extreme

Suggested fair band filter:

- `min_prob_band <= fair_up <= max_prob_band`

with the same logic implied for `fair_down`.

## Hard Inventory and Time Rules

These rules are mandatory in the real strategy, even if the probe script does not trade.

### Inventory

- fixed small notional per trade
- hard per-market notional cap
- no averaging down
- no simultaneous directional doubling

### Time

- no new entries when `tau_sec < min_entry_sec`
- reduction-only when `tau_sec < reduce_only_sec`
- force flat when `tau_sec < force_flat_sec`

### Market Loss Control

- hard max loss per market
- if hit, stop trading that market until rollover

## Initial Parameter Table

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `probe_interval_sec` | `2` | Snapshot cadence |
| `sigma_window_points` | `120` | Recent spot points used for sigma |
| `sigma_min_points` | `20` | Minimum points required before realized sigma is trusted |
| `sigma_default` | `0.60` | Fallback annualized sigma |
| `sigma_floor` | `0.20` | Lower clamp for sigma |
| `sigma_ceiling` | `1.20` | Upper clamp for sigma |
| `min_edge` | `0.04` | Minimum underpricing needed to flag a candidate entry |
| `min_prob_band` | `0.08` | Avoid new entries at extremely low fair value |
| `max_prob_band` | `0.92` | Avoid new entries at extremely high fair value |
| `min_entry_sec` | `90` | No new entries below this time remaining |
| `reduce_only_sec` | `30` | Only reduce risk inside this window |
| `force_flat_sec` | `15` | Intended hard flat threshold for real trading |
| `orderbook_levels_limit` | `5` | Depth levels recorded per side |
| `market_poll_lookahead` | `4` | Number of future 15m markets considered |

## Probe Script Requirements

The probe script is intentionally non-trading. It should:

- resolve the current BTC 15m market
- fetch `K`, token IDs, and token outcomes
- fetch BTC spot `S`
- estimate `sigma`
- compute `fair_up` and `fair_down`
- fetch top-of-book `UP` and `DOWN`
- compute `edge_buy_up` and `edge_buy_down`
- write snapshots into `trade_journal.db`

It must not:

- place any order
- alter balances
- modify strategy state used by the live bot

## DB Recording Spec

Write into existing `strategy_runs` and `strategy_events`.

### Run Metadata

- `mode = "probe"`
- `maker_mode = 0`
- `test_mode = 1`

### Event Types

- `PURE_PROBE_MARKET`
  - market switch or initial market metadata
- `PURE_SIGNAL_SNAPSHOT`
  - every poll; includes spot, strike, time, sigma, fair values, and top-of-book
- `PURE_SIGNAL_CANDIDATE`
  - only when an edge exceeds threshold

### Snapshot Payload Fields

- `slug`
- `market_start_ts`
- `market_end_ts`
- `time_left_sec`
- `spot`
- `strike`
- `sigma_annual`
- `fair_up`
- `fair_down`
- `up_token_id`
- `down_token_id`
- `bid_up`
- `ask_up`
- `bid_down`
- `ask_down`
- `edge_buy_up`
- `edge_buy_down`
- `candidate_side`
- `candidate_edge`

## Validation Plan

The probe is used to answer one question:

`Do observed edge signals correspond to profitable real trades often enough to justify a live implementation?`

Validation can be done later by comparing:

- probe `PURE_SIGNAL_CANDIDATE` timestamps and slugs
- real `order_events` and `strategy_events`
- eventual settlement outcome

Useful checks:

- how often candidate signals occurred
- how long signals persisted
- whether the bot traded during or near those signals
- whether candidates had positive realized edge versus settlement

## Non-Goals

This spec intentionally excludes:

- complex regime filters
- discretionary direction scoring
- maker inventory skew logic
- stop-loss patch chains
- auto-redeem and execution management

Those can be reconsidered only after the pure signal layer proves that the market offers persistent, tradeable mispricing.

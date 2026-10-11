# E5B-D pre-registration: price-adjusted edge shadow, calibration `edge_cal_v1`

Written 2026-10-11 before the calibration table was computed and before any
post-cutoff outcome was inspected for D. Shadow telemetry only: D never gates,
sizes, prices, submits or cancels anything.

## Data and cutoff
- Development data: `reports/entry_research_e0_e4_20261011/e0_decision_points.parquet`
  (5 s grid, executable bid/ask per side) joined to official outcomes
  (`e0_labels_dev.csv`, `e0_labels_holdout_SEALED.csv`; unresolved excluded).
- Only decision points with `t < FORWARD_CUTOFF_TS = 1791691110` (2026-10-11T03:58:30Z).
  Everything at or after the cutoff is the clean forward set.
- The E0–E4 holdout days (Taipei >= 2026-10-07, pre-cutoff) are used for fitting:
  their entry hypotheses were already evaluated, and D is a new calibration whose
  clean test is the forward set.

## Features and transformation (fixed)
- `executable_ask` of the evaluated side (best ask; never a midpoint), valid range [0.01, 0.97).
- `TTE` (seconds to market end).
- Cells: ask {<0.50, 0.50–0.65, 0.65–0.75, 0.75–0.85, 0.85–0.97} x TTE {<=480, >480}
  (bucket edges reused from E5A; no search).
- side_score, strike distance, Binance/Chainlink move, TWAP state and spread are
  recorded alongside but are NOT inputs of `edge_cal_v1` (E5A: no score benefit after price).

## Calibration method
- Unit: one observation per (market, side, cell): the first grid point of that
  market/side inside the cell.
- Per cell: n markets, wins, mean ask, Taipei days.
- Estimate (AMENDMENT 1, see below): per-cell residual shrunk toward 0
  (prior = the market price is calibrated),
  `residual_cell = (wins - sum_ask) / (n + K)`, K = 20, and at runtime
  `p_cal = clip(executable_ask + residual_cell, 0.001, 0.999)`.
- A cell is usable only with n >= 20 and >= 3 Taipei days; otherwise D emits
  `EDGE_STATE=UNKNOWN`.

## Amendment 1 (2026-10-11, before any forward outcome was inspected)
The first draft used one constant `p_cal` per cell (Beta shrinkage toward the cell
mean ask). Computing the development table showed that this makes RAW_EDGE a
mechanical function of where the ask sits inside its bucket (cheap end positive,
expensive end negative) regardless of information. The estimator form was
therefore changed to the residual form above, which keeps the market price as the
baseline. Features, cells, K, usability thresholds, fee model, penalties, metrics
and kill criteria are unchanged. Original draft sha256: 66e29381ad9a…9cce.

## Edge decomposition (per share, taker at the executable ask)
- `RAW_EDGE = p_cal - executable_ask`
- `fee_equivalent = p_cal * fee_frac(executable_ask)`; `fee_frac` = official crypto taker
  curve as fee shares / shares (`execution.rebate_model.estimate_taker_buy_fee_shares`),
  i.e. taker net EV/share = p(1 - frac) - ask.
- `NET_EDGE_BEFORE_SELECTION = RAW_EDGE - fee_equivalent`
- `NET_EDGE_AFTER_SELECTION = NET_EDGE_BEFORE_SELECTION - penalty`, emitted for
  penalty in {0.000 (none), 0.018 (reference: E2 market-level adverse selection)}.
  The E5A T1 attempt-level 0.0875 is NOT used as truth (wide, small n).
- Also recorded: `maker_raw_edge = p_cal - planned_maker_price` (maker fills are fee-free).

## Forward evaluation metrics (fixed; computed only after the forward sample exists)
- Calibration table by p_cal bucket: predicted vs observed win rate (Wilson 95%),
  calibration error; Brier score secondary; AUC secondary only.
- Executable-price-adjusted EV/share of entries grouped by sign of NET_EDGE_BEFORE_SELECTION.
- Day-level sign counts and leave-one-Taipei-day-out stability.

## Acceptance / kill (research only; never production behaviour)
- Forward sample >= 150 canonical attempts, >= 7 Taipei days, >= 2 weekend days,
  >= 60 T1 attempts with adequate path telemetry.
- PROMISING only if the entries with NET_EDGE_BEFORE_SELECTION > 0 have forward
  EV/share > 0 with all LODO signs > 0 and calibration error within +/-0.05 per bucket.
- Kill: forward EV <= 0, any LODO sign flip, or > 30% of attempts UNKNOWN.

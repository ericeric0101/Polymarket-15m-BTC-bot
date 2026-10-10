# FROZEN SPEC — Historical Early-Warning Audit (pre-registration)

Written 2026-10-10 (+08) BEFORE any scorecard was computed. After this file is hashed, nothing below is added, dropped or retuned. Anything else is EXPLORATORY and cannot enter the shortlist.

Disclosure: the analyst has already seen the 3 positions of the 2026-10-10 LIVE run in detail (previous post-LIVE audit). Those 3 positions are a separate PROSPECTIVE cohort and are never used in DEV or HOLDOUT.

## 1. Unit, universe, cohorts

- Unit = ACTUAL LIVE POSITION = all LIVE BUY fills of one market (`strategy_runs.mode='LIVE'`), aggregated (qty sum, VWAP). Entry time = first BUY fill. Clusters: market, Taipei day of first fill (UTC day for the weekend question).
- DEV = the earliest 9 Taipei days with LIVE fills (2026-09-10 … 2026-09-25); HOLDOUT = the remaining September days (2026-09-27 … 2026-09-30, 4 days); PROSPECTIVE = 2026-10-10 (3 positions). Chronological split by day, never by trade. HOLDOUT_VALID requires ≥3 HOLDOUT days with ≥5 triggered positions for the evaluated rule.
- Timing features may pool across sizing/breaker cohorts (same feature semantics). PnL is reported in USDC, per share and per notional; magnitudes are not pooled across sizing regimes without normalization.
- NON_ACTIONABLE positions: sellable qty < 5 shares at signal time → no warning can be acted on; kept for signal quality, excluded from economics.

## 2. Sources and clocks (no midpoint as executable)

| Feature | Sep source | Oct source | Clock |
|---|---|---|---|
| Binance spot | cold `reference_1s` source=`binance` (same run/slug) | `PREDICTION_RESEARCH_SNAPSHOT.btc_spot` | local receipt (`received_epoch_ns` / `snapshot_ts`) |
| Chainlink spot | `reference_1s` `polymarket_spot` | n/a (journal only if present) | local receipt |
| Official 60 s TWAP | `reference_1s` `polymarket_twap` | snapshot `official_twap` (twap_fresh) | local receipt |
| Strike | journal `MARKET_STRIKE_LOCKED` | same | — |
| Executable bid + engine net | journal `EXIT_POLICY_DECISION`, `STOP_TIMING_*`, `ORDER_TAKER_EXIT_SUBMIT` (best_bid, net_if_exit/est_net) | same + snapshot `best_bid_*` (bid only) | journal host wall clock |
| Bid only | `EXIT_AUDIT` | same | journal clock |
| Markout, score, spread, depth | `FILL_MARKOUT` (entry_*), `ORDER_SUBMIT` | same | — |
| Sigma / diffusion-z | any recorded `required_move_sigma` / `required_move_z_diffusion` | snapshots / telemetry | — |
| Outcome | official Gamma cache (journal label never primary) | same (fetched for missing) | — |

`reference_1s` `polymarket_bbo` and snapshot `up_mid` are midpoint/ambiguous-token series → UNUSABLE for token signals or economics.

Entry bid baseline (token drawdown): FILL_MARKOUT `entry_bbo_bid` → else BUY `ORDER_SUBMIT` book best_bid → else first recorded executable bid ≤30 s after entry. Token drawdown = baseline − current executable bid.

## 3. Frozen signals (first occurrence per position; fires at the observation time it becomes known)

Single features:
- BINANCE adverse ≥ 1 / 2 / 3 / 5 / 10 bps (signed against held side vs Binance at entry = latest Binance obs ≤ entry fill).
- TOKEN bid drawdown ≥ 0.05 / 0.10 / 0.15 / 0.20.
- PNL (engine net_if_exit) ≤ −1 / −1.5 / −2 / −2.5 / −3 USDC.
- STRIKE/REFERENCE: Binance adverse strike cross; Chainlink-spot adverse strike cross; TWAP adverse strike cross. A cross = first observation after entry whose state is adverse (tie settles UP). If already adverse at the first post-entry observation it fires there and is flagged `ADVERSE_AT_ENTRY` (Binance–Chainlink basis).
- SIGMA legacy required_move_sigma ≥ 0.5 / 1 / 2 (not a probability or z-score); DIFFUSION-Z only if coverage passes the small-sample rule.
- TTE ≤ 300 / 180 / 120 / 60 s.
- ENTRY/QUALITY (fire at entry, or entry+horizon for markouts): low score = |entry side score| < 0.12 (existing `exit_conviction_band_min_score_abs`); negative 1 s markout; negative 30 s markout (available entry+30 s); high entry price > 0.70 (share_v1 boundary); shallow depth = qty > 10 % of entry top-of-book ask depth (existing `depth_risk_depth_fraction=0.10`); wide spread = entry spread > 0.01 (more than one tick).
- Persistence variants (fire at t+k when confirmed): Binance ≥3 and ≥5 bps still adverse at every observation over +5/+15/+30/+60 s; token ≥0.15 and ≥0.20 still met at every observation over +5/+15/+30/+60 s (needs ≥1 observation at ≥ t+k).

Combinations (state-based: fire at the first time t when all conditions hold simultaneously using the latest known value of each feature at t):
A Binance≥3; B Binance≥5; C token≥0.15; D token≥0.20; E Binance≥3 AND token≥0.15; F E AND PnL<0; G E AND TTE≤180; H E AND TTE≤120; I E AND sigma≥0.5; J E AND sigma≥1; K E with both conditions persisting ≥15 s; L Binance≥5 AND token≥0.15 persisting ≥15 s; M token≥0.15 AND negative 30 s markout; N Binance≥3 AND |TWAP−strike| < 10 USD (existing `endgame_twap_exit_min_distance_usd=10`).

Total examined: 5+4+5+3+3+4+6 single + 16 persistence + 14 combos = 60 rules (multiple-comparison warning).

## 4. Targets

A final economic loss: realized PnL (fills + official payout on residual) < 0. B severe drawdown after signal: min future engine net ≤ −2/−3/−4 (observed grid; censored at exit). C conditional_absolute_loss_breaker submit (`absolute_max_loss_breaker`) occurs after the signal (observed submit; eligibility components not reconstructable for September → labeled). D non-recovery: no engine net above signal-time net within 15/30/60 s (UNKNOWN when no observation in window) and/or final adverse settlement. E recoverable winner: after the signal net ≥ 0 is observed, or a TP sell fills, or official outcome favourable.

## 5. Economics

Theoretical exit = first engine row (best_bid + net_if_exit) at or after signal time within 25 s (≈ one 21 s grid step); PnL = that net_if_exit + realized PnL of any earlier partial sells. If the actual full exit happened before the signal → censored (= actual). DELTA = theoretical − actual realized. SAVED_LOSS = Σ positive DELTA; WHIPSAW_COST = Σ negative DELTA on positions that finally won (or whose actual > theoretical); NET_BENEFIT = Σ DELTA. Also vs hold-to-settlement (official payout). No midpoint ever.

## 6. Timing quality

Binance–Binance 1 s = APPROX_SAME_SECOND; any pairing with the journal engine grid (~21 s in September, 5 s for breaker evaluation, ~1 s snapshots in October) = OBSERVED_GRID_APPROX with the coarsest grid involved. A lead smaller than the coarsest grid = SIMULTANEOUS. Clock-incompatible pairs are not aggregated.

## 7. Small-sample rule and acceptance gates (defaults, unedited)

- Rates only for rules triggering on ≥5 positions across ≥3 independent days; else counts + INSUFFICIENT.
- Shortlist (max 3) requires ALL: ≥5 triggered positions over ≥3 days; median lead vs breaker ≥ 2× coarsest grid involved and TIMING_QUALITY ≠ UNUSABLE; executable-bid coverage ≥80 % of triggers; NET_BENEFIT > 0 on DEV and (if HOLDOUT_VALID) on HOLDOUT; recoverable-winner exits reported and tolerable (whipsaw cost < saved loss); frozen-list membership.
- No production sell authority results from this audit.

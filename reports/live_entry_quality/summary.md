# Live Entry Quality / Shadow Risk 報告

- Journal: `logs/trade_journal.db`
- Research snapshots: 0; unique markets: 0; submitted: 0; filled: 0; settled filled candidates: 0.
- `ENTRY_DECISION_TRACE` rows in the selected journal: 66082.
- This is observation-only. Shadow reject and size-down columns are counterfactual labels; they do not alter trading authority.
- Research review threshold: at least 30 independent markets per bucket for screening; policy review should prefer 50–100+ independent markets over multiple weeks and weekday/weekend coverage.
- Maker fill-to-candidate attribution uses the durable research candidate ID. Historical rows without it are not force-matched. Settlement PnL is market-level settlement telemetry, not an isolated per-order realized PnL.
- Markouts are signed per-share follow-ups where available; missing horizons remain null. Public/venue fills and BBO are not synthesized.

## Coverage and caveats

See `candidate_level.csv` for source, freshness, strike provenance, BBO/depth, economics components, candidate status, fills, markouts and settlement join fields. A missing strike/spot/volatility value remains unavailable, not zero.
- If this report says zero snapshots, the selected journal predates the new `research_snapshot` payload or is not the live journal; it is not evidence that no historical candidates existed.
- Normal-maker `fair` comes from the configured pricer (normally digital when canonical spot/strike are available). The legacy `calibrated_probability` field copies this fair value; its name alone does not prove empirical calibration. Qualified strong-directional regimes use measured historical win rates.
- Normal maker quotes are passive at the quote plan's BUY price. Outcome fast-follow uses a target-token executable ask and a separate FOK economics calculation.
- `shadow_reject_counterfactual.csv` is a settlement-direction/full-fill gross proxy at the recorded candidate price and quantity, not realizable PnL; maker snapshots are passive prices and may never have been executable. It excludes fees, fills, queue position, impact, exits and sizing constraints.

## Bucket reports

`edge_buckets.csv`, `price_buckets.csv`, `strike_distance_buckets.csv`, `safety_sigma_buckets.csv`, `crossing_buckets.csv`, `weekday_weekend.csv`, `hour_blocks.csv`, `shadow_reject_counterfactual.csv`, and `shadow_size_counterfactual.csv` report candidate/fill/settlement/markout counts. Empty buckets are retained so missing evidence is visible.

## Live economics formula audit

- Maker passive quote: spread-capture expected net is `expected_spread_capture + expected_rebate - adverse_selection_buffer`; the quote engine also computes `robust_net = expected_net - execution_penalty`. The normal maker path intentionally does not universally subtract the imported markout penalty from its admission gate; strong-directional regime eligibility gates on measured `resolution_ev >= min_expected_net`, while markout-adjusted robust net is telemetry. See final audit in task response.
- Outcome FOK: `resolution_ev = qty × (fair_probability_for_outcome - limit_price)`; `robust_net = resolution_ev - taker_fee - empirical_adverse_markout_penalty`; the FOK allow/reject compares expected net to the configured minimum.
- No live parameters or behavior are changed by this report generator.

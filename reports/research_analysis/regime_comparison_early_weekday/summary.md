# EARLY WEEKDAY VS WEEKEND COMPARISON

## 1. Analysis provenance

Frozen baseline: `c8eeaaacb660a32fd1be305115ed31fd1c67c5fa`. Inputs are consistent SQLite online-backup snapshots under `data/analysis_snapshots/20261005_065718_+0800/`. Both report `PRAGMA quick_check = ok`; `snapshot_manifest.json` records source mtime, snapshot time, byte size, and table counts.

Research provenance identifies 404 runs / 198 markets over 2026-10-02 through 2026-10-05: 28 weekday and 170 weekend markets across all history. All 404 runs are `LEGACY_UNKNOWN`; no git commit, config hash, or schema-version manifests exist. The primary canonical comparison has 21 synchronized Monday markets (2026-10-05) and 144 synchronized weekend markets. It excludes 85 weekday settlement summaries without comparable prediction snapshots. These run records do not prove frozen-code or schema compatibility.

## 2. Cohort definition

| Regime | Discovered | Completed | Synchronized | FULL | GOOD | PARTIAL | INTERRUPTED | UNUSABLE | Median coverage | Median joint freshness | Median max gap |
|---|---:|---:|---:|---|---|---|---|---:|---|---|---|
| Current Monday synchronized | 21 | 21 | 21 | NA | NA | NA | NA | 0 | NA | NA | NA |
| Historical synchronized weekend | 144 | 144 | 144 | NA | NA | NA | NA | 0 | NA | NA | NA |
| Legacy weekday excluded | 85 | 85 | 0 | NA | NA | NA | NA | 85 | NA | NA | NA |

Canonical preliminary export does not expose quality buckets or market-level freshness/coverage medians. Checkpoint usable N is lower than catalog count (T-300 weekday 17, weekend 124); missing probability values remain missing.

## 3. Comparability assessment

- Coverage / settlement: **PARTIALLY_COMPARABLE**; both catalogs contain settled markets with synchronized prediction timelines, but usable N differs by checkpoint.
- Freshness / cadence: **PARTIALLY_COMPARABLE**; some checkpoint values are absent, and full joint-freshness/cadence distributions are not exported.
- Restart rate, schema, and provenance: **NOT_COMPARABLE**; run manifests are absent.
- No pooled weekday/weekend outcomes are used. See `dataset_comparability.csv`.

## 4. Time-of-day matching

Monday market starts cover Taipei hours 00–06. The comparable weekend subset in those same hours is 31 markets (from Saturday and Sunday), versus 21 Monday markets. All-weekend N is 144. This is coarse hour matching only.

| Checkpoint | Weekday N: flips (rate) | Matched weekend N: flips (rate) | Weekday−weekend rate | Weekday Brier market/p_ex | Matched weekend Brier market/p_ex |
|---|---:|---:|---:|---:|---:|
| T-300 | 17: 2 (0.118) | 26: 4 (0.154) | -0.036 | 0.130/0.127 | 0.100/0.100 |
| T-180 | 18: 1 (0.056) | 25: 0 (0.000) | 0.056 | 0.050/0.105 | 0.009/0.050 |
| T-120 | 18: 2 (0.111) | 24: 1 (0.042) | 0.069 | 0.031/0.143 | 0.012/0.059 |
| T-60 | 18: 2 (0.111) | 25: 0 (0.000) | 0.111 | 0.102/0.083 | 0.036/0.022 |
| T-30 | 18: 1 (0.056) | 29: 0 (0.000) | 0.056 | 0.001/0.004 | 0.037/0.002 |

## 5. Flip probability by checkpoint

Frozen canonical definitions available here are T-300, T-180, T-120, T-60, and T-30. It does not define the requested T-600, T-480, T-360, T-240, T-90, T-15, or T-5; none were fabricated. Exact N, flips, observed rate, mean market/analytic p, Brier, and calibration error are in `flip_probability_by_checkpoint.csv`. Markets repeated at checkpoints are never considered independent across checkpoints.

## 6. Market vs analytic calibration

At T-300 weekday is 2/17 = 11.8%, with market Brier 0.1303 and analytic Brier 0.1266; weekend is 15/124 = 12.1%, with 0.0922 and 0.1129. Weekday-minus-weekend Brier differences are +0.0381 market and +0.0137 analytic. At other checkpoints the ranking changes. This does not establish that one model wins. p_ex remains a structural/fair-value reference, not a predictive edge.

## 7. Required_move_sigma

At T-300 the weekday bins are: <0.5σ 1/6; 0.5–1σ 1/6; 1–2σ 0/4; 2–3σ 0/1; 3–5σ 0/0; >5σ 0/0. Weekend: 13/56, 2/40, 0/21, 0/4, 0/1, 0/2. This is suggestive of stratification, but weekday N is tiny and the weekend curve is not monotonic at every checkpoint. Verdict **MIXED**, not replicated yet. Per-checkpoint distributions and quantiles are in `flip_rate_by_sigma.csv` and `sigma_distribution.csv`.

## 8. Required_move_bps

**NOT MEASURABLE**: canonical preliminary checkpoint output does not export `required_move_bps`. No imputation was made; zero is not confused with missing.

## 9. Same-price structural risk

**INSUFFICIENT_N / NOT MEASURABLE**: no price-matched sigma comparison is emitted by the canonical pipeline. No entry threshold is derived.

## 10. BTC fast-move / repricing

Canonical repricing events use the existing ≥10c coalesced event threshold. All synchronized markets: weekday 75 events / 21 markets = 3.57 per market, with ≥1 event in 16/21; weekend 442 /144 = 3.07 per market, with ≥1 in 117/144. In matched hours: weekend 123 events /31 = 3.97 per market, ≥1 in 27/31. Weekday minus all-weekend is +0.50 event/market; versus matched weekend −0.39. The canonical run does not emit all ≥5c events, so those remain unavailable.

Lead/lag batch has 336 measurable p_ex events: p_ex leads by >2s in 9 and lags by >2s in 223; median lead-vs-market is −3.80s. This is event-level descriptive evidence, not causal proof or a weekday-specific effect. See `btc_fast_warning.csv` and `repricing_behavior.csv`.

## 11. Entry performance

These are **settled shadow entries only**, not actual live fills. All synchronized: weekday 11/14 wins (78.6%), weekend 35/41 (85.4%), difference −6.8 percentage points. In the matched window: weekday 11/14 versus weekend 12/13 (92.3%), difference −13.7 points. Weekday classifications: 2 VERIFIED_EX_MARKET_EDGE, 8 MARKET_FOLLOWING, 4 UNVERIFIED; weekend: 5, 18, 18. Win rate alone does not establish alpha. Gross PnL is reported in capital-efficiency section; entry-level classification data is retained in `synchronized_cohorts/entries.csv`.

## 12. Capital efficiency

Settled shadow cohort only; N is small. All synchronized weekday: N=14, capital-minutes 659.8, gross PnL $6.66, PnL/capital-minute 0.01009. Weekend: N=41, 1657.9, $7.32, 0.00442. Average PnL/trade difference is $0.30; capital efficiency difference is 0.00568. Hour-matched weekend N=13 has PnL/capital-minute 0.01554 versus weekday 0.01009. These values are extremely sample-sensitive and not a reason to optimize.

## 13. Tail-loss / lifecycle availability

Settled shadow losers: weekday 3 (average $-5.97, median $-5.50, worst $-6.90); all-weekend 6 (average $-5.49, median $-5.50, worst $-5.50). Comparable structural state / lifecycle / stop traces are **NOT ESTABLISHED** because run schema tags are missing; absence is not zero. See `instrumentation_availability.csv`.

## 14. Early findings

**ALREADY_VISIBLE:** synchronized Monday data exists; T-300 observed rates are close in the unadjusted comparison (2/17 vs 15/124). Market and analytic Brier differences are small on Monday but uncertain. ≥10c repricing event rates differ modestly and reverse direction under hour matching.

**POSSIBLE_BUT_UNDERPOWERED:** sigma appears to stratify risk in weekend observations and perhaps in weekday, but weekday bins have 0–6 markets. Shadow entry wins and capital outcomes differ, but only 14 weekday entries are present.

**NOT_REPLICATED:** no stable weekday sigma/calibration/entry pattern relative to weekend.

**NOT_YET_MEASURABLE:** ≥5c repricing frequency, required_move_bps, same-price structural risk, BTC causal warning, real-fill PnL comparison, and cohort-comparable lifecycle instrumentation.

Confidence: **LOW** for weekday findings. Exact market N is retained; checkpoint rows and multiple events per market are not independent.

## 15. What NOT to conclude yet

Do not attribute these differences to weekday regime; they may reflect session timing, a few markets, or legacy provenance. Do not claim p_ex predictive edge, use wins alone as alpha, or change entry/stop/hold rules.

## 16. Required additional weekday sample

Collect at least **100 additional completed synchronized weekday markets across five full weekdays**, with manifested commit/config/schema provenance and comparable Taipei-hour coverage, before formal comparison. This is a pragmatic descriptive floor, not a formal power guarantee.

## 17. Primary questions and final provisional verdict

- Q1 structurally different? **Not established.**
- Q2 sigma replicates as useful stratifier? **Mixed; weekend descriptive trend, weekday too small/nonmonotonic.**
- Q3 market beats p_ex? **No consistent winner; weekday T-300 p_ex slightly lower Brier, weekend market is lower.**
- Q4 weekday flip risk lower/higher? **Currently indistinguishable** (T-300 11.8% vs 12.1%; hour-matched 11.8% vs 15.4%, small N).
- Q5 persist after time matching? **No robust difference established; repricing direction reverses and flip-rate point estimate shifts, both underpowered.**
- Q6 entry strategy differs? **Shadow win-rate points lower on Monday (−6.8 pp all cohort), but N=14 and not evidence of a regime effect.**
- Q7 change entry/stop logic? **NO.**
- Q8 additional data? **At least 100 more completed weekday synchronized markets across five weekdays, with matched-hour coverage.**

**CONTINUE_COLLECTION_WITH_DATA_QUALITY_WATCH** — snapshots are consistent, but provenance/schema and exported quality counters need watching. No strategy change recommended.

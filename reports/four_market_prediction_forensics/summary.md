# Four-market prediction forensics

Run: `run_1790985778_95f5cc02`.  Analysis scope is exactly the four completed market slugs in
`data_quality.csv`; the subsequent partial market is excluded.  Repricing is
an UP-token probability move over 30 seconds (nearest fresh synchronized point
within ±3 seconds), coalesced where overlapping same-direction 30-second
windows describe one continuous move.  Predictor comparisons use only
`joint_fresh` snapshots.  A positive lead means the predictor reached its
defined directional change before market mid first moved 5¢ from the event
baseline.

## Executive verdict

- **Q1 p_ex lead: MIXED.** 13 ≥10¢ repricing events; p_ex was measurable for 9. It led by >2s in 0, lagged by >2s in 7, and was within ±2s in 2. Median lead was -7.13s (positive means p_ex first).
- **Q2 +0.10 residual replicated: NOT_REPLICATED.** Previous 14-market descriptive result: mean +0.2135 30s UP-mid repricing (N=10, 3 markets). This run: N=7, 1 market(s), mean -0.0086, median -0.0100, directional hit 0.2857. The new extreme-positive rows moved slightly **down**, not up.
- **Q3 negative residual symmetry: NOT_REPLICATED.** `<=−0.10` has N=1, mean 0.0100; it did not show the expected negative 30s repricing.
- **Q4 BTC disagreement: MIXED.** Agreement adverse rate 0.2615; disagreement 0.3429. But ≥5¢ adverse was 0.0615 versus 0.0571, so the stronger adverse threshold does not confirm it.
- **Q5 required sigma fragility: NO.** `<0.5σ` adverse rate 0.2500, versus `1–2σ` 0.4444; low required sigma was not the more fragile bucket in this run.
- **Q6 wrong DOWN fast-follow cause: YES — the existing ex-market and market variables already contradicted it.** At the candidate, p_up_ex_market had fallen from about 0.92 but remained 0.67–0.76, the fresh UP mid was 0.695/0.705, official TWAP state remained UP, and required move was still a negative UP-favoring path distance. The trigger was an Outcome/TWAP follower event, not independent DOWN confirmation. See `fast_follow_case.csv`.
- **Q7 winning entries classification: {'MARKET_FOLLOWING': 2, 'UNVERIFIED_DUE_TO_DATA': 1}.** No completed winning simulation has a clean positive `p_ex − recorded entry price`: they are market-following or unverified, not evidence of independent model edge. See `entry_edge_classification.csv`.

## Repricing event table

| Market | Direction | Start mid → end mid | 30s change | p_ex lead vs mid | BTC10 lead vs mid | Residual lead vs mid | Bot-side lead vs mid |
|---|---|---:|---:|---:|---:|---:|---:|
| btc-updown-15m-1790985600 | UP | 0.065 → 0.525 | 0.460 | 0.00 | 0.00 | — | — |
| btc-updown-15m-1790986500 | DOWN | 0.655 → 0.555 | -0.100 | — | 23.56 | — | — |
| btc-updown-15m-1790986500 | UP | 0.545 → 0.645 | 0.100 | -8.11 | 9.18 | — | — |
| btc-updown-15m-1790986500 | DOWN | 0.685 → 0.575 | -0.110 | -9.78 | 1.07 | — | — |
| btc-updown-15m-1790986500 | UP | 0.575 → 0.715 | 0.140 | -26.28 | -17.23 | — | — |
| btc-updown-15m-1790986500 | DOWN | 0.815 → 0.645 | -0.170 | — | — | — | — |
| btc-updown-15m-1790986500 | UP | 0.655 → 0.765 | 0.110 | — | — | — | — |
| btc-updown-15m-1790987400 | UP | 0.565 → 0.715 | 0.150 | -26.50 | 0.00 | — | — |
| btc-updown-15m-1790987400 | UP | 0.815 → 0.915 | 0.100 | — | 0.00 | — | — |
| btc-updown-15m-1790987400 | DOWN | 0.935 → 0.705 | -0.230 | -2.48 | 0.00 | — | — |
| btc-updown-15m-1790987400 | UP | 0.705 → 0.845 | 0.140 | -7.13 | -7.13 | — | — |
| btc-updown-15m-1790988300 | UP | 0.565 → 0.695 | 0.130 | 0.00 | 11.81 | — | 0.00 |
| btc-updown-15m-1790988300 | UP | 0.735 → 0.845 | 0.110 | -5.31 | 0.00 | — | — |

`required_move_sigma` is reported per event in `repricing_events.csv`, but is not ranked as an UP/DOWN lead because it is a distance-to-boundary magnitude rather than a directional predictor.

## Signal lead ranking

| Rank | Signal | Measurable events | Markets | Median lead seconds | Mean lead seconds | Qualification |
|---:|---|---:|---:|---:|---:|---|
| 1 | BTC 10s | 11 | 4 | 0.00 | 1.93 | DESCRIPTIVE |
| — | bot side | 1 | 1 | 0.00 | 0.00 | LOW_SAMPLE |
| 2 | BTC 5s | 13 | 4 | 0.00 | -0.59 | DESCRIPTIVE |
| 3 | p_ex | 9 | 4 | -7.13 | -9.51 | DESCRIPTIVE |
| — | residual | 0 | 0 | — | — | LOW_SAMPLE |
| — | TWAP state | 0 | 0 | — | — | LOW_SAMPLE |

## Residual replication

The fixed residual-bin result is in `residual_replication.csv`.  The key contrast is previous `>=+0.10`: +0.2135 mean 30s repricing versus this run: -0.0086, N=7.  This run weakens, rather than confirms, that earlier extreme-positive residual observation.

## BTC disagreement

`btc_disagreement.csv` compares the held token’s subsequent 30-second midpoint.  Disagreement had a somewhat higher any-negative rate, but not a higher ≥5¢ or ≥10¢ adverse rate; the result is **MIXED**, not a usable rejection rule.

## Required-path fragility

`required_sigma_repricing.csv` shows no monotonic pattern where a low required move made held-side repricing more adverse.  In particular, <0.5σ had a positive mean held-side repricing (0.0236) while 1–2σ was negative (-0.0100).  This run weakens the “low sigma = immediate fragile position” hypothesis.

## Fast-follow failure case

The DOWN candidate was created at `1790987899.382476`.  It did not have independent DOWN support: p_ex stayed UP-favoring and fresh UP market probability was already high.  BTC’s short 5s/10s return was briefly negative near the candidate, but it was the isolated conflicting input; TWAP state and p_ex never supplied a DOWN reversal.  If market mid is excluded, ex-market probability alone would still reject a DOWN entry; the candidate came from the separate Outcome follower path.

## Entry edge classification

The three settled simulation entries are itemized in `entry_edge_classification.csv`.  Classification compares fresh p_ex with the recorded order price, not a quote snapshot that may have repriced between simulation submission and fill.  A final win does not retroactively establish an independent edge.

## Final research verdict

**B. BTC short-term movement 是目前最有希望的 predictor** for this run’s descriptive event forensics: p_ex/residual did not consistently lead the fresh market repricings, and required-path sigma was not monotonic.  Short BTC movement is the most plausible existing early input because it is independent of the market and showed up before some repricings, but its disagreement comparison is mixed; it is not a live authority.

## Next two tests

### NEXT TEST 1

- **Hypothesis:** A BTC 5s/10s move only becomes useful when p_ex and market mid have not already repriced in the same direction.
- **Evidence from this run:** The DOWN fast-follow error had brief negative BTC movement while p_ex/TWAP state stayed UP; the broad disagreement cohort did not improve large-adverse detection.
- **Exact metric:** Among joint-fresh rows, compare 30s held-side repricing for BTC direction alone versus BTC direction plus p_ex/market sign agreement, using the existing `btc_return_5s_bps`, `btc_return_10s_bps`, `p_up_ex_market`, and fresh mid fields.
- **Confirm:** The combined condition improves ≥5¢ adverse/repricing hit rate over BTC-only in both directions.
- **Reject:** The combined condition remains no better than BTC-only or loses direction symmetry.

### NEXT TEST 2

- **Hypothesis:** Extreme residual is useful only after its direction persists, rather than at the first extreme print.
- **Evidence from this run:** `>=+0.10` residual did not reproduce the earlier 30s effect and its observations all came from one market; individual raw extremes reversed.
- **Exact metric:** Reuse existing snapshots to compare first extreme residual versus residual persisting for at least two 1Hz snapshots, measuring future 30s UP-mid movement by sign.
- **Confirm:** Persisted extremes have directional 30s repricing while first-print extremes do not.
- **Reject:** Persistence does not improve directional hit rate or remains asymmetric.

## Limitation

The first market has partial opening coverage; event rows, not 2,412 correlated snapshots, are the unit for the headline lead comparison.  The report is offline research only and makes no live strategy change.

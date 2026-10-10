# Stage 2 — outcome-provenance rebuild (offline after one Gamma fetch)

- Official cache: data/research_export/official_resolution/official_resolutions_20261009T041657Z.json
  file sha256 7795b2c9…, content sha256 5356cf95…, 954 markets, all resolved (UP 486 / DOWN 468); mirrored to iCloud.
- Provenance dataset: data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv (sha256 ea744003…).
  outcome_source_used: POLYMARKET_OFFICIAL 954; confidence HIGH 952, HIGH_OFFICIAL_TWAP_CONFLICT 2.

| comparison | compared | mismatch | rate |
|---|---|---|---|
| journal MARKET_SETTLEMENT.outcome vs official | 895 | 68 | 7.6% |
| — LIVE period (<10-01) | 462 | 44 | 9.5% |
| — DRY-RUN period (>=10-01) | 433 | 24 | 5.5% |
| canonical TWAP vs official | 437 | 2 | 0.46% (both: last TWAP tick 2.5-4.4 s old, near-tie) |

## LIVE (2026-09-10..09-30, 11 days) — realized = fills + official payoff
- 125 positions (official labels, full coverage): total -$9.28, mean -$0.074, median +$0.92, win 74.4%.
- Journal MARKET_CYCLE_PNL total +$15.37 is NOT reliable: on the 108 traded markets that both label sets cover, labels flip 0 times;
  the gap comes from ledger accounting defects (residual inventory double-counted after SELL fills, e.g. 1789745400 journal
  +$6.76 vs +$0.94; inventory missing at settlement, e.g. 1790568000 journal $0 vs -$6.60 held DOWN, official UP).
- Reconstruction check: 21/23 positions with on-chain REDEEM_EXECUTED match residual shares; 2 unexplained (partial redeem logs).
- Stop vs hold: 8 stops (journal labels saw 5); 1/8 held side would have won; stop - hold = +$8.26. TP vs hold (75): +$9.66.

## DRY-RUN shadow (2026-10-01..10-08, 8 days) — counterfactual, optimistic fills
- 266 markets; 20 relabelled. Mean PnL/market: journal +$0.065 -> official -$0.036 (SIGN CHANGED); total +$17.27 -> -$9.60.
- Price bin 0.75-0.80: journal +$0.19 -> official -$0.11 (SIGN CHANGED). Other bins: magnitudes changed, signs unchanged.

## Prior conclusions that changed
1. "Journal LIVE PnL +$15.37" -> corrected LIVE -$9.28 (125 positions); prior report's -$19.79 used incomplete label coverage (111).
2. DRY-RUN shadow entries are not profitable overall under official labels (-$0.036/market).
3. 0.75-0.80 entry-price bin turns negative in DRY-RUN; only >=0.80 stays positive.
4. LIVE score/price/TTE bins unchanged by relabelling (0 flips among traded markets).
## New defect (not fixed in this task): live ledger cycle-PnL accounting (see above) — journal PnL must not be used as truth.

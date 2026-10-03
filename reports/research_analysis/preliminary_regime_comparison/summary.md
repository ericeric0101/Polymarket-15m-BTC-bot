# Preliminary weekend vs weekday regime comparison

Mode: PRELIMINARY_REGIME_COMPARISON. Weekend and weekday are never pooled.

## Market counts

- Weekend: sampled coverage=7.62 hours across completed=34;
  market-start span=16.25 hours;
  synchronized usable=34; actual T-300 leader flips=2.
- Weekday: synchronized usable=0; actual T-300 leader flips=0.
- Legacy weekday: 85 summaries lack comparable synchronized
  prediction snapshots and are excluded from primary calibration, lead/lag,
  residual, and flip comparisons.

## Numeric direction visible now

- Weekend T-300: N=33, observed flip rate=0.06060606060606061,
  market-implied flip probability=0.22105555555555556,
  analytic flip probability=0.23674085674357573.
- Comparable weekday rows: 0. Weekday curves and cross-regime
  calibration are NOT_MEASURABLE, rather than zero or pooled with legacy data.

## Weekend checkpoint curve

| Checkpoint | N | Observed flip rate | Market flip p | Analytic flip p | Market Brier | Analytic Brier |
|---|---:|---:|---:|---:|---:|---:|
| T-300 | 33 | 0.06060606060606061 | 0.22105555555555556 | 0.23674085674357573 | 0.09675502777777777 | 0.08305908842647206 |
| T-180 | 33 | 0.030303030303030304 | 0.1179375 | 0.17968942847924443 | 0.029944531250000003 | 0.0512508289320199 |
| T-120 | 30 | 0.03333333333333333 | 0.12435 | 0.14690095654093538 | 0.04127672499999999 | 0.05299161092830997 |
| T-60 | 29 | 0.0 | 0.057888888888888886 | 0.06379222669331802 | 0.01631122222222222 | 0.014962866551001708 |
| T-30 | 31 | 0.03225806451612903 | 0.060272727272727276 | 0.04864170073776394 | 0.03552754545454546 | 0.009091674839190413 |

Market versus analytic calibration is MIXED by checkpoint: analytic is lower
Brier at T-300, T-60 and T-30, while market is lower Brier at T-180 and T-120.
This is an early signal only; it is not a cross-regime result.

## Weekend sigma flip-rate curve

| Required move | N | Flips | Observed rate | Market flip p | Analytic flip p |
|---|---:|---:|---:|---:|---:|
| <0.5sigma | 31 | 4 | 0.12903225806451613 | 0.28035714285714286 | 0.3832111080693937 |
| 0.5-1sigma | 33 | 1 | 0.030303030303030304 | 0.117 | 0.20913613642771742 |
| 1-2sigma | 32 | 0 | 0.0 | 0.038722222222222234 | 0.042796401033762276 |
| 2-3sigma | 9 | 0 | 0.0 | 0.01 | 0.0022606712263888006 |
| >3sigma | 32 | 0 | 0.0 | 0.005090909090909092 | 1.1365681804164501e-06 |

Within this weekend-only sample the observed rate decreases monotonically from
the <0.5sigma bin through >3sigma. This is an EARLY_SIGNAL (five observed
checkpoint flips), not proof of a stable curve.

## Actual flip events

There are 2
weekend markets whose T-300 official-TWAP leader differed from final settlement.
Earliest persistent directional labels: {'BTC_5S': 1, 'BOT_SIDE': 1}. Event timestamps
and every candidate signal are in actual_flip_events.csv. There is no directly
comparable weekday flip-event cohort.

## Preliminary weekend vs weekday verdict

E. Data quality / comparable weekday coverage is still the main limitation.
The strongest early difference and strongest similarity are both NOT_MEASURABLE:
there is no synchronized weekday cohort with this schema. The most useful
metric to keep collecting is fresh T-300/T-180/T-120 flip probability,
separately by regime. A same-schema weekday capture cohort would change this
preliminary conclusion.

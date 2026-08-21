# P5 Behavior Audit

This document is the evidence boundary for Phase 5, exit and risk-policy
convergence. It does not change entry gates, take-profit placement, recovery
eligibility, order TIF, or position sizing.

## Scope

P5 covers only these existing protective-exit paths:

- normal fixed take-profit SELL;
- confirmed-invalidation recovery ladder;
- offside-near-close exit; and
- the market-FOK fallback used by the existing offside path.

It explicitly excludes ordinary BUY requoting, quote-stream watchdog recovery,
wallet/allowance handling, and market discovery. Those remain separate
lifecycle or operational work.

## Audit Contract

For every confirmed locked-side invalidation with adverse fresh TWAP evidence,
the journal must make the following chain reconstructable:

1. `EXIT_AUDIT`: quantity, sellable quantity, bid/ask, remaining time, entry
   cost basis, TWAP evidence, recovery ratio, and final eligibility block.
2. `EXIT_POLICY_DECISION`: the policy branch selected for that position.
3. `EXIT_AUDIT_OUTCOME`: each replacement/cancellation/submission and its
   terminal venue result: filled, rejected, cancelled, or expired.
4. The corresponding order event and fill/reject record, where one exists.

An audit is incomplete when a submitted protective order has no terminal
outcome. Incomplete audits are counted separately and must not be used to tune
recovery price, TIF, passive TTL, or recovery-ratio thresholds.

## Current Evidence

The current implementation has regression coverage for:

- releasing a normal take-profit SELL before recovery takes its token
  reservation;
- passive recovery submission and its TTL handoff;
- price-bounded limit FAK escalation, which permits partial fills;
- recovery SELL reservation ownership; and
- filled, rejected, and cancelled terminal audit records.

Historic journal evidence previously identified four filled and three rejected
`invalidation_recovery` exits, with passive submissions and existing-SELL
handoffs also observed. That establishes that the ladder is executable; it is
not enough evidence to optimize its thresholds.

## Regression Set

Before a Phase 5 behavior change, the following cases must stay covered:

| Case | Required result |
| --- | --- |
| Invalidation not confirmed | Hold/redeem path remains unchanged. |
| TWAP not adverse or stale | No recovery exit is submitted. |
| Minimum hold not elapsed | Audit records the hold block; no exit. |
| Normal TP reserves tokens | TP cancellation is requested before recovery evaluates sellable quantity. |
| Passive stage still within TTL | No duplicate recovery order is submitted. |
| Passive TTL expired | Passive order is cancelled, then a bounded limit FAK is submitted. |
| Venue rejects the exit | A rejected terminal outcome is journaled with requested TIF and venue reason. |
| Exit partially or fully fills | Filled terminal outcome includes price, quantity, and effective fee. |

## Decision Gate For P5 Tuning

Do not change recovery eligibility or execution thresholds solely because a
small set of loss cases looks expensive. A threshold change requires at least
20 confirmed-invalidation recovery submissions with complete terminal outcomes
in the same deployment regime. The comparison must report, per candidate
policy:

- fill/reject/expiry rate;
- recovered gross value and fee-adjusted value;
- time from confirmation to first order submission;
- remaining loss versus settlement; and
- the count excluded by each safety gate.

Until then, P5 work is limited to audit completeness, regression coverage, and
fixes that preserve the documented existing policy.

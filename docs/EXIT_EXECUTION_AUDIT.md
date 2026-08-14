# Exit Execution Audit

P0--P3 add evidence and verification only. P4 changes only the execution
mechanism for an already-approved `invalidation_recovery` exit. It does not
change entry gates, the normal `0.97` take-profit, recovery thresholds, or
directional logic.

## What is recorded

When a locked-side invalidation is evaluated, `EXIT_AUDIT` records the quote,
sellable quantity, entry basis, remaining time, TWAP confirmation, recovery
ratio, and the exact policy block reason. `EXIT_AUDIT_OUTCOME` records whether
the protective order was submitted, filled, rejected, or cancelled. This makes
an order terminal-state gap observable after a node restart or venue timeout.

Use these reports after a run:

```bash
./.venv/bin/python scripts/pnl_attribution_report.py --hours 24
./.venv/bin/python scripts/invalidation_counterfactual_report.py --hours 24
./.venv/bin/python scripts/verify_exit_order_semantics.py
```

The counterfactual report is explicitly gross and excludes fees and fill
probability. It only uses a bid journaled at a confirmed invalidation. Older
runs without `EXIT_AUDIT` are labeled `submitted_taker_exit_fallback`; they do
not establish that an earlier exit was executable.

## Verified adapter behavior

The generic adapter conversion maps a **limit** IOC order to Polymarket `FAK`,
which can partially fill. However, the installed Nautilus
`PolymarketExecutionClient` market-order path constructs and posts `FOK`
explicitly. A strategy-created market order is therefore all-or-kill.

## P4: recovery execution ladder

An `invalidation_recovery` still requires every pre-existing safety gate:
confirmed locked-side invalidation, adverse fresh TWAP confirmation, minimum
hold, minimum bid, and recovery ratio. Once approved:

1. With at least `RECOVERY_EXIT_PASSIVE_MIN_TIME_LEFT_SEC` remaining, the bot
   submits a GTC SELL limit at the current ask and waits
   `RECOVERY_EXIT_PASSIVE_TTL_SEC` seconds.
2. If it remains unfilled, it cancels that passive order and submits a SELL
   limit with IOC. The adapter maps this to Polymarket `FAK`, so it is a
   price-bounded active exit that may partially fill.
3. In the tail, it skips the passive wait and uses the same price-bounded FAK
   limit immediately. The existing `offside_near_close` market-FOK fallback is
   intentionally unchanged in this phase.

The versioned profile enables the ladder with a 15-second passive TTL and a
120-second minimum remaining-time threshold. Restart the bot after deploying
this version for the new configuration to take effect.

# Bot Engineering Principles

This document records implementation invariants for the current codebase.
Operational settings and commands belong in [readme_ZH.md](readme_ZH.md).

## Safety Invariants

- A new order cannot use invalid, stale, or conflicting reference data when
  the configured hard-safety layer rejects it.
- A market's projected combined binary exposure cannot exceed
  `MARKET_MAX_POSITION_SHARES`, including active pending buys on both outcomes.
- Cancel acknowledgement/reconciliation precedes any replacement that could
  increase exposure.
- A sell is limited to confirmed sellable conditional tokens.
- Recovery paths reduce risk only; they must not create a new opposite-side
  position.

## Decision Invariants

- Entry evaluation is ordered: hard safety, direction, model consistency,
  economics, execution.
- The first rejected layer owns the final journal reason.
- The first entry threshold is never lower than the regular threshold.
- All live entries use one economics calculation, `robust_net`; special trend
  discounts do not create an untracked second economics rule.

## Runtime Invariants

- `run_bot.py` is the production entrypoint.
- Dry run and live share the strategy and simulated/live order lifecycle;
  wallet submission is the meaningful mode boundary.
- `logs/trade_journal.db` is the evidence source for replay and investigation.
- Runtime configuration is profile first, local `.env` second, then shell/CI.
- Secrets remain in ignored local configuration and never in a versioned
  profile, report, fixture, or log output.

## Change Invariants

- Make one causal strategy change per experiment.
- Add or update a regression test for changed safety, inventory, or decision
  behavior.
- Run `./.venv/bin/python -m pytest -q` and compare the same replay interval
  before observing dry-run or restricted live behavior.

# Journal Replay

Run the dry-run executable replay before changing entry parameters:

```bash
./.venv/bin/python scripts/replay_journal_signals.py --hours 168 --selection first --shares 6
```

The default `dry-run-fill` mode selects `SHADOW_SIM_ENTRY_FILLED` records only.
Those records have passed the live entry and submission gates, used the live
sizing decision, and then received a simulated passive maker fill. It reports
settlement-only PnL and win rate using each recorded fill quantity. Outcomes
come from the corresponding `SHADOW_SIM_SETTLED` record, not from the live
inventory `MARKET_SETTLEMENT` event. The report shows `unscored` separately
when a fill has not yet received a shadow settlement. Fees and exit behavior
remain excluded.

For a broader signal diagnostic that intentionally ignores live gates and fill
probability, use:

```bash
./.venv/bin/python scripts/replay_journal_signals.py --mode signal-baseline --hours 168 --selection first --shares 6
```

Do not compare `signal-baseline` output to `dry-run-fill` output. Use the same
mode, window, and selection policy when comparing parameter changes.
Do not enable `MAKER_AUTO_TUNE` until this baseline has enough settled markets
to be meaningful.

## Fair-edge bucket shadow report

Keep the live fair-edge gate unchanged while measuring the six below-threshold
bands with the rest of the live submit-time path intact. Candidates remain
shadow-only and can fill only when a later ask reaches the passive limit:

```bash
./.venv/bin/python scripts/fair_edge_bucket_shadow_report.py --hours 168
```

Enable this independent telemetry with `FAIR_EDGE_BUCKET_SHADOW_ENABLED=1`.
It must not be interpreted as live or executable PnL until enough passive fills
and settlements accumulate.

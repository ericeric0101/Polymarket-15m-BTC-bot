# Journal Replay

Run a deterministic, offline baseline before changing entry parameters:

```bash
./.venv/bin/python scripts/replay_journal_signals.py --hours 168 --selection first --shares 6
```

The replay selects one recorded candidate per market and scores it against the
recorded `MARKET_SETTLEMENT` outcome. It reports settlement-only PnL and win
rate. It deliberately excludes fill probability, order-book impact, fees, and
exit behavior, so it is a baseline comparison tool rather than a claim of
live executable performance.

Use the same window and selection policy when comparing parameter changes.
Do not enable `MAKER_AUTO_TUNE` until this baseline has enough settled markets
to be meaningful.

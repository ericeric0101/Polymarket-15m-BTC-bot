# Feedback Legacy Status

`feedback/learning_engine.py` is not part of the current BTC 15-minute live maker path.

It depends on older `core.strategy_brain` fusion components and should be treated as
legacy / sidecar research code until it is explicitly reconnected to the live bot.

Implications:

- do not use this module as evidence that the live bot is learning online
- do not patch this module as part of trading hotfixes
- if learning / adaptive weighting is needed again, design it against the current
  `run_bot.py` + `bot/` architecture rather than assuming the old fusion stack can
  be dropped back in safely

See:

- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/LEGACY_PATCH_STATUS.md`

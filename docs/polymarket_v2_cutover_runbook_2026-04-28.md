# Polymarket V2 Cutover Runbook

Target cutover window: 2026-04-28 around 11:00 UTC.

This runbook assumes:

- V1 stays live until the Polymarket maintenance window starts
- V2 is prepared in the separate worktree at [repo root](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main)
- V1 remains in [restart-9a6c280-visible](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/restart-9a6c280-visible)

## Working Directories

### V1

- Directory: [restart-9a6c280-visible](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/restart-9a6c280-visible)
- Branch: `restart-from-9a6c280`
- Python: [/.worktrees/restart-9a6c280/.venv/bin/python](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/.worktrees/restart-9a6c280/.venv/bin/python)

### V2

- Directory: [repo root](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main)
- Branch: `feat/polymarket-v2-migration`
- Python: [/.venv/bin/python](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/.venv/bin/python)

## Standard Start Commands

### V1 live

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/restart-9a6c280-visible
./.venv/bin/python run_bot.py --live
```

### V2 live

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main
./.venv/bin/python run_bot.py --live
```

### V2 probe only

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main
./.venv/bin/python scripts/pure_signal_probe.py --paper-trade --verbose
```

## Before Cutover Day

Complete all of these before 2026-04-28:

1. Confirm V2 wallet state
   - `pUSD` balance is present
   - V2 exchange allowances are non-zero
   - Polygon wallet has enough `MATIC` for gas

2. Confirm V2 `.env`
   - `POLYMARKET_CLOB_BASE_URL` points to the intended endpoint
   - `POLYMARKET_CHAIN_ID=137`
   - `POLYMARKET_PK`, API key, secret, passphrase are correct
   - `TAIL_PROTECT_*` values are present if you want the same behavior as V1
   - `POLY_BUILDER_CODE` is set only if you actually want builder attribution

3. Confirm one more V2 end-to-end test
   - place order
   - cancel order
   - get order status

4. Keep V1 and V2 runtime state separate
   - separate `.env`
   - separate `logs/`
   - separate tmux/screen session names
   - separate DB/Redis assumptions where applicable

## Cutover-Day Sequence

### Phase 1: Before maintenance starts

1. Keep V1 running normally until Polymarket maintenance begins.
2. Do not start V2 live trading early against production unless you explicitly intend to cut over.
3. Record the active V1 process/session name so you stop the correct process.

### Phase 2: Maintenance window begins

1. Stop the V1 bot cleanly.
2. Do not assume any pre-cutover open orders still exist afterward.
3. Wait until Polymarket maintenance is clearly complete.

## After Polymarket declares V2 live

1. Re-check wallet readiness

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main
./.venv/bin/python scripts/check_allowance.py --check-only
```

2. Verify the V2 bot environment one final time
   - correct `.env`
   - correct branch
   - correct Python executable

3. Start V2 live

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main
./.venv/bin/python run_bot.py --live
```

4. Watch the first startup cycle closely
   - market discovery
   - Polymarket client auth
   - instrument load
   - initial order placement behavior
   - allowance / balance errors

5. Assume order book wipe has happened
   - any orders needed after maintenance must be placed again by the V2 bot

## Immediate Rollback Rule

If V2 production startup shows any of these, stop and do not force it:

- repeated auth failures
- repeated `not enough balance / allowance`
- obvious wrong wallet / wrong environment
- order placement succeeds in sandbox but fails in production for configuration reasons

At that point:

1. stop the V2 bot
2. inspect `.env`
3. inspect wallet balances and allowances
4. only resume once the problem is concretely identified

## Fast Verification Commands

### Confirm current branch

```bash
git branch --show-current
```

### Confirm Python path

```bash
python -c "import sys; print(sys.executable)"
```

### Confirm V2 allowance state

```bash
./.venv/bin/python scripts/check_allowance.py --check-only
```

## Operational Notes

- `restart-from-9a6c280` remains your V1 safety lane until cutover.
- `feat/polymarket-v2-migration` is already pushed and can be reviewed independently on GitHub.
- Do not switch branches inside a single running bot directory. Use the two directories exactly as they are now.

# Polymarket V2 Remaining Work

This note tracks the remaining V1-era assumptions or incomplete V2 migration items in the repository as of 2026-04-19.

## Summary

The V2 migration is already functional for the core order lifecycle:

- V2 SDK is installed and used in the live Polymarket client path
- V2 sandbox order placement, cancel, and order-status checks have succeeded
- `USDC.e -> pUSD` approval / wrap flow has been validated

What remains is mostly cleanup, operational hardening, and non-blocking side-system alignment.

## Remaining Items

### 1. Fee-rate side systems still use V1-style assumptions

These files still carry explicit `fee_rate_bps` or `/fee-rate` logic:

- [execution/fee_rate_client.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/execution/fee_rate_client.py:1)
- [execution/sim_adapter.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/execution/sim_adapter.py:44)
- [bot/settings.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/bot/settings.py:144)
- [bot/fill_ledger.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/bot/fill_ledger.py:251)
- [bot/db_runtime.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/bot/db_runtime.py:68)
- [monitoring/trade_journal_db.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/monitoring/trade_journal_db.py:66)

Impact:

- This does not block V2 SDK order placement.
- It does mean the bot still carries its own fee observation / simulation / reporting model rather than being fully re-grounded on V2 protocol semantics.

Recommendation:

- Keep this for now if the goal is operational continuity.
- Before full production confidence, review whether the fee model is still only used for diagnostics or whether it affects live quoting or risk logic.

### 2. Market-order V2 fee-aware input is not implemented

V2 supports optional `userUSDCBalance` when creating market buy orders to improve fee-adjusted fill sizing.

Current state:

- The live order path in [execution/polymarket_client.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/execution/polymarket_client.py:316) uses `OrderArgs` and limit-style order creation.
- No active repo path was found using a V2 market-order helper with `userUSDCBalance`.

Impact:

- No immediate issue if the strategy stays on maker / limit-driven flow.
- This remains a gap if market-buy paths are added later.

### 3. Some docs and messages still reference `USDC.e`

Examples:

- [docs/readme_ZH.md](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/docs/readme_ZH.md:77)
- [docs/readme_ZH.md](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/docs/readme_ZH.md:299)
- [bot/merge_ops.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/bot/merge_ops.py:82)
- [scripts/check_allowance.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/check_allowance.py:105)

Impact:

- In code paths like merge/redeem and onramp, `USDC.e` still exists as the intermediate underlying asset before wrapping to `pUSD`, so not every `USDC.e` mention is wrong.
- The remaining documentation should be reviewed so it is clear when `USDC.e` is legacy collateral and when it is still the underlying used by the adapter/onramp flow.

### 4. Builder code is wired, but only if you actually use it

Builder support is now exposed via:

- [.env.example](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/.env.example:14)
- [execution/polymarket_client.py](/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/execution/polymarket_client.py:317)

Impact:

- If `POLY_BUILDER_CODE` is empty or zero, this is fine for a non-builder deployment.
- If builder attribution matters, set the real builder code in V2 `.env`.

### 5. Order-book wipe handling is operational, not automated

Known V2 cutover behavior:

- All open orders will be cleared during the migration window.

Current state:

- No dedicated repo automation was found for cutover-time bulk re-placement.

Impact:

- The strategy must be restarted cleanly after cutover.
- Any live open-order assumptions from pre-cutover should be treated as invalid.

## Checklist Status

### Completed enough to run V2

- V2 SDK package installation
- V2 SDK imports in live client paths
- V2 order creation fields in the active client path
- V2 exchange / neg-risk / adapter allowances
- `USDC.e -> pUSD` wrap path
- V2 sandbox order lifecycle test

### Partially complete

- Fee-model cleanup
- Documentation cleanup
- Market-order fee-aware sizing support
- Cutover-time re-entry runbook automation

## Recommended Priority

1. Keep V1 live until 2026-04-28.
2. Continue V2 sandbox/test-market validation with the current V2 worktree.
3. Before cutover day, do one more focused pass on:
   - live fee/risk assumptions
   - post-cutover startup and order re-placement
   - operator `.env` correctness

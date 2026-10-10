# Live configuration audit — Part 2 result + Part 3 post-fix gate

Generated 2026-10-09 19:18 +0800. The bot was never started; no network was used;
nothing was pushed. Tags: VERIFIED / INFERRED / UNVERIFIED.
Part 1 baseline: `live_config_audit_20261009_171726_+0800.md`.

## 1. Commit and scope

- HEAD_BEFORE `95f0860` → HEAD_AFTER **`7640a44`** `fix(execution): size entries by sellable shares` (local only). VERIFIED
- Changed files (`git diff --stat 95f0860 7640a44`):
  - `.env.example`, `bot/app_config.py`, `bot/launcher.py`, `bot/order_submission.py`,
    `bot/pricing_runtime.py` (docstring only), `bot/quote_runtime.py`, `bot/quote_service.py`,
    `bot/settings.py`, `config/profiles/btc15_twap_v3.env`, `project_overview.md`, `run_bot.py`,
    `tests/test_live_path_regressions.py`;
  - new: `bot/entry_sizing.py`, `tests/test_share_entry_sizing.py`.
- Excluded areas — **no functional change** (VERIFIED by diff review):
  - stop-loss logic (`exit_engine.py`, `taker_exit.py` untouched);
  - entry score / side decision;
  - candidate policy (`bot/research/candidate_policy.py` untouched);
  - maker/L2 (`cap_buy_quantity`, MakerEngine untouched);
  - session/daily guard (untouched; its R still = `DEPTH_RISK_MAX_LOSS_USDC` 10);
  - partial-fill handling (untouched; documented by test);
  - execution-safety chokepoint (untouched).
- One behaviour change outside pure sizing, required by the balance item: the cycle-level
  `MAKER_QUOTE_SIZE_USDC × 1.1` gate, which forced SELL-only, was removed. It is replaced by the
  per-BUY submit check. SELLs and protective exits never consult the balance (regression tests below).
- `MARKET_MAX_POSITION_SHARES` default 25 → 10 (VERIFIED by isolated resolution).
  - Readers: locked-side full gate (`quote_service.py:645`), depth headroom (`run_bot.py:3546`),
    submit inventory cap (`order_submission.py:380`), inventory-overage sell-only (`quote_runtime.py:27`),
    MakerEngine inventory skew (`maker_engine.py:334-337`), status log.
  - The effective value with the files on disk was already 10 (`.env`) and is now 10 in the profile too.
    So **no runtime path changes**.
  - The code default matters only if both `.env` and the profile lack the key. Then 10 replaces 25,
    which narrows the inventory cap and makes the MakerEngine skew ratio steeper. That is a fallback-only effect.

## 2. Final rule — `SIZING_RULE_VERSION = share_v1_2026-10-09`

`bot/entry_sizing.py`, one function `size_entry` / `target_entry_shares`, the same for LIVE and DRY-RUN:

| Item | Value |
|---|---|
| Boundary | tick-normalized price `<= 0.70` → 10.0 shares; `> 0.70` → 5.5 shares (strict, as in 8de45cb) |
| Normalization | nearest 0.01 tick before comparing; `0.7000000000000001`, `0.6999999999999999`, `"0.70"` → 10 |
| Final quantity | `min(target, existing L2/risk/inventory cap)`; cap already carries the multipliers; 2-dp round-down |
| `MIN_ENTRY_SHARES` | 5.5 → skip below it, never round up; skip consumes no BUY count, no cooldown, no traded marker; one log per market per reason |
| Submit boundary | re-checks the final quantity (< 5.5 → skip; > target for the final limit price → skip) |
| Balance | LIVE BUY needs `qty × limit × 1.1`, else skip; unknown balance → skip; never shrinks |
| Config bounds | targets ∈ [5.5, 10], high target ≤ 5.5, threshold = 0.70 on final resolved values → else LIVE refuses (launcher, again in strategy settings), DRY-RUN warns and uses the canonical rule |

Tightening beyond the literal bounds: a shell export `HIGH_PRICE_TARGET_SHARES=10` is inside
[5.5, 10], but it would raise the >0.70 notional to $9.90. LIVE therefore also refuses a high target
above 5.5. A smaller in-bounds override is allowed and logged as a WARNING, never silently. VERIFIED (tests).

## 3. Effective sizing table (VERIFIED: isolated post-fix copy, real functions, deep book)

Default config (files on disk; `.env`-missing and `.env`+profile-missing give identical rows):

| Price | Normalized | Base target | Risk/depth cap (m=1) | Multiplier | Final | Notional | Balance need (×1.1) | Result |
|---|---|---|---|---|---|---|---|---|
| 0.30 | 0.30 | 10.0 | 10 (inventory) | 1 | 10.00 | $3.00 | $3.30 | BUY |
| 0.50 | 0.50 | 10.0 | 10 (inventory) | 1 | 10.00 | $5.00 | $5.50 | BUY |
| 0.70 | 0.70 | 10.0 | 10 (inventory) | 1 | 10.00 | $7.00 | $7.70 | BUY |
| 0.71 | 0.71 | 5.5 | 10 (inventory) | 1 | 5.50 | $3.905 | $4.2955 | BUY |
| 0.80 | 0.80 | 5.5 | 10 | 1 | 5.50 | $4.40 | $4.84 | BUY |
| 0.90 | 0.90 | 5.5 | 10 | 1 | 5.50 | $4.95 | $5.445 | BUY |
| 0.95 | 0.95 | 5.5 | 10 | 1 | 5.50 | $5.225 | $5.7475 | BUY |
| 0.99 | 0.99 | 5.5 | 10 | 1 | 5.50 | $5.445 | $5.9895 | BUY |

With existing multipliers (cap = $10 × m / price):
- m = 0.55: 0.70 → 7.85; > 0.70 → 5.50 everywhere.
- m = 0.5: 0.70 → 7.14; 0.71–0.90 → 5.50; **0.95 → 5.26 SKIP, 0.99 → 5.05 SKIP**.

Invalid overrides (simulated shell exports):

| Override | Violation | LIVE | DRY-RUN | Rule used |
|---|---|---|---|---|
| `MARKET_TARGET_SHARES=25` | low target outside [5.5, 10] | **REFUSE** | warn, canonical | 10 / 5.5 |
| `HIGH_PRICE_TARGET_SHARES=4` | high target outside bounds | **REFUSE** | warn, canonical | 10 / 5.5 |
| `HIGH_PRICE_THRESHOLD=0.80` | threshold ≠ 0.70 | **REFUSE** | warn, canonical | 10 / 5.5 |
| `MARKET_TARGET_SHARES=` (empty) | not a number | **REFUSE** | warn, canonical | 10 / 5.5 |
| `HIGH_PRICE_TARGET_SHARES=10` | exceeds canonical 5.5 | **REFUSE** | warn, canonical | 10 / 5.5 |
| `MARKET_TARGET_SHARES=8` (in bounds, smaller) | none | allowed + WARNING | allowed + WARNING | 8 / 5.5 (≤0.70 → 8.00) |

In every invalid case the effective table equals the default table. Larger-than-allowed sizing is never produced.

## 4. Exposure and day-loss (arithmetic; not guaranteed bounds)

- **Max configured exposure per trade: $7.00** (10 × 0.70). Above 0.70 it is ≤ $5.445. VERIFIED arithmetic.
  It was the same $7.00 before the fix; above 0.70 it was $5.50.
- **Per market: $7.00.** One BUY event per slug; inventory cap 10. VERIFIED (code + tests).
- **Expected max day realized loss ≈ $22.00** = session V2 lock at −$15.00 realized
  (thresholds unchanged, VERIFIED Part 1) + one more full $7.00 loss. This is **not** guaranteed. Assumptions:
  1. Each loss is realized at settlement before the next market's first BUY (≥ 5 min later). INFERRED
  2. Session guard state persists and restores across restarts (tested). VERIFIED
  3. No overlapping positions across markets. INFERRED
  4. No venue overfill beyond 10 shares (`allow_overfills=True` can exceed it slightly). INFERRED
  5. No near-tie UNKNOWN label that defers realization (deferral would let more entries through). INFERRED
  6. Weekday-only BUY session; weekends observation only. VERIFIED
  7. The `$2 hard` breaker is **CONDITIONAL**: it needs a confirmed adverse trend, 60 s hold, and
     15 s + 2 votes or ≤ 120 s left. Up to ≈ $5 per entry beyond nominal (full notional) is possible.
  8. Protective exits **may be unavailable** during kill switch, error pause, Telegram pause, or
     quote/feed outage (P1-A). Every open position can then reach full notional.
  9. A sub-5-share partial fill cannot be exited; it is held to settlement (accepted residual risk).

## 5. Historical skip impact (read-only journal reconstruction)

Method: new final = `round_down(min(target(price), old submitted qty))`. The old submitted qty
equals the old depth/risk cap (pre-fix composition). Source: `logs/trade_journal.db` opened `mode=ro`.

| Cohort | N | Would SKIP | By price bucket | Cause |
|---|---|---|---|---|
| LIVE maker BUY submits 2026-09-22..09-30 | 241 (83 filled) | **2** (1 filled) | 0.51–0.70: 1 (submitted at 0.70 after a retreat from >0.70); 0.81–0.90: 1 | L2 depth cap: qty 5.0 and 5.239 are far below $10 × 0.55 / price (INFERRED; limiting factor not journaled) |
| DRY-RUN submits 2026-10-01..10-09 | 982 | **4** | 0.71–0.80: 2; 0.81–0.90: 2 | L2 depth cap INFERRED (qty below every possible risk cap with m ≥ 0.5); multiplier fields not journaled |

- Entry-quality multiplier: 0 skips. Confirmation multiplier: 0 skips. VERIFIED from LIVE payload fields.
- Size reduced but still BUY (5.5 instead of 5.5/price above 0.70): LIVE 151, DRY-RUN 773.
- Before 2026-09-22 there are no comparable maker submit rows. Fast-follow (65) is retired and excluded.
  Exact cause attribution for DRY-RUN is **UNVERIFIED**.

## 6. Tests

- Targeted (new + affected): 36 new (`tests/test_share_entry_sizing.py`); 334 affected-suite run earlier. All pass. VERIFIED
- Full suite (PATH includes `/usr/sbin`): **1350 passed**, 2 known warnings. `git diff --check` PASS. VERIFIED
- Pre-fix comparison (`git archive 95f0860`):
  - the new test module cannot import there (`ModuleNotFoundError: bot.entry_sizing`);
  - the pre-fix production composition gives 0.71 → 7.7465, 0.80 → 6.875, 0.90 → 6.111, 0.99 → 5.556
    shares, i.e. **5.5/price, not 5.5 shares**: FAIL;
  - post-fix gives 5.50 at each of those prices. VERIFIED
- New regression highlights:
  - zero balance still submits a SELL of an existing position and still dispatches the absolute breaker;
  - LIVE and DRY-RUN submit identical quantities;
  - `main --live` returns before patches, preflight, prompt and lock on an invalid override;
  - SDK 2-dp normalization preserves 5.5 and 10;
  - a full 5.5-share position stays ≥ 5 after the haircut and round-down (5.49 / 5.45).

## 7. Graph diff of the sizing path (codebase-memory, refreshed after the commit)

- **Before:** `_evaluate_quote_targets` → `apply_high_entry_price_size_adjustment` (×0.55) →
  `_compute_maker_order_qty` → `cap_buy_quantity` → override → `submit_maker_quote` → chokepoint.
- **After:** `_evaluate_quote_targets` → `apply_high_entry_price_size_adjustment` → `target_entry_shares`;
  `cap_buy_quantity` → `apply_share_entry_sizing` → `size_entry` → `target_entry_shares`;
  `submit_maker_quote` → `target_entry_shares` / `round_down_to_venue_size` / balance → `ExecutionSafetyMixin.submit_order`.
- `_compute_maker_order_qty` now has a single caller (SELL default, `order_submission.py:201`, grep).
- Real submit sites unchanged: `order_submission.py:659`, `taker_exit.py:1074,1283,1634`. VERIFIED (grep)

## 8. Live-readiness gate

| Gate | Result | Basis |
|---|---|---|
| RUNTIME_SAFE_FOR_TINY_LIVE | **CONDITIONAL** | Missing: (1) P1-A exits unavailable under kill switch/pause/feed outage; (2) P1-B $2 breaker conditional; (3) startup orphan cancel UNVERIFIED in LIVE; (4) HEAD `7640a44` not runtime-validated (nothing since `bbbdc09`); (5) storage WARNING (13.24 GiB free, critical 10 GiB). Met: chokepoint, hard loss independent of STOP_LOSS, duplicate-entry guard, kill switch exists, per-entry/market/day bounds computed, sizing + sellability safe for full fills, settlement authority |
| RESEARCH_SUPPORTED_FOR_TINY_LIVE | **NO** | No positive net edge after realistic fills; day-blocked CIs include 0 (`project_overview.md` §11); DRY-RUN fills are optimistic |
| TINY_LIVE_AS_MEASUREMENT_EXPERIMENT_ACCEPTABLE | **CONDITIONAL** | Only if all of the following hold (my required conditions; the decision is yours) — see below |
| FULL_LIVE_READY | **NO** | No multi-week, multi-regime out-of-sample evidence; no post-fix real-fill evidence |

Conditions for a measurement-only tiny LIVE run:
- one short post-fix DRY-RUN smoke first (≤ 60 min, after re-checking free space ≥ 12 GiB);
- weekday only, ≤ 2 h, operator present the whole time;
- no Telegram pause while holding, since P1-A disables exits;
- share_v1 sizing only (≤ $7.00 per entry, 1 entry per market);
- operator loss ceiling ≤ $10 realized, or 3 losing markets, whichever comes first (tighter than the
  −$15 guard), then stop manually;
- stop immediately on any kill switch, feed outage > 30 s, orphan order at startup, unexpected
  balance skip, or sub-5-share residual;
- record every fill for real-fill markout analysis.

## 9. Open items (not fixed here)

- P1: hard-loss breakers not evaluated during kill switch, error pause, Telegram pause, quote/feed outage.
- P1: `$2 hard` breaker only after a confirmed adverse trend; not a guaranteed cap (≈ $5 extra possible).
- Client order id not idempotent (equivalent duplicate-entry guard exists).
- Startup orphan-order cancel: UNVERIFIED in LIVE.
- DRY-RUN skips exits/breakers; proves nothing about protective exits.
- Disk WARNING (≈ 13 GiB free, critical ≈ 10 GiB); avoid back-to-back DRY-RUNs for ≈ 24 h.
- HEAD not runtime-validated since `bbbdc09`.
- Accepted residual risk: sub-5-share partial fill is unsellable and held to settlement.

```text
AUDIT_COMPLETENESS=FULL
HEAD_BEFORE=95f0860 HEAD_AFTER=7640a44 WORKING_TREE_DIRTY=NO(tracked) BOT_RUNNING=NO SECRETS_PRINTED=NO CODE_CHANGED=sizing+config-safety+balance-check
NEW_SIZE_MODE=share_target SIZING_RULE_VERSION=share_v1_2026-10-09 LOW_PRICE_THRESHOLD=0.70(strict >) LOW_PRICE_SHARES=10.0 HIGH_PRICE_SHARES=5.5 MIN_ENTRY_SHARES=5.5 MIN_SELL_SHARES=5
TARGET_5_5_SELLABILITY_SAFE=YES_FOR_FULL_FILLS(partial<5 residual accepted) MAX_NOTIONAL_CONFLICT=NONE LIVE_DRY_PARITY=VERIFIED
WORST_CASE_LOSS_PER_ENTRY=7.00 WORST_CASE_LOSS_PER_MARKET=7.00 EXPECTED_MAX_DAY_LOSS=~22.00(assumption-bound, not guaranteed)
HISTORICAL_SKIP=LIVE 2/241, DRY 4/982 (L2 depth cap, inferred)
TARGETED_TESTS=36 new pass FULL_TESTS=1350 passed GIT_DIFF_CHECK=PASS COMMIT=7640a44 PUSHED=NO BOT_RESTARTED=NO NETWORK=NO
RUNTIME_SAFE_FOR_TINY_LIVE=CONDITIONAL RESEARCH_SUPPORTED_FOR_TINY_LIVE=NO TINY_LIVE_AS_MEASUREMENT_EXPERIMENT_ACCEPTABLE=CONDITIONAL FULL_LIVE_READY=NO
```

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import random
import subprocess
import sys
import time
import threading
from typing import Any, Callable


def log_strategy_run_start(
    trade_db: Any,
    run_id: str,
    is_dry_run_mode: bool,
    test_mode: bool,
    maker_mode: bool,
    instrument_id: Any,
    selected_slug: str | None,
    maker_quote_sides: str,
    maker_quote_size_usdc: Any,
) -> None:
    if not trade_db:
        return
    trade_db.log_run_start(
        run_id=run_id,
        mode="TEST_DRY_RUN" if is_dry_run_mode else "LIVE",
        test_mode=test_mode,
        maker_mode=maker_mode,
        instrument_id=str(instrument_id) if instrument_id else None,
        selected_slug=selected_slug,
        notes={
            "quote_sides": maker_quote_sides,
            "quote_size_usdc": float(maker_quote_size_usdc),
        },
    )


def log_strategy_run_stop(
    trade_db: Any,
    run_id: str,
    is_dry_run_mode: bool,
    test_mode: bool,
    maker_mode: bool,
    instrument_id: Any,
    selected_slug: str | None,
    final_inventory_shares: Any,
    market_cycle_realized_net_usdc: Any,
) -> None:
    if not trade_db:
        return
    trade_db.log_run_stop(
        run_id=run_id,
        notes={
            "mode": "TEST_DRY_RUN" if is_dry_run_mode else "LIVE",
            "test_mode": bool(test_mode),
            "maker_mode": bool(maker_mode),
            "instrument_id": str(instrument_id) if instrument_id else None,
            "selected_slug": selected_slug,
            "final_inventory_shares": float(final_inventory_shares),
            "market_cycle_realized_net_usdc": float(market_cycle_realized_net_usdc),
        },
    )


def handle_waiting_phase_search(
    search_next_market_fn: Callable[[], bool],
    update_market_phase_fn: Callable[[], Any],
    schedule_auto_redeem_fn: Callable[[str], None] | None,
    next_market_slug: str | None,
    market_next_poll_sec: float,
    waiting_miss_count: int,
    max_waiting_misses: int,
    lifecycle_wait_fn: Callable[[float], None],
    logger_info_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
    request_rollover_fn: Callable[[], None],
) -> int:
    found = search_next_market_fn()
    if found:
        logger_info_fn("Lifecycle timer: new market found, transitioning to ACTIVE")
        update_market_phase_fn()
        return 0

    waiting_miss_count += 1
    if waiting_miss_count >= max_waiting_misses and next_market_slug:
        logger_warning_fn(
            f"Lifecycle timer: {waiting_miss_count} consecutive misses for "
            f"{next_market_slug}. Instruments stale — requesting node rollover."
        )
        request_rollover_fn()
        return 0

    logger_info_fn(
        f"Lifecycle timer: no market yet (miss {waiting_miss_count}/{max_waiting_misses}), "
        f"retry in {market_next_poll_sec}s"
    )
    lifecycle_wait_fn(market_next_poll_sec)
    return waiting_miss_count


def extend_synthetic_history(
    price_history: list[Decimal],
    target_count: int,
    existing_count: int,
) -> int:
    base_price = price_history[-1] if price_history else Decimal("0.5")
    needed = target_count - existing_count
    if needed <= 0:
        return 0
    for _ in range(needed):
        change = Decimal(str(random.uniform(-0.03, 0.03)))
        new_price = base_price * (Decimal("1.0") + change)
        new_price = max(Decimal("0.01"), min(Decimal("0.99"), new_price))
        price_history.append(new_price)
        base_price = new_price
    return needed


def dedupe_price_history(price_history: list[Any]) -> list[Any]:
    seen: set[str] = set()
    unique_history: list[Any] = []
    for price in price_history:
        price_str = str(price)
        if price_str in seen:
            continue
        seen.add(price_str)
        unique_history.append(price)
    return unique_history


def should_run_quote_watchdog(
    now_ts: float,
    last_quote_watchdog_check_ts: float,
    quote_healthcheck_interval_sec: float,
    last_valid_quote_ts: float,
    quote_stale_sec: float,
    consecutive_invalid_quote_ticks: int,
    quote_invalid_tick_reload_threshold: int,
) -> tuple[bool, float]:
    if now_ts - last_quote_watchdog_check_ts < quote_healthcheck_interval_sec:
        return False, 0.0
    stale_for = (now_ts - last_valid_quote_ts) if last_valid_quote_ts > 0 else 0.0
    stale_hit = last_valid_quote_ts > 0 and stale_for >= quote_stale_sec
    invalid_hit = consecutive_invalid_quote_ticks >= quote_invalid_tick_reload_threshold
    return stale_hit or invalid_hit, stale_for


def handle_quote_watchdog_recovery(
    trigger: str,
    now_ts: float,
    last_quote_watchdog_reload_ts: float,
    quote_reload_cooldown_sec: float,
    instrument_id: Any,
    last_valid_quote_ts: float,
    consecutive_invalid_quote_ticks: int,
    db_strategy_event_fn: Callable[[str, dict[str, Any]], None],
    cancel_active_maker_orders_fn: Callable[[], None],
    find_btc_instrument_fn: Callable[[], bool],
    logger_warning_fn: Callable[[str], None],
    logger_error_fn: Callable[[str], None],
) -> tuple[bool, float, float | None, str | None]:
    if now_ts - last_quote_watchdog_reload_ts < quote_reload_cooldown_sec:
        return False, last_quote_watchdog_reload_ts, None, None
    prev_instrument = str(instrument_id) if instrument_id else None
    stale_for = (now_ts - last_valid_quote_ts) if last_valid_quote_ts > 0 else None
    logger_warning_fn(
        "Quote watchdog triggered: "
        f"trigger={trigger} stale_for={stale_for if stale_for is not None else -1:.1f}s "
        f"invalid_ticks={consecutive_invalid_quote_ticks}"
    )
    db_strategy_event_fn(
        "QUOTE_WATCHDOG_TRIGGERED",
        {
            "trigger": trigger,
            "stale_for_sec": stale_for,
            "invalid_ticks": consecutive_invalid_quote_ticks,
            "instrument_before": prev_instrument,
        },
    )
    cancel_active_maker_orders_fn()
    selected_ok = find_btc_instrument_fn()
    return selected_ok, now_ts, stale_for, prev_instrument


def should_skip_auto_redeem_run(
    now_ts: float,
    auto_redeem_min_gap_sec: float,
    last_redeem_run_ts: float,
) -> tuple[bool, float]:
    if auto_redeem_min_gap_sec <= 0 or last_redeem_run_ts <= 0:
        return False, 0.0
    elapsed_since_last = now_ts - last_redeem_run_ts
    return elapsed_since_last < auto_redeem_min_gap_sec, elapsed_since_last


def run_auto_redeem_script(
    repo_root: Path,
    reason: str,
    auto_redeem_slug_filter: str | None,
    auto_redeem_apply: bool,
    auto_redeem_timeout_sec: int,
    logger_info_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
    db_strategy_event_fn: Callable[[str, dict[str, Any]], None],
) -> None:
    script = repo_root / "scripts" / "check_positions_and_redeem.py"
    if not script.exists():
        logger_warning_fn(f"Auto redeem script not found: {script}")
        return

    cmd = [sys.executable, str(script)]
    if auto_redeem_slug_filter:
        cmd.extend(["--slug", auto_redeem_slug_filter])
    if auto_redeem_apply:
        cmd.append("--apply")

    try:
        started_ts = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=auto_redeem_timeout_sec,
            check=False,
        )
        elapsed = time.time() - started_ts
        stdout_full = (proc.stdout or "").strip()
        stdout_tail = "\n".join(stdout_full.splitlines()[-8:])
        stderr_tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:])
        logger_info_fn(
            f"Auto redeem run done: reason={reason} rc={proc.returncode} elapsed={elapsed:.1f}s "
            f"apply={'ON' if auto_redeem_apply else 'OFF'}"
        )
        if stdout_tail:
            logger_info_fn(f"Auto redeem output (tail):\n{stdout_tail}")
        if stderr_tail:
            logger_warning_fn(f"Auto redeem stderr (tail):\n{stderr_tail}")

        # Parse stdout for individual redeem results and total redeemable size.
        redeem_results: list[dict[str, Any]] = []
        condition_slug_by_id: dict[str, str] = {}
        condition_size_by_id: dict[str, float] = {}
        selected_total_size = 0.0
        redeemable_count_raw = 0
        for line in stdout_full.splitlines():
            line_s = line.strip()
            # Parse report lines from check_positions_and_redeem.py:
            # - condition=0x... redeemable=True ... slug=btc-updown-15m-... title=...
            if line_s.startswith("- condition="):
                parts: dict[str, str] = {}
                for token in line_s.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        parts[k.lstrip("-")] = v
                condition_id = parts.get("condition", "")
                slug = parts.get("slug", "")
                if condition_id and slug:
                    condition_slug_by_id[condition_id] = slug
                if condition_id and parts.get("redeemable") == "True" and "size" in parts:
                    try:
                        condition_size_by_id[condition_id] = float(parts["size"])
                    except ValueError:
                        pass
            # Parse: redeemPositions condition=0x... tx=0x... status=1
            if line_s.startswith("redeemPositions "):
                parts: dict[str, str] = {}
                for token in line_s.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        parts[k] = v
                condition_id = parts.get("condition", "")
                slug = condition_slug_by_id.get(condition_id, "")
                result = {
                    "condition_id": condition_id,
                    "tx_hash": parts.get("tx", ""),
                    "status": int(parts.get("status", "0")),
                }
                if condition_id in condition_size_by_id:
                    result["redeem_size_usdc"] = condition_size_by_id[condition_id]
                if slug:
                    result["slug"] = slug
                    result["market_slug"] = slug
                redeem_results.append(result)
                db_strategy_event_fn("REDEEM_EXECUTED", result)
            # Parse: Selected total redeemable size: 1.234567
            if "Selected total redeemable size:" in line_s:
                try:
                    selected_total_size = float(line_s.split(":")[-1].strip())
                except ValueError:
                    pass
            # Parse: redeemable=3
            if "redeemable=" in line_s and "positions_total=" in line_s:
                try:
                    for token in line_s.split():
                        if token.startswith("redeemable="):
                            redeemable_count_raw = int(token.split("=")[1])
                except (ValueError, IndexError):
                    pass

        db_strategy_event_fn(
            "AUTO_REDEEM_RUN",
            {
                "reason": reason,
                "return_code": proc.returncode,
                "elapsed_sec": elapsed,
                "apply": auto_redeem_apply,
                "redeems_executed": len(redeem_results),
                "redeems_succeeded": sum(1 for r in redeem_results if r.get("status") == 1),
                "redeemable_positions_found": redeemable_count_raw,
                "selected_total_size": selected_total_size,
            },
        )
    except subprocess.TimeoutExpired:
        logger_warning_fn(f"Auto redeem timeout after {auto_redeem_timeout_sec}s (reason={reason})")
        db_strategy_event_fn(
            "AUTO_REDEEM_RUN",
            {
                "reason": reason,
                "return_code": -1,
                "elapsed_sec": auto_redeem_timeout_sec,
                "apply": auto_redeem_apply,
                "error": "timeout",
            },
        )


def adjust_inventory_after_merge(
    tokens: list[dict[str, Any]],
    deduct_qty: Decimal,
    live_inventory_cost: dict[str, dict[str, Any]],
    inventory_delta_shares: Decimal,
    instrument_key_fn: Callable[[Any], str],
) -> tuple[Decimal, Decimal]:
    for token in tokens:
        inst_key = instrument_key_fn(token["instrument"].id)
        state = live_inventory_cost.get(inst_key)
        if not state:
            continue
        old_qty = Decimal(str(state.get("qty", "0")))
        if old_qty <= deduct_qty:
            live_inventory_cost.pop(inst_key, None)
            continue
        state["qty"] = old_qty - deduct_qty
        alloc = deduct_qty / old_qty if old_qty > 0 else Decimal("0")
        state["entry_fee_remaining"] = state.get("entry_fee_remaining", Decimal("0")) * (Decimal("1") - alloc)
    old_delta = inventory_delta_shares
    new_delta = max(Decimal("0"), inventory_delta_shares - deduct_qty)
    return old_delta, new_delta


def start_background_thread(target: Callable[[], None], name: str | None = None) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True, name=name)
    thread.start()
    return thread


def stop_event_threads(stop_events: list[Any], threads: list[Any], join_timeout_sec: float = 2.0) -> None:
    for stop_event in stop_events:
        if stop_event is not None:
            stop_event.set()
    for thread in threads:
        if thread and thread.is_alive():
            thread.join(timeout=join_timeout_sec)

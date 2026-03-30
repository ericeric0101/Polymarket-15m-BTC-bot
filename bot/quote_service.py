from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional


@dataclass
class QuoteInstrumentContext:
    inst_id: Any
    inst_key: str
    quote: tuple[Decimal, Decimal] | None
    fair: Decimal | None
    instrument: Any
    tick: Decimal
    token_id: str | None
    dynamic_fee_rate: Decimal | None
    fee_rate_val: Decimal
    bid_levels: Any
    ask_levels: Any
    bid_depth: Any
    ask_depth: Any
    diag_context: dict[str, Any]


def extract_instrument_tick(instrument: Any, default_tick: str = "0.01") -> Decimal:
    tick = Decimal(default_tick)
    if instrument is not None:
        try:
            raw_tick = getattr(instrument, "price_increment", None)
            if raw_tick is not None:
                tick = Decimal(str(raw_tick))
            elif hasattr(instrument, "info") and instrument.info:
                maybe_tick = instrument.info.get("minimum_tick_size")
                if maybe_tick is not None:
                    tick = Decimal(str(maybe_tick))
        except Exception:
            tick = Decimal(default_tick)
    if tick <= 0:
        tick = Decimal(default_tick)
    return tick


def build_directional_snapshot(desired: dict[str, Any]) -> dict[str, Any]:
    return {
        "directional_edge_ps": desired.get("directional_edge_ps"),
        "directional_edge_usdc": desired.get("directional_edge_usdc"),
        "p_fair": desired.get("p_fair"),
        "fee_ps": desired.get("fee_ps"),
        "other_cost_ps": desired.get("other_cost_ps"),
        "exec_penalty_usdc": desired.get("exec_penalty"),
        "robust_net_usdc": desired.get("robust_net"),
    }


def reconcile_unwanted_quotes(
    active_maker_orders: dict[str, Any],
    desired_quotes: dict[str, dict[str, Any]],
    target_inst_set: set[str],
    now_ts: float,
    cancel_cooldown_sec: float,
    gate_block_grace_sec: float,
    reason_family_fn: Callable[[str], str],
    cancel_order_fn: Callable[[str, str], None],
    gate_block_since_by_order_key: dict[str, float],
    gate_block_reason_by_order_key: dict[str, str],
    gate_last_cancel_ts_by_order_key: dict[str, float],
) -> None:
    for order_key, state in list(active_maker_orders.items()):
        state_inst = str(state.get("instrument_id", "") or "")
        if state_inst not in target_inst_set:
            continue
        desired = desired_quotes.get(order_key)
        if desired is None:
            cancel_order_fn(order_key, "risk:no_desired_quote")
            continue
        if bool(desired.get("should_quote", False)):
            gate_block_since_by_order_key.pop(order_key, None)
            gate_block_reason_by_order_key.pop(order_key, None)
            continue

        reason = str(desired.get("diag_reason", "risk") or "risk")
        reason_family = reason_family_fn(reason)
        if reason_family == "sell_pause":
            continue

        soft_block = reason_family in {
            "econ_gate",
            "reduce_only",
            "reduce_only_tail_guard",
            "balance_forced_sell_only",
            "side_disabled",
        }
        if soft_block:
            prev_reason = gate_block_reason_by_order_key.get(order_key, "")
            if prev_reason != reason_family:
                gate_block_reason_by_order_key[order_key] = reason_family
                gate_block_since_by_order_key[order_key] = now_ts
            blocked_for = now_ts - float(gate_block_since_by_order_key.get(order_key, now_ts))
            if blocked_for < float(gate_block_grace_sec):
                continue
        else:
            gate_block_reason_by_order_key.pop(order_key, None)
            gate_block_since_by_order_key.pop(order_key, None)

        last_cancel = float(gate_last_cancel_ts_by_order_key.get(order_key, 0.0))
        if now_ts - last_cancel < float(cancel_cooldown_sec):
            continue
        gate_last_cancel_ts_by_order_key[order_key] = now_ts
        cancel_order_fn(order_key, f"risk:{reason}")


def log_no_quote_diagnostics(
    submitted_attempts: int,
    target_instruments: list[Any],
    desired_quotes: dict[str, dict[str, Any]],
    diag_context_by_inst: dict[str, dict[str, Any]],
    now_ts: float,
    no_quote_diag_interval_sec: float,
    phase_value: str,
    instrument_key_fn: Callable[[Any], str],
    active_order_keys_fn: Callable[..., list[str]],
    last_no_quote_diag_ts_by_inst: dict[str, float],
    logger_info_fn: Callable[[str], None],
    reason_family_fn: Optional[Callable[[str], str]] = None,
    strategy_event_fn: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> None:
    if submitted_attempts != 0:
        return
    for inst_id in target_instruments:
        inst_key = instrument_key_fn(inst_id)
        per_inst = [
            desired for desired in desired_quotes.values()
            if instrument_key_fn(desired.get("instrument_id")) == inst_key
        ]
        if any(bool(desired.get("should_quote", False)) for desired in per_inst):
            continue
        if active_order_keys_fn(instrument_id=inst_id):
            continue
        last_diag = float(last_no_quote_diag_ts_by_inst.get(inst_key, 0.0))
        if now_ts - last_diag < float(no_quote_diag_interval_sec):
            continue
        last_no_quote_diag_ts_by_inst[inst_key] = now_ts
        ctx = diag_context_by_inst.get(inst_key, {})
        blocked = ", ".join(
            f"{desired.get('side')}={desired.get('diag_reason') or 'blocked'}"
            for desired in per_inst
        ) if per_inst else str(ctx.get("reason", "no_quote_tick"))
        fair = ctx.get("fair")
        bid = ctx.get("bid")
        ask = ctx.get("ask")
        fee_rate = ctx.get("fee_rate")
        msg_parts = [
            f"NO_QUOTE diagnostic: inst={inst_key}",
            f"phase={phase_value}",
        ]
        if isinstance(fair, Decimal):
            msg_parts.append(f"fair={float(fair):.4f}")
        if isinstance(bid, Decimal) and isinstance(ask, Decimal):
            msg_parts.append(f"bid={float(bid):.4f}")
            msg_parts.append(f"ask={float(ask):.4f}")
        if isinstance(fee_rate, Decimal):
            msg_parts.append(f"fee_rate={float(fee_rate):.6f}")
        msg_parts.append(f"blocked={blocked}")
        logger_info_fn(" ".join(msg_parts))
        if strategy_event_fn is not None:
            buy_desired = next((desired for desired in per_inst if str(desired.get("side")) == "buy"), None)
            primary = buy_desired or (per_inst[0] if per_inst else None)
            primary_reason = str(primary.get("diag_reason", "")) if primary else ""
            family = reason_family_fn(primary_reason) if (reason_family_fn and primary_reason) else ""
            event_type = ""
            if family == "econ_gate":
                event_type = "NO_TRADE_ECON_GATE"
            elif family == "reduce_only":
                event_type = "NO_TRADE_REDUCE_ONLY"
            elif family == "reduce_only_tail_guard":
                event_type = "NO_TRADE_REDUCE_ONLY_TAIL_GUARD"
            elif family == "trend_protection":
                event_type = "NO_TRADE_TREND_PROTECTION"
            elif family == "side_disabled" and primary_reason.startswith("side_disabled:edge_gate_buy"):
                event_type = "NO_TRADE_DIRECTIONAL_EDGE_GATE"
            if event_type:
                payload = {
                    "instrument_id": str(inst_id),
                    "blocked": blocked,
                    "primary_reason": primary_reason,
                    "phase": phase_value,
                }
                if isinstance(fair, Decimal):
                    payload["fair"] = float(fair)
                if isinstance(bid, Decimal):
                    payload["bid"] = float(bid)
                if isinstance(ask, Decimal):
                    payload["ask"] = float(ask)
                if isinstance(fee_rate, Decimal):
                    payload["fee_rate"] = float(fee_rate)
                strategy_event_fn(event_type, payload)


def retreat_crossing_buy_quote(
    limit_price: Decimal,
    instrument: Any,
    quote_now: tuple[Decimal, Decimal] | None,
    align_price_fn: Callable[[Decimal, str, Any], Decimal],
    logger_warning_fn: Callable[[str], None],
    logger_info_fn: Callable[[str], None],
) -> Decimal | None:
    if quote_now is None:
        return limit_price
    _best_bid_now, best_ask_now = quote_now
    if limit_price < best_ask_now:
        return limit_price
    tick = Decimal("0.01")
    try:
        raw_tick = getattr(instrument, "price_increment", None) if instrument is not None else None
        if raw_tick is not None:
            tick = Decimal(str(raw_tick.as_decimal() if hasattr(raw_tick, "as_decimal") else raw_tick))
        elif instrument is not None and hasattr(instrument, "info") and instrument.info:
            min_tick = instrument.info.get("minimum_tick_size")
            if min_tick is not None:
                tick = Decimal(str(min_tick))
    except Exception:
        tick = Decimal("0.01")
    if tick <= 0:
        tick = Decimal("0.01")
    old_limit_price = limit_price
    limit_price = align_price_fn(best_ask_now - tick, "buy", instrument)
    if limit_price >= best_ask_now:
        logger_warning_fn(
            f"Skip crossing BUY quote {float(old_limit_price):.4f} >= ask {float(best_ask_now):.4f} "
            f"(retreat failed -> {float(limit_price):.4f})"
        )
        return None
    logger_info_fn(
        "Adjusted BUY quote to avoid crossing: "
        f"{float(old_limit_price):.4f} -> {float(limit_price):.4f} "
        f"(ask={float(best_ask_now):.4f})"
    )
    return limit_price


def apply_sellable_inventory_guard(
    qty_dec: Decimal,
    precision: int,
    sellable_qty: Decimal,
    maker_exchange_min_shares: Decimal,
) -> tuple[Decimal | None, str | None]:
    if sellable_qty < Decimal("0.01"):
        return None, "no_sellable_inventory"
    if sellable_qty + Decimal("0.000001") < qty_dec:
        qty_dec = sellable_qty.quantize(Decimal(str(10 ** (-precision))))
    if qty_dec + Decimal("0.000001") < maker_exchange_min_shares:
        return None, "sellable_below_min_after_reduce"
    return qty_dec, None


def build_limit_order(
    order_factory: Any,
    order_kwargs: dict[str, Any],
    maker_use_post_only: bool,
    maker_post_only_strict: bool,
    logger_error_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
) -> tuple[Any | None, bool]:
    if maker_use_post_only:
        try:
            return order_factory.limit(**order_kwargs, post_only=True), maker_use_post_only
        except TypeError:
            if maker_post_only_strict:
                logger_error_fn("Order factory does not support post_only while strict mode is enabled; skip quote.")
                return None, maker_use_post_only
            logger_warning_fn("Order factory post_only unsupported; falling back to normal limit order.")
            maker_use_post_only = False
    return order_factory.limit(**order_kwargs), maker_use_post_only


def violates_final_crossing_guard(
    side: str,
    limit_price: Decimal,
    quote: tuple[Decimal, Decimal] | None,
    maker_use_post_only: bool,
    maker_post_only_strict: bool,
    logger_warning_fn: Callable[[str], None],
) -> bool:
    if quote is None:
        return False
    best_bid, best_ask = quote
    if side == "buy" and limit_price >= best_ask:
        logger_warning_fn(f"Skip crossing BUY quote {float(limit_price):.4f} >= ask {float(best_ask):.4f}")
        return True
    if side == "sell" and limit_price <= best_bid:
        logger_warning_fn(f"Skip crossing SELL quote {float(limit_price):.4f} <= bid {float(best_bid):.4f}")
        return True
    return False


def build_active_maker_order_state(
    order: Any,
    econ: Any,
    directional_snapshot: dict[str, Any] | None,
    limit_price: Decimal,
    side: str,
    instrument_id: Any,
    token_id: str | None,
    token_qty: float,
    created_ts: float,
    target_version: int,
) -> dict[str, Any]:
    return {
        "order": order,
        "econ": econ,
        "directional_snapshot": directional_snapshot or {},
        "price": limit_price,
        "side": side,
        "instrument_id": instrument_id,
        "token_id": token_id,
        "quantity": Decimal(str(token_qty)),
        "created_ts": created_ts,
        "target_version": target_version,
    }


async def build_quote_instrument_context(
    inst_id: Any,
    normalize_instrument_id_fn: Callable[[Any], Any],
    instrument_key_fn: Callable[[Any], str],
    get_quote_for_instrument_fn: Callable[[Any], tuple[Decimal, Decimal] | None],
    compute_fair_probability_fn: Callable[..., Any],
    cache_instrument_fn: Callable[[Any], Any],
    extract_token_id_fn: Callable[[str], str | None],
    get_dynamic_fee_rate_fn: Callable[..., Any],
    get_orderbook_levels_fn: Callable[[str | None], Any],
    latest_quote_depth_by_inst: dict[str, tuple[Any, Any]],
    maker_econ_fee_rate_decimal: Decimal,
) -> QuoteInstrumentContext:
    inst_key = instrument_key_fn(inst_id)
    quote = get_quote_for_instrument_fn(inst_id)
    if quote is None:
        return QuoteInstrumentContext(
            inst_id=inst_id,
            inst_key=inst_key,
            quote=None,
            fair=None,
            instrument=None,
            tick=Decimal("0.01"),
            token_id=None,
            dynamic_fee_rate=None,
            fee_rate_val=maker_econ_fee_rate_decimal,
            bid_levels=None,
            ask_levels=None,
            bid_depth=None,
            ask_depth=None,
            diag_context={"reason": "no_quote_tick"},
        )

    inst_bid, inst_ask = quote
    fair = await compute_fair_probability_fn((inst_bid + inst_ask) / 2, instrument_id=inst_id)
    instrument_for_tick = normalize_instrument_id_fn(inst_id)
    instrument = cache_instrument_fn(instrument_for_tick) if instrument_for_tick else None
    tick = extract_instrument_tick(instrument, default_tick="0.01")

    token_id = extract_token_id_fn(str(inst_id))
    dynamic_fee_rate = await get_dynamic_fee_rate_fn(token_id=token_id)
    bid_levels, ask_levels = await get_orderbook_levels_fn(token_id)
    bid_depth, ask_depth = latest_quote_depth_by_inst.get(str(inst_id), (None, None))
    return QuoteInstrumentContext(
        inst_id=inst_id,
        inst_key=inst_key,
        quote=quote,
        fair=fair,
        instrument=instrument,
        tick=tick,
        token_id=token_id,
        dynamic_fee_rate=dynamic_fee_rate,
        fee_rate_val=maker_econ_fee_rate_decimal,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        diag_context={
            "reason": "ok",
            "fair": fair,
            "bid": inst_bid,
            "ask": inst_ask,
            "fee_rate": maker_econ_fee_rate_decimal,
        },
    )


def build_desired_quote_entry(
    order_key: str,
    side: str,
    inst_id: Any,
    quote_data: tuple[Any, ...],
    side_disable_reason_by_side: dict[str, str],
    reduce_only_reason: str | None,
    reduce_only_tail_sell_block: bool,
    reduce_only_no_new_sell_last_sec: int,
    forced_sell_only: bool,
    min_expected_net_usdc: Decimal,
    now_ts: float,
    sell_pause_until: float,
    is_dry_run_mode: bool,
    sellable_qty: Decimal | None,
    maker_exchange_min_shares: Decimal,
    avg_entry: Decimal,
    emergency_window: bool,
    high_cost_exit_cooldown_enabled: bool,
    high_cost_exit_cooldown_sec: float,
    high_cost_exit_cooldown_until: float,
    maker_sell_cost_protect_enabled: bool,
    maker_sell_cost_protect_fee_buffer_ps: Decimal,
    maker_sell_min_profit_floor_ps: Decimal = Decimal("0"),
    thesis_weakened: bool = False,
    offside_confirmed: bool = False,
) -> dict[str, Any]:
    limit_price = quote_data[0]
    econ = quote_data[1]
    should_quote = quote_data[2]
    robust_net = quote_data[3] if len(quote_data) > 3 else None
    exec_penalty = quote_data[4] if len(quote_data) > 4 else None
    directional_edge_ps = quote_data[5] if len(quote_data) > 5 else None
    directional_edge_usdc = quote_data[6] if len(quote_data) > 6 else None
    p_fair = quote_data[7] if len(quote_data) > 7 else None
    fee_ps = quote_data[8] if len(quote_data) > 8 else None
    other_cost_ps = quote_data[9] if len(quote_data) > 9 else None

    diag_reason = ""
    if not should_quote:
        robust_net_val = robust_net if isinstance(robust_net, Decimal) else None
        robust_net_display = float(robust_net) if isinstance(robust_net, Decimal) else float("nan")
        exec_penalty_display = float(exec_penalty) if isinstance(exec_penalty, Decimal) else 0.0
        if robust_net_val is not None and robust_net_val < min_expected_net_usdc:
            diag_reason = (
                f"econ_gate robust_net={float(robust_net_val):.6f} "
                f"(expected_net={float(econ.expected_net_usdc):.6f}, "
                f"exec_penalty={exec_penalty_display:.6f}) "
                f"< min={float(min_expected_net_usdc):.6f}"
            )
        else:
            side_disable_reason = side_disable_reason_by_side.get(side, "unspecified")
            diag_reason = (
                f"side_disabled:{side_disable_reason} robust_net={robust_net_display:.6f} "
                f"(expected_net={float(econ.expected_net_usdc):.6f}, "
                f"exec_penalty={exec_penalty_display:.6f})"
            )

    if side == "sell":
        if now_ts < sell_pause_until:
            should_quote = False
            diag_reason = f"sell_pause {sell_pause_until - now_ts:.1f}s"
        if should_quote and not is_dry_run_mode and sellable_qty is not None:
            if sellable_qty + Decimal("0.000001") < maker_exchange_min_shares:
                should_quote = False
                diag_reason = (
                    f"sellable_below_min sellable={float(sellable_qty):.6f} "
                    f"< min={float(maker_exchange_min_shares):.6f}"
                )
        # Allow loss-selling when:
        # 1) emergency window near expiry,
        # 2) thesis weakened,
        # 3) inventory is confirmed offside against a locked side decision.
        allow_loss_sell = emergency_window or thesis_weakened or offside_confirmed
        if (
            should_quote
            and high_cost_exit_cooldown_enabled
            and high_cost_exit_cooldown_sec > 0
            and now_ts < high_cost_exit_cooldown_until
            and avg_entry > 0
            and limit_price < avg_entry
            and not allow_loss_sell
        ):
            should_quote = False
            diag_reason = (
                f"high_cost_exit_cooldown sell={float(limit_price):.4f} "
                f"< avg_entry={float(avg_entry):.4f}"
            )
        if (
            should_quote
            and maker_sell_cost_protect_enabled
            and avg_entry > 0
            and limit_price < (avg_entry + maker_sell_cost_protect_fee_buffer_ps)
            and not allow_loss_sell
        ):
            should_quote = False
            diag_reason = (
                f"sell_cost_protect sell={float(limit_price):.4f} "
                f"< min={float(avg_entry + maker_sell_cost_protect_fee_buffer_ps):.4f}"
            )
        # Minimum profit floor — block sells that are technically above cost but
        # yield too little profit to justify using a buy quota slot.
        if (
            should_quote
            and maker_sell_min_profit_floor_ps > 0
            and avg_entry > 0
            and limit_price < (avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps)
            and not allow_loss_sell
        ):
            should_quote = False
            min_sell = avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps
            diag_reason = (
                f"min_profit_floor sell={float(limit_price):.4f} "
                f"< min={float(min_sell):.4f} "
                f"(entry={float(avg_entry):.4f}+fee={float(maker_sell_cost_protect_fee_buffer_ps):.4f}"
                f"+floor={float(maker_sell_min_profit_floor_ps):.4f})"
            )

    if reduce_only_reason and side == "buy":
        diag_reason = f"reduce_only: {reduce_only_reason}"
    if reduce_only_tail_sell_block and side == "sell":
        diag_reason = f"reduce_only_tail_guard: <= {reduce_only_no_new_sell_last_sec}s"
    if forced_sell_only and side == "buy":
        diag_reason = "balance_forced_sell_only"

    return {
        "order_key": order_key,
        "side": side,
        "instrument_id": inst_id,
        "price": limit_price,
        "econ": econ,
        "should_quote": should_quote,
        "diag_reason": diag_reason,
        "robust_net": robust_net,
        "exec_penalty": exec_penalty,
        "directional_edge_ps": directional_edge_ps,
        "directional_edge_usdc": directional_edge_usdc,
        "p_fair": p_fair,
        "fee_ps": fee_ps,
        "other_cost_ps": other_cost_ps,
    }


def compute_requote_target_version(
    order_key: str,
    limit_price: Decimal,
    tick: Decimal,
    maker_requote_hysteresis_ticks: int,
    target_anchor_price_by_order_key: dict[str, Decimal],
    target_version_by_order_key: dict[str, int],
) -> int:
    prev_anchor = target_anchor_price_by_order_key.get(order_key)
    target_version = int(target_version_by_order_key.get(order_key, 0))
    if prev_anchor is None:
        target_version += 1
        target_anchor_price_by_order_key[order_key] = limit_price
        target_version_by_order_key[order_key] = target_version
        return target_version
    if abs(limit_price - prev_anchor) >= (maker_requote_hysteresis_ticks * tick):
        target_version += 1
        target_anchor_price_by_order_key[order_key] = limit_price
        target_version_by_order_key[order_key] = target_version
    return target_version


def should_requote_existing_order(
    current: dict[str, Any] | None,
    target_version: int,
    now_ts: float,
    maker_requote_min_age_sec: float,
    side: str = "",
    maker_requote_min_age_sec_sell: float = 0,
) -> bool:
    if not current:
        return False
    if current.get("pending_cancel"):
        return False
    current_target_version = int(current.get("target_version", 0) or 0)
    if current_target_version >= target_version:
        return False
    created_ts = float(current.get("created_ts", 0.0))
    # Use sell-specific min age if this is a sell order and one is configured.
    effective_min_age = maker_requote_min_age_sec
    if side.lower() == "sell" and maker_requote_min_age_sec_sell > 0:
        effective_min_age = maker_requote_min_age_sec_sell
    if effective_min_age > 0 and created_ts > 0 and (now_ts - created_ts) < effective_min_age:
        return False
    return True

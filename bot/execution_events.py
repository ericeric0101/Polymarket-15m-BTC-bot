from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Iterable


@dataclass
class CancelAckResult:
    should_skip: bool
    cancel_reason: str


@dataclass
class RejectResult:
    rejected_side: str
    rejected_inst: Any
    reason: str
    is_taker_exit_reject: bool
    rejected_inst_key: str


def is_benign_cancel_reject_reason(reason: str) -> bool:
    """Whether an exchange cancel rejection confirms the order is terminal."""
    normalized = str(reason or "").lower()
    return any(
        marker in normalized
        for marker in (
            "already canceled",
            "already cancelled",
            "order can't be found",
            "order cannot be found",
            "matched orders can't be canceled",
        )
    )


def reconcile_cancel_ack(
    canceled_id: str,
    event: Any,
    active_maker_orders: dict[str, dict[str, Any]],
    last_cancel_ack_ts_by_client_order_id: dict[str, float],
    cancel_ack_dedupe_window_sec: float,
) -> CancelAckResult:
    now_ts = time.time()
    if canceled_id:
        last_ack_ts = float(last_cancel_ack_ts_by_client_order_id.get(canceled_id, 0.0))
        if (now_ts - last_ack_ts) < cancel_ack_dedupe_window_sec:
            return CancelAckResult(should_skip=True, cancel_reason="")
        last_cancel_ack_ts_by_client_order_id[canceled_id] = now_ts

    cancel_reason = ""
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        state_coid = str(state.get("client_order_id", "") or "")
        if (order and str(order.client_order_id) == canceled_id) or (state_coid and state_coid == canceled_id):
            cancel_reason = str(state.get("cancel_reason", "") or "")
            active_maker_orders.pop(order_key, None)
            break
    return CancelAckResult(should_skip=False, cancel_reason=cancel_reason)


def reconcile_rejected_order(
    denied_id: str,
    event: Any,
    active_maker_orders: dict[str, dict[str, Any]],
    normalize_side_text_fn,
    instrument_key_fn,
) -> RejectResult:
    rejected_side = ""
    rejected_inst: Any = None
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        if order and str(order.client_order_id) == denied_id:
            rejected_side = str(state.get("side", "") or "")
            rejected_inst = state.get("instrument_id")
            active_maker_orders.pop(order_key, None)
            break
    if not rejected_side:
        rejected_side = normalize_side_text_fn(getattr(event, "order_side", ""))
    if rejected_inst is None:
        rejected_inst = getattr(event, "instrument_id", None)
    if not rejected_side and denied_id.startswith("BTC-15M-TAKER-EXIT-"):
        rejected_side = "sell"
    is_taker_exit_reject = denied_id.startswith("BTC-15M-TAKER-EXIT-")
    rejected_inst_key = instrument_key_fn(rejected_inst) if rejected_inst is not None else ""
    return RejectResult(
        rejected_side=rejected_side,
        rejected_inst=rejected_inst,
        reason=str(getattr(event, "reason", "") or ""),
        is_taker_exit_reject=is_taker_exit_reject,
        rejected_inst_key=rejected_inst_key,
    )


def reconcile_benign_cancel_reject(
    rejected_id: str,
    active_maker_orders: dict[str, dict[str, Any]],
) -> bool:
    for order_key, state in list(active_maker_orders.items()):
        order = state.get("order")
        state_coid = str(state.get("client_order_id", "") or "")
        if (order and str(order.client_order_id) == rejected_id) or (state_coid and state_coid == rejected_id):
            active_maker_orders.pop(order_key, None)
            return True
    return False


def audit_reconciliation(events: Iterable[dict], *, local_inventory: dict | None = None,
                         venue_inventory: dict | None = None, open_orders: Iterable[dict] = (),
                         complete_history: bool = False) -> dict:
    """Read-only diagnostics over recorded events and explicitly supplied snapshots.

    This does not recover, cancel or liquidate anything. Quantity projection
    reuses InventoryLedger and research identity uses its existing annotations.
    """
    from decimal import Decimal
    from bot.inventory import InventoryLedger
    ledger, seen, clients, finalized, identities = {}, {}, {}, set(), {}
    finalization_events, cycle_versions, pending_settlements = [], {}, set()
    issues = []
    rank = {'CONSISTENT': 0, 'RECOVERABLE_MISMATCH': 1, 'UNRESOLVED_MISMATCH': 2, 'CRITICAL_INCONSISTENCY': 3}
    def issue(code, severity, **context):
        issues.append({'reason_code': code, 'status': severity, **context})
    for event in events:
        payload = event.get('payload', {})
        event_type = event.get('event_type', '')
        inst = str(event.get('instrument_id') or payload.get('instrument_id') or '')
        client = str(event.get('client_order_id') or '')
        if event.get('payload_status') == 'INVALID_PAYLOAD':
            issue('INVALID_PAYLOAD', 'UNRESOLVED_MISMATCH', event_id=event.get('id')); continue
        if event_type.endswith('_SUBMIT'):
            if client and client in clients:
                issue('DUPLICATE_CLIENT_ORDER_ID', 'UNRESOLVED_MISMATCH', client_order_id=client)
            if client:
                clients[client] = {'qty': event.get('qty'), 'filled': Decimal(0), 'instrument_id': inst}
        if event_type == 'ORDER_FILLED':
            fill_id = payload.get('fill_event_id') or payload.get('trade_id')
            key = (str(event.get('run_id')), inst, str(fill_id))
            fingerprint = (event.get('side'), event.get('qty'), event.get('price'), client,
                           event.get('commission_usdc'), payload.get('effective_fee_shares'))
            if fill_id and key in seen:
                issue('DUPLICATE_FILL' if seen[key] == fingerprint else 'CONFLICTING_FILL_ID',
                      'RECOVERABLE_MISMATCH' if seen[key] == fingerprint else 'CRITICAL_INCONSISTENCY', client_order_id=client)
                continue
            if fill_id:
                seen[key] = fingerprint
            else:
                issue('FILL_ID_UNKNOWN', 'UNRESOLVED_MISMATCH', client_order_id=client)
            try:
                if event.get('commission_usdc') is None or payload.get('effective_fee_shares') is None:
                    issue('FEE_INPUT_INCOMPLETE', 'UNRESOLVED_MISMATCH', event_id=event.get('id'))
                qty, price = Decimal(str(event['qty'])), Decimal(str(event['price']))
                fee, shares = Decimal(str(event.get('commission_usdc') or 0)), Decimal(str(payload.get('effective_fee_shares') or 0))
                if not all(v.is_finite() for v in (qty, price, fee, shares)) or qty <= 0 or price <= 0:
                    raise ValueError('invalid numeric fill')
                side = str(event.get('side') or '').lower()
                if side not in {'buy', 'sell'} or not inst:
                    raise ValueError('invalid side/instrument')
                before = ledger.get(inst, {}).get('qty', Decimal(0))
                if side == 'sell' and qty > before:
                    issue('SELL_EXCEEDS_RECORDED_INVENTORY',
                          'CRITICAL_INCONSISTENCY' if complete_history else 'UNRESOLVED_MISMATCH', instrument_id=inst)
                InventoryLedger.update_from_fill(ledger, inst, side, price, qty, fee, shares, 1.0)
                identity = payload.get('position_lifecycle_id')
                if side == 'buy' and identity:
                    if before <= 0 and identities.get(inst) == identity:
                        issue('REOPEN_REUSES_LIFECYCLE', 'CRITICAL_INCONSISTENCY', instrument_id=inst)
                    elif before > 0 and identities.get(inst) and identities[inst] != identity:
                        issue('SCALEIN_CHANGES_LIFECYCLE', 'UNRESOLVED_MISMATCH', instrument_id=inst)
                    identities[inst] = identity
                if client in clients:
                    clients[client]['filled'] += qty
                    submitted = clients[client]['qty']
                    if submitted is not None and clients[client]['filled'] > Decimal(str(submitted)):
                        issue('FILLS_EXCEED_ORDER_QTY', 'CRITICAL_INCONSISTENCY', client_order_id=client)
            except (KeyError, ValueError, ArithmeticError):
                issue('INVALID_FILL', 'CRITICAL_INCONSISTENCY', event_id=event.get('id'))
        if 'CANCEL' in event_type and client in clients and clients[client]['filled'] > 0:
            issue('CANCELLED_WITH_PARTIAL_FILL', 'RECOVERABLE_MISMATCH', client_order_id=client)
        if event_type in {'MARKET_CYCLE_PNL', 'MARKET_PNL_RECONCILED'}:
            import json
            market = payload.get('market_slug') or payload.get('slug')
            tx = payload.get('redeem_tx_hash') or payload.get('tx_hash')
            reconciliation_id = tx or payload.get('reconciliation_id')
            corrected = (event_type == 'MARKET_PNL_RECONCILED' or
                         payload.get('source') == 'redeem_reconciliation' or
                         payload.get('cycle_pnl_reconciled_source') == 'onchain_redeem')
            values = tuple(payload.get(key) for key in ('cycle_fill_realized_usdc',
                'cycle_settlement_pnl_usdc', 'cycle_combined_pnl_usdc'))
            # Ignore insertion/reconciliation timestamps: repeated cash evidence
            # with the same transaction and amounts is still a duplicate.
            # An in-place corrected cycle row and its MARKET_PNL_RECONCILED
            # notification are complementary representations, not duplicate writes.
            fingerprint = json.dumps([event_type, values, reconciliation_id, payload.get('redeem_condition_id'),
                payload.get('cycle_pnl_reconciled_source'), payload.get('source')], sort_keys=True)
            versions = cycle_versions.setdefault(market, set()) if market else set()
            if not market or any(value is None for value in values) or (corrected and not reconciliation_id):
                classification = 'IDENTITY_INSUFFICIENT'
                issue('FINALIZATION_IDENTITY_INSUFFICIENT', 'UNRESOLVED_MISMATCH', market_slug=market)
            elif fingerprint in versions:
                classification = 'TRUE_DUPLICATE'
                issue('REPEATED_FINALIZATION', 'UNRESOLVED_MISMATCH', market_slug=market)
            elif corrected:
                classification = 'LEGITIMATE_RECONCILIATION_UPDATE'
            elif not versions:
                classification = 'INITIAL_FINALIZATION'
            else:
                classification = 'IDENTITY_INSUFFICIENT'
                issue('FINALIZATION_IDENTITY_INSUFFICIENT', 'UNRESOLVED_MISMATCH', market_slug=market)
            if classification != 'IDENTITY_INSUFFICIENT':
                versions.add(fingerprint)
            finalization_events.append({'event_id': event.get('id'), 'event_type': event_type,
                'market_slug': market, 'classification': classification, 'reconciliation_tx_hash': tx})
        if event_type in {'MARKET_SETTLEMENT', 'REDEEM_RECONCILIATION', 'REDEEM_EXECUTED'}:
            market = payload.get('market_slug') or payload.get('slug')
            if not market:
                issue('FINALIZATION_IDENTITY_UNKNOWN', 'UNRESOLVED_MISMATCH'); continue
            key = (event_type, payload.get('tx_hash') or market) if event_type == 'REDEEM_EXECUTED' else (event_type, market)
            # A pending UNKNOWN settlement is not final: its later canonical
            # relabel (deferred or startup Gamma) supersedes it once.
            if key in finalized and key not in pending_settlements:
                issue('REPEATED_FINALIZATION', 'UNRESOLVED_MISMATCH', market_slug=market)
            finalized.add(key)
            pending_settlements.discard(key)
            if event_type == 'MARKET_SETTLEMENT' and (
                    payload.get('settlement_pending') or str(payload.get('outcome') or '').upper() == 'UNKNOWN'):
                pending_settlements.add(key)
    for order in open_orders:
        client = str(order.get('client_order_id') or '')
        if client not in clients:
            issue('RESTART_OPEN_ORDER_WITHOUT_INTENT', 'UNRESOLVED_MISMATCH', client_order_id=client)
        elif order.get('filled_qty') is not None:
            try:
                filled = Decimal(str(order['filled_qty']))
                if not filled.is_finite():
                    raise ValueError('non-finite partial quantity')
                if filled != clients[client]['filled']:
                    issue('RESTART_PARTIAL_FILL_MISMATCH', 'UNRESOLVED_MISMATCH', client_order_id=client)
            except (ValueError, ArithmeticError, TypeError):
                issue('INVALID_OPEN_ORDER_QUANTITY', 'CRITICAL_INCONSISTENCY', client_order_id=client)
    for label, snapshot in [('JOURNAL_LOCAL_QTY_MISMATCH', local_inventory), ('VENUE_LOCAL_QTY_MISMATCH', venue_inventory)]:
        if snapshot is None:
            continue
        comparison = ledger if label.startswith('JOURNAL') or local_inventory is None else local_inventory
        for inst in set(comparison) | set(snapshot):
            def qty(source):
                value = source.get(inst, 0)
                return Decimal(str(value.get('qty', 0) if isinstance(value, dict) else value))
            try:
                expected, observed = qty(comparison), qty(snapshot)
                if not expected.is_finite() or not observed.is_finite():
                    raise ValueError('non-finite quantity')
                if expected != observed:
                    actual_label = 'VENUE_JOURNAL_QTY_MISMATCH' if label.startswith('VENUE') and local_inventory is None else label
                    issue(actual_label, 'UNRESOLVED_MISMATCH', instrument_id=inst)
            except (ValueError, ArithmeticError, TypeError):
                issue('INVALID_SNAPSHOT_QUANTITY', 'CRITICAL_INCONSISTENCY', instrument_id=inst)
    return {'status': max((i['status'] for i in issues), key=rank.get, default='CONSISTENT'),
            'issues': issues, 'finalization_events': finalization_events, 'journal_inventory': {key: str(value['qty']) for key, value in ledger.items()},
            'venue_snapshot_available': venue_inventory is not None, 'execution_action': None,
            'scope': 'RECORDED_FILL_PROJECTION', 'complete_history_asserted': complete_history}

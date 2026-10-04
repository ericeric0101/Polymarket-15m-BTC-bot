"""Deterministic offline replay helpers for recorded signal candidates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ReplayCandidate:
    slug: str
    ts: str
    side: str
    entry_price: float
    qty: float | None = None


@dataclass(frozen=True)
class ReplayResult:
    slug: str
    ts: str
    side: str
    entry_price: float
    outcome: str
    won: bool
    pnl_per_share: float
    qty: float
    pnl: float


def candidate_from_payload(ts: str, payload: dict[str, Any]) -> ReplayCandidate | None:
    slug = str(payload.get("slug") or payload.get("market_slug") or "")
    side = str(payload.get("shadow_candidate_side") or payload.get("main_candidate_side") or "").upper()
    if not slug or side not in {"BUY_UP", "BUY_DOWN"}:
        return None
    entry_key = "ask_up" if side == "BUY_UP" else "ask_down"
    try:
        entry_price = float(payload.get(entry_key))
    except (TypeError, ValueError):
        return None
    if not 0 < entry_price < 1:
        return None
    return ReplayCandidate(slug=slug, ts=ts, side=side, entry_price=entry_price)


def dry_run_fill_from_payload(
    ts: str,
    payload: dict[str, Any],
    *,
    side: str | None,
    price: float | None,
    qty: float | None,
) -> ReplayCandidate | None:
    """Build a replay candidate from a filled dry-run maker simulation."""
    slug = str(payload.get("slug") or payload.get("market_slug") or "")
    outcome_side = str(payload.get("side") or side or "").upper()
    if outcome_side in {"UP", "DOWN"}:
        outcome_side = f"BUY_{outcome_side}"
    if not slug or outcome_side not in {"BUY_UP", "BUY_DOWN"}:
        return None
    try:
        entry_price = float(payload.get("entry_price", price))
    except (TypeError, ValueError):
        return None
    if not 0 < entry_price < 1:
        return None
    try:
        filled_qty = float(payload.get("qty", qty))
    except (TypeError, ValueError):
        return None
    if filled_qty <= 0:
        return None
    return ReplayCandidate(
        slug=slug,
        ts=ts,
        side=outcome_side,
        entry_price=entry_price,
        qty=filled_qty,
    )


def select_one_candidate_per_market(
    candidates: Iterable[ReplayCandidate], *, selection: str = "first"
) -> list[ReplayCandidate]:
    """Select a stable, explicit one-entry policy for every market."""
    if selection not in {"first", "last"}:
        raise ValueError("selection must be 'first' or 'last'")
    selected: dict[str, ReplayCandidate] = {}
    for candidate in candidates:
        if selection == "first":
            selected.setdefault(candidate.slug, candidate)
        else:
            selected[candidate.slug] = candidate
    return [selected[slug] for slug in sorted(selected)]


def replay_candidates(
    candidates: Iterable[ReplayCandidate],
    outcomes_by_slug: dict[str, str],
    *,
    default_qty: float = 1.0,
) -> list[ReplayResult]:
    """Score binary-token entries at settlement before fees and execution effects."""
    results: list[ReplayResult] = []
    for candidate in candidates:
        outcome = str(outcomes_by_slug.get(candidate.slug, "")).upper()
        if outcome not in {"UP", "DOWN"}:
            continue
        won = candidate.side.removeprefix("BUY_") == outcome
        pnl = (1.0 - candidate.entry_price) if won else -candidate.entry_price
        qty = candidate.qty if candidate.qty is not None else default_qty
        results.append(
            ReplayResult(
                slug=candidate.slug,
                ts=candidate.ts,
                side=candidate.side,
                entry_price=candidate.entry_price,
                outcome=outcome,
                won=won,
                pnl_per_share=pnl,
                qty=qty,
                pnl=pnl * qty,
            )
        )
    return results


def replay_evidence(events: Iterable[dict], *, kind: str, decision_ts: float,
                    guard_config=None, complete_history: bool = False) -> dict:
    """Canonical captured-evidence replay; no venue, I/O or live strategy object.

    EXACT refers to pure evaluation on complete recorded inputs, never to
    counterfactual venue execution. Missing initial inventory/fees/config makes
    accounting approximate. No entry/stop formula is invented here.
    """
    from decimal import Decimal
    from bot.research.clocks import available_at, epoch
    from bot.session_pnl_guard import SessionPnlGuard
    from bot.inventory import InventoryLedger
    from bot.db_runtime import StrategyDBRuntimeMixin
    if kind not in {'MARKET', 'DECISION', 'ACCOUNTING'}:
        raise ValueError('unsupported replay kind')
    rows = []
    excluded = []
    for index, original in enumerate(events):
        row = dict(original)
        if not available_at(row, decision_ts, timestamp='event_ts'):
            excluded.append({'index': index, 'reason': 'EVIDENCE_UNAVAILABLE_AT_DECISION'})
            continue
        rows.append((epoch(row['event_ts']), index, row))
    rows.sort(key=lambda item: (item[0], item[1]))
    output, ledger, guards = [], {}, {}
    classification = 'EXACT_REPLAY' if complete_history else 'APPROXIMATE_REPLAY'
    reasons = [] if complete_history else ['HISTORY_COMPLETENESS_UNVERIFIED']
    if kind == 'DECISION' and guard_config is None:
        return {'kind': kind, 'classification': 'NOT_REPLAYABLE', 'reason_codes': ['HISTORICAL_POLICY_CONFIG_REQUIRED'],
                'rows': [], 'excluded': excluded}
    seen = set()
    finalized = set()
    for ts, _, row in rows:
        payload = row.get('payload', row)
        if kind == 'MARKET':
            output.append(row)
        elif kind == 'DECISION':
            # Completed-cycle policy progression reuses the deployed session
            # key and guard. It cannot reconstruct unrecorded interim decisions.
            if row.get('event_type') != 'MARKET_CYCLE_PNL':
                continue
            market = payload.get('market_slug') or payload.get('slug')
            if market and market in finalized:
                classification = 'NOT_REPLAYABLE'; reasons.append('REPEATED_FINALIZATION'); continue
            if market:
                finalized.add(market)
            amount = payload.get('cycle_combined_pnl_usdc')
            if amount is None:
                classification = 'NOT_REPLAYABLE'; reasons.append('PNL_UNKNOWN'); continue
            date = StrategyDBRuntimeMixin._taipei_session_date(ts)
            guard = guards.setdefault(date, SessionPnlGuard(guard_config, session_date=date))
            before = guard.decision()
            value = Decimal(str(amount))
            if not value.is_finite():
                raise ValueError('non-finite accounting event')
            guard.apply_realized_delta(value)
            after = guard.decision()
            output.append({'event_ts': ts, 'session_date': date, 'buy_allowed_before': before.allowed,
                           'buy_allowed_after': after.allowed, 'reason_code': after.reason,
                           'realized_pnl_usdc': str(after.realized_pnl_usdc),
                           'high_water_usdc': str(after.realized_high_water_usdc)})
        else:
            if row.get('event_type') != 'ORDER_FILLED':
                continue
            fill_id = payload.get('fill_event_id') or payload.get('trade_id')
            key = (row.get('run_id'), row.get('instrument_id'), fill_id)
            if fill_id and key in seen:
                excluded.append({'fill_event_id': fill_id, 'reason': 'DUPLICATE_FILL_EVENT'}); continue
            if fill_id:
                seen.add(key)
            else:
                classification = 'APPROXIMATE_REPLAY'; reasons.append('FILL_ID_UNKNOWN')
            if payload.get('effective_fee_shares') is None or row.get('commission_usdc') is None:
                classification = 'APPROXIMATE_REPLAY'; reasons.append('FEE_INPUT_INCOMPLETE')
            values = [Decimal(str(value)) for value in
                      (row.get('price'), row.get('qty'), row.get('commission_usdc') or 0,
                       payload.get('effective_fee_shares') or 0)]
            if not all(value.is_finite() for value in values):
                raise ValueError('non-finite fill')
            instrument = str(row.get('instrument_id') or '')
            side = str(row.get('side') or '').lower()
            if not instrument or side not in {'buy', 'sell'} or values[0] <= 0 or values[1] <= 0:
                classification = 'NOT_REPLAYABLE'; reasons.append('INVALID_FILL'); continue
            if side == 'sell' and values[1] > ledger.get(instrument, {}).get('qty', Decimal(0)):
                classification = 'NOT_REPLAYABLE'; reasons.append('INITIAL_INVENTORY_OR_FILL_MISSING'); continue
            realized = InventoryLedger.update_from_fill(ledger, instrument, side, *values, ts)
            output.append({'event_ts': ts, 'instrument_id': instrument,
                           'qty': str(ledger[instrument]['qty']),
                           'realized_delta_usdc': str(realized) if realized is not None else None})
    return {'kind': kind, 'classification': classification, 'reason_codes': sorted(set(reasons)),
            'rows': output, 'excluded': excluded}

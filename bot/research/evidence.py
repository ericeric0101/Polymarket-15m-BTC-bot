"""Typed read-only projection of already-persisted prediction snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class MarketEvidence:
    market_slug: str
    timestamp: float
    run_id: str
    time_left_sec: float | None
    market_start_ts: float | None
    market_start_taipei: str | None
    session_regime: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any], *, run_id: str,
                      market_start_taipei: str | None, session_regime: str) -> "MarketEvidence":
        value = dict(payload)
        return cls(
            market_slug=str(value.get("market_slug") or ""),
            timestamp=float(value.get("snapshot_ts") or 0.0),
            run_id=str(run_id),
            time_left_sec=_number(value.get("time_left_sec")),
            market_start_ts=_number(value.get("market_start_ts")),
            market_start_taipei=market_start_taipei,
            session_regime=session_regime,
            payload=value,
        )

    def get(self, name: str) -> Any:
        """Expose canonical captured values without recalculation."""
        return self.payload.get(name)

    def display_fields(self) -> dict[str, Any]:
        """Safe read-only dashboard/diagnostic serialization."""
        names = ("best_bid_up", "best_ask_up", "up_mid", "spread_up", "official_twap",
                 "settlement_state_side", "required_move_sigma", "required_move_bps",
                 "p_up_ex_market", "btc_return_5s_bps", "btc_return_10s_bps",
                 "btc_return_30s_bps", "market_quote_fresh", "btc_fresh",
                 "twap_fresh", "p_ex_fresh", "joint_fresh")
        provenance = {name: value for name, value in self.payload.items()
                      if name.endswith(("_fresh", "_schema_version")) or name in
                      {"run_id", "snapshot_ts", "config_hash", "git_commit"}}
        return {"run_id": self.run_id, "snapshot_ts": self.timestamp,
                **provenance, "market_slug": self.market_slug, "time_left_sec": self.time_left_sec,
                "session_regime": self.session_regime, **{name: self.payload.get(name) for name in names}}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bounded_l2(*, bids=None, asks=None, source_ts: float | None = None, limit: int = 5) -> dict:
    """Serialize at most five canonical, already sorted book levels per side.

    A BBO is never supplied as a substitute for a book. Source time is null
    unless actually known; receive/capture time is not promoted to source time.
    """
    from itertools import islice
    from decimal import Decimal
    limit = min(5, max(1, int(limit)))
    def levels(rows, reverse):
        result = []
        for row in islice(rows if rows is not None else (), limit):
            price = getattr(row, 'price', row[0] if isinstance(row, (list, tuple)) else None)
            qty = getattr(row, 'size', row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else None)
            price = Decimal(str(price() if callable(price) else price))
            qty = Decimal(str(qty() if callable(qty) else qty))
            if not price.is_finite() or not qty.is_finite() or price <= 0 or qty <= 0:
                raise ValueError('invalid L2 level')
            result.append([float(price), float(qty)])
        if result != sorted(result, key=lambda item: item[0], reverse=reverse):
            raise ValueError('canonical book order required')
        return result
    try:
        bid, ask = levels(bids, True), levels(asks, False)
        if not bid and not ask:
            return {'status': 'L2_NOT_AVAILABLE', 'bids': None, 'asks': None, 'source_ts': source_ts}
        return {'status': 'L2_AVAILABLE', 'bids': bid or None, 'asks': ask or None,
                'source_ts': source_ts, 'depth_limit': limit, 'coverage': 'BOUNDED_LOCAL_BOOK'}
    except Exception:
        return {'status': 'L2_NOT_AVAILABLE', 'bids': None, 'asks': None, 'source_ts': source_ts,
                'reason_code': 'L2_SERIALIZATION_FAILED'}


STRATEGY_TRACE_EVENTS = frozenset({'ENTRY_DECISION_TRACE', 'SESSION_PNL_UPDATE',
    'SESSION_BUY_LOCKED', 'SESSION_PROFIT_GUARD_ARMED', 'SESSION_DAY_RESET'})


def order_trace_expected(event_type: str) -> bool:
    return event_type.endswith('_SUBMIT') or event_type == 'ORDER_FILLED'


TRACE_FIELDS = ('time_left_sec', 'required_move_sigma', 'required_move_bps', 'p_up_ex_market',
                'p_down_ex_market', 'market_mid_up', 'market_mid_down', 'best_bid_up', 'best_ask_up',
                'best_bid_down', 'best_ask_down', 'btc_return_5s_bps', 'btc_return_10s_bps',
                'btc_return_30s_bps', 'joint_fresh', 'p_ex_fresh', 'btc_fresh', 'twap_fresh',
                'market_mid_fresh', 'position_pnl', 'realized_net_usdc', 'holding_sec', 'guard_state')


def decision_projection(*, kind: str, run_id: str, slug: str, decision_ts: float,
                        side=None, decision=None, reason_code=None, lifecycle_id=None,
                        evidence: dict | None = None, l2: dict | None = None) -> dict:
    """Capture a decision already made. Never calculate policy or fill missing inputs."""
    evidence = evidence or {}
    return {'trace_schema_version': 2, 'kind': kind, 'run_id': run_id, 'market_slug': slug,
            'position_lifecycle_id': lifecycle_id, 'decision_ts': decision_ts, 'timestamp_kind': 'DECISION_TS',
            'side': side, 'decision': decision, 'reason_code': reason_code,
            'decision_ts_status': 'EXPLICIT' if decision_ts is not None else 'UNKNOWN',
            'evidence_ts': evidence.get('snapshot_ts'),
            **{key: evidence.get(key) for key in TRACE_FIELDS},
            'l2': l2 if l2 is not None else bounded_l2()}


def annotate_decision(strategy, payload: dict, *, kind: str, decision=None,
                      side=None, reason_code=None, instrument_id=None, now_ts=None,
                      source_event_type=None, client_order_id=None) -> dict:
    """Best-effort bounded annotation at an existing sparse emission boundary."""
    import time
    from bot.research.clocks import latest_evidence
    try:
        now = time.time() if now_ts is None else float(now_ts)
        slug = str(payload.get('market_slug') or payload.get('slug') or getattr(strategy, 'current_market_slug', '') or '')
        inst = str(instrument_id or payload.get('submitted_instrument_id') or payload.get('instrument_id') or getattr(strategy, 'instrument_id', '') or '')
        snapshotter = getattr(strategy, 'prediction_research_snapshotter', None)
        captured = getattr(snapshotter, '_last_payload_by_slug', {}).get(slug)
        evidence = latest_evidence([captured] if captured else [], now, max_age_sec=8) or {}
        evidence = dict(evidence)
        evidence['guard_state'] = payload.get('state')
        state = getattr(strategy, 'live_inventory_cost', {}).get(inst, {})
        if state.get('opened_ts'):
            evidence['holding_sec'] = max(0, now - float(state['opened_ts']))
        # No position/MTM authority is available at these sparse journal boundaries.
        evidence['position_pnl'] = None
        evidence['realized_net_usdc'] = payload.get('realized_net_usdc')
        book = None
        if kind in {'ENTRY_DECISION', 'EXECUTION_DECISION', 'STOP_DECISION'}:
            try:
                # Journal identity is a string; cache identity uses the existing runtime resolver.
                raw_id = instrument_id or payload.get('submitted_instrument_id') or payload.get('instrument_id') or getattr(strategy, 'instrument_id', None)
                resolver = getattr(strategy, '_normalize_instrument_id', None)
                cache_id = resolver(raw_id) if callable(resolver) else None
                if cache_id is not None:
                    book = strategy.cache.order_book(cache_id)
            except Exception:
                pass
        try:
            l2 = bounded_l2(bids=book.bids() if book is not None else None,
                            asks=book.asks() if book is not None else None)
        except Exception:
            l2 = bounded_l2()
        trace = decision_projection(kind=kind, run_id=str(getattr(strategy, 'run_id', '')),
            slug=slug, decision_ts=payload.get('decision_ts', payload.get('observed_ts')), side=side, decision=decision, reason_code=reason_code,
            lifecycle_id=payload.get('position_lifecycle_id') or state.get('position_lifecycle_id'), evidence=evidence, l2=l2)
        trace['captured_ts'] = now
        trace['capture_clock'] = 'HOST_WALL_UTC'
        if l2['status'] == 'L2_AVAILABLE':
            # Raw depth uses the existing async writer only. Never grow the
            # synchronous authoritative journal write with an L2 payload.
            evidence_id = '|'.join((trace['run_id'], str(source_event_type or kind),
                                    str(client_order_id or payload.get('research_candidate_id') or ''), f'{now:.9f}'))
            accepted = False
            try:
                writer = getattr(strategy, 'twap_research_db', None)
                if writer is not None:
                    accepted = bool(writer.enqueue_decision(run_id=trace['run_id'], slug=slug, market_id=None,
                        decision_epoch_ns=int(now * 1e9), payload={
                            'event_type': 'DECISION_POINT_L2', 'l2_schema_version': 1,
                            'l2_evidence_id': evidence_id, 'source_event_type': source_event_type or kind,
                            'market_slug': slug, 'instrument_id': inst, 'client_order_id': client_order_id,
                            'position_lifecycle_id': trace['position_lifecycle_id'],
                            'captured_ts': now, 'decision_ts': trace['decision_ts'], 'l2': l2}))
            except Exception:
                pass
            trace['l2'] = {'status': 'L2_ENQUEUED' if accepted else 'L2_NOT_PERSISTED',
                           'l2_evidence_id': evidence_id, 'bids': None, 'asks': None,
                           'reason_code': None if accepted else 'ASYNC_RESEARCH_WRITER_UNAVAILABLE'}
        # Submit/fill handlers do not provide an exact policy-trigger clock.
        # Keep it null rather than relabeling the annotation time as a trigger.
        return {**payload, 'decision_trace': trace}
    except Exception:
        return dict(payload)

"""Offline clock contracts and the shared as-of evidence selector (UTC seconds)."""
from __future__ import annotations
import math
from datetime import datetime
from typing import Any, Iterable


def epoch(value: Any) -> float | None:
    try:
        try:
            result = float(value)
        except (TypeError, ValueError):
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None  # Never depend on the machine timezone.
            result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


# Clock domains of captured timestamp fields (see reports/.../clock_domain_fix.md).
# LOCAL fields are host-clock UTC seconds and decide causal availability.
# UPSTREAM_SOURCE fields come from the venue/feed clock (Polymarket CLOB, Binance,
# Chainlink); they are diagnostic metadata and are never compared with the host clock.
LOCAL_TS_SUFFIXES = ('_received_ts', '_receive_ts', '_evidence_ts')
LOCAL_TS_NAMES = frozenset({'snapshot_ts', 'fill_ts', 'actual_stop_ts', 'decision_ts'})
UPSTREAM_SOURCE_SUFFIXES = ('_source_ts',)


def timestamp_domain(name: str) -> str:
    if name.endswith(UPSTREAM_SOURCE_SUFFIXES):
        return 'UPSTREAM_SOURCE'
    if name.endswith(('_received_ts', '_receive_ts')):
        return 'LOCAL_RECEIPT'
    if name.endswith('_evidence_ts') or name in LOCAL_TS_NAMES:
        return 'LOCAL_WALL'
    if name.endswith(('_monotonic', '_monotonic_ns', '_mono_ns')):
        return 'LOCAL_MONOTONIC'
    return 'UNKNOWN'


def available_at(row: dict, decision_ts: float, *, timestamp: str = 'snapshot_ts',
                 max_age_sec: float | None = None) -> bool:
    """A captured row is indivisible: reject it if any LOCAL input/receipt is future.

    Causal availability uses host-clock (LOCAL) timestamps only: the row's own
    timestamp, local receipt clocks and local evidence clocks must not be later
    than the decision.  Upstream *_source_ts values are a different clock domain
    (venue/feed clocks routinely lead the host by up to ~0.6 s) and are therefore
    never compared with the host-clock decision time; they stay as diagnostics
    (``source_clock_lead_sec``).  Declared clocks must be UTC seconds; nanosecond
    monotonic durations are not eligible as wall-clock evidence.  Persistence
    clocks are checked separately by journal readers.
    """
    ts = epoch(row.get(timestamp))
    decision = epoch(decision_ts)
    if ts is None or decision is None or ts > decision:
        return False
    if max_age_sec is not None and decision - ts > max_age_sec:
        return False
    def inputs_available(mapping):
        for name, value in mapping.items():
            if isinstance(value, dict):
                if not inputs_available(value):
                    return False
            elif value is not None and (name.endswith(LOCAL_TS_SUFFIXES) or name in LOCAL_TS_NAMES):
                observed = epoch(value)
                if observed is None or observed > decision:
                    return False
        return True
    return inputs_available(row)


def source_clock_lead_sec(row: dict, reference_ts: float) -> float | None:
    """Largest upstream-source lead over a LOCAL reference time (diagnostic only)."""
    ref = epoch(reference_ts)
    if ref is None:
        return None
    leads = []
    def walk(mapping):
        for name, value in mapping.items():
            if isinstance(value, dict):
                walk(value)
            elif value is not None and name.endswith(UPSTREAM_SOURCE_SUFFIXES):
                observed = epoch(value)
                if observed is not None and observed > 0:
                    leads.append(observed - ref)
    walk(row)
    return max(leads) if leads else None


def latest_evidence(rows: Iterable[dict], decision_ts: float, *, timestamp='snapshot_ts',
                    max_age_sec: float | None = None, require_joint_fresh: bool = False) -> dict | None:
    eligible = [row for row in rows if available_at(row, decision_ts, timestamp=timestamp, max_age_sec=max_age_sec)
                and (not require_joint_fresh or row.get('joint_fresh') is True)]
    return max(eligible, key=lambda row: epoch(row[timestamp])) if eligible else None


def compare_clocks(left: dict, right: dict, *, justification: str | None = None) -> dict:
    """Only identical named clocks compare automatically. Cross-domain is explicit."""
    a, b = epoch(left.get('ts')), epoch(right.get('ts'))
    valid = {'SOURCE_TS', 'RECEIVE_TS', 'DECISION_TS', 'PERSIST_TS'}
    compatible = (left.get('kind') in valid and right.get('kind') in valid and
                  (bool(left.get('clock')) and (left['kind'], left.get('clock')) == (right['kind'], right.get('clock')) or bool(justification)))
    return {'status': 'COMPARABLE' if compatible and a is not None and b is not None else 'CLOCKS_NOT_COMPARABLE',
            'delta_sec': b - a if compatible and a is not None and b is not None else None,
            'justification': justification}


def observed_source_reference(source_times: dict, receipt_times: dict, instruments: Iterable,
                              snapshot_ts: float) -> float | None:
    """Latest existing CLOB source observation, admitted by LOCAL receipt only.

    This is relative value age, not an estimate of remote current wall time.
    Source timestamps ahead of the local clock are valid metadata.
    """
    sources = []
    for instrument in instruments:
        source, receipt = epoch(source_times.get(instrument)), epoch(receipt_times.get(instrument))
        if source is not None and source > 0 and receipt is not None and 0 < receipt <= snapshot_ts:
            sources.append(source)
    return max(sources, default=None)

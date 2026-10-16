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


def available_at(row: dict, decision_ts: float, *, timestamp: str = 'snapshot_ts',
                 max_age_sec: float | None = None) -> bool:
    """A captured row is indivisible: reject it if any input/receipt is future.

    Declared source/receive clocks must be UTC seconds; nanosecond monotonic
    durations are not eligible as wall-clock evidence. Persistence clocks are
    checked separately by journal readers, not confused with source clocks.
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
            elif value is not None and (name.endswith(('_source_ts', '_received_ts', '_receive_ts', '_evidence_ts'))
                    or name in {'snapshot_ts', 'fill_ts', 'actual_stop_ts', 'decision_ts'}):
                observed = epoch(value)
                if observed is None or observed > decision:
                    return False
        return True
    return inputs_available(row)


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

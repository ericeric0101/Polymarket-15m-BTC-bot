"""Validated portable fallback for execution-cost calibration.

The snapshot is deliberately a small, versioned evidence artifact rather than
a copy of a live SQLite journal.  It allows a new host to retain the approved
D.4 economics policy without pretending it has local fill history.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional


DEFAULT_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "config" / "execution_penalty_snapshot.json"


def load_execution_penalty_snapshot(
    path: Path | str = DEFAULT_SNAPSHOT_PATH,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Return a validated, unexpired D.4 fallback or ``None``.

    Invalid, expired, or scope-mismatched artifacts never lower the live
    economics gate.  The caller can then remain blocked rather than use an
    unverifiable penalty.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != 1:
            return None
        scope = payload["scope"]
        penalty = payload["penalty"]
        evidence = payload["evidence"]
        if not all(isinstance(value, dict) for value in (scope, penalty, evidence)):
            return None
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc):
            return None
        if (
            str(scope.get("liquidity_class")) != "maker"
            or str(scope.get("side")) != "BUY"
            or int(scope.get("horizon_sec", 0)) != 10
            or int(scope.get("lookback_hours", 0)) != 168
            or int(scope.get("minimum_independent_samples", 0)) < 30
            or int(scope.get("markout_context_schema_version", 0)) != 2
        ):
            return None
        adverse = Decimal(str(penalty["adverse_markout_per_share"]))
        raw_mean = Decimal(str(penalty["raw_mean_adverse_markout_per_share"]))
        if not adverse.is_finite() or not raw_mean.is_finite() or adverse <= 0 or raw_mean <= 0:
            return None
        if int(evidence.get("settled_training_samples", 0)) < 30:
            return None
        return {
            "snapshot_id": str(payload["snapshot_id"]),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "source": "d4_portable_168h_snapshot",
            "sample_count": int(evidence["settled_training_samples"]),
            "horizon_sec": 10,
            "lookback_hours": 168.0,
            "adverse_markout_per_share": adverse,
            "raw_mean_adverse_markout_per_share": raw_mean,
            "winsor_cap_per_share": None,
            "method": str(penalty["method"]),
            "fallback_reason": "insufficient_current_journal_samples",
            "minimum_independent_samples": int(scope["minimum_independent_samples"]),
            "evidence": evidence,
        }
    except (KeyError, OSError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return None

"""DRY-RUN measurement plumbing: execution-mode tags and population separation (research only).

Pure helpers, no I/O beyond reading an env mapping, no order authority.  Every observer record
produced while the bot runs DRY-RUN carries ``execution_mode=DRY_RUN`` and
``evidence_tier=SIMULATED_ENV``; RUN_HEADER and the research policy fingerprint carry
``PLUMBING_REVISION`` so data recorded before this plumbing existed (PRE_PLUMBING) and after it
can never be merged silently.  ``check_population`` refuses to pool mixed execution modes,
plumbing revisions or policy fingerprints, and refuses non-evaluable run labels.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping, Optional

PLUMBING_REVISION = "dryrun_plumbing_v1"
PRE_PLUMBING = "PRE_PLUMBING"
EXECUTION_MODE_DRY_RUN = "DRY_RUN"
EXECUTION_MODE_LIVE = "LIVE"
EXECUTION_MODE_UNSPECIFIED = "UNSPECIFIED"
EVIDENCE_TIER_SIMULATED = "SIMULATED_ENV"
EVIDENCE_TIER_LIVE = "LIVE_VENUE"
RUN_LABEL_ENV = "RESEARCH_RUN_LABEL"
# Runs carrying these labels exist to verify plumbing; they are never part of a formal evaluation.
NON_EVALUABLE_RUN_LABELS = frozenset({"PLUMBING_SMOKE"})
_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


class PopulationMergeRefused(ValueError):
    """Records from different execution populations were offered to one evaluation."""


def execution_mode(strategy: Any) -> str:
    fn = getattr(strategy, "_is_dry_run_mode", None)
    if not callable(fn):
        return EXECUTION_MODE_UNSPECIFIED
    try:
        return EXECUTION_MODE_DRY_RUN if bool(fn()) else EXECUTION_MODE_LIVE
    except Exception:
        return EXECUTION_MODE_UNSPECIFIED


def evidence_tier(mode: str) -> Optional[str]:
    return {EXECUTION_MODE_DRY_RUN: EVIDENCE_TIER_SIMULATED, EXECUTION_MODE_LIVE: EVIDENCE_TIER_LIVE}.get(mode)


def execution_tags(strategy: Any) -> dict[str, Any]:
    mode = execution_mode(strategy)
    return {"execution_mode": mode, "evidence_tier": evidence_tier(mode), "plumbing_revision": PLUMBING_REVISION}


def run_label_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    raw = str((os.environ if env is None else env).get(RUN_LABEL_ENV, "") or "").strip()
    if not raw:
        return None
    if not _LABEL_RE.match(raw):
        raise ValueError(f"{RUN_LABEL_ENV} must match {_LABEL_RE.pattern}")
    return raw


def labelled_run_id(base_run_id: str, label: Optional[str]) -> str:
    return f"{label}_{base_run_id}" if label else base_run_id


def _header_key(h: Mapping[str, Any]) -> tuple[str, str, str]:
    fp = h.get("policy_fingerprint")
    fp_sha = (fp or {}).get("policy_fingerprint_sha256") if isinstance(fp, Mapping) else None
    return (str(h.get("execution_mode") or EXECUTION_MODE_UNSPECIFIED),
            str(h.get("plumbing_revision") or PRE_PLUMBING),
            str(fp_sha or "NONE"))


def check_population(records: Iterable[Mapping[str, Any]], *, allow_non_evaluable: bool = False) -> dict[str, Any]:
    """One execution population or PopulationMergeRefused.

    ``records`` may be RUN_HEADERs only or a whole telemetry stream; any record that carries an
    ``execution_mode`` tag (LIFECYCLE / MARKOUT / E5B labels) must agree with the headers.
    """
    headers, tagged_modes = [], set()
    for r in records:
        if r.get("record_type") == "RUN_HEADER":
            headers.append(r)
        elif r.get("execution_mode"):
            tagged_modes.add(str(r["execution_mode"]))
    keys = {_header_key(h) for h in headers}
    if len({k[0] for k in keys}) > 1:
        raise PopulationMergeRefused(f"mixed execution_mode {sorted({k[0] for k in keys})}; refusing to pool")
    if len({k[1] for k in keys}) > 1:
        raise PopulationMergeRefused(f"mixed plumbing_revision {sorted({k[1] for k in keys})}; refusing to pool")
    if len({k[2] for k in keys}) > 1:
        raise PopulationMergeRefused(f"mixed policy fingerprints {sorted({k[2] for k in keys})}; refusing to pool")
    mode = next(iter(keys))[0] if keys else None
    if tagged_modes and (len(tagged_modes) > 1 or (mode is not None and tagged_modes != {mode})):
        raise PopulationMergeRefused(f"record execution_mode {sorted(tagged_modes)} != run headers {mode}")
    labels = sorted({str(h.get("run_label")) for h in headers if h.get("run_label")})
    blocked = [lab for lab in labels if lab in NON_EVALUABLE_RUN_LABELS]
    if blocked and not allow_non_evaluable:
        raise PopulationMergeRefused(f"non-evaluable run label(s) {blocked}; plumbing runs are never evaluated")
    return {"execution_mode": mode if mode is not None else (next(iter(tagged_modes)) if tagged_modes else None),
            "plumbing_revision": next(iter(keys))[1] if keys else None,
            "policy_fingerprint_sha256": next(iter(keys))[2] if keys else None,
            "run_labels": labels, "run_headers": len(headers)}

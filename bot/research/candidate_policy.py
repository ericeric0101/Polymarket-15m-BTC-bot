"""Research-only side-by-side evaluation of CANDIDATE_ENTRY_POLICY_V1 vs the current entry policy.

No execution authority: this module never changes a decision and never touches orders. An
"opportunity" is one (market, intended side) and a record is written only when the pass/fail
state of either policy changes for it (decision transitions, not quote cycles), so evidence is
one row per decision transition and storage stays bounded (~tens of rows per market at most).

Field tiers (for storage guards): every field below is REQUIRED except those in OPTIONAL_FIELDS.
Estimated volume: <= ~1.5k rows/day x ~0.6 KB = < 1 MB/day.
"""
from __future__ import annotations

from typing import Any

POLICY_VERSION = "CANDIDATE_ENTRY_POLICY_V1"
CURRENT_POLICY_VERSION = "CURRENT_PRODUCTION"
# FROZEN research candidates chosen from prior data; not production thresholds.
SCORE_MIN = 0.30
ENTRY_PRICE_MIN = 0.75
FIRST_ENTRY_TTE_MAX_SEC = 480.0
EVENT_TYPE = "CANDIDATE_POLICY_SHADOW"
OPTIONAL_FIELDS = frozenset({"current_reason"})
MAX_TRACKED = 512


def evaluate_candidate_v1(*, score: Any, entry_price: Any, time_left_sec: Any) -> dict[str, Any]:
    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s, p, t = num(score), num(entry_price), num(time_left_sec)
    fails = []
    if s is None or abs(s) < SCORE_MIN:
        fails.append("score_below_0.30" if s is not None else "score_missing")
    if p is None or p < ENTRY_PRICE_MIN:
        fails.append("entry_price_below_0.75" if p is not None else "entry_price_missing")
    if t is None or t > FIRST_ENTRY_TTE_MAX_SEC:
        fails.append("tte_above_480" if t is not None else "tte_missing")
    return {"candidate_pass": not fails, "candidate_fail_reasons": fails,
            "score_abs": abs(s) if s is not None else None, "entry_price": p, "time_left_sec": t}


class CandidatePolicyShadow:
    def __init__(self, *, db: Any, run_id: str) -> None:
        self.db, self.run_id = db, str(run_id)
        self._last: dict[tuple[str, str], tuple[bool, bool]] = {}
        self.counters = {"observed": 0, "written": 0, "write_failures": 0}

    def observe(self, *, slug: str, intended_side: str, current_pass: bool, current_reason: str,
                score: Any, entry_price: Any, time_left_sec: Any, now_ts: float) -> dict[str, Any] | None:
        self.counters["observed"] += 1
        evaluation = evaluate_candidate_v1(score=score, entry_price=entry_price, time_left_sec=time_left_sec)
        key = (str(slug), str(intended_side).upper())
        state = (bool(current_pass), bool(evaluation["candidate_pass"]))
        if self._last.get(key) == state:
            return None
        if key not in self._last and len(self._last) >= MAX_TRACKED:
            self._last.pop(next(iter(self._last)))
        self._last[key] = state
        payload = {
            "event_type": EVENT_TYPE, "policy_version": POLICY_VERSION,
            "current_policy_version": CURRENT_POLICY_VERSION, "slug": str(slug), "intended_side": key[1],
            "observed_ts": float(now_ts), "current_pass": state[0], "current_reason": str(current_reason or ""),
            **evaluation, "agreement": state[0] == state[1],
            "authority": "research_only_no_order_or_ownership",
        }
        if self.db is not None:
            try:
                ok = self.db.enqueue_decision(run_id=self.run_id, slug=str(slug), market_id=None,
                                              decision_epoch_ns=int(float(now_ts) * 1e9), payload=payload)
                self.counters["written" if ok is not False else "write_failures"] += 1
            except Exception:
                self.counters["write_failures"] += 1
        return payload

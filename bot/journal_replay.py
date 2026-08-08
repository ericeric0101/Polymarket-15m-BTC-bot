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


@dataclass(frozen=True)
class ReplayResult:
    slug: str
    ts: str
    side: str
    entry_price: float
    outcome: str
    won: bool
    pnl_per_share: float


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
    candidates: Iterable[ReplayCandidate], outcomes_by_slug: dict[str, str]
) -> list[ReplayResult]:
    """Score binary-token entries at settlement before fees and execution effects."""
    results: list[ReplayResult] = []
    for candidate in candidates:
        outcome = str(outcomes_by_slug.get(candidate.slug, "")).upper()
        if outcome not in {"UP", "DOWN"}:
            continue
        won = candidate.side.removeprefix("BUY_") == outcome
        pnl = (1.0 - candidate.entry_price) if won else -candidate.entry_price
        results.append(
            ReplayResult(
                slug=candidate.slug,
                ts=candidate.ts,
                side=candidate.side,
                entry_price=candidate.entry_price,
                outcome=outcome,
                won=won,
                pnl_per_share=pnl,
            )
        )
    return results

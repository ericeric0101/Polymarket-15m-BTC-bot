"""Read-only threshold replay over persisted Outcome/TWAP references."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import ReferenceTick


@dataclass(frozen=True)
class ReplayConfig:
    shock_cents: int
    residual_cents: int
    debounce_ticks: int
    baseline_warmup_samples: int = 30
    horizon_ms: int = 5_000
    follower_confirm_cents: int = 100
    follower_confirm_window_ms: int = 5_000
    max_outcome_return_interval_ms: int = 6_000


def replay_rows(
    rows: Iterable[tuple[str, str, str, int, int]], config: ReplayConfig,
) -> dict[str, float | int | None]:
    """Replay persisted signal references without touching live execution.

    Rows are ``run_id, slug, source, price_cents, received_epoch_ns`` and must
    be sorted chronologically within each run/slug scope.  Only Outcome and
    settlement-TWAP references participate in the state machine.
    """
    states: dict[tuple[str, str], OutcomeLeadLagState] = {}
    pending: list[tuple[str, str, int, int, int]] = []
    moves: list[int] = []
    candidate_count = 0

    for run_id, slug, source, price_cents, received_epoch_ns in rows:
        scope = (str(run_id), str(slug))
        state = states.setdefault(scope, OutcomeLeadLagState(OutcomeLeadLagStateConfig(
            shock_cents=int(config.shock_cents),
            residual_cents=int(config.residual_cents),
            debounce_ticks=int(config.debounce_ticks),
            baseline_warmup_samples=int(config.baseline_warmup_samples),
            follower_confirm_cents=int(config.follower_confirm_cents),
            follower_confirm_window_ms=int(config.follower_confirm_window_ms),
            max_outcome_return_interval_ms=int(config.max_outcome_return_interval_ms),
        )))
        tick = ReferenceTick(
            source=source, price_cents=int(price_cents),
            received_epoch_ns=int(received_epoch_ns),
            received_monotonic_ns=int(received_epoch_ns), run_id=scope[0], slug=scope[1],
        )
        decision = state.apply(tick)
        if source == "polymarket_twap":
            remaining: list[tuple[str, str, int, int, int]] = []
            for pending_run, pending_slug, direction, baseline, created_ns in pending:
                if (pending_run, pending_slug) != scope:
                    remaining.append((pending_run, pending_slug, direction, baseline, created_ns))
                    continue
                elapsed_ms = (int(received_epoch_ns) - created_ns) // 1_000_000
                if elapsed_ms >= config.horizon_ms:
                    moves.append((int(price_cents) - baseline) * direction)
                else:
                    remaining.append((pending_run, pending_slug, direction, baseline, created_ns))
            pending = remaining
        if decision.state == "adverse_confirmed" and decision.follower_price_cents is not None:
            candidate_count += 1
            pending.append((scope[0], scope[1], decision.direction,
                            int(decision.follower_price_cents), int(received_epoch_ns)))

    return {
        "candidate_count": candidate_count,
        "observed_markout_count": len(moves),
        "direction_hit_rate": (
            sum(move > 0 for move in moves) / len(moves) if moves else None
        ),
        "mean_signed_twap_move_usd": (
            sum(moves) / len(moves) / 100 if moves else None
        ),
        "mean_abs_twap_move_usd": (
            sum(abs(move) for move in moves) / len(moves) / 100 if moves else None
        ),
    }

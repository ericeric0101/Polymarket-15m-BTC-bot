"""Shadow-only candidate and micro-markout recorder; it cannot trade."""
from __future__ import annotations

import time
from decimal import Decimal


class OutcomeLeadLagShadow:
    HORIZONS_MS = (250, 1_000, 5_000, 10_000, 30_000, 60_000)

    def __init__(self, strategy) -> None:
        self.strategy, self.pending = strategy, []

    def record_candidate(self, candidate) -> None:
        held = str(getattr(self.strategy, "active_side", "NONE"))
        direction = int(candidate.decision.direction)
        supports = (held.endswith("UP") and direction > 0) or (held.endswith("DOWN") and direction < 0)
        self.strategy.lead_lag_db.enqueue_decision(
            run_id=candidate.run_id, slug=candidate.slug, market_id=candidate.market_id,
            decision_epoch_ns=time.time_ns(), payload={
                "kind": "shadow_candidate", "intent": "supports_position" if supports else "adverse_candidate",
                "execution_blocked": True, "decision": candidate.decision.__dict__, "held_side": held,
            },
        )
        try:
            baseline = int((Decimal(str(getattr(self.strategy, "_polymarket_chainlink_twap_price", 0))) * 100).to_integral_value())
        except Exception:
            baseline = None
        self.pending.append((candidate, baseline if baseline and baseline > 0 else None, set()))

    def on_tick(self, tick, _decision) -> None:
        if tick.source != "polymarket_twap":
            return
        updated = []
        for candidate, baseline, written in self.pending:
            baseline = tick.price_cents if baseline is None else baseline
            elapsed_ms = (tick.received_epoch_ns - candidate.created_epoch_ns) // 1_000_000
            for horizon in self.HORIZONS_MS:
                if horizon not in written and elapsed_ms >= horizon:
                    self.strategy.lead_lag_db.enqueue_markout(
                        run_id=candidate.run_id, slug=candidate.slug, market_id=candidate.market_id,
                        candidate_epoch_ns=candidate.created_epoch_ns, horizon_ms=horizon,
                        observed_epoch_ns=tick.received_epoch_ns,
                        payload={"execution_blocked": True, "baseline_twap_cents": baseline,
                                 "markout_twap_cents": tick.price_cents,
                                 "twap_change_cents": tick.price_cents - baseline,
                                 "decision": candidate.decision.__dict__},
                    )
                    written.add(horizon)
            if len(written) < len(self.HORIZONS_MS):
                updated.append((candidate, baseline, written))
        self.pending = updated

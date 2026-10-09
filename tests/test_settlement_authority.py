"""Settlement authority: the canonical official-TWAP label, never the latest spot (hermetic)."""
import ast
import time
from decimal import Decimal
from pathlib import Path

import pytest

from bot.enums import ActiveSide
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.post_trade import compute_settlement_summary
from bot.shadow_simulation import ShadowSimulationMixin

SLUG = "btc-updown-15m-1791478800"
STRIKE = 80000.0
UP_INST = "up-inst"


class Host(ShadowSimulationMixin, StrategyLifecycleMixin):
    """Real settlement + shadow-simulation code; journal/guards are recorded in memory."""

    def __init__(self, *, twap, spot, twap_age=1.0, window=60, qty=Decimal("0"), entry=Decimal("0.75")):
        self.current_market_slug = SLUG
        self.market_strike_cache_by_slug = {SLUG: Decimal(str(STRIKE))}
        self.latest_external_spot = Decimal(str(spot))
        self.last_external_spot = None
        self.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
        self._polymarket_chainlink_twap_price = twap
        self._polymarket_chainlink_twap_observation_ts = None if twap is None else time.time() - twap_age
        self._polymarket_chainlink_twap_window_sec = window
        self.inventory_delta_shares = qty
        self.live_inventory_cost = ({UP_INST: {"qty": qty, "avg_entry_price": entry}} if qty > 0 else {})
        self.market_cycle_realized_net_usdc = Decimal("0")
        self.recent_market_combined_pnls = []
        self.active_side = ActiveSide.UP
        self._cycle_total_trades = self._cycle_total_wins = 0
        self.terminal_dashboard = None
        self.events, self.order_events, self.session_pnl, self.regime = [], [], [], []
        self.shadow_simulation_enabled = True
        self.trade_db = object()
        self.test_mode = True
        self._shadow_state = {SLUG: {"simulation_id": "sim", "slug": SLUG, "side": "UP", "status": "FILLED",
                                     "entry_price": 0.75, "qty": 4.0, "instrument_id": UP_INST}}

    # --- collaborators -------------------------------------------------------------------------
    def _is_dry_run_mode(self):
        return True

    def _side_for_instrument_id(self, inst):
        return ActiveSide.UP if str(inst) == UP_INST else ActiveSide.DOWN

    def _load_shadow_simulation_for_slug(self, slug):
        return self._shadow_state.get(slug)

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def _db_order_event(self, **event):
        self.order_events.append(event)

    def _record_session_realized_pnl(self, delta, *, source):
        self.session_pnl.append((Decimal(str(delta)), source))
        return True

    def _append_cycle_and_maybe_trigger_regime_guard(self, **kwargs):
        self.regime.append(kwargs)

    def _update_terminal_dashboard_snapshot(self):
        return None

    def settle(self):
        self._record_market_settlement()
        return self

    def event(self, event_type):
        return next(p for e, p in self.events if e == event_type)


def test_spot_and_twap_disagree_settlement_uses_canonical_twap():
    host = Host(twap=STRIKE - 5, spot=STRIKE + 48).settle()  # spot says UP, official TWAP says DOWN
    settlement = host.event("MARKET_SETTLEMENT")
    assert settlement["outcome"] == "DOWN" and settlement["outcome_source"] == "canonical_twap"
    assert settlement["latest_spot_diagnostic"] == STRIKE + 48


def test_spot_crossing_after_the_window_does_not_change_the_outcome():
    first = Host(twap=STRIKE + 3, spot=STRIKE + 3).settle().event("MARKET_SETTLEMENT")["outcome"]
    later = Host(twap=STRIKE + 3, spot=STRIKE - 500).settle().event("MARKET_SETTLEMENT")["outcome"]
    assert first == later == "UP"


@pytest.mark.parametrize("twap,expected", [
    (STRIKE, "UP"),                 # exact tie settles UP (>=)
    (STRIKE + 1e-9, "UP"),
    (STRIKE - 1e-6, "DOWN"),
    (80000.00000000001, "UP"),      # float rounding is deterministic: same input, same label
])
def test_exact_strike_and_rounding_semantics_are_deterministic(twap, expected):
    labels = {Host(twap=twap, spot=0.0).settle().event("MARKET_SETTLEMENT")["outcome"] for _ in range(3)}
    assert labels == {expected}


def test_cycle_pnl_uses_the_corrected_outcome():
    host = Host(twap=STRIKE - 5, spot=STRIKE + 48, qty=Decimal("4")).settle()
    cycle = host.event("MARKET_CYCLE_PNL")
    assert host.event("MARKET_SETTLEMENT")["outcome"] == "DOWN"
    assert cycle["cycle_settlement_pnl_usdc"] == pytest.approx(-3.0)   # held UP lost: 0 - 4*0.75
    assert host.regime[0]["cycle_combined_pnl"] == pytest.approx(-3.0)


def test_session_pnl_guard_uses_the_corrected_outcome():
    host = Host(twap=STRIKE - 5, spot=STRIKE + 48, qty=Decimal("4")).settle()
    assert host.session_pnl == [(Decimal("-3.0"), "settlement")]


def test_shadow_settlement_uses_the_corrected_outcome():
    host = Host(twap=STRIKE - 5, spot=STRIKE + 48).settle()
    settled = next(e for e in host.order_events if e["event_type"] == "SHADOW_SIM_SETTLED")
    assert settled["payload"]["outcome"] == "DOWN" and settled["payload"]["won"] is False


def test_no_consumer_derives_settlement_from_latest_spot():
    def offending(path):
        tree = ast.parse(Path(path).read_text())
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Compare):
                names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
                if "spot" in names and "strike" in names:
                    hits.append(node.lineno)
        return hits
    for path in ("bot/lifecycle_runtime.py", "bot/shadow_simulation.py", "bot/post_trade.py"):
        assert offending(path) == [], path
    with pytest.raises(TypeError):
        compute_settlement_summary(spot=1.0, strike=0.5, inventory_shares=1.0, live_inventory_cost={},
                                   market_cycle_realized_net_usdc=Decimal("0"))


@pytest.mark.parametrize("kwargs", [
    {"twap": None},                 # no canonical TWAP tick
    {"twap": STRIKE + 5, "twap_age": 120.0},  # stale tick
    {"twap": STRIKE + 5, "window": 30},       # wrong settlement window
])
def test_missing_or_degraded_twap_is_unknown_without_spot_fallback(kwargs):
    host = Host(spot=STRIKE + 48, qty=Decimal("4"), **kwargs).settle()
    settlement = host.event("MARKET_SETTLEMENT")
    assert settlement["outcome"] == "UNKNOWN" and settlement["settlement_pending"] is True
    assert settlement["outcome_source"] == "unavailable"
    # No cycle PnL / guard update: startup reconciliation resolves it from the official result later.
    assert not [e for e, _ in host.events if e == "MARKET_CYCLE_PNL"]
    assert host.session_pnl == [] and host.regime == []
    assert not [e for e in host.order_events if e["event_type"] == "SHADOW_SIM_SETTLED"]

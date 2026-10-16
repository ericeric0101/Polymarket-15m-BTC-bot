from decimal import Decimal
import json
import sqlite3

from bot.depth_risk import cap_buy_quantity, simulate_large_order_ladder
from bot.depth_risk_shadow import DepthRiskShadowMixin
from scripts.depth_risk_shadow_report import build_report


def test_buy_cap_is_minimum_of_risk_depth_and_inventory():
    decision = cap_buy_quantity(
        entry_price=Decimal("0.50"), tick_size=Decimal("0.01"),
        asks=[(Decimal("0.50"), Decimal("100")), (Decimal("0.51"), Decimal("100"))],
        max_entry_notional_usdc=Decimal("10"), max_loss_usdc=Decimal("8"),
        depth_fraction=Decimal("0.10"), boundary_ticks=1,
        inventory_headroom=Decimal("15"),
    )
    assert decision.quantity == Decimal("15")
    assert decision.limiting_factor == "inventory"
    assert decision.risk_notional_quantity == Decimal("20")
    assert decision.risk_loss_quantity == Decimal("16")
    assert decision.depth_quantity == Decimal("20.00")


def test_buy_cap_fails_closed_when_l2_is_missing():
    decision = cap_buy_quantity(
        entry_price=Decimal("0.50"), tick_size=Decimal("0.01"), asks=None,
        max_entry_notional_usdc=Decimal("10"), max_loss_usdc=Decimal("10"),
        depth_fraction=Decimal("0.10"), boundary_ticks=1,
        inventory_headroom=Decimal("10"),
    )
    assert decision.quantity == 0
    assert decision.limiting_factor == "missing_l2"
    assert not decision.valid_l2


def test_ladder_exposes_partial_exit_and_immediate_round_trip_markout():
    rows = simulate_large_order_ladder(
        asks=[(Decimal("0.50"), Decimal("10")), (Decimal("0.51"), Decimal("20"))],
        bids=[(Decimal("0.49"), Decimal("20")), (Decimal("0.48"), Decimal("20"))],
        tick_size=Decimal("0.01"), boundary_ticks=2, quantities=[Decimal("25")],
    )
    assert len(rows) == 1
    assert rows[0]["entry"].filled_quantity == Decimal("25")
    assert rows[0]["entry"].fill_rate == Decimal("1")
    assert rows[0]["exit"].filled_quantity == Decimal("20")
    assert rows[0]["exit"].fill_rate == Decimal("0.8")
    assert rows[0]["immediate_round_trip_markout_usdc"] < 0


class _DepthShadowHost(DepthRiskShadowMixin):
    def __init__(self):
        self.depth_risk_shadow_enabled = True
        self.depth_risk_shadow_interval_sec = 1
        self.depth_risk_price_boundary_ticks = 2
        self.trade_db = object()
        self.current_market_slug = "btc-updown-test"
        self.events = []
        self._depth_risk_shadow_states = {}
        self._depth_risk_shadow_last_ts_by_inst = {}

    class _Side:
        value = "UP"

    def _side_for_instrument_id(self, _instrument_id):
        return self._Side()

    def _db_order_event(self, **kwargs):
        self.events.append(kwargs)


def test_shadow_records_ladder_and_later_executable_markouts():
    host = _DepthShadowHost()
    host._record_depth_risk_shadow(
        instrument_id="UP", tick_size=Decimal("0.01"), now_ts=100.0,
        asks=[(Decimal("0.50"), Decimal("300"))],
        bids=[(Decimal("0.49"), Decimal("300"))],
    )
    assert len(host.events) == 5
    host._depth_risk_shadow_on_quote("UP", Decimal("0.48"), Decimal("0.49"), 105.0)
    assert len(host.events) == 10
    assert {event["event_type"] for event in host.events} == {
        "DEPTH_RISK_SHADOW_CANDIDATE", "DEPTH_RISK_SHADOW_MARKOUT"
    }


def test_report_groups_candidate_liquidity_and_markout_by_size_and_horizon(tmp_path):
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE order_events (event_type TEXT, payload_json TEXT)")
    conn.execute(
        "INSERT INTO order_events VALUES (?, ?)",
        ("DEPTH_RISK_SHADOW_CANDIDATE", json.dumps({
            "simulation_id": "one", "requested_quantity": 25,
            "entry": {"fill_rate": 1, "slippage_per_share": 0.002},
            "exit": {"fill_rate": 0.8}, "immediate_round_trip_markout_usdc": -0.3,
        })),
    )
    conn.execute(
        "INSERT INTO order_events VALUES (?, ?)",
        ("DEPTH_RISK_SHADOW_MARKOUT", json.dumps({
            "simulation_id": "one", "requested_quantity": 25,
            "markout_horizon_sec": 10, "markout_usdc": -0.2,
        })),
    )
    conn.commit()
    conn.close()
    rows = build_report(str(db_path))
    assert rows[0]["shares"] == 25
    assert rows[0]["mean_entry_fill_rate"] == 1
    assert rows[1]["horizon_sec"] == 10
    assert rows[1]["mean_bbo_markout_usdc"] == -0.2

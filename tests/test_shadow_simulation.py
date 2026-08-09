from decimal import Decimal
from types import SimpleNamespace
import time

from bot.enums import ActiveSide
from bot.shadow_simulation import ShadowSimulationMixin
from monitoring.trade_journal_db import TradeJournalDB


class _ShadowHost(ShadowSimulationMixin):
    def __init__(self, db):
        self.trade_db = db
        self.run_id = "shadow-test"
        self.current_market_slug = "btc-updown-15m-test"
        self.shadow_simulation_enabled = True
        self.fair_edge_bucket_shadow_enabled = True
        self.shadow_simulation_fill_timeout_sec = 90.0
        self.shadow_simulation_max_quote_age_sec = 2.0
        self.shadow_simulation_aged_quote_max_age_sec = 30.0
        self._shadow_simulations_by_slug = {}
        self._fair_edge_bucket_shadow_by_id = {}
        self.side_decision_score = Decimal("0.42")
        self.side_decision_reason = "test_signal"
        self.latest_external_spot = Decimal("100")
        self.market_strike_cache_by_slug = {self.current_market_slug: Decimal("99")}
        now_ts = time.time()
        self.current_market_end_timestamp = now_ts + 600.0
        self.last_quote_update_ts_by_inst = {"inst-up": now_ts}
        self.last_quote_received_ts_by_inst = {"inst-up": now_ts}
        self._quote = (Decimal("0.69"), Decimal("0.70"))

    def _is_dry_run_mode(self):
        return True

    def _get_quote_for_instrument(self, _instrument_id):
        return self._quote

    def _side_for_instrument_id(self, _instrument_id):
        return ActiveSide.UP

    def _db_order_event(self, event_type, **kwargs):
        self.trade_db.log_order_event(
            run_id=self.run_id,
            event_type=event_type,
            **kwargs,
        )

    def _db_strategy_event(self, event_type, payload):
        self.trade_db.log_strategy_event(self.run_id, event_type, payload)


def _event_count(db, event_type):
    with db._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM order_events WHERE event_type=?", (event_type,)
        ).fetchone()[0]


def test_shadow_simulation_is_one_per_market_and_settles(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    host = _ShadowHost(db)
    econ = SimpleNamespace(
        expected_net_usdc=Decimal("0.12"),
        expected_rebate_usdc=Decimal("0.03"),
    )
    snapshot = {
        "p_fair": Decimal("0.76"),
        "directional_edge_ps": Decimal("0.08"),
        "robust_net_usdc": Decimal("0.12"),
        "planned_best_bid": Decimal("0.68"),
        "planned_best_ask": Decimal("0.69"),
        "planned_quote_ts": time.time(),
        "entry_mode": "value",
    }
    host.last_quote_update_ts_by_inst["inst-up"] = time.time()
    host.last_quote_received_ts_by_inst["inst-up"] = time.time()

    host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=econ,
        directional_snapshot=snapshot,
    )
    host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.67"),
        qty=Decimal("5"),
        econ=econ,
        directional_snapshot=snapshot,
    )
    assert _event_count(db, "SHADOW_SIM_ENTRY_CANDIDATE") == 1
    state = host._shadow_simulations_by_slug[host.current_market_slug]
    assert state["fair"] == 0.76
    assert state["time_left_sec"] is not None
    assert state["quote_age_sec"] <= 2.0

    # The passive limit has not become executable yet.
    host.last_quote_update_ts_by_inst["inst-up"] = state["created_ts"] + 1.0
    host.last_quote_received_ts_by_inst["inst-up"] = state["created_ts"] + 1.0
    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.69"), Decimal("0.70"), state["created_ts"] + 1.0
    )
    assert _event_count(db, "SHADOW_SIM_ENTRY_FILLED") == 0

    fill_ts = state["created_ts"] + 2.0
    host.last_quote_update_ts_by_inst["inst-up"] = fill_ts
    host.last_quote_received_ts_by_inst["inst-up"] = fill_ts
    host._shadow_simulation_on_quote("inst-up", Decimal("0.67"), Decimal("0.68"), fill_ts)
    assert _event_count(db, "SHADOW_SIM_ENTRY_FILLED") == 1

    host.last_quote_update_ts_by_inst["inst-up"] = fill_ts + 31.0
    host.last_quote_received_ts_by_inst["inst-up"] = fill_ts + 31.0
    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.75"), Decimal("0.76"), fill_ts + 31.0
    )
    host.last_quote_update_ts_by_inst["inst-up"] = fill_ts + 61.0
    host.last_quote_received_ts_by_inst["inst-up"] = fill_ts + 61.0
    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.77"), Decimal("0.78"), fill_ts + 61.0
    )
    assert _event_count(db, "SHADOW_SIM_MARKOUT") == 2

    host._settle_shadow_simulation(
        slug=host.current_market_slug,
        spot=100.0,
        strike=99.0,
    )
    settled = db.load_shadow_simulation(host.current_market_slug)
    assert settled is not None
    assert settled["status"] == "SETTLED"
    assert settled["won"] is True
    assert round(settled["simulated_gross_pnl_usdc"], 2) == 1.6
    assert round(settled["simulated_expected_rebate_usdc"], 2) == 0.03
    assert round(settled["simulated_pnl_usdc"], 2) == 1.63


def test_shadow_simulation_collects_stale_instrument_quote_as_directional_sample(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    host = _ShadowHost(db)
    host.last_quote_update_ts_by_inst["inst-up"] = time.time() - 30.1
    host.last_quote_received_ts_by_inst["inst-up"] = time.time() - 30.1

    host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=SimpleNamespace(expected_net_usdc=Decimal("0.12")),
        directional_snapshot={"p_fair": Decimal("0.76")},
    )

    assert _event_count(db, "SHADOW_SIM_ENTRY_CANDIDATE") == 1
    state = host._shadow_simulations_by_slug[host.current_market_slug]
    assert state["quote_freshness_tier"] == "STALE"
    assert state["executable_quote_sample"] is False


def test_shadow_simulation_restores_filled_state_after_restart_and_settles(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    first_host = _ShadowHost(db)
    first_host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=SimpleNamespace(expected_net_usdc=Decimal("0.12")),
        directional_snapshot={"p_fair": Decimal("0.76")},
    )
    state = first_host._shadow_simulations_by_slug[first_host.current_market_slug]
    fill_ts = state["created_ts"] + 1.0
    first_host.last_quote_received_ts_by_inst["inst-up"] = fill_ts
    first_host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.67"), Decimal("0.68"), fill_ts
    )
    assert _event_count(db, "SHADOW_SIM_ENTRY_FILLED") == 1

    restarted_host = _ShadowHost(db)
    restored = restarted_host._restore_shadow_simulation_for_slug(
        restarted_host.current_market_slug
    )
    assert restored is not None
    assert restored["status"] == "FILLED"

    restarted_host._settle_shadow_simulation(
        slug=restarted_host.current_market_slug,
        spot=100.0,
        strike=99.0,
    )
    settled = db.load_shadow_simulation(restarted_host.current_market_slug)
    assert settled is not None
    assert settled["status"] == "SETTLED"
    assert _event_count(db, "SHADOW_SIM_SETTLED") == 1


def test_fair_edge_bucket_shadow_requires_passive_fill_and_settles(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    host = _ShadowHost(db)
    host._record_fair_edge_bucket_shadow_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=SimpleNamespace(expected_net_usdc=Decimal("0.12")),
        directional_snapshot={"p_fair": Decimal("0.64")},
        bucket="neg_0_05_to_neg_0_02",
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_CANDIDATE") == 1

    state = next(iter(host._fair_edge_bucket_shadow_by_id.values()))
    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.69"), Decimal("0.70"), state["created_ts"] + 1
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_FILLED") == 0

    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.67"), Decimal("0.68"), state["created_ts"] + 2
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_FILLED") == 1

    host._settle_shadow_simulation(
        slug=host.current_market_slug,
        spot=100.0,
        strike=99.0,
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_SETTLED") == 1


def test_fair_edge_bucket_shadow_restores_after_restart(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    first_host = _ShadowHost(db)
    first_host._record_fair_edge_bucket_shadow_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=SimpleNamespace(expected_net_usdc=Decimal("0.12")),
        directional_snapshot={"p_fair": Decimal("0.64")},
        bucket="neg_0_05_to_neg_0_02",
    )
    state = next(iter(first_host._fair_edge_bucket_shadow_by_id.values()))
    first_host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.67"), Decimal("0.68"), state["created_ts"] + 1
    )

    restarted_host = _ShadowHost(db)
    restarted_host._restore_shadow_simulation_for_slug(restarted_host.current_market_slug)
    restored = next(iter(restarted_host._fair_edge_bucket_shadow_by_id.values()))
    assert restored["status"] == "FILLED"

    restarted_host._settle_shadow_simulation(
        slug=restarted_host.current_market_slug, spot=100.0, strike=99.0
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_SETTLED") == 1

from decimal import Decimal
from types import SimpleNamespace
import time

from bot.enums import ActiveSide
from bot.order_runtime import OrderRuntimeMixin
from bot.order_submission import submit_maker_quote
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
        self.active_maker_orders = {}
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


class _DryOrderHost(_ShadowHost, OrderRuntimeMixin):
    def __init__(self, db):
        super().__init__(db)
        self.instrument = SimpleNamespace(size_precision=6, price_precision=3)
        self.maker_use_post_only = False
        self.maker_post_only_strict = False
        self.maker_min_shares = Decimal("5")
        self.maker_exchange_min_shares = Decimal("5")
        self.continuation_entry_size_multiplier = Decimal("1")
        self.stop_loss_reentry_pause_until_by_inst = {}
        self.inventory_delta_shares = Decimal("0")
        self.maker_max_inventory_shares = Decimal("10")
        self._sell_recovery_required_by_inst = {}
        self._sell_recovery_reason_by_inst = {}
        self._sell_recovery_venue_cap_by_inst = {}
        self.maker_order_ttl_sec = 20
        self.maker_cancel_cooldown_sec = 2
        self.maker_cancel_ack_timeout_sec = 8
        self.maker_cancel_max_retries = 3
        self.maker_error_pause_sec = 30
        self.quote_pause_until_ts = 0.0
        self.rebate_reporter = SimpleNamespace(record_cancel=lambda _reason: None)

    @property
    def cache(self):
        return SimpleNamespace(instrument=lambda _instrument_id: self.instrument)

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _align_price_to_tick(self, price, _side, _instrument):
        return price

    def _should_skip_buy_submit_for_quote_drift(self, **_kwargs):
        return False

    def _compute_maker_order_qty(self, _price, _precision):
        return Decimal("5")

    def _instrument_key(self, instrument_id):
        return str(instrument_id)

    def _project_inventory_after_fill(self, side, qty, instrument_id=None):
        return self.inventory_delta_shares + qty if side == "buy" else self.inventory_delta_shares - qty

    def _extract_token_id_from_instrument(self, _instrument_id):
        return "token"

    def _order_key_for(self, side, instrument_id):
        return f"{side}:{instrument_id}"


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
    assert _event_count(db, "SHADOW_SIM_ENTRY_REQUOTED") == 1
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
    # Requotes replace the previous limit: the updated 0.67 quote cannot
    # claim a fill until the later ask reaches 0.67 too.
    host._shadow_simulation_on_quote("inst-up", Decimal("0.66"), Decimal("0.67"), fill_ts)
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
    assert round(settled["simulated_gross_pnl_usdc"], 2) == 1.65
    assert round(settled["simulated_expected_rebate_usdc"], 2) == 0.03
    assert round(settled["simulated_pnl_usdc"], 2) == 1.68


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


def test_shadow_simulation_cancel_prevents_fill_until_live_style_requote(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    host = _ShadowHost(db)
    econ = SimpleNamespace(expected_net_usdc=Decimal("0.12"))
    host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.68"),
        qty=Decimal("5"),
        econ=econ,
        directional_snapshot={"p_fair": Decimal("0.76")},
        target_version=1,
        order_key="buy:inst-up",
    )
    state = host._shadow_simulations_by_slug[host.current_market_slug]
    host._shadow_simulation_on_order_cancel(order_key="buy:inst-up", reason="requote")
    assert state["status"] == "CANCELED"

    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.67"), Decimal("0.68"), state["created_ts"] + 1.0
    )
    assert _event_count(db, "SHADOW_SIM_ENTRY_FILLED") == 0

    host._record_shadow_simulated_entry(
        instrument_id="inst-up",
        limit_price=Decimal("0.67"),
        qty=Decimal("5"),
        econ=econ,
        directional_snapshot={"p_fair": Decimal("0.76")},
        target_version=2,
        order_key="buy:inst-up",
    )
    requoted = host._shadow_simulations_by_slug[host.current_market_slug]
    host._shadow_simulation_on_quote(
        "inst-up", Decimal("0.66"), Decimal("0.67"), requoted["created_ts"] + 1.0
    )
    assert _event_count(db, "SHADOW_SIM_ENTRY_FILLED") == 1


def test_dry_run_submit_uses_active_order_lifecycle_and_local_cancel(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    host = _DryOrderHost(db)
    econ = SimpleNamespace(expected_net_usdc=Decimal("0.12"), expected_rebate_usdc=Decimal("0"))

    submit_maker_quote(
        host,
        instrument_id="inst-up",
        side="buy",
        limit_price=Decimal("0.68"),
        econ=econ,
        directional_snapshot={"p_fair": Decimal("0.76")},
        target_version=1,
    )

    key = "buy:inst-up"
    assert host.active_maker_orders[key]["dry_run_simulated"] is True
    assert _event_count(db, "ORDER_DRY_RUN_SUBMITTED") == 1
    created_ts = host.active_maker_orders[key]["created_ts"]
    assert host._is_order_ttl_expired(key, created_ts + 19.9) is False
    assert host._is_order_ttl_expired(key, created_ts + 20.0) is True
    host._cancel_maker_order_key(key, reason="requote")
    assert key not in host.active_maker_orders
    assert _event_count(db, "ORDER_DRY_RUN_CANCELLED") == 1
    assert _event_count(db, "SHADOW_SIM_ENTRY_CANCELLED") == 1


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
        directional_snapshot={
            "p_fair": Decimal("0.64"),
            "fee_ps": Decimal("0.01"),
            "other_cost_ps": Decimal("0.02"),
            "exec_penalty_usdc": Decimal("0.15"),
            "execution_penalty_components": {"vwap": Decimal("0.10")},
        },
        bucket="neg_0_05_to_neg_0_02",
    )
    assert _event_count(db, "FAIR_EDGE_BUCKET_SHADOW_CANDIDATE") == 1

    state = next(iter(host._fair_edge_bucket_shadow_by_id.values()))
    assert state["fee_ps"] == 0.01
    assert state["other_cost_ps"] == 0.02
    assert state["exec_penalty_usdc"] == 0.15
    assert state["execution_penalty_components"] == {"vwap": 0.1}
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

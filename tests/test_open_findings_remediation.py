"""Regression tests for the audit findings left open after b9b3f8b (hermetic; tmp paths only)."""
import math
import sqlite3
import subprocess
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.order_runtime import OrderRuntimeMixin


# --- P2-006: repaired BBO never prices a BUY --------------------------------------------------

class _QuoteHost:
    def __init__(self, bid, ask):
        price = lambda value: None if value is None else SimpleNamespace(as_decimal=lambda: Decimal(value))
        self.cache = SimpleNamespace(quote_tick=lambda _inst: SimpleNamespace(bid_price=price(bid), ask_price=price(ask)))

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id


@pytest.mark.parametrize("bid,ask,reason", [
    (None, "0.55", "missing_bid"), ("0.55", None, "missing_ask"), ("0.60", "0.50", "crossed_book"), ("0.50", "0.52", None),
])
def test_quote_repair_is_recorded_per_instrument(bid, ask, reason):
    host = _QuoteHost(bid, ask)
    assert OrderRuntimeMixin._get_quote_for_instrument(host, "inst") is not None
    assert OrderRuntimeMixin._quote_synthesis_reason(host, "inst") == reason


def test_real_quote_clears_previous_repair_flag():
    host = _QuoteHost(None, "0.55")
    OrderRuntimeMixin._get_quote_for_instrument(host, "inst")
    host.cache = _QuoteHost("0.50", "0.52").cache
    OrderRuntimeMixin._get_quote_for_instrument(host, "inst")
    assert OrderRuntimeMixin._quote_synthesis_reason(host, "inst") is None


def _econ():
    return SimpleNamespace(expected_net_usdc=Decimal(".02"), expected_rebate_usdc=Decimal("0"),
                           expected_spread_capture_usdc=Decimal("0"), fee_equivalent_usdc=Decimal("0"))


def test_buy_is_not_submitted_from_a_repaired_quote_but_sell_is_unaffected():
    from bot.order_submission import submit_maker_quote
    from test_live_path_regressions import DummyTrendSubmitStrategy

    events = []
    host = DummyTrendSubmitStrategy()
    host._get_quote_for_instrument = lambda _inst: (Decimal("0.59"), Decimal("0.60"))
    host._quote_synthesis_reason = lambda _inst: "crossed_book"
    original_event = host._db_order_event
    host._db_order_event = lambda **event: (events.append(event), original_event(**event))[1]
    submit_maker_quote(host, instrument_id="inst-up", side="buy", limit_price=Decimal(".58"), econ=_econ())
    assert host.submitted_orders == []
    assert [e["event_type"] for e in events] == ["ORDER_SKIP_UNREAL_QUOTE"]
    assert events[0]["reason"] == "crossed_book"

    seller = DummyTrendSubmitStrategy()
    seller.inventory_delta_shares = Decimal("6")
    seller._quote_synthesis_reason = lambda _inst: "missing_bid"
    submit_maker_quote(seller, instrument_id="inst-up", side="sell", limit_price=Decimal(".6"), econ=_econ())
    assert seller.submitted_orders


# --- P2-007: client order ids ------------------------------------------------------------------

def test_client_order_ids_are_unique_within_one_millisecond(monkeypatch):
    from bot import order_ids
    monkeypatch.setattr(order_ids.time, "time", lambda: 1000.0)
    ids = {str(order_ids.new_client_order_id("maker-buy")) for _ in range(500)}
    assert len(ids) == 500
    assert all(item.startswith("BTC-15M-MAKER-BUY-1000000-") for item in ids)


def test_every_submit_site_uses_the_unique_id_factory():
    from pathlib import Path
    for path in ("bot/order_submission.py", "bot/taker_exit.py"):
        source = Path(path).read_text()
        assert 'ClientOrderId(f"BTC-15M-' not in source
        assert "new_client_order_id(" in source


# --- S-2: orphan venue orders and unresolved BUY intents ---------------------------------------

class _OrphanHost:
    def __init__(self, *, test_mode, orders):
        from bot.recovery import StrategyRecoveryMixin
        self._cancel = StrategyRecoveryMixin._cancel_orphan_venue_orders_on_startup
        self.test_mode = test_mode
        self._is_dry_run_mode = lambda: test_mode
        slugs = {"up": "btc-updown-15m-1791386100", "other": "eth-updown-15m-1791386100"}
        self.cache = SimpleNamespace(
            orders_open=lambda: orders,
            instrument=lambda inst: SimpleNamespace(info={"market_slug": slugs[inst]}),
        )
        tracked = orders[0]
        self.active_maker_orders = {"buy:up": {"order": tracked}}
        self.cancelled, self.events = [], []

    def _extract_market_slug_from_instrument(self, instrument):
        return instrument.info["market_slug"]

    def cancel_order(self, order):
        self.cancelled.append(str(order.client_order_id))

    def _db_order_event(self, **event):
        self.events.append(event)

    def run(self):
        return self._cancel(self)


def _orders():
    make = lambda coid, inst: SimpleNamespace(client_order_id=coid, instrument_id=inst, side=None, quantity=5)
    return [make("BTC-15M-MAKER-BUY-tracked", "up"), make("O-20261008-RECONCILED", "up"), make("O-other", "other")]


def test_live_startup_cancels_only_untracked_btc15m_orders_regardless_of_id_prefix():
    host = _OrphanHost(test_mode=False, orders=_orders())
    assert host.run() == 1
    assert host.cancelled == ["O-20261008-RECONCILED"]
    assert host.events[0]["event_type"] == "ORDER_ORPHAN_CANCEL_ON_START"


def test_dry_run_startup_never_cancels_venue_orders():
    host = _OrphanHost(test_mode=True, orders=_orders())
    assert host.run() == 0
    assert host.cancelled == []


def test_unresolved_buy_intent_consumes_market_buy_budget_after_restart(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-1791386100"
    for coid in ("crashed", "cancelled", "filled"):
        db.log_order_event("run", "ORDER_MAKER_INTENT", client_order_id=coid, side="BUY", payload={"slug": slug})
    db.log_order_event("run", "ORDER_CANCELED", client_order_id="cancelled", side="BUY", payload={"slug": slug})
    db.log_order_event("run", "ORDER_FILLED", client_order_id="filled", side="BUY", payload={"slug": slug})
    counts = db.load_market_guard_counts(slug)
    db.stop()
    assert counts["unresolved_buy_intent_count"] == 1
    assert counts["buy_count"] == 2  # one fill + one possibly-resting crashed intent


# --- P2-005: run provenance ----------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "a.py").write_text("A = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_untracked_reports_do_not_mark_the_runtime_dirty(repo):
    from bot.research.provenance import _git_metadata
    (repo / "reports").mkdir()
    (repo / "reports" / "x.md").write_text("report")
    meta = _git_metadata(repo)
    assert meta["git_dirty"] is False and meta["dirty_diff_hash"] is None
    assert meta["untracked_file_count"] == 1


def test_untracked_runtime_module_and_tracked_edit_are_hashed_and_archived(repo):
    from bot.research.provenance import _git_metadata
    (repo / "bot" / "new_module.py").write_text("B = 2\n")
    first = _git_metadata(repo)
    assert first["git_dirty"] is True and first["git_dirty_tracked"] is False
    assert first["untracked_runtime_files"] == ["bot/new_module.py"]
    assert first["dirty_diff_hash"] and (repo / first["dirty_diff_archive"]).is_file()
    (repo / "bot" / "a.py").write_text("A = 3\n")
    second = _git_metadata(repo)
    assert second["git_dirty_tracked"] is True
    assert second["dirty_diff_hash"] != first["dirty_diff_hash"]
    archived = (repo / second["dirty_diff_archive"]).read_bytes()
    assert b"A = 3" in archived and b"B = 2" in archived


# --- P1-004: diffusion-consistent z ---------------------------------------------------------------

YEAR = 365.25 * 24 * 3600


def test_pre_final_window_z_uses_average_variance_without_tte_decay():
    from bot.live_entry_research import build_diffusion_flip_z
    out = build_diffusion_flip_z(spot=100_000, target=100_100, sigma_annual=0.5,
                                 time_left_sec=300, twap_window_sec=60, remaining_window_sec=None)
    horizon = 240 + 60 / 3
    expected = math.log(100_100 / 100_000) / (0.5 * math.sqrt(horizon / YEAR))
    assert out["required_move_z_variance_horizon_sec"] == pytest.approx(horizon)
    assert out["required_move_z_diffusion"] == pytest.approx(expected)
    assert out["p_terminal_flip_diffusion"] == pytest.approx(0.5 * math.erfc(expected / math.sqrt(2)))


def test_final_window_uses_one_third_of_remaining_window():
    from bot.live_entry_research import build_diffusion_flip_z
    out = build_diffusion_flip_z(spot=100_000, target=99_950, sigma_annual=0.5,
                                 time_left_sec=30, twap_window_sec=60, remaining_window_sec=30)
    assert out["required_move_z_variance_horizon_sec"] == pytest.approx(10.0)


@pytest.mark.parametrize("kwargs", [
    {"sigma_annual": None}, {"target": None}, {"time_left_sec": 30, "remaining_window_sec": None},
])
def test_diffusion_z_is_unavailable_rather_than_invented(kwargs):
    from bot.live_entry_research import build_diffusion_flip_z
    base = dict(spot=100_000, target=100_100, sigma_annual=0.5, time_left_sec=300,
                twap_window_sec=60, remaining_window_sec=None)
    out = build_diffusion_flip_z(**{**base, **kwargs})
    assert out["required_move_z_diffusion"] is None and out["p_terminal_flip_diffusion"] is None


def test_prediction_snapshot_carries_diffusion_fields_only_when_twap_fresh():
    from bot.prediction_research_snapshot import build_prediction_snapshot
    context = {"snapshot_ts": 100.0, "required_move_z_diffusion": 1.5, "p_terminal_flip_diffusion": 0.07,
               "required_move_z_variance_horizon_sec": 260.0, "required_move_z_model": "m",
               "required_move_z_sigma_source": "s", "twap_received_ts": 99.5, "twap_source_ts": 99.0}
    fresh = build_prediction_snapshot(context)
    assert fresh["required_move_z_diffusion"] == 1.5 and fresh["required_move_z_model"] == "m"
    stale = build_prediction_snapshot({**context, "twap_received_ts": 50.0})
    assert stale["required_move_z_diffusion"] is None


# --- P2-002: storage bounds ---------------------------------------------------------------------

def test_diagnostic_bucket_never_contains_exposure_or_risk_events():
    from monitoring.diagnostic_budget import diagnostic_bucket
    from monitoring.trade_journal_db import TradeJournalDB
    for event in TradeJournalDB._CRITICAL_ORDER_EVENTS:
        assert diagnostic_bucket("order", event) is None
    for event in TradeJournalDB._CRITICAL_STRATEGY_EVENTS | {"LIVE_SIGNAL_COMPARE", "MARKET_CYCLE_PNL", "ENTRY_DECISION_TRACE"}:
        assert diagnostic_bucket("strategy", event) is None
    assert diagnostic_bucket("order", "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE") == "ORDER_SKIP_*"


def test_token_bucket_spreads_the_daily_budget_over_time():
    from monitoring.diagnostic_budget import DiagnosticBudget
    clock = SimpleNamespace(now=0.0)
    budget = DiagnosticBudget(bytes_per_day=86_400 * 10, clock=lambda: clock.now)  # 10 B/s, 36 kB burst
    assert sum(budget.allow("T", 1_000) for _ in range(100)) == 36
    clock.now = 100.0  # +1000 bytes
    assert budget.allow("T", 1_000) is True and budget.allow("T", 1_000) is False
    clock.now = 4000.0
    summary = budget.take_summary()
    assert summary["dropped"]["T"]["rows"] == 65


def test_journal_samples_diagnostics_but_always_persists_exposure_events(tmp_path, monkeypatch):
    from monitoring.trade_journal_db import TradeJournalDB
    monkeypatch.setenv("TRADE_JOURNAL_DIAGNOSTIC_MB_PER_TYPE_DAY", "0.024")  # ~1 kB/hour burst
    db = TradeJournalDB(tmp_path / "journal.db")
    big = {"blob": "x" * 400}
    for _ in range(20):
        assert db.log_strategy_event("run", "QUOTE_TRANSPORT_TELEMETRY", big) is True
        assert db.log_order_event("run", "ORDER_FILLED", client_order_id="f", side="BUY", payload=big) is True
    db._diagnostic_budget._last_summary -= 7200
    db.log_strategy_event("run", "QUOTE_TRANSPORT_TELEMETRY", big)
    db.stop()
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        traces = conn.execute("SELECT count(*) FROM strategy_events WHERE event_type='QUOTE_TRANSPORT_TELEMETRY'").fetchone()[0]
        fills = conn.execute("SELECT count(*) FROM order_events WHERE event_type='ORDER_FILLED'").fetchone()[0]
        summaries = conn.execute("SELECT count(*) FROM strategy_events WHERE event_type='DIAGNOSTIC_SAMPLING_SUMMARY'").fetchone()[0]
    assert fills == 20
    assert 0 < traces < 20
    assert summaries == 1


@pytest.mark.parametrize("guard,slug_offset,expected", [
    ({"triggered": False}, 400, (1.0, "normal")),
    ({"triggered": True, "reason": "db_size_cap"}, 400, (5.0, "storage_db_size_cap")),
    ({"triggered": True, "reason": "free_disk_low"}, 400, (15.0, "storage_free_disk_low")),
    ({"triggered": True, "reason": "free_disk_low"}, 10, (1.0, "opening_full_rate")),
])
def test_prediction_snapshot_rate_follows_research_storage_guard(guard, slug_offset, expected):
    from bot.prediction_research_snapshot import PredictionResearchSnapshotter
    snapshotter = PredictionResearchSnapshotter(db=None, run_id="r")
    strategy = SimpleNamespace(twap_forward_shadow=SimpleNamespace(storage_guard_status=lambda: guard))
    start = 1_791_386_100
    assert snapshotter._effective_interval(strategy, slug=f"btc-updown-15m-{start}", now=start + slug_offset) == expected


# --- P3-003: BTC1S telemetry stays in one clock domain -------------------------------------------

def test_btc1s_jitter_is_zero_under_a_constant_clock_offset(tmp_path):
    from bot.btc_1s_history import BTC1sHistoryCollector
    collector = BTC1sHistoryCollector(tmp_path / "btc")
    for index in range(5):
        source_ms = 1_791_386_100_000 + index * 250
        # Local clock 414 ms behind the source: the old metric reported -414 "latency".
        collector.observe_aggtrade(price=100_000 + index, source_ts_ms=source_ms,
                                   received_ts=(source_ms - 414) / 1000)
    assert list(collector._jitter_ms) == [0.0, 0.0, 0.0, 0.0]
    assert collector._last_received_ms == 1_791_386_100_000 + 4 * 250 - 414


# --- P1-003: day-level inference tool -------------------------------------------------------------

def test_day_permutation_reports_minimum_attainable_p_for_four_days():
    from scripts.flip_regime_day_level import day_permutation_test
    by_day = {"sat": (0, 30), "sun": (0, 20), "mon": (6, 18), "tue": (4, 31)}
    result = day_permutation_test(by_day, {"sat", "sun"})
    assert result["distinct_permutations"] == 6
    assert result["min_attainable_p"] == pytest.approx(1 / 6)
    assert result["p_one_sided"] == pytest.approx(1 / 6)  # the most extreme split still cannot reach 0.05


def test_flip_tool_reports_excluded_markets_next_to_complete_ones():
    from scripts.flip_regime_day_level import summarize
    rows = [
        {"day": "2026-10-03", "weekend": True, "complete": True, "flip": {300: 0}},
        {"day": "2026-10-03", "weekend": True, "complete": False, "flip": {300: 1}},
        {"day": "2026-10-05", "weekend": False, "complete": True, "flip": {300: 1}},
    ]
    result = summarize(rows)["cutoffs"]["T-300"]
    assert result["complete"]["n_markets"] == 2 and result["complete"]["n_days"] == 2
    assert result["excluded_incomplete"]["flips"] == 1

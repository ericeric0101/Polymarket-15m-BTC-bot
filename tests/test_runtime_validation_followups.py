"""Follow-ups from the d24b576 60-minute dry-run (hermetic; no network or production DB)."""
from types import SimpleNamespace

import pytest

from bot.market_runtime import resolve_open_market_strike_early, wall_open_market_slug


# --- Opening capture: strike lookup starts at the wall-clock open --------------------------------

def _strike_host(*, cached=None):
    calls = []
    host = SimpleNamespace(
        current_market_slug="btc-updown-15m-900",
        research_market_instruments_by_slug={"btc-updown-15m-1800": {"UP": "up-inst", "DOWN": "down-inst"}},
        market_strike_cache_by_slug=dict(cached or {}),
    )

    async def resolver(instrument_id):
        calls.append(instrument_id)
        host.market_strike_cache_by_slug["btc-updown-15m-1800"] = 100

    host._get_market_strike_for_instrument = resolver
    return host, calls


def test_wall_open_slug_only_while_trading_holds_an_expired_market():
    host, _ = _strike_host()
    assert wall_open_market_slug(host, 1799.9) is None   # old market still live
    assert wall_open_market_slug(host, 1800.5) == "btc-updown-15m-1800"
    host.research_market_instruments_by_slug = {}
    assert wall_open_market_slug(host, 1800.5) is None   # never invent an unknown market


def test_strike_lookup_starts_at_open_through_the_authoritative_resolver():
    host, calls = _strike_host()
    assert resolve_open_market_strike_early(host, now_ts=1800.5) is True
    assert calls == ["up-inst"]
    assert host.current_market_slug == "btc-updown-15m-900"  # trading lifecycle untouched


def test_strike_lookup_skips_when_cached_or_before_open():
    host, calls = _strike_host(cached={"btc-updown-15m-1800": 99})
    assert resolve_open_market_strike_early(host, now_ts=1800.5) is False
    host2, calls2 = _strike_host()
    assert resolve_open_market_strike_early(host2, now_ts=1700.0) is False
    assert calls == [] and calls2 == []


def test_resolver_failure_never_escapes_the_research_worker():
    host, _ = _strike_host()

    async def broken(_instrument_id):
        raise RuntimeError("gamma down")

    host._get_market_strike_for_instrument = broken
    assert resolve_open_market_strike_early(host, now_ts=1800.5) is False


def test_research_only_worker_resolves_strike_before_capturing(monkeypatch):
    import threading
    from bot import market_runtime
    order = []
    monkeypatch.setattr(market_runtime, "resolve_open_market_strike_early",
                        lambda strategy, now_ts: order.append("strike"))
    snapshotter = SimpleNamespace(interval_sec=1.0, capture=lambda *a, **k: order.append("capture"))
    host = SimpleNamespace(_maker_worker_lock=threading.Lock(), _maker_worker_running=False, _stopping=False,
                           prediction_research_snapshotter=snapshotter)
    threads = []
    monkeypatch.setattr(market_runtime.threading, "Thread",
                        lambda target, daemon: threads.append(target) or SimpleNamespace(start=lambda: None))
    market_runtime.start_maker_worker(host, 0, 0, research_only=True)
    threads[0]()
    assert order == ["strike", "capture"]


def test_strike_retry_window_extends_past_the_old_handoff_deadline():
    from bot.spot_pricer import SpotPricerMixin
    # Lookups now begin ~19 s earlier; the deadline must not end sooner than
    # the previous handoff-relative one (19 s + 30 s).
    assert SpotPricerMixin._MARKET_STRIKE_INITIAL_RESOLUTION_WINDOW_SEC >= 49


# --- Decision traces are not sampled by the diagnostic budget --------------------------------

def test_entry_decision_trace_is_never_sampled(tmp_path, monkeypatch):
    import sqlite3
    from monitoring.trade_journal_db import TradeJournalDB
    monkeypatch.setenv("TRADE_JOURNAL_DIAGNOSTIC_MB_PER_TYPE_DAY", "0.024")
    db = TradeJournalDB(tmp_path / "journal.db")
    for _ in range(30):
        db.log_strategy_event("run", "ENTRY_DECISION_TRACE", {"blob": "x" * 400})
    db.stop()
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        rows = conn.execute("SELECT count(*) FROM strategy_events WHERE event_type='ENTRY_DECISION_TRACE'").fetchone()[0]
    assert rows == 30


# --- Research store growth -----------------------------------------------------------------------

def test_threshold_noise_around_a_level_emits_one_entry_not_a_pair_per_tick():
    from bot.twap_forward_shadow import TwapForwardShadow
    shadow = TwapForwardShadow()
    persisted = []
    shadow._persist = lambda slug, ts, event_type, payload: persisted.append(
        (payload["threshold"], payload["crossing_direction"]))
    values = [0.49, 0.51, 0.495, 0.505, 0.49, 0.51, 0.499]  # jitter < 2 points around 0.50
    for ts, value in enumerate(values):
        shadow._emit_threshold_crossings("m", {"observed_ts": ts}, "market_mid_probability_up", value, (.50,))
    assert persisted == [(.50, "entered")]
    shadow._emit_threshold_crossings("m", {"observed_ts": 9}, "market_mid_probability_up", 0.47, (.50,))
    assert persisted[-1] == (.50, "left")  # a real move past the band still records the exit


def test_low_side_threshold_hysteresis_mirrors_direction():
    from bot.twap_forward_shadow import TwapForwardShadow
    shadow = TwapForwardShadow()
    persisted = []
    shadow._persist = lambda slug, ts, event_type, payload: persisted.append(payload["crossing_direction"])
    for ts, value in enumerate([0.12, 0.09, 0.11, 0.095, 0.115, 0.13]):
        shadow._emit_threshold_crossings("m", {"observed_ts": ts}, "p_up_ex_market", value, (.10,))
    # 0.09 enters; 0.11/0.115 stay inside the 0.12 leave band; 0.13 leaves.
    assert persisted == ["entered", "left"]


class _Recorder:
    def __init__(self):
        self.rows = []

    def enqueue_decision(self, **kwargs):
        self.rows.append(kwargs["payload"])
        return True


@pytest.mark.parametrize("reason,expected", [
    ("", (5.0, 1.0, "normal")),
    ("db_size_cap", (30.0, 5.0, "storage_db_size_cap")),
    ("free_disk_low", (60.0, 15.0, "storage_free_disk_low")),
])
def test_forward_shadow_capture_policy_follows_storage_guard(reason, expected):
    from bot.forward_shadow import ForwardShadowExperiment
    experiment = ForwardShadowExperiment(db=_Recorder(), run_id="r", storage_pressure=lambda: reason)
    assert experiment._capture_policy() == expected


def test_forward_shadow_bbo_snapshots_slow_down_under_pressure():
    from bot.forward_shadow import ForwardShadowExperiment
    db = _Recorder()
    experiment = ForwardShadowExperiment(db=db, run_id="r", storage_pressure=lambda: "db_size_cap")
    for now in range(1000, 1061, 5):
        experiment._record_bbo(slug="m", now=float(now), instrument_id="up", side="UP", bid=.5, ask=.51,
                               bid_size=1.0, ask_size=1.0, bids=[], asks=[], time_left=300.0,
                               quote_ts=float(now), reference_ts=float(now))
    snapshots = [row for row in db.rows if row["event_type"] == "SHADOW_BBO_SNAPSHOT"]
    assert [row["event_ts"] for row in snapshots] == [1000.0, 1030.0, 1060.0]
    assert {row["capture_policy"] for row in snapshots} == {"storage_db_size_cap"}


def test_settings_wire_forward_shadow_to_the_research_storage_guard():
    import inspect
    from bot import settings
    source = inspect.getsource(settings.initialize_strategy_settings)
    assert "storage_pressure=research_storage_pressure" in source

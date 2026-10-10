"""Protective-evaluation gap observability (pre-12-market engineering pass, F-A).

A held position that goes longer than PROTECTIVE_EVAL_GAP_WARN_SEC without a
protective evaluation must surface as a rate-limited PROTECTIVE_EVAL_GAP
warning from the quote-independent watchdog.  Observability only: the monitor
never evaluates exits and never submits orders.
"""
import time
from decimal import Decimal

from bot.protective_exit import PROTECTIVE_EVAL_GAP_EVENT, PROTECTIVE_EVAL_GAP_WARN_SEC
from test_protective_exit_availability import _ProtectiveHost, _cycle


def _gap_events(host):
    return host.event_names(PROTECTIVE_EVAL_GAP_EVENT)


def test_quote_cycle_records_protective_evaluation_time():
    host = _ProtectiveHost()
    before = time.time()
    _cycle(host)
    assert host._protective_eval_last_ts_by_inst["up"] >= before
    assert host._protective_cycle_last_ts >= before


def test_held_position_without_evaluation_emits_rate_limited_gap_warning():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    host._report_protective_exit_availability(now)
    events = _gap_events(host)
    assert len(events) == 1
    payload = events[0][2]
    assert payload["instrument_id"] == "up"
    assert payload["gap_sec"] >= 40.0 - 1e-6
    assert payload["threshold_sec"] == PROTECTIVE_EVAL_GAP_WARN_SEC
    assert payload["observability_only"] is True
    host._report_protective_exit_availability(now + 5.0)
    assert len(_gap_events(host)) == 1  # rate limited
    host._report_protective_exit_availability(now + 31.0)
    assert len(_gap_events(host)) == 2
    assert host.venue_submissions == []


def test_recent_evaluation_does_not_warn():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 2.0}
    host._report_protective_exit_availability(now)
    assert _gap_events(host) == []


def test_never_evaluated_position_uses_open_time_as_gap_start():
    host = _ProtectiveHost()
    now = time.time()
    host.live_inventory_cost["up"]["opened_ts"] = now - 3.0
    host._report_protective_exit_availability(now)
    assert _gap_events(host) == []
    host.live_inventory_cost["up"]["opened_ts"] = now - 25.0
    host._report_protective_exit_availability(now)
    assert len(_gap_events(host)) == 1
    assert _gap_events(host)[0][2]["last_eval_ts"] is None


def test_gap_payload_names_high_cost_cooldown_skip_reason():
    host = _ProtectiveHost()
    now = time.time()
    host.maker_high_cost_exit_cooldown_enabled = True
    host.maker_high_cost_exit_cooldown_sec = 45
    host.high_cost_exit_cooldown_until_by_inst = {"up": now + 30.0}
    _cycle(host)  # quote 0.20 < avg 0.69 inside cooldown -> evaluation skipped
    assert "up" not in getattr(host, "_protective_eval_last_ts_by_inst", {})
    assert host._protective_eval_skip_reason_by_inst["up"] == "high_cost_cooldown"
    host._report_protective_exit_availability(now + 20.0)
    payload = _gap_events(host)[0][2]
    assert payload["last_skip_reason"] == "high_cost_cooldown"
    assert payload["high_cost_cooldown_remaining_sec"] is not None


def test_gap_monitor_never_evaluates_or_submits(monkeypatch):
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 60.0}

    async def _boom(*_a, **_k):
        raise AssertionError("monitor must not evaluate exits")

    monkeypatch.setattr(host, "_maybe_taker_exit_positions", _boom, raising=False)
    host._report_protective_exit_availability(now)
    assert len(_gap_events(host)) == 1
    assert host.venue_submissions == []


def test_unsellable_dust_position_is_not_reported_as_gap():
    host = _ProtectiveHost(qty="0.01", sellable="0.01")
    now = time.time()
    host._protective_eval_last_ts_by_inst = {}
    host.live_inventory_cost["up"]["opened_ts"] = now - 120.0
    host._report_protective_exit_availability(now)
    assert _gap_events(host) == []


def test_gap_warning_failure_never_raises():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 60.0}

    def _broken(_name, _payload):
        raise RuntimeError("journal down")

    host._db_strategy_event = _broken
    host._report_protective_eval_gaps(now, host._held_protective_positions())  # must not raise

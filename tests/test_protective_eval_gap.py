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


# ---------------------------------------------- pre-LIVE telemetry semantics
from bot.protective_exit import PROTECTIVE_EVAL_GAP_END_EVENT  # noqa: E402


def test_gap_payload_has_research_fields_and_cause_class():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    host.last_quote_update_ts_by_inst = {"up": now - 1.0, "down": now}
    host._report_protective_exit_availability(now)
    payload = _gap_events(host)[0][2]
    assert payload["gap_start_ts"] == now - 40.0
    assert payload["gap_end_ts"] is None and payload["gap_open"] is True
    assert payload["gap_duration_sec"] == payload["gap_sec"]
    assert payload["held_position_qty"] == 10.0 and payload["held_side"] == "UP"
    assert abs(payload["last_quote_age_sec"] - 1.0) < 1e-6
    assert "last_reference_age_sec" in payload and "skip_reason" in payload
    assert payload["cooldown_active"] is False
    assert payload["cause_class"] == "UNKNOWN"
    assert host.venue_submissions == []


def test_quote_stream_stall_cause():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    host.last_quote_update_ts_by_inst = {"up": now - 35.0, "down": now - 35.0}
    host._report_protective_exit_availability(now)
    assert _gap_events(host)[0][2]["cause_class"] == "QUOTE_STREAM_STALL"


def test_high_cost_and_reject_cooldown_causes():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    host.high_cost_exit_cooldown_until_by_inst = {"up": now + 30.0}
    host._report_protective_exit_availability(now)
    payload = _gap_events(host)[0][2]
    assert payload["cause_class"] == "HIGH_COST_COOLDOWN" and payload["cooldown_active"] is True
    other = _ProtectiveHost()
    other._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    other.taker_exit_reject_cooldown_until_by_inst = {"up": now + 10.0}
    other._report_protective_exit_availability(now)
    assert _gap_events(other)[0][2]["cause_class"] == "REJECT_COOLDOWN"


def test_reported_gap_end_is_recorded_without_exit_path_io():
    host = _ProtectiveHost()
    now = time.time()
    host._protective_eval_last_ts_by_inst = {"up": now - 40.0}
    host._report_protective_exit_availability(now)
    events_before = len(host.events)
    host._note_protective_eval("up", now + 3.0)       # exit path: memory only, no journal write
    assert len(host.events) == events_before
    host._report_protective_exit_availability(now + 4.0)
    (end,) = host.event_names(PROTECTIVE_EVAL_GAP_END_EVENT)
    payload = end[2]
    assert payload["gap_start_ts"] == now - 40.0 and payload["gap_end_ts"] == now + 3.0
    assert abs(payload["gap_duration_sec"] - 43.0) < 1e-6
    host._report_protective_exit_availability(now + 40.0)
    assert len(host.event_names(PROTECTIVE_EVAL_GAP_END_EVENT)) == 1  # emitted once


def test_unreported_short_pause_creates_no_gap_end():
    host = _ProtectiveHost()
    now = time.time()
    host._note_protective_eval("up", now)
    host._report_protective_exit_availability(now + 2.0)
    assert host.event_names(PROTECTIVE_EVAL_GAP_END_EVENT) == []

"""Deferred relabel of near-tie UNKNOWN settlements (hermetic).

A near-tie settlement labeled from an off-end TWAP tick is UNKNOWN
(tests/test_near_tie_settlement_guard.py).  RTDS emits a tick stamped exactly
at market end ~1.3 s after it (210/221 observed boundaries), so the lifecycle
timer upgrades the UNKNOWN once that tick arrives, within a bounded deadline,
from a settlement-time snapshot that survives the next market's rollover.
"""
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

import bot.lifecycle_runtime as lr
from bot.enums import MarketPhase
from bot.execution_events import audit_reconciliation
from test_settlement_authority import Host, UP_INST

# Second official/TWAP conflict: pre-end tick labeled DOWN, official was UP.
SLUG = "btc-updown-15m-1791144000"
STRIKE = 85411.9999740108
PRE_END_TWAP = 85406.90636769912
END_TWAP_UP = STRIKE * (1 + 0.3 / 1e4)   # end-stamped tick on the official side


def _armed_host(*, qty=Decimal("4"), cycle_realized=Decimal("0.25"), tick_minus_end=-2.0, age=2.5,
                stop_timing=None):
    host = Host(twap=PRE_END_TWAP, spot=0.0, twap_age=age, qty=qty)
    if stop_timing is not None:
        host.stop_timing_telemetry = stop_timing
    host.current_market_slug = SLUG
    host.market_strike_cache_by_slug = {SLUG: Decimal(str(STRIKE))}
    host._shadow_state = {}
    tick_ts = time.time() - age
    host._polymarket_chainlink_twap_observation_ts = tick_ts
    host.current_market_end_timestamp = tick_ts - tick_minus_end
    host.market_cycle_realized_net_usdc = cycle_realized
    host.settle()
    return host, host.current_market_end_timestamp


def _events(host, event_type):
    return [p for e, p in host.events if e == event_type]


def _roll_over(host):
    """What market rollover does to per-market state (run_bot.py reset)."""
    host.current_market_slug = "btc-updown-15m-1791144900"
    host.current_market_end_timestamp = 1791144900 + 900
    host.inventory_delta_shares = Decimal("2")
    host.live_inventory_cost = {"next-up-inst": {"qty": Decimal("2"), "avg_entry_price": Decimal("0.40")}}
    host.market_cycle_realized_net_usdc = Decimal("1.5")


def test_near_tie_unknown_is_armed_and_relabeled_from_end_stamped_tick():
    host, end = _armed_host()
    unknown = host.event("MARKET_SETTLEMENT")
    assert unknown["outcome"] == "UNKNOWN" and unknown["settlement_pending"] is True
    assert unknown["settlement_relabel_pending"] is True
    assert unknown["settlement_relabel_deadline_ts"] == pytest.approx(end + lr.SETTLEMENT_RELABEL_MAX_WAIT_SEC)
    assert not _events(host, "MARKET_CYCLE_PNL")
    assert host.market_cycle_realized_net_usdc == 0  # live state reset as before
    assert host._process_pending_settlement_relabel(now_ts=end + 1.0) is True  # still waiting

    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    assert host._process_pending_settlement_relabel(now_ts=end + 1.4) is False

    final = _events(host, "MARKET_SETTLEMENT")[-1]
    assert final["outcome"] == "UP" and final["outcome_source"] == "canonical_twap_deferred_relabel"
    assert final["settlement_relabel_of_pending"] is True
    assert final["settlement_reference_end_offset_sec"] == pytest.approx(0.0)
    assert final["settlement_initial_reference_end_offset_sec"] == pytest.approx(2.0)
    # 4 UP shares at 0.75: redeem 4.00 - cost 3.00; plus snapshot fill realized 0.25.
    assert final["settlement_pnl_usdc"] == pytest.approx(1.0)
    (cycle,) = _events(host, "MARKET_CYCLE_PNL")
    assert cycle["slug"] == SLUG and cycle["cycle_combined_pnl_usdc"] == pytest.approx(1.25)
    assert host.session_pnl == [(Decimal("1.0"), "settlement_deferred_relabel")]
    assert [r["source"] for r in host.regime] == ["settlement_deferred_relabel"]
    assert host._pending_settlement_relabel is None


def test_relabel_after_rollover_uses_snapshot_not_next_market_state():
    host, end = _armed_host()
    _roll_over(host)
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    host._process_pending_settlement_relabel(now_ts=end + 2.0)

    final = _events(host, "MARKET_SETTLEMENT")[-1]
    assert final["slug"] == SLUG and final["inventory_shares"] == pytest.approx(4.0)
    assert final["inventory_cost_usdc"] == pytest.approx(3.0)
    (cycle,) = _events(host, "MARKET_CYCLE_PNL")
    assert cycle["slug"] == SLUG and cycle["cycle_fill_realized_usdc"] == pytest.approx(0.25)
    # The next market's live state is untouched.
    assert host.market_cycle_realized_net_usdc == Decimal("1.5")
    assert host.inventory_delta_shares == Decimal("2")
    assert list(host.live_inventory_cost) == ["next-up-inst"]


def test_concurrent_tick_rollover_and_timer_finalize_exactly_once():
    for _ in range(50):
        host, end = _armed_host()
        start = threading.Barrier(4)

        def rtds():
            start.wait()
            for offset in (-1.0, 0.0, 1.0):
                host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end + offset, 60)

        def timer():
            start.wait()
            for _ in range(200):
                host._process_pending_settlement_relabel(now_ts=end + 1.5)

        def rollover():
            start.wait()
            _roll_over(host)

        threads = [threading.Thread(target=f) for f in (rtds, timer, timer, rollover)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        host._process_pending_settlement_relabel(now_ts=end + 1.5)

        cycles = _events(host, "MARKET_CYCLE_PNL")
        assert len(cycles) == 1 and cycles[0]["slug"] == SLUG
        assert cycles[0]["cycle_combined_pnl_usdc"] == pytest.approx(1.25)
        assert len(host.session_pnl) == 1
        assert host.market_cycle_realized_net_usdc == Decimal("1.5")


def test_deadline_expiry_leaves_unknown_for_gamma_reconciliation():
    host, end = _armed_host()
    assert host._process_pending_settlement_relabel(now_ts=end + lr.SETTLEMENT_RELABEL_MAX_WAIT_SEC) is False
    (expired,) = _events(host, "MARKET_SETTLEMENT_RELABEL_EXPIRED")
    assert expired["slug"] == SLUG and expired["reason"] == "end_stamped_twap_tick_not_received"
    # A late end tick no longer relabels anything.
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    host._process_pending_settlement_relabel(now_ts=end + 9.0)
    assert [p["outcome"] for p in _events(host, "MARKET_SETTLEMENT")] == ["UNKNOWN"]
    assert not _events(host, "MARKET_CYCLE_PNL") and host.session_pnl == []


@pytest.mark.parametrize("offset,window", [(-1.0, 60), (1.0, 60), (0.0, 30)])
def test_only_end_stamped_60s_ticks_relabel(offset, window):
    host, end = _armed_host()
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end + offset, window)
    assert host._process_pending_settlement_relabel(now_ts=end + 2.0) is True
    assert not _events(host, "MARKET_CYCLE_PNL")


def test_end_tick_seen_before_settlement_ran_is_used():
    host = Host(twap=PRE_END_TWAP, spot=0.0, qty=Decimal("4"))
    end = time.time() - 3.0
    # End tick arrived, then a later post-end tick overwrote the latest fields.
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    host._observe_settlement_relabel_twap_tick(PRE_END_TWAP, end + 2.0, 60)
    host.current_market_slug = SLUG
    host.market_strike_cache_by_slug = {SLUG: Decimal(str(STRIKE))}
    host._shadow_state = {}
    host._polymarket_chainlink_twap_observation_ts = end + 2.0
    host.current_market_end_timestamp = end
    host.settle()
    assert host.event("MARKET_SETTLEMENT")["outcome"] == "UNKNOWN"
    host._process_pending_settlement_relabel()
    assert _events(host, "MARKET_SETTLEMENT")[-1]["outcome"] == "UP"
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1


def test_no_inventory_near_tie_relabels_label_only():
    host, end = _armed_host(qty=Decimal("0"), cycle_realized=Decimal("0.4"))
    first = host.event("MARKET_SETTLEMENT")
    assert first["outcome"] == "UNKNOWN" and first["outcome_only"] is True
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1  # fill-only PnL written as before
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    host._process_pending_settlement_relabel(now_ts=end + 1.5)
    final = _events(host, "MARKET_SETTLEMENT")[-1]
    assert final["outcome"] == "UP" and final["outcome_only"] is True
    assert final["settlement_pnl_usdc"] == 0.0
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1 and host.session_pnl == []


def test_next_settlement_supersedes_unfinished_relabel():
    host, end = _armed_host()
    _roll_over(host)
    host._schedule_settlement_relabel({**host._pending_settlement_relabel, "slug": "next"})
    (expired,) = _events(host, "MARKET_SETTLEMENT_RELABEL_EXPIRED")
    assert expired["slug"] == SLUG and expired["reason"] == "superseded_by_next_settlement"
    assert host._pending_settlement_relabel["slug"] == "next"


def test_settlement_and_tick_hooks_never_wait_for_the_end_tick():
    started = time.perf_counter()
    host, end = _armed_host()
    for i in range(100):
        host._observe_settlement_relabel_twap_tick(PRE_END_TWAP, end - 100 + i, 60)
    assert time.perf_counter() - started < 0.2
    assert host._pending_settlement_relabel is not None


def test_lifecycle_timer_polls_relabel_with_short_bounded_waits():
    host, end = _armed_host()
    host.market_phase = MarketPhase.SETTLING
    host._stopping = False
    host.maker_min_minutes_to_close = 2.0
    host.market_settling_grace_sec = 15.0
    host._market_settling_since_ts = time.time()
    host._update_market_phase = lambda: host.market_phase
    waits = []

    def wait(sec):
        waits.append(sec)
        if len(waits) == 3:  # end-stamped tick arrives while the timer is waiting
            host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
        if len(waits) >= 6:
            host._lifecycle_stop_event.set()
        return False

    host._lifecycle_stop_event = SimpleNamespace(is_set=lambda: len(waits) >= 6, wait=wait, set=lambda: None)
    host._start_market_lifecycle_timer()
    assert waits[:3] == [lr.SETTLEMENT_RELABEL_POLL_SEC] * 3
    assert waits[3] == pytest.approx(5.0, abs=0.1)  # back to the normal settling cadence
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1


def test_audit_accepts_canonical_relabel_after_pending_unknown():
    pending = {"event_type": "MARKET_SETTLEMENT", "payload": {"slug": "m", "outcome": "UNKNOWN", "settlement_pending": True}}
    final = {"event_type": "MARKET_SETTLEMENT", "payload": {"slug": "m", "outcome": "UP"}}
    codes = [i["reason_code"] for i in audit_reconciliation([pending, final])["issues"]]
    assert "REPEATED_FINALIZATION" not in codes
    codes = [i["reason_code"] for i in audit_reconciliation([pending, final, final])["issues"]]
    assert "REPEATED_FINALIZATION" in codes


class _StopTimingRecorder:
    """Research-only telemetry stand-in; may be made to raise."""

    def __init__(self, fail=False):
        self.fail, self.settlements, self.relabels = fail, [], []

    def on_settlement_safe(self, **kwargs):
        self.settlements.append(kwargs)

    def on_settlement_relabel_safe(self, **kwargs):
        self.relabels.append(kwargs)
        if self.fail:
            raise RuntimeError("telemetry boom")


@pytest.mark.parametrize("qty", [Decimal("4"), Decimal("0")])
def test_relabel_forwards_upgraded_outcome_to_stop_timing_telemetry(qty):
    rec = _StopTimingRecorder()
    host, end = _armed_host(qty=qty, stop_timing=rec)
    assert [s["outcome"] for s in rec.settlements] == ["UNKNOWN"]
    host._observe_settlement_relabel_twap_tick(END_TWAP_UP, end, 60)
    host._process_pending_settlement_relabel(now_ts=end + 1.4)
    (relabel,) = rec.relabels
    assert relabel["slug"] == SLUG and relabel["outcome"] == "UP"
    assert relabel["outcome_source"] == "canonical_twap_deferred_relabel"
    assert relabel["settlement_ts"] == rec.settlements[0]["settlement_ts"]
    assert relabel["relabel_ts"] == pytest.approx(end + 1.4)
    assert relabel["reference_is_canonical"] is True


def test_expired_relabel_forwards_nothing_and_telemetry_fault_never_changes_settlement():
    rec = _StopTimingRecorder()
    host, end = _armed_host(stop_timing=rec)
    host._process_pending_settlement_relabel(now_ts=end + lr.SETTLEMENT_RELABEL_MAX_WAIT_SEC)
    assert rec.relabels == []

    baseline, b_end = _armed_host()
    faulty = _StopTimingRecorder(fail=True)
    host, end = _armed_host(stop_timing=faulty)
    for h, e in ((baseline, b_end), (host, end)):
        h._observe_settlement_relabel_twap_tick(END_TWAP_UP, e, 60)
        h._process_pending_settlement_relabel(now_ts=e + 1.4)
    assert len(faulty.relabels) == 1
    strip = lambda evs: [(t, {k: v for k, v in p.items() if not k.endswith("_ts") and "delay" not in k
                               and "offset" not in k and "age" not in k and "margin" not in k})
                         for t, p in evs]
    assert strip(host.events) == strip(baseline.events)
    assert host.session_pnl == baseline.session_pnl

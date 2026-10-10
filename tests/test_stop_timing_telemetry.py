"""Prospective stop-timing telemetry: reconstruction and isolation guarantees."""
import asyncio
import copy
import itertools
import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.enums import ActiveSide
from bot.entry_sizing import SIZING_RULE_VERSION
from bot.exit_engine import ExitPolicyEngine
from bot.stop_timing_telemetry import (
    MAX_EVENTS_PER_EPOCH, StopTimingTelemetry, absolute_breaker_components, signed_strike_distance,
)
from monitoring.trade_journal_db import TradeJournalDB
from scripts.stop_timing_report import reconstruct
from test_absolute_max_loss_breaker import (
    _BreakerExitHost, _make_config, _position, _signal, _snapshot,
)

SLUG = "btc-updown-15m-1790000000"
END = 1_790_000_900.0


class Sink:
    def __init__(self):
        self.rows = []

    def __call__(self, event_type, payload):
        self.rows.append((event_type, copy.deepcopy(payload)))
        return True

    def types(self):
        return [t for t, _ in self.rows]

    def of(self, event_type):
        return [p for t, p in self.rows if t == event_type]


def _decision(reason="no_exit_signal", net="-0.50", gross="-0.40", px="0.60"):
    return SimpleNamespace(reason=reason, net_if_exit=Decimal(net), gross_if_exit=Decimal(gross),
                           exit_px_effective=Decimal(px), decision_type=SimpleNamespace(value="NONE"))


def _obs(tel, now, *, twap, bid="0.60", decision=None, src_age=0.5, sigma=1.7, z=1.2,
         time_left=None, votes=None, persistence=0.0, invalidated=False, signal=None, mode="PRE_FINAL_STRIKE_PROXY",
         strike="100000"):
    tel.observe_safe(
        now_ts=now, slug=SLUG, instrument_id="up-token", held_side="UP",
        state={"qty": Decimal("10"), "avg_entry_price": Decimal("0.65"), "opened_ts": END - 600,
               "entry_fee_remaining": Decimal("0")},
        qty=Decimal("10"), sellable_qty=Decimal("10"), best_bid=Decimal(bid), best_ask=Decimal(bid) + Decimal("0.01"),
        time_left_sec=END - now if time_left is None else time_left, market_end_ts=END,
        strike=Decimal(strike) if strike is not None else None, reference_spot=Decimal("100010"),
        reference_source="polymarket_chainlink_twap_60s_ws", reference_ts=now - 0.4,
        twap_features=None if twap is None else {
            "official_current_twap": twap, "source_ts": now - src_age, "required_move_sigma": sigma,
            "required_move_z_diffusion": z, "sigma_ex_market_source": "polymarket_chainlink_spot_history",
            "required_move_z_sigma_source": "raw_realized_chainlink_no_decay", "required_move_mode": mode,
            "probability_model_mode": "PRE_FINAL_WINDOW_APPROX"},
        exit_decision=decision or _decision(),
        engine_config=_make_config(), signal_decision=signal or _signal(),
        locked_side_invalidated=invalidated, adverse_persistence_sec=persistence,
        thesis_votes=votes or {}, hold_sec=now - (END - 600),
    )


def test_full_lifecycle_reconstructs_cross_breaker_and_settlement():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    t0 = END - 500
    _obs(tel, t0, twap=100020.0)                                 # opened, favourable
    _obs(tel, t0 + 5, twap=99990.0, bid="0.50")                  # adverse cross
    for dt in (10, 20, 35):
        _obs(tel, t0 + 5 + dt, twap=99980.0, bid="0.40")
    opp = _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN")
    votes = {"weakening_count": 2, "available_count": 3}
    _obs(tel, t0 + 50, twap=99970.0, bid="0.40", decision=_decision("no_exit_signal", "-2.50", "-2.40", "0.40"),
         signal=opp, votes=votes, persistence=16.0)
    _obs(tel, t0 + 55, twap=99970.0, bid="0.40",
         decision=_decision("absolute_max_loss_breaker", "-2.55", "-2.45", "0.40"), signal=opp, votes=votes,
         persistence=21.0)
    tel.on_settlement_safe(slug=SLUG, outcome="DOWN", settlement_ts=END + 1,
                           reference_source="polymarket_chainlink_twap_60s_ws", reference_is_canonical=True)

    opened = sink.of("STOP_TIMING_POSITION_OPENED")[0]
    assert opened["held_side"] == "UP" and opened["entry_qty"] == 10.0
    assert opened["entry_cost_usdc"] == pytest.approx(6.5)
    assert opened["tte_at_entry_sec"] == pytest.approx(600.0)
    assert opened["signed_distance_usd"] == pytest.approx(20.0)
    assert opened["signed_distance_bps"] == pytest.approx(2.0)
    cross = sink.of("STOP_TIMING_ADVERSE_CROSS")
    assert len(cross) == 1 and cross[0]["obs_wall_ts"] == t0 + 5
    assert cross[0]["signed_distance_bps"] == pytest.approx(-1.0)
    assert cross[0]["required_move_sigma_legacy"] == 1.7 and cross[0]["required_move_z_diffusion"] == 1.2
    assert cross[0]["best_bid"] == 0.5 and cross[0]["net_if_exit"] == -0.5
    assert cross[0]["time_left_sec"] == pytest.approx(495.0) and cross[0]["obs_interval_sec"] == 5.0
    cps = sink.of("STOP_TIMING_PERSISTENCE_CHECKPOINT")
    assert [c["checkpoint_sec"] for c in cps] == [5, 15, 30]
    assert all(c["still_adverse"] for c in cps)
    assert cps[0]["capture_lag_sec"] == pytest.approx(5.0)
    firsts = {p["component"]: p for p in sink.of("STOP_TIMING_COMPONENT_FIRST_TRUE")}
    assert firsts["abs_loss_threshold"]["obs_wall_ts"] == t0 + 50
    assert firsts["abs_adverse_trend_confirmed"]["obs_wall_ts"] == t0 + 50
    elig = sink.of("STOP_TIMING_BREAKER_FIRST_ELIGIBLE")
    assert len(elig) == 1 and elig[0]["breaker"] == "conditional_absolute_loss_breaker"
    assert elig[0]["obs_wall_ts"] == t0 + 55 and elig[0]["abs_components"]["abs_eligible_recomputed"] is True
    settle = sink.of("STOP_TIMING_POSITION_SETTLEMENT")[0]
    assert settle["settlement_outcome_runtime"] == "DOWN"
    assert settle["counterfactual_hold_gross_pnl"] == pytest.approx(-6.5)
    assert settle["first_times"]["adverse_cross"] == t0 + 5
    assert settle["first_times"]["abs_breaker_eligible"] == t0 + 55
    assert settle["official_outcome"] == "PENDING_OFFICIAL_RESOLUTION"


def test_favorable_recross_and_checkpoint_resolution():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink)
    t0 = END - 400
    _obs(tel, t0, twap=100010.0)
    _obs(tel, t0 + 2, twap=99995.0)
    _obs(tel, t0 + 8, twap=100005.0, bid="0.66", decision=_decision(net="0.05", gross="0.10"))
    _obs(tel, t0 + 40, twap=100005.0)
    rc = sink.of("STOP_TIMING_FAVORABLE_RECROSS")
    assert len(rc) == 1 and rc[0]["adverse_duration_sec"] == pytest.approx(6.0)
    assert rc[0]["net_if_exit"] == 0.05 and rc[0]["time_left_sec"] == pytest.approx(392.0)
    assert sink.of("STOP_TIMING_PERSISTENCE_CHECKPOINT") == []   # re-crossed before the 5 s check was observed


def test_identical_observations_are_idempotent_and_bounded():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink)
    for i in range(500):
        _obs(tel, END - 600 + i, twap=100020.0)
    keys = [(t, p.get("component")) for t, p in sink.rows]
    assert len(keys) == len(set(keys)) and len(keys) <= 6    # each transition once, never per quote
    assert sink.types()[0] == "STOP_TIMING_POSITION_OPENED"
    assert not sink.of("STOP_TIMING_ADVERSE_CROSS")
    # Oscillation storm stays within the per-epoch cap.
    sink2 = Sink()
    tel2 = StopTimingTelemetry(emit=sink2)
    for i in range(2000):
        _obs(tel2, END - 800 + i * 0.3, twap=100010.0 if i % 2 else 99990.0)
    assert len(sink2.rows) <= MAX_EVENTS_PER_EPOCH
    assert len(sink2.of("STOP_TIMING_ADVERSE_CROSS")) <= 12


def test_unknown_and_degraded_inputs_are_never_fabricated():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink)
    _obs(tel, END - 300, twap=99990.0, src_age=30.0)          # stale settlement reference
    _obs(tel, END - 295, twap=None)                           # no reference at all
    _obs(tel, END - 290, twap=99990.0, strike=None)           # strike unavailable
    opened = sink.of("STOP_TIMING_POSITION_OPENED")[0]
    assert opened["settlement_reference_state"] == "DEGRADED" and opened["cross_state"] == "UNKNOWN"
    assert opened["signed_distance_usd"] is None and opened["required_move_sigma_legacy"] is None
    assert opened["required_move_z_diffusion"] is None
    assert sink.of("STOP_TIMING_ADVERSE_CROSS") == []
    reasons = {p["degraded_reason"] for p in sink.of("STOP_TIMING_DEGRADED_FIRST")}
    assert reasons == {"settlement_reference_stale", "settlement_reference_unknown", "strike_unavailable"}


def test_tie_semantics_and_diffusion_absent():
    assert signed_strike_distance("UP", Decimal("100"), Decimal("100"))[2] == "FAVORABLE"
    assert signed_strike_distance("DOWN", Decimal("100"), Decimal("100"))[2] == "ADVERSE"
    assert signed_strike_distance("DOWN", Decimal("99"), Decimal("100"))[:2] == (1.0, 100.0)
    assert signed_strike_distance("NONE", Decimal("99"), Decimal("100"))[2] == "UNKNOWN"
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink)
    _obs(tel, END - 300, twap=100010.0, z=None)
    assert sink.of("STOP_TIMING_POSITION_OPENED")[0]["required_move_z_diffusion"] is None


def test_component_mirror_matches_engine_absolute_breaker():
    engine = ExitPolicyEngine(_make_config())
    cfg = engine.config
    grid = itertools.product(
        ("0.20", "0.55", "0.70"), (30.0, 90.0), (60.0, 300.0),
        ((Decimal("-0.30"), True, False, "DOWN"), (Decimal("0.30"), True, True, "UP"),
         (Decimal("0"), False, False, "NONE"), (Decimal("-0.03"), True, False, "DOWN")),
        (False, True), (0.0, 16.0), ((2, 3), (1, 3), (2, 1)))
    checked = 0
    for bid, hold, tleft, sig, inval, persist, (weak, avail) in grid:
        snapshot, position = _snapshot(bid, time_left_sec=tleft), _position("0.69", hold_sec=hold)
        signal = _signal(score=sig[0], locked=sig[1], matches=sig[2], active_side=sig[3])
        decision = engine.evaluate(snapshot, position, signal, locked_side_invalidated=inval,
                                   confirmed_adverse_exit_active=inval, adverse_persistence_sec=persist,
                                   adverse_thesis_weakening_count=weak, adverse_thesis_available_count=avail)
        comps = absolute_breaker_components(
            enabled=cfg.absolute_max_loss_enabled, min_hold_sec=cfg.absolute_max_loss_min_hold_sec,
            loss_usdc=cfg.absolute_max_loss_usdc, thesis_min_score_abs=cfg.stop_loss_thesis_min_score_abs,
            hold_sec=hold, avg_entry=Decimal("0.69"), best_bid=snapshot.best_bid,
            gross_if_exit=decision.gross_if_exit, net_if_exit=decision.net_if_exit, time_left_sec=tleft,
            locked_side_invalidated=inval, signal_matches_position=sig[2], signal_side=sig[3],
            signal_locked=sig[1], signal_score=sig[0], adverse_persistence_sec=persist,
            weakening_count=weak, available_count=avail)
        assert comps["abs_eligible_recomputed"] == (decision.reason == "absolute_max_loss_breaker")
        checked += 1
    assert checked > 500


# ---------------------------------------------------------------- isolation
def test_sink_exceptions_slow_writes_and_rejections_never_escape():
    def boom(_t, _p):
        raise sqlite3.OperationalError("database is locked")
    tel = StopTimingTelemetry(emit=boom)
    _obs(tel, END - 300, twap=99990.0)
    assert tel.counters["exceptions"] >= 1

    rejected = StopTimingTelemetry(emit=lambda _t, _p: False)
    _obs(rejected, END - 300, twap=99990.0)
    assert rejected.counters["sink_rejected"] >= 1 and rejected.counters["emitted"] == 0

    calls = []
    def slow(_t, _p):
        calls.append(1)
        time.sleep(0.06)
        return True
    slow_tel = StopTimingTelemetry(emit=slow)
    for i in range(10):
        _obs(slow_tel, END - 300 + i * 6, twap=100010.0 if i % 2 else 99990.0)
    assert slow_tel.disabled and slow_tel.counters["slow_calls"] == 3
    frozen = len(calls)
    _obs(slow_tel, END - 100, twap=99990.0)
    assert len(calls) == frozen

    # Internal fault (bad state object) is swallowed too.
    bad = StopTimingTelemetry(emit=Sink())
    bad.observe_safe(now_ts=1.0, state=None)
    bad.on_settlement_safe(slug=None)
    assert bad.counters["exceptions"] == 2


def test_real_journal_queue_is_non_blocking_when_busy(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db", backup_interval_sec=3600)
    try:
        tel = StopTimingTelemetry(emit=lambda t, p: db.enqueue_strategy_event("r", t, p))
        db._backup_lock.acquire()          # simulate backup/flush holding the journal worker
        try:
            started = time.perf_counter()
            _obs(tel, END - 300, twap=99990.0)
            assert time.perf_counter() - started < 0.05
            assert tel.counters["sink_rejected"] >= 1
        finally:
            db._backup_lock.release()
        _obs(tel, END - 295, twap=100010.0)
        db._drain_telemetry()
        count = sqlite3.connect(tmp_path / "journal.db").execute(
            "SELECT COUNT(*) FROM strategy_events WHERE event_type LIKE 'STOP_TIMING_%'").fetchone()[0]
        assert count >= 1
    finally:
        db.stop()


class _Raising:
    disabled = False

    def __init__(self):
        self.calls = 0

    def observe_safe(self, **_kwargs):
        self.calls += 1
        StopTimingTelemetry(emit=lambda *_: (_ for _ in ()).throw(RuntimeError("x"))).observe_safe(**_kwargs)


def _run_host(telemetry, *, breaker):
    host = _BreakerExitHost()
    if breaker:
        host.active_side = ActiveSide.DOWN
        host.side_decision_score = Decimal("-0.30")
    host.market_strike_cache_by_slug = {"breaker-test": Decimal("100")}
    if telemetry is not None:
        host.stop_timing_telemetry = telemetry
    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=False))
    return host


@pytest.mark.parametrize("breaker", [True, False])
def test_execution_authority_identical_with_failing_or_absent_telemetry(breaker):
    baseline = _run_host(None, breaker=breaker)
    failing = _Raising()
    with_fault = _run_host(failing, breaker=breaker)
    sink = Sink()
    with_sink = _run_host(StopTimingTelemetry(emit=sink), breaker=breaker)
    assert failing.calls == 1
    assert baseline.submissions == with_fault.submissions == with_sink.submissions
    assert len(baseline.submissions) == (1 if breaker else 0)
    assert baseline.pending_taker_exit_by_inst == with_sink.pending_taker_exit_by_inst
    assert "STOP_TIMING_POSITION_OPENED" in sink.types()
    if breaker:
        assert sink.of("STOP_TIMING_BREAKER_FIRST_ELIGIBLE")


def test_dry_run_never_evaluates_telemetry():
    host = _BreakerExitHost()
    tel = StopTimingTelemetry(emit=Sink())
    host.stop_timing_telemetry = tel
    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=True))
    assert tel.counters["observations"] == 0 and host.submissions == []


def test_checkpoints_grant_no_sell_authority():
    host = _BreakerExitHost()            # matching locked trend: engine must hold
    sink = Sink()
    host.stop_timing_telemetry = StopTimingTelemetry(emit=sink)
    host.market_strike_cache_by_slug = {"breaker-test": Decimal("100")}
    for i in range(8):                    # 0..35 s after the adverse cross (twap 99 < strike 100)
        host.twap_forward_shadow = SimpleNamespace(latest=lambda _s, t=10_000.0 + i * 5: {
            "official_current_twap": 99.0, "projected_settlement_side_trend": "DOWN", "source_ts": t - 0.1})
        asyncio.run(host._maybe_taker_exit_positions(10_000.0 + i * 5, is_simulation=False))
    assert [p["checkpoint_sec"] for p in sink.of("STOP_TIMING_PERSISTENCE_CHECKPOINT")] == [5, 15, 30]
    assert host.submissions == []


def test_report_reconstructs_orders_and_settlement_link(tmp_path):
    path = tmp_path / "journal.db"
    db = TradeJournalDB(path, backup_interval_sec=3600)
    try:
        sink = Sink()
        tel = StopTimingTelemetry(emit=sink, run_id="r1")
        t0 = END - 500
        _obs(tel, t0, twap=100020.0)
        _obs(tel, t0 + 5, twap=99990.0, bid="0.40")
        opp = _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN")
        _obs(tel, t0 + 70, twap=99970.0, bid="0.40",
             decision=_decision("absolute_max_loss_breaker", "-2.55", "-2.45", "0.40"), signal=opp,
             votes={"weakening_count": 2, "available_count": 2}, persistence=20.0)
        tel.on_settlement_safe(slug=SLUG, outcome="DOWN", settlement_ts=END + 1)
        for event_type, payload in sink.rows:
            db.log_strategy_event("r1", event_type, {"sizing_rule_version_runtime": SIZING_RULE_VERSION, **payload})
        db.log_strategy_event("r1", "MARKET_SETTLEMENT", {"slug": SLUG, "outcome": "DOWN", "outcome_source": "canonical_twap"})
        db.log_order_event("r1", "ORDER_SUBMIT", client_order_id="BTC-15M-MAKER-BUY-1", side="BUY", price=0.65,
                           qty=10, status="SUBMITTED", instrument_id="up-token", payload={"slug": SLUG})
        db.log_order_event("r1", "ORDER_FILLED", client_order_id="BTC-15M-MAKER-BUY-1", side="BUY", price=0.65,
                           qty=10, status="FILLED", instrument_id="up-token")
        db.log_order_event("r1", "ORDER_TAKER_EXIT_SUBMIT", client_order_id="TAKER-EXIT-1", side="SELL", price=0.40,
                           qty=10, status="SUBMITTED", reason="stop_loss", instrument_id="up-token",
                           payload={"slug": SLUG, "decision_reason": "absolute_max_loss_breaker"})
        db.log_order_event("r1", "ORDER_REJECTED", client_order_id="TAKER-EXIT-1", status="REJECTED",
                           instrument_id="up-token")
    finally:
        db.stop()
    conn = sqlite3.connect(path)
    m = reconstruct(conn, "r1")[SLUG]
    pos = next(iter(m["positions"].values()))
    assert pos["entry"]["held_side"] == "UP" and pos["entry"]["entry_qty"] == 10.0
    assert pos["entry"]["sizing_rule_version_runtime"] == SIZING_RULE_VERSION
    assert pos["crosses"][0]["obs_wall_ts"] == t0 + 5
    assert pos["first"]["abs_breaker_eligible"]["obs_wall_ts"] == t0 + 70
    assert pos["settlement"]["counterfactual_hold_gross_pnl"] == pytest.approx(-6.5)
    assert m["settlement"]["runtime_outcome"] == "DOWN"
    buy, sell = m["orders"]["BTC-15M-MAKER-BUY-1"], m["orders"]["TAKER-EXIT-1"]
    assert buy["kind"] == "BUY" and buy["filled_qty"] == 10.0 and buy["submit_count"] == 1
    assert sell["kind"] == "PROTECTIVE_SELL" and sell["rejected"] and sell["filled_qty"] == 0
    assert sell["events"][0]["reason"] == "absolute_max_loss_breaker"


def test_deferred_relabel_emits_follow_up_settlement_row():
    # LIVE run_1791621743 / btc-updown-15m-1791623700: MARKET_SETTLEMENT fired
    # UNKNOWN, then canonical_twap_deferred_relabel DOWN one second later.
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    _obs(tel, END - 300, twap=100010.0)
    tel.on_settlement_safe(slug=SLUG, outcome="UNKNOWN", settlement_ts=END)
    tel.on_settlement_relabel_safe(slug=SLUG, outcome="DOWN", settlement_ts=END, relabel_ts=END + 1,
                                   reference_source="polymarket_chainlink_twap_60s_ws",
                                   reference_is_canonical=True,
                                   outcome_source="canonical_twap_deferred_relabel")
    first, follow = sink.of("STOP_TIMING_POSITION_SETTLEMENT")
    assert first["settlement_outcome_runtime"] == "UNKNOWN" and first["counterfactual_hold_gross_pnl"] is None
    assert first["settlement_relabel"] is False
    assert follow["settlement_relabel"] is True
    assert follow["position_epoch"] == first["position_epoch"]
    assert follow["settlement_outcome_runtime"] == "DOWN"
    assert follow["superseded_outcome_runtime"] == "UNKNOWN"
    assert follow["settlement_outcome_source"] == "canonical_twap_deferred_relabel"
    assert follow["settlement_relabel_ts"] == END + 1 and follow["settlement_ts"] == END
    assert follow["counterfactual_hold_gross_pnl"] == pytest.approx(10.0 * (0.0 - 0.65))
    assert follow["first_times"] == first["first_times"]
    # Exactly once; unknown slugs and UNKNOWN relabels emit nothing.
    tel.on_settlement_relabel_safe(slug=SLUG, outcome="DOWN", settlement_ts=END)
    tel.on_settlement_relabel_safe(slug="other", outcome="UP", settlement_ts=END)
    assert len(sink.of("STOP_TIMING_POSITION_SETTLEMENT")) == 2


def test_known_settlement_is_not_retained_for_relabel():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    _obs(tel, END - 300, twap=100010.0)
    tel.on_settlement_safe(slug=SLUG, outcome="UP", settlement_ts=END)
    tel.on_settlement_relabel_safe(slug=SLUG, outcome="DOWN", settlement_ts=END)
    (only,) = sink.of("STOP_TIMING_POSITION_SETTLEMENT")
    assert only["settlement_outcome_runtime"] == "UP"


def test_report_uses_the_relabelled_settlement_row(tmp_path):
    path = tmp_path / "journal.db"
    db = TradeJournalDB(path, backup_interval_sec=3600)
    try:
        sink = Sink()
        tel = StopTimingTelemetry(emit=sink, run_id="r1")
        _obs(tel, END - 300, twap=100010.0)
        tel.on_settlement_safe(slug=SLUG, outcome="UNKNOWN", settlement_ts=END)
        tel.on_settlement_relabel_safe(slug=SLUG, outcome="DOWN", settlement_ts=END, relabel_ts=END + 1,
                                       outcome_source="canonical_twap_deferred_relabel")
        for event_type, payload in sink.rows:
            db.log_strategy_event("r1", event_type, payload)
    finally:
        db.stop()
    pos = next(iter(reconstruct(sqlite3.connect(path), "r1")[SLUG]["positions"].values()))
    assert pos["settlement"]["settlement_outcome_runtime"] == "DOWN"
    assert pos["settlement"]["settlement_relabel"] is True
    assert pos["settlement"]["superseded_outcome_runtime"] == "UNKNOWN"
    assert pos["settlement"]["counterfactual_hold_gross_pnl"] == pytest.approx(-6.5)

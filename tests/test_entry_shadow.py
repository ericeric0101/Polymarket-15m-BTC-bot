"""E5B entry shadow (D / C / B): shadow-only telemetry, frozen E5A semantics."""
import ast
import dataclasses
import inspect
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.entry_shadow as es
import bot.entry_shadow_runtime as esr
from bot.entry_shadow import (
    B_HORIZON_SEC,
    C_HORIZON_SEC,
    D_HORIZONS_SEC,
    EdgeCalibration,
    EntryShadowSnapshot,
    EntryShadowTracker,
    QuoteSample,
    depth_state,
    edge_cell,
    evaluate_chb,
    evaluate_ec1,
    evaluate_edge,
    taker_fee_frac,
)

ROOT = Path(__file__).resolve().parents[1]
SLUG = "btc-updown-15m-1791700000"   # Taipei Sun 2026-10-11 -> WEEKEND
CAL = EdgeCalibration.load()


def snap(**kw):
    base = dict(
        schema_version="entry_shadow_v1", timestamp=1000.0, attempt_id=f"{SLUG}|c1", market=SLUG, instrument="up",
        side="UP", horizon_sec=15.0, tte_sec=400.0, side_score=0.35, side_score_signed=0.35,
        entry_side_score_signed=0.40, strike=100.0, strike_verified=True, quote_receipt_ts=1014.0,
        quote_age_at_horizon_sec=1.0, observation_state="OBSERVED", best_bid=0.69, best_ask=0.70, spread=0.01,
        top_ask_size=50.0, top_bid_size=40.0, l2_age_sec=0.5, binance_spot=100.0, binance_move_bps_signed=0.0,
        chainlink_spot=100.0, chainlink_age_sec=1.0, chainlink_move_bps_signed=0.0, twap_value=100.0,
        twap_state="FAVORABLE", signed_distance_usd=1.0, placement_ask=0.70, placement_bid=0.69,
        planned_maker_price=0.69, planned_qty=10.0, maker_age_sec=15.0, maker_filled_qty=0.0,
        maker_status="RESTING", canonical_filled_by_horizon=False, market_filled_by_horizon=False,
        production_action="MAKER_RESTING", market_day_type="WEEKEND")
    base.update(kw)
    return EntryShadowSnapshot(**base)


# ------------------------------------------------------------------ D
def test_d3_unknown_on_insufficient_features():
    assert evaluate_edge(snap(observation_state="CENSORED", best_ask=None), CAL)["edge_state"] == "UNKNOWN"
    assert evaluate_edge(snap(tte_sec=None), CAL)["edge_state"] == "UNKNOWN"
    assert evaluate_edge(snap(best_ask=0.98), CAL)["edge_state"] == "UNKNOWN"   # outside calibrated range
    assert evaluate_edge(snap(), EdgeCalibration(None))["edge_state"] == "UNKNOWN"
    insufficient = EdgeCalibration({"version": "t", "cells": {edge_cell(0.70, 400.0): {"usable": False, "residual_shrunk": 0.1}}})
    assert evaluate_edge(snap(), insufficient)["calibration_status"] == "CELL_INSUFFICIENT"


def test_d4_fee_matches_project_fee_model():
    from execution.rebate_model import estimate_taker_buy_fee_shares
    frac = taker_fee_frac(0.70)
    assert frac == pytest.approx(float(estimate_taker_buy_fee_shares(shares=Decimal("10"), probability=Decimal("0.70"))) / 10)
    assert frac == pytest.approx(0.072 * 0.70 * 0.30, rel=1e-6)   # E5A-verified curve (0.1512 sh per 10 sh)
    out = evaluate_edge(snap(), CAL)
    p = out["calibrated_probability"]
    assert out["fee_equivalent"] == pytest.approx(p * frac, abs=1e-6)
    assert out["net_edge_before_selection"] == pytest.approx(out["raw_edge"] - out["fee_equivalent"], abs=2e-6)
    assert out["net_edge_after_selection_penalty_reference"] == pytest.approx(out["net_edge_before_selection"] - 0.018, abs=2e-6)
    assert out["net_edge_after_selection_penalty_none"] == out["net_edge_before_selection"]


def test_d_calibration_is_market_anchored_residual_form():
    out = evaluate_edge(snap(best_ask=0.66), CAL)
    out2 = evaluate_edge(snap(best_ask=0.74), CAL)
    # Same cell -> same residual: raw edge does not depend on position inside the bucket.
    assert out["calibration_cell"] == out2["calibration_cell"]
    assert out["raw_edge"] == pytest.approx(out2["raw_edge"], abs=1e-6)
    doc = json.loads((ROOT / "config/research/entry_edge_calibration_v1.json").read_text())
    assert doc["forward_cutoff_ts"] == es.FORWARD_CUTOFF_TS and doc["estimator"] == "ask_plus_shrunk_cell_residual"


def test_d5_d6_no_future_or_outcome_inputs():
    params = set(inspect.signature(evaluate_edge).parameters)
    assert params == {"snapshot", "calibration"}
    fields = {f.name for f in dataclasses.fields(EntryShadowSnapshot)}
    for forbidden in ("official_outcome", "win", "eventual_fill", "future_ask", "settlement_outcome"):
        assert forbidden not in fields
    # Runtime only needs the frozen calibration file, never post-cutoff outcomes.
    src = inspect.getsource(es)
    assert "official_outcome" not in src and "e0_labels" not in src


def test_d7_deterministic_for_same_snapshot():
    s = snap()
    assert evaluate_edge(s, CAL) == evaluate_edge(s, CAL)


# ------------------------------------------------------------------ C (EC1@15s)
@pytest.mark.parametrize("ask,expected", [(0.69, "WOULD_CANCEL"), (0.68, "WOULD_CANCEL"), (0.70, "WOULD_KEEP"), (0.72, "WOULD_KEEP")])
def test_c1_c2_ec1_exact_rule(ask, expected):
    assert evaluate_ec1(snap(best_ask=ask, placement_ask=0.70))["decision"] == expected


def test_c3_insufficient_state_unknown_and_filled_not_applicable():
    assert evaluate_ec1(snap(observation_state="CENSORED", best_ask=None))["decision"] == "UNKNOWN"
    assert evaluate_ec1(snap(placement_ask=None))["decision"] == "UNKNOWN"
    assert evaluate_ec1(snap(canonical_filled_by_horizon=True))["decision"] == "NOT_APPLICABLE"


# ------------------------------------------------------------------ B (CH_B@10s)
@pytest.mark.parametrize("ask,score,spread,expected", [
    (0.75, 0.10, 0.02, "WOULD_CHASE"), (0.75, 0.0, 0.03, "WOULD_CHASE"),
    (0.74, 0.10, 0.02, "WOULD_NOT_CHASE"), (0.75, -0.01, 0.02, "WOULD_NOT_CHASE"), (0.75, 0.10, 0.04, "WOULD_NOT_CHASE")])
def test_b1_b2_chb_exact_rule(ask, score, spread, expected):
    s = snap(horizon_sec=10.0, best_ask=ask, best_bid=ask - spread, spread=spread, side_score_signed=score, placement_ask=0.70)
    assert evaluate_chb(s)["decision"] == expected


def test_b3_insufficient_depth_is_unknown_not_executable():
    assert depth_state(None, None, 10.0, None)["depth_state"] == "DEPTH_UNKNOWN"
    assert depth_state(None, 4.0, 10.0, None)["depth_state"] == "DEPTH_PARTIAL"
    full = depth_state(((0.75, 6.0), (0.76, 10.0)), 6.0, 10.0, 0.4)
    assert full["depth_state"] == "DEPTH_SUPPORTED" and full["depth_vwap"] == pytest.approx((6 * 0.75 + 4 * 0.76) / 10)
    assert depth_state(((0.75, 3.0),), 3.0, 10.0, 0.4)["depth_state"] == "DEPTH_PARTIAL"


# ------------------------------------------------------------------ tracker
class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def sample(t, ask, bid=None, score=0.35, size=50.0):
    return QuoteSample(sample_ts=t, quote_receipt_ts=t, best_bid=Decimal(str(bid if bid is not None else ask - 0.01)),
                       best_ask=Decimal(str(ask)), top_ask_size=Decimal(str(size)), top_bid_size=Decimal("40"),
                       side_score=score, tte_sec=400.0, strike=100.0, strike_verified=True)


def make_tracker(d=True, c=True, b=True):
    events = []
    tracker = EntryShadowTracker(emit=lambda t, p: events.append((t, p)) or True, calibration=CAL,
                                 d_enabled=d, c_enabled=c, b_enabled=b)
    return tracker, events


def run_path(tracker, path, place=1000.0, end=1045.0, step=0.5):
    """path(t) -> ask; ticks every `step` seconds."""
    t = place
    while t <= end:
        tracker.on_tick(t, lambda inst, t=t: sample(t, path(t)))
        t += step


def test_s1_one_immutable_snapshot_per_horizon_shared_by_strategies():
    tracker, events = make_tracker()
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    run_path(tracker, lambda t: 0.70 if t < 1012 else 0.68)
    d15 = next(p for t, p in events if t == "ENTRY_EDGE_SHADOW" and p["horizon_sec"] == 15)
    c15 = next(p for t, p in events if t == "ENTRY_EARLY_CANCEL_SHADOW")
    snapshot_keys = {f.name for f in dataclasses.fields(EntryShadowSnapshot)} - {"timestamp"}
    assert {k: d15[k] for k in snapshot_keys} == {k: c15[k] for k in snapshot_keys}
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap().best_ask = 0.5


def test_c5_c6_b6_one_canonical_event_per_attempt_strategy_horizon():
    tracker, events = make_tracker()
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    run_path(tracker, lambda t: 0.70 if t < 1008 else 0.76, step=0.1)   # many ticks
    kinds = [(t, p.get("horizon_sec")) for t, p in events]
    assert kinds.count(("ENTRY_EARLY_CANCEL_SHADOW", 15.0)) == 1
    assert kinds.count(("ENTRY_CHASE_SHADOW", 10.0)) == 1
    assert sorted(h for t, h in kinds if t == "ENTRY_EDGE_SHADOW") == [float(h) for h in D_HORIZONS_SEC]
    assert not [k for k in kinds if k[0] == "ENTRY_EARLY_CANCEL_SHADOW" and k[1] != 15.0]
    assert not [k for k in kinds if k[0] == "ENTRY_CHASE_SHADOW" and k[1] != 10.0]


def test_c1_tracker_ec1_triggers_and_b1_tracker_chb_triggers():
    tracker, events = make_tracker()
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    run_path(tracker, lambda t: 0.70 if t < 1012 else 0.69)
    c = next(p for t, p in events if t == "ENTRY_EARLY_CANCEL_SHADOW")
    assert c["c_rule_decision"] == "WOULD_CANCEL" and c["ec1_ask_change"] == pytest.approx(-0.01)
    tracker2, events2 = make_tracker()
    tracker2.on_buy_submit(coid="c2", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                           sample=sample(999.5, 0.70))
    run_path(tracker2, lambda t: 0.70 if t < 1008 else 0.75)
    b = next(p for t, p in events2 if t == "ENTRY_CHASE_SHADOW")
    assert b["b_rule_decision"] == "WOULD_CHASE" and b["chase_economic_state"] == "EXECUTABLE"
    assert b["exec_ask_at_latency"] == pytest.approx(0.75)


def test_b5_latency_price_never_changes_the_decision():
    tracker, events = make_tracker(d=False, c=False, b=True)
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    # Escape is only visible AFTER the 10 s decision (during the latency window).
    run_path(tracker, lambda t: 0.70 if t < 1011 else 0.90)
    b = next(p for t, p in events if t == "ENTRY_CHASE_SHADOW")
    assert b["b_rule_decision"] == "WOULD_NOT_CHASE"
    assert b["exec_ask_at_latency"] == pytest.approx(0.90)   # recorded for economics only


def test_cancel_race_and_filled_before_horizon():
    tracker, events = make_tracker(d=False, c=True, b=False)
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    run_path(tracker, lambda t: 0.70 if t < 1012 else 0.69, end=1015.5)
    tracker.on_fill(coid="c1", fill_ts=1016.0, qty=10)          # inside [15, 17.4]
    run_path(tracker, lambda t: 0.69, place=1016.0, end=1045.0)
    c = next(p for t, p in events if t == "ENTRY_EARLY_CANCEL_SHADOW")
    assert c["c_rule_decision"] == "WOULD_CANCEL" and c["race_canonical_fill_within_latency"] is True
    tracker2, events2 = make_tracker(d=False, c=True, b=False)
    tracker2.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                           sample=sample(999.5, 0.70))
    tracker2.on_fill(coid="c1", fill_ts=1005.0, qty=10)
    run_path(tracker2, lambda t: 0.69)
    assert next(p for t, p in events2 if t == "ENTRY_EARLY_CANCEL_SHADOW")["c_rule_decision"] == "NOT_APPLICABLE"


def test_s2_canonical_attempt_identity_survives_reject_and_replace():
    tracker, events = make_tracker()
    tracker.on_buy_submit(coid="rej", market=SLUG, instrument="up", side="UP", maker_price=0.70, qty=10, place_ts=990.0,
                          sample=sample(989.5, 0.70))
    tracker.on_reject(coid="rej")                                 # post-only cross: not an attempt
    assert SLUG not in tracker.attempts
    tracker.on_buy_submit(coid="c1", market=SLUG, instrument="up", side="UP", maker_price=0.69, qty=10, place_ts=1000.0,
                          sample=sample(999.5, 0.70))
    tracker.on_buy_submit(coid="c2", market=SLUG, instrument="up", side="UP", maker_price=0.68, qty=10, place_ts=1004.0,
                          sample=sample(1003.5, 0.70))           # requote: linked, not a new attempt
    assert tracker.attempts[SLUG].attempt_id == f"{SLUG}|c1" and tracker.attempts[SLUG].linked_coids == ["c2"]
    run_path(tracker, lambda t: 0.70)
    assert {p["attempt_id"] for t, p in events} == {f"{SLUG}|c1"}
    assert tracker.counters["attempts"] == 1


def test_s3_weekend_field_recorded_but_not_a_decision_input():
    weekday = snap(market_day_type="WEEKDAY")
    weekend = snap(market_day_type="WEEKEND")
    for fn in (evaluate_ec1, evaluate_chb):
        assert fn(weekday) == fn(weekend)
    assert evaluate_edge(weekday, CAL) == evaluate_edge(weekend, CAL)
    assert es.market_day_type(SLUG) == "WEEKEND"


# ------------------------------------------------------------------ shadow-only isolation
FORBIDDEN_CALLS = {"submit_order", "cancel_order", "cancel_all_orders", "modify_order", "replace_order",
                   "_cancel_maker_order_key", "_cancel_maker_order_side", "_submit_taker_exit_order",
                   "_submit_maker_order", "order_factory", "_cancel_active_maker_orders"}


@pytest.mark.parametrize("module", [es, esr])
def test_d1_d2_c4_b4_shadow_modules_contain_no_order_side_effects(module):
    tree = ast.parse(inspect.getsource(module))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (names & FORBIDDEN_CALLS)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any("nautilus" in m or "order_submission" in m or "taker_exit" in m for m in modules)
    assert "OrderSide" not in imported


class _StrictStrategy:
    """Any order side-effect raises; records attribute writes."""

    def __init__(self, tracker):
        self.entry_shadow = tracker
        self.current_market_slug = SLUG
        self.current_market_end_timestamp = 1400.0
        self.latest_quote_by_inst = {"up": (Decimal("0.69"), Decimal("0.70"))}
        self.last_quote_update_ts_by_inst = {"up": 999.9}
        self.latest_quote_depth_by_inst = {"up": (Decimal("40"), Decimal("50"))}
        self.side_decision_score = Decimal("0.35")
        self.active_maker_orders = {"buy:up": {"price": "0.69"}}
        self.market_strike_cache_by_slug = {SLUG: 100.0}

    def _instrument_key(self, inst): return str(inst)
    def _side_for_instrument_id(self, inst): return SimpleNamespace(value="UP")
    def submit_order(self, *a, **k): raise AssertionError("shadow submitted an order")
    def cancel_order(self, *a, **k): raise AssertionError("shadow cancelled an order")
    def _cancel_maker_order_key(self, *a, **k): raise AssertionError("shadow cancelled an order")
    def _cancel_maker_order_side(self, *a, **k): raise AssertionError("shadow cancelled an order")


def test_s6_hooks_with_all_flags_on_never_touch_orders(monkeypatch):
    tracker, events = make_tracker()
    strategy = _StrictStrategy(tracker)
    before = json.dumps(strategy.active_maker_orders, sort_keys=True)
    t = {"now": 1000.0}
    monkeypatch.setattr(esr.time, "time", lambda: t["now"])
    esr.hook_buy_submit(strategy, order=SimpleNamespace(client_order_id="c1"), instrument_id="up", limit_price=0.69, qty=10)
    for i in range(1, 90):
        t["now"] = 1000.0 + i * 0.5
        strategy.last_quote_update_ts_by_inst["up"] = t["now"]
        esr.hook_tick(strategy)
    esr.hook_fill(strategy, "c1", 10)
    esr.hook_cancel_ack(strategy, "c1", None, "requote")
    esr.hook_reject(strategy, "zzz")
    assert json.dumps(strategy.active_maker_orders, sort_keys=True) == before
    assert {t for t, p in events} >= {"ENTRY_EDGE_SHADOW", "ENTRY_EARLY_CANCEL_SHADOW", "ENTRY_CHASE_SHADOW"}
    assert tracker.counters["exceptions"] == 0


def test_s5_hooks_are_noops_when_shadow_absent():
    class Exploding:
        def __getattr__(self, name):
            if name == "entry_shadow":
                return None
            raise AssertionError(f"hook touched strategy.{name} with shadow off")
    s = Exploding()
    esr.hook_buy_submit(s, order=None, instrument_id="up", limit_price=0.7, qty=10)
    esr.hook_tick(s)
    esr.hook_fill(s, "c1", 1)
    esr.hook_cancel_ack(s, "c1", None, None)
    esr.hook_reject(s, "c1")


def test_s4_s12_flags_default_off_and_in_manifest(monkeypatch):
    from bot.app_config import AppConfig
    from bot.research.provenance import build_run_manifest
    keys = ("PRICE_ADJUSTED_EDGE_SHADOW_ENABLED", "EARLY_CANCEL_SHADOW_ENABLED", "MAKER_CHASE_SHADOW_ENABLED")
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    ops = AppConfig.from_env(enable_terminal_dashboard=False).operations
    assert (ops.price_adjusted_edge_shadow_enabled, ops.early_cancel_shadow_enabled, ops.maker_chase_shadow_enabled) == (False,) * 3
    off = build_run_manifest(run_id="r", config=AppConfig.from_env(enable_terminal_dashboard=False), mode="T", test_mode=True, maker_mode=True)
    monkeypatch.setenv("EARLY_CANCEL_SHADOW_ENABLED", "1")
    on = build_run_manifest(run_id="r", config=AppConfig.from_env(enable_terminal_dashboard=False), mode="T", test_mode=True, maker_mode=True)
    assert "early_cancel_shadow_enabled" in json.dumps(off) and off["config_hash"] != on["config_hash"]
    assert esr.build_entry_shadow(SimpleNamespace(), d=False, c=False, b=False) is None
    assert "=0" in esr.entry_shadow_summary(False, False, False)


def test_s7_to_s11_production_modules_untouched_by_e5b():
    """Breaker V2, TP, maker pricing, sizing and entry thresholds are not edited."""
    import subprocess
    changed = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only",
                              "08bcb32347db33341395553e542fa43426d5b67b", "--", "bot", "execution"],
                             capture_output=True, text=True, check=False).stdout.split()
    allowed = {"bot/entry_shadow.py", "bot/entry_shadow_runtime.py", "bot/order_submission.py", "bot/order_events.py",
               "bot/quote_runtime.py", "bot/app_config.py", "bot/settings.py", "bot/runtime_env.py"}
    if not changed:
        pytest.skip("not a git checkout of the E5B branch")
    assert set(changed) <= allowed
    for protected in ("bot/exit_engine.py", "bot/taker_exit.py", "bot/breaker_authority.py", "bot/entry_sizing.py",
                      "bot/quote_service.py", "bot/quoting.py", "bot/entry_decision.py", "bot/side_decision.py"):
        assert protected not in changed

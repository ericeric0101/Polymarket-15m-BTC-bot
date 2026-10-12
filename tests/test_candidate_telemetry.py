"""Entry V3 candidate telemetry + execution-selection shadow (hermetic, no network, no LIVE data).

Numbers in test names refer to the setup brief's S7 list (1..25).
"""
import ast
import dataclasses
import hashlib
import importlib.util
import inspect
import json
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.candidate_telemetry as ct
import bot.execution_selection_eval as ev
from bot.strike_finality import StrikeFinalityTracker, parse_crypto_price_payload

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / "tests/fixtures/e5b_v1_replay_fixture.json").read_text())
SLUG = "btc-updown-15m-1791711900"
START = 1791711900.0
FORBIDDEN = {"submit_order", "cancel_order", "modify_order", "cancel_all_orders", "_submit_taker_exit_order",
             "_cancel_maker_order_key", "_cancel_maker_order_side", "submit_maker_quote", "order_factory",
             "_place_order", "redeem", "_schedule_auto_redeem"}


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _MemSink:
    def __init__(self):
        self.records = []

    def put(self, rec):
        self.records.append(json.loads(json.dumps(rec, default=str)))
        return True


class _Strategy:
    """Cached state only; any order side effect raises."""

    def __init__(self, *, bid=0.73, ask=0.74, receipt=START + 300.0, pex=(0.36, 0.64), snap_ts=START + 299.5,
                 score=Decimal("-0.30"), strike=82956.85, spot=82936.12, snapshotter=True, tracker=None):
        self.run_id = "run_test"
        self.runtime_git_revision = "deadbeef"
        self.current_market_slug = SLUG
        self.current_market_end_timestamp = START + 900
        self.latest_quote_by_inst = {"down": (bid, ask)}
        self.last_quote_update_ts_by_inst = {"down": receipt}
        self.latest_quote_depth_by_inst = {"down": (40.0, 50.0)}
        self.side_decision_score = score
        self.market_strike_cache_by_slug = {SLUG: strike}
        self.latest_external_spot = spot
        self.strike_finality = tracker
        self.absolute_max_loss_enabled = True
        self.absolute_max_loss_usdc = Decimal("2.00")
        self.tail_protect_tp_enabled = True
        self.tail_protect_tp_price = Decimal("0.97")
        self.hold_to_redeem_enabled = True
        if snapshotter:
            snap = {"snapshot_ts": snap_ts, "p_up_ex_market": pex[0], "p_down_ex_market": pex[1],
                    "required_move_sigma": 0.33, "market_quote_source_ts": snap_ts + 0.3}
            self.prediction_research_snapshotter = SimpleNamespace(_last_payload_by_slug={SLUG: snap})

    def _instrument_key(self, inst): return str(inst)
    def _normalize_instrument_id(self, inst): return inst
    def _side_for_instrument_id(self, inst): return SimpleNamespace(value="DOWN" if inst == "down" else "UP")
    def _extract_token_id_from_instrument(self, inst): return f"tok-{inst}"
    def submit_order(self, *a, **k): raise AssertionError("telemetry submitted an order")
    def cancel_order(self, *a, **k): raise AssertionError("telemetry cancelled an order")
    def _cancel_maker_order_key(self, *a, **k): raise AssertionError("telemetry cancelled an order")
    def _submit_taker_exit_order(self, *a, **k): raise AssertionError("telemetry sold")


def _recorder(strategy, *, fqo=True, markout=True, t=START + 300.2):
    sink = _MemSink()
    clock = _Clock(t)
    rec = ct.CandidateTelemetryRecorder(sink=sink, run_header=ct.build_run_header(strategy, {"X": True}),
                                        fqo_enabled=fqo, markout_enabled=markout, clock=clock, mono=lambda: 123)
    return rec, sink, clock


def _decide(rec, strategy, et="ORDER_SUBMIT", price=0.74, coid="c1", reason=None):
    rec.observe_order_event(strategy, event_type=et, side="BUY", price=price, qty=5.5, client_order_id=coid,
                            reason=reason, payload={"instrument_id": "down", "p_fair": 0.745})


def _kinds(sink, kind):
    return [r for r in sink.records if r.get("record_type") == kind]


# 1 ----------------------------------------------------------------------------------------------
def test_1_candidate_id_deterministic_and_restart_safe():
    a = ct.candidate_id(SLUG, "FIRST_ACCEPTED", "DOWN", "run1", 1)
    assert a == ct.candidate_id(SLUG, "FIRST_ACCEPTED", "UP", "run2", 9)  # stable role ignores run/seq
    t1 = ct.candidate_id(SLUG, "TRANSITION", "DOWN", "run1", 3)
    t2 = ct.candidate_id(SLUG, "TRANSITION", "DOWN", "run2", 3)  # restart: same seq, new run -> no collision
    assert t1 != t2 and t1 == ct.candidate_id(SLUG, "TRANSITION", "DOWN", "run1", 3)
    assert len({a, t1, t2, ct.candidate_id(SLUG, "FIRST_EVALUATION")}) == 4


# 2 ----------------------------------------------------------------------------------------------
def test_2_snapshot_is_immutable_and_value_copied():
    s = _Strategy()
    rec, sink, _ = _recorder(s)
    _decide(rec, s)
    snap_rec = _kinds(sink, "CANDIDATE")[0]
    s.latest_quote_by_inst["down"] = (0.10, 0.11)
    s.prediction_research_snapshotter._last_payload_by_slug[SLUG]["p_down_ex_market"] = 0.01
    assert snap_rec["best_ask"] == 0.74 and snap_rec["raw_p_ex"] == 0.64
    snap = ct.build_snapshot(s, event_type="ORDER_SUBMIT", role="FIRST_ACCEPTED", side_hint=None, price=0.74, qty=5.5,
                             client_order_id="c", reason=None, payload={"instrument_id": "down"}, run_fingerprint="f",
                             seq=1, wall_ts=START + 301, mono_ns=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.best_ask = 0.5


# 3 ----------------------------------------------------------------------------------------------
def test_3_decision_ts_is_input_tick_time_not_wall_clock():
    s = _Strategy(receipt=START + 299.9)
    rec, sink, _ = _recorder(s, t=START + 300.4)
    _decide(rec, s)
    c = _kinds(sink, "CANDIDATE")[0]
    assert c["decision_ts"] == START + 299.9 and c["decision_ts_source"] == "BBO_RECEIPT_TS"
    assert c["decision_wall_ts"] == START + 300.4 and c["decision_monotonic_ns"] == 123
    s2 = _Strategy()
    s2.last_quote_update_ts_by_inst = {}
    rec2, sink2, _ = _recorder(s2)
    _decide(rec2, s2)
    assert _kinds(sink2, "CANDIDATE")[0]["decision_ts_source"] == "WALL_FALLBACK"


# 4, 5 -------------------------------------------------------------------------------------------
def test_4_5_accepted_and_rejected_with_reason_are_recorded():
    s = _Strategy()
    rec, sink, _ = _recorder(s)
    _decide(rec, s, et="ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW", coid=None, reason="first_entry_too_early")
    _decide(rec, s, et="ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW", coid=None, reason="first_entry_too_early")  # no change
    _decide(rec, s, et="ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE", coid=None, reason="gate")
    _decide(rec, s, et="ORDER_SUBMIT", coid="c1")
    roles = [(c["candidate_role"], c["eligibility"], c["reject_reason"]) for c in _kinds(sink, "CANDIDATE")]
    assert roles == [("FIRST_EVALUATION", "REJECTED", "first_entry_too_early"), ("TRANSITION", "REJECTED", "gate"),
                     ("FIRST_ACCEPTED", "ACCEPTED", None)]
    canon = ev.canonical_candidates(sink.records)
    assert len(canon) == 1 and canon[0]["candidate_role"] == "FIRST_ACCEPTED"


def test_5_never_accepted_market_uses_first_evaluation_as_canonical():
    s = _Strategy()
    rec, sink, _ = _recorder(s)
    _decide(rec, s, et="ORDER_OBSERVE_BUY_BLOCKED", coid=None, reason="reduce_only")
    canon = ev.canonical_candidates(sink.records)
    assert canon[0]["candidate_role"] == "FIRST_EVALUATION" and canon[0]["reject_reason"] == "reduce_only"


# 6 ----------------------------------------------------------------------------------------------
def test_6_unfilled_candidate_is_recorded_with_path_stats():
    s = _Strategy()
    rec, sink, clock = _recorder(s)
    _decide(rec, s)
    for i in range(5):
        clock.t += 1
        s.last_quote_update_ts_by_inst["down"] = clock.t
        rec.sample(s)
    rec.close_market(SLUG, clock.t)
    (path,) = _kinds(sink, "PATH_STATS")
    assert path["canonical_fill_ts"] is None and path["first_quote_price"] == 0.74 and path["path_samples"] == 5
    assert ev.classify_fqo(path)["lower"] == ev.NOT_FILL  # never withdrawn, never filled: observed


# 7 ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw,reason", [({"snapshotter": False}, "PEX_SNAPSHOTTER_ABSENT"),
                                       ({"snap_ts": START + 280.0}, "PEX_STALE"),
                                       ({"pex": (None, None)}, "PEX_NOT_COMPUTED_AT_DECISION")])
def test_7_pex_missing_reason_is_explicit(kw, reason):
    s = _Strategy(**kw)
    rec, sink, _ = _recorder(s)
    _decide(rec, s)
    c = _kinds(sink, "CANDIDATE")[0]
    assert c["raw_p_ex"] is None and c["pex_reason"] == reason and c["missing_reasons"]["raw_p_ex"] == reason


def test_7_upstream_clock_lead_does_not_blank_pex():
    """Root cause of the 52% gap: a source clock ahead of the host must not reject the snapshot."""
    s = _Strategy()
    rec, sink, _ = _recorder(s)
    _decide(rec, s)
    c = _kinds(sink, "CANDIDATE")[0]
    assert c["raw_p_ex"] == 0.64 and c["pex_reason"] is None
    assert c["source_clock_lead_sec"] == pytest.approx(0.3)


# 8, 9, 13 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("module", [ct, ev])
def test_8_9_13_shadow_modules_have_no_order_authority(module):
    tree = ast.parse(inspect.getsource(module))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (names & FORBIDDEN)
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(x in m for m in mods for x in ("nautilus", "order_submission", "taker_exit", "order_runtime",
                                                 "execution", "py_clob_client"))


def test_8_9_13_runtime_full_lifecycle_never_touches_orders():
    s = _Strategy()
    rec, sink, clock = _recorder(s)
    _decide(rec, s, price=0.71, coid="c1")
    _decide(rec, s, price=0.73, coid="c2")  # requote
    rec.observe_order_event(s, event_type="ORDER_CANCELED", side="BUY", price=0.71, qty=5.5, client_order_id="c1",
                            reason="requote", payload={})
    for i in range(40):
        clock.t += 1
        s.last_quote_update_ts_by_inst["down"] = clock.t
        rec.sample(s)
    rec.observe_order_event(s, event_type="ORDER_FILLED", side="BUY", price=0.73, qty=5.5, client_order_id="c2",
                            reason=None, payload={})
    rec.close_market(SLUG, clock.t)
    assert rec.counters["exceptions"] == 0


# 10, 11, 12 -------------------------------------------------------------------------------------
def _v1_snapshot(row, tte=None):
    spec = importlib.util.spec_from_file_location("rep", ROOT / "scripts/replay_entry_shadow_e5a.py")
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    from bot.entry_shadow import EntryShadowSnapshot
    s = rep.to_snapshot(row)
    return EntryShadowSnapshot(**{**s.as_payload(), "tte_sec": rep._num(tte)}) if tte is not None else s


def test_10_11_12_v1_files_byte_identical_to_frozen():
    for rel, sha in FIX["v1_file_sha256"].items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == sha, rel


def test_10_c_ec1_reproduces_frozen_v1_rows():
    from bot.entry_shadow import evaluate_ec1
    for x in FIX["c"]:
        assert evaluate_ec1(_v1_snapshot(x["row"]))["decision"] == x["expected"]


def test_11_b_chb_reproduces_frozen_v1_rows():
    from bot.entry_shadow import evaluate_chb
    for x in FIX["b"]:
        assert evaluate_chb(_v1_snapshot(x["row"]))["decision"] == x["expected"]


def test_12_d_edge_reproduces_frozen_v1_rows():
    from bot.entry_shadow import EdgeCalibration, evaluate_edge
    cal = EdgeCalibration.load()
    for x in FIX["d"]:
        out = evaluate_edge(_v1_snapshot(x["row"], x["tte"]), cal)
        assert {k: out[k] for k in x["expected"]} == x["expected"]


# 14 ---------------------------------------------------------------------------------------------
def test_14_held_side_markouts_record_bid_ask_mid_and_freshness():
    s = _Strategy()
    rec, sink, clock = _recorder(s, fqo=False, markout=True)
    _decide(rec, s)
    for i in range(31):
        clock.t += 1
        if i < 20:
            s.last_quote_update_ts_by_inst["down"] = clock.t  # quote goes stale after 20 s
        rec.sample(s)
    mk = {m["horizon_sec"]: m for m in _kinds(sink, "MARKOUT") if m["anchor"] == "FIRST_SUBMIT"}
    assert set(mk) == set(ct.MARKOUT_HORIZONS_SEC)
    assert mk[5]["best_bid"] == 0.73 and mk[5]["best_ask"] == 0.74 and mk[5]["mid"] == pytest.approx(0.735)
    assert mk[5]["bbo_age_sec"] is not None and mk[5]["reason"] is None
    assert mk[30]["best_bid"] is None and mk[30]["reason"] == "BBO_STALE"


# 15, 16 -----------------------------------------------------------------------------------------
def test_15_outcomes_never_written_online():
    s = _Strategy()
    rec, sink, _ = _recorder(s)
    _decide(rec, s)
    blob = json.dumps(sink.records)
    for k in ("official_outcome", "\"win\"", "HOLD_Y", "winner"):
        assert k not in blob
    assert "official" not in inspect.getsource(ct).replace("officially", "")


def test_16_candidates_join_outcomes_and_paired_deltas():
    rows = ev.paired_rows([
        {"market_slug": "m1", "win": 1, "qty": 10, "current_filled": True, "current_fill_price": 0.74,
         "fqo": {"lower": ev.FILL, "upper": ev.FILL, "fill_price": 0.71}, "c_decision": "WOULD_KEEP"},
        {"market_slug": "m2", "win": 0, "qty": 10, "current_filled": True, "current_fill_price": 0.74,
         "fqo": {"lower": ev.NOT_FILL, "upper": ev.FILL, "fill_price": 0.71}, "c_decision": "WOULD_CANCEL",
         "c_removes_fill": True},
        {"market_slug": "m3", "win": 1, "qty": 10, "current_filled": False, "current_fill_price": None,
         "fqo": {"lower": ev.NOT_FILL, "upper": ev.NOT_FILL, "fill_price": 0.70}, "b_decision": "WOULD_CHASE",
         "b_chase_price": 0.80},
        {"market_slug": "m4", "win": None, "qty": 10},
    ])
    r1, r2, r3, r4 = rows
    assert r1["d_fqo_lower"] == pytest.approx(0.3) and r2["d_fqo_lower"] == pytest.approx(7.4)
    assert r2["d_fqo_upper"] == pytest.approx(0.3) and r2["d_c"] == pytest.approx(7.4)
    assert r3["d_b"] == pytest.approx(2.0) and r4["excluded"] == "OUTCOME_UNKNOWN"
    sel = {x["comparator"]: x for x in ev.selection_table(rows)}
    assert sel["FQO_LOWER"]["avoided_losers"] == 1 and sel["EARLY_CANCEL_C"]["avoided_losers"] == 1


# 17, 18 -----------------------------------------------------------------------------------------
def test_17_flags_default_off_and_no_recorder_built(monkeypatch):
    from bot.app_config import AppConfig
    for k in ("CANDIDATE_DECISION_TELEMETRY_ENABLED", "FIRST_QUOTE_ONLY_SHADOW_ENABLED",
              "HELD_SIDE_MARKOUT_TELEMETRY_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    ops = AppConfig.from_env(enable_terminal_dashboard=False).operations
    assert (ops.candidate_decision_telemetry_enabled, ops.first_quote_only_shadow_enabled,
            ops.held_side_markout_telemetry_enabled) == (False, False, False)
    assert ct.build_candidate_telemetry(SimpleNamespace(), candidate=False, fqo=True, markout=True) is None
    assert ct.flags_from_env({}) == {k: False for k in ct.flags_from_env({})}


def _db_host_class():
    from bot.db_runtime import StrategyDBRuntimeMixin
    from test_live_path_regressions import DummyTrendSubmitStrategy

    class Host(DummyTrendSubmitStrategy):
        # Only the real journal choke point (where the observer hook lives) is taken from production.
        _db_order_event = StrategyDBRuntimeMixin._db_order_event

        def __init__(self):
            DummyTrendSubmitStrategy.__init__(self)
            self.journal_rows = []
            self.trade_db = SimpleNamespace(log_order_event=lambda **kw: self.journal_rows.append(kw) or True)
            self.run_id = "run_diff"
            self.instrument_id = "inst-up"
            self.last_observed_fee_rate_bps = 0
            self.current_market_slug = SLUG

        def _normalize_side_text(self, side):
            return str(side).lower() if side else None
    return Host


def _run_decisions(host):
    from bot.order_submission import submit_maker_quote
    econ = SimpleNamespace(expected_net_usdc=Decimal("0.01"), expected_rebate_usdc=Decimal("0"),
                           expected_spread_capture_usdc=Decimal("0"), fee_equivalent_usdc=Decimal("0"))
    for price, bal in (("0.70", "100"), ("0.72", "1"), ("0.74", "100"), ("0.69", None)):
        host.available_usdc = Decimal(bal) if bal is not None else None
        submit_maker_quote(host, instrument_id="inst-up", side="buy", limit_price=Decimal(price), econ=econ,
                           directional_snapshot={"size_multiplier": Decimal("1")})
    volatile = ("client_order_id", "research_candidate_id", "position_lifecycle_id", "decision_trace", "order_key")

    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if k not in volatile and not k.endswith(("_ts", "_ns"))}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    intents = [(type(o).__name__, str(getattr(o, "price", "")), str(getattr(o, "quantity", "")),
                str(getattr(o, "side", "")), str(getattr(o, "time_in_force", ""))) for o in host.submitted_orders]
    rows = [clean({k: v for k, v in r.items() if k != "payload"}) for r in host.journal_rows]
    payloads = [clean(r.get("payload") or {}) for r in host.journal_rows]
    return intents, rows, payloads


def test_17_18_differential_flags_off_vs_on_identical_order_intents(tmp_path):
    Host = _db_host_class()
    off = Host()
    off.candidate_telemetry = None
    on = Host()
    on.candidate_telemetry = ct.CandidateTelemetryRecorder(
        sink=ct.JsonlSink(tmp_path, threaded=False, disk_free_fn=lambda p: 10 ** 15),
        run_header=ct.build_run_header(on, {"CANDIDATE_DECISION_TELEMETRY_ENABLED": True}),
        fqo_enabled=True, markout_enabled=True)
    a, b = _run_decisions(off), _run_decisions(on)
    assert a[0] and a == b
    assert on.candidate_telemetry.counters["candidates"] >= 1


class _Exploding:
    def __getattr__(self, name):
        if name == "candidate_telemetry":
            return None
        raise AssertionError(f"hook touched strategy.{name} with telemetry off")


def test_17_hook_is_noop_when_recorder_absent():
    from bot.db_runtime import StrategyDBRuntimeMixin
    s = _Exploding()
    # trade_db access after the hook is production behaviour; the hook itself must touch nothing else.
    with pytest.raises(AssertionError, match="trade_db"):
        StrategyDBRuntimeMixin._db_order_event(s, event_type="ORDER_SUBMIT", side="BUY")


# 19, 20, 21, 22 ---------------------------------------------------------------------------------
def test_19_telemetry_exceptions_are_isolated():
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")
    rec, sink, _ = _recorder(_Strategy())
    rec.observe_order_event(Broken(), event_type="ORDER_SUBMIT", side="BUY", price=0.7, qty=1, client_order_id="x",
                            reason=None, payload={"market_slug": SLUG})
    rec.sample(Broken())
    assert rec.counters["exceptions"] >= 0  # never raised


def test_20_full_queue_drops_without_blocking(tmp_path):
    sink = ct.JsonlSink(tmp_path, queue_max=3, threaded=False, disk_free_fn=lambda p: 10 ** 15)
    t0 = time.monotonic()
    results = [sink.put({"i": i}) for i in range(10)]
    assert time.monotonic() - t0 < 0.5
    assert results.count(True) == 3 and sink.counters["dropped"] == 7
    assert sink.flush() == 3


def test_21_disk_guard_disables_telemetry_once(tmp_path):
    warnings = []
    sink = ct.JsonlSink(tmp_path, threaded=False, disk_free_fn=lambda p: 1, warn=warnings.append)
    sink.put({"a": 1})
    assert sink.flush() == 0 and sink.enabled is False and sink.counters["disk_guard_tripped"] == 1
    sink.put({"a": 2})
    sink.flush()
    assert len(warnings) == 1 and sink.counters["dropped"] >= 2
    assert not list(tmp_path.glob("*.jsonl"))


def test_22_every_record_has_schema_version(tmp_path):
    s = _Strategy()
    rec, sink, clock = _recorder(s)
    _decide(rec, s)
    for i in range(32):
        clock.t += 1
        s.last_quote_update_ts_by_inst["down"] = clock.t
        rec.sample(s)
    rec.close_market(SLUG, clock.t)
    assert sink.records and all(r.get("schema_version") == ct.SCHEMA_VERSION for r in sink.records)
    assert {r["record_type"] for r in sink.records} >= {"RUN_HEADER", "CANDIDATE", "MARKOUT", "PATH_STATS"}


# 23 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path,fill_ts,expected", [
    (None, None, (ev.UNKNOWN, ev.UNKNOWN)),
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": None, "path_samples": 10, "path_end_ts": 900}, 50,
     (ev.FILL, ev.FILL)),                                                        # filled before any requote
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": None, "path_samples": 10, "path_end_ts": 900}, None,
     (ev.NOT_FILL, ev.NOT_FILL)),                                                # rested to end, never filled
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": 20, "t_touch_after_withdraw": 30,
      "t_through_after_withdraw": None, "path_samples": 10, "path_end_ts": 900}, None,
     (ev.NOT_FILL, ev.FILL)),                                                    # touch only: upper fills
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": 20, "t_touch_after_withdraw": 30,
      "t_through_after_withdraw": 40, "path_samples": 10, "path_end_ts": 900}, None,
     (ev.FILL, ev.FILL)),                                                        # traded through
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": 20, "t_touch_after_withdraw": 950,
      "t_through_after_withdraw": 960, "path_samples": 10, "path_end_ts": 900}, None,
     (ev.NOT_FILL, ev.NOT_FILL)),                                                # after market end
    ({"first_quote_price": 0.71, "first_quote_withdrawn_ts": 20, "path_samples": 0, "path_end_ts": 900}, None,
     (ev.UNKNOWN, ev.UNKNOWN)),                                                  # no trade print / no samples
])
def test_23_fqo_classification_edges(path, fill_ts, expected):
    out = ev.classify_fqo(path, current_canonical_fill_ts=fill_ts)
    assert (out["lower"], out["upper"]) == expected
    if out["lower"] == ev.FILL:
        assert out["fill_price"] == 0.71


def test_23_fqo_requote_path_recorded_end_to_end():
    s = _Strategy(ask=0.74)
    rec, sink, clock = _recorder(s)
    _decide(rec, s, price=0.72, coid="c1")
    clock.t += 2
    _decide(rec, s, price=0.74, coid="c2")  # requote -> first quote withdrawn
    for ask in (0.73, 0.72, 0.71):
        clock.t += 1
        s.latest_quote_by_inst["down"] = (ask - 0.01, ask)
        s.last_quote_update_ts_by_inst["down"] = clock.t
        rec.sample(s)
    rec.close_market(SLUG, clock.t)
    (p,) = _kinds(sink, "PATH_STATS")
    assert p["requotes"][0]["price"] == 0.74 and p["first_quote_withdrawn_ts"] is not None
    out = ev.classify_fqo(p)
    assert (out["lower"], out["upper"]) == (ev.FILL, ev.FILL)
    assert out["t_fill_upper"] < out["t_fill_lower"]


# 24, 25 -----------------------------------------------------------------------------------------
def test_24_strike_status_comes_from_strike_module(monkeypatch):
    tr = StrikeFinalityTracker()
    tr.register_provisional(SLUG, Decimal("82956.85342236787"), source="x", start_ts=START, end_ts=START + 900, now=START)
    s = _Strategy(tracker=tr)
    assert ct.strike_status_for(s, SLUG) == ("PROVISIONAL", None)
    tr.observe(SLUG, parse_crypto_price_payload({"openPrice": 82956.36849728331, "completed": True, "incomplete": False},
                                                fetched_ts=1.0), now=START + 970)
    assert ct.strike_status_for(s, SLUG) == ("FINAL", None)
    assert ct.strike_status_for(_Strategy(tracker=None), SLUG) == ("UNKNOWN", "STRIKE_TRACKER_ABSENT")
    import bot.strike_finality as sf
    monkeypatch.setattr(sf, "strike_finality_provenance", lambda t, slug: {"status": "SENTINEL"})
    assert ct.strike_status_for(s, SLUG)[0] == "SENTINEL"  # read from the module, not hard-coded


def test_25_run_header_exit_policy_fingerprint_changes_with_policy():
    s = _Strategy()
    h1 = ct.build_run_header(s, {"A": True})
    assert h1["exit_policy"]["absolute_max_loss_enabled"] == "True" and h1["schema_version"] == ct.SCHEMA_VERSION
    s.absolute_max_loss_enabled = False
    h2 = ct.build_run_header(s, {"A": True})
    assert h1["run_fingerprint"] != h2["run_fingerprint"]
    assert h1["strike_module_version"] and h1["git_head"] == "deadbeef"


def test_check_script_and_offline_eval_on_synthetic(tmp_path):
    """Integrity gate + offline tables run on synthetic telemetry produced by the real recorder."""
    spec = importlib.util.spec_from_file_location("syn", ROOT / "scripts/entry_v3_synthetic_telemetry.py")
    syn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(syn)
    jsonl, outcomes = syn.generate(tmp_path, n_markets=40, seed=7)
    spec2 = importlib.util.spec_from_file_location("chk", ROOT / "scripts/check_candidate_telemetry.py")
    chk = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(chk)
    report = chk.check([jsonl])
    assert report["core_coverage_ok"] and report["duplicate_ids"] == 0
    spec3 = importlib.util.spec_from_file_location("off", ROOT / "scripts/entry_v3_offline_eval.py")
    off = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(off)
    tables = off.evaluate([jsonl], outcomes, v1_labels={})
    assert {t["stage"] for t in tables["funnel"]} >= {"All candidates (canonical markets)", "Filled CURRENT"}
    assert [t["policy"] for t in tables["comparators"]] == ["CURRENT", "FQO_LOWER", "FQO_UPPER", "EARLY_CANCEL_C", "CHASE_B"]


def test_market_state_is_bounded_over_long_runs():
    s = _Strategy()
    rec, sink, clock = _recorder(s)
    for i in range(30):
        s.current_market_slug = f"btc-updown-15m-{1791711900 + 900 * i}"
        s.current_market_end_timestamp = 1791711900 + 900 * (i + 1)
        rec.observe_order_event(s, event_type="ORDER_SUBMIT", side="BUY", price=0.7, qty=5.5, client_order_id=f"c{i}",
                                reason=None, payload={"instrument_id": "down"})
        clock.t = 1791711900 + 900 * (i + 1) + 200
        rec.sample(s)
    assert len(rec._markets) <= ct.MAX_TRACKED_MARKETS
    assert len(_kinds(sink, "PATH_STATS")) == 30  # every market closed with path stats before pruning


def test_selection_table_counts_added_fills():
    rows = ev.paired_rows([
        {"market_slug": "m1", "win": 1, "qty": 10, "current_filled": False, "current_fill_price": None,
         "fqo": {"lower": ev.NOT_FILL, "upper": ev.FILL, "fill_price": 0.70}},
        {"market_slug": "m2", "win": 0, "qty": 10, "current_filled": False, "current_fill_price": None,
         "fqo": {"lower": ev.FILL, "upper": ev.FILL, "fill_price": 0.70}},
    ])
    sel = {x["comparator"]: x for x in ev.selection_table(rows)}
    assert (sel["FQO_UPPER"]["added_winners"], sel["FQO_UPPER"]["added_losers"]) == (1, 1)
    assert (sel["FQO_LOWER"]["added_winners"], sel["FQO_LOWER"]["added_losers"]) == (0, 1)

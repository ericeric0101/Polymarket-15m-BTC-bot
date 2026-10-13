"""DRY-RUN measurement plumbing (G1 / G2 / G3): observation only, no strategy change.

G1  DRY-RUN maker submits / cancels / simulated fills reach the frozen E5B C/B/D observer
    through one normalized order event (no order authority).
G2  SHADOW_SIM_ENTRY_FILLED -> candidate LIFECYCLE SIMULATED_FILL (stable order-id join),
    exactly one terminal state per accepted DRY-RUN candidate, fill-anchored markouts.
G3  production_p_fair captured atomically from the decision event, or an explicit reason.
Hermetic: in-memory journal, patched clock, no network, no LIVE data.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import inspect
import json
import random
import subprocess
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.candidate_telemetry as ct
import bot.dry_run_plumbing as dp
import bot.dry_run_observer as dro
import bot.entry_shadow_runtime as esr
import bot.execution_selection_eval as ev
from bot.db_runtime import StrategyDBRuntimeMixin
from bot.entry_shadow import D_HORIZONS_SEC, EntryShadowTracker
from bot.order_submission import submit_maker_quote

from test_shadow_simulation import _DryOrderHost

ROOT = Path(__file__).resolve().parents[1]
SLUG = "btc-updown-15m-1791711900"
START = 1791711900.0
END = START + 900.0
T0 = START + 300.0
SHADOW_EVENTS = ("ENTRY_EDGE_SHADOW", "ENTRY_EARLY_CANCEL_SHADOW", "ENTRY_CHASE_SHADOW")
OLD_MARKOUT_KEYS = {"record_type", "schema_version", "slug", "anchor", "horizon_sec", "anchor_ts", "candidate_id",
                    "client_order_id", "sample_ts", "best_bid", "best_ask", "mid", "bbo_age_sec", "reason",
                    "run_fingerprint"}
FORBIDDEN_CALLS = {"submit_order", "cancel_order", "cancel_all_orders", "modify_order", "replace_order",
                   "_cancel_maker_order_key", "_cancel_maker_order_side", "_submit_taker_exit_order",
                   "_submit_maker_order", "order_factory", "_cancel_active_maker_orders", "submit_maker_quote",
                   "_record_shadow_simulated_entry", "_shadow_simulation_on_order_cancel"}


# ------------------------------------------------------------------ fixtures
class _Journal:
    """In-memory trade journal: records every row exactly as production writes it."""

    def __init__(self):
        self.orders: list[dict] = []
        self.strategy: list[tuple[str, dict]] = []

    def log_order_event(self, **kw):
        self.orders.append(json.loads(json.dumps(kw, default=str)))
        return True

    def log_strategy_event(self, run_id, event_type, payload=None, **_kw):
        self.strategy.append((event_type, json.loads(json.dumps(payload or {}, default=str))))
        return True

    def enqueue_strategy_event(self, run_id, event_type, payload):
        self.strategy.append((event_type, json.loads(json.dumps(payload, default=str))))
        return True

    def load_shadow_simulation(self, slug):
        return None

    def load_fair_edge_bucket_shadow_simulations(self, slug):
        return []


class _MemSink:
    def __init__(self):
        self.records: list[dict] = []

    def put(self, rec):
        self.records.append(json.loads(json.dumps(rec, default=str)))
        return True


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(T0)
    monkeypatch.setattr(time, "time", c)
    return c


class PlumbHost(_DryOrderHost):
    """Production DRY-RUN submit / cancel / simulation code + the real journal choke point."""

    _db_order_event = StrategyDBRuntimeMixin._db_order_event

    def __init__(self, journal, *, dry=True):
        super().__init__(journal)
        self._dry = dry
        self.run_id = "run_plumb"
        self.runtime_git_revision = "plumbtest"
        self.current_market_slug = SLUG
        self.current_market_end_timestamp = END
        self.instrument_id = "inst-up"
        self.last_observed_fee_rate_bps = 0
        self.maker_order_ttl_sec = 25
        self.latest_quote_by_inst = {}
        self.last_quote_update_ts_by_inst = {}
        self.last_quote_received_ts_by_inst = {}
        self.latest_quote_depth_by_inst = {}
        self.market_strike_cache_by_slug = {SLUG: 100.0}
        self.entry_shadow = None
        self.candidate_telemetry = None
        self.submitted_orders = []

    def _is_dry_run_mode(self):
        return self._dry

    def _get_quote_for_instrument(self, instrument_id):
        return self.latest_quote_by_inst.get(str(instrument_id))

    def _normalize_side_text(self, side):
        return str(side).lower() if side else None

    def submit_order(self, order):  # never reached in DRY-RUN
        raise AssertionError("DRY-RUN must never submit a venue order")

    def cancel_order(self, order):
        raise AssertionError("DRY-RUN must never cancel a venue order")


def make_host(clock, *, shadow=True, telemetry=True, dry=True):
    host = PlumbHost(_Journal(), dry=dry)
    set_quote(host, clock, 0.69, 0.70, feed=False)
    if shadow:
        host.entry_shadow = dro.build_entry_shadow(host, d=True, c=True, b=True)
    if telemetry:
        host.candidate_telemetry = ct.CandidateTelemetryRecorder(
            sink=_MemSink(), run_header=ct.build_run_header(host, {"CANDIDATE_DECISION_TELEMETRY_ENABLED": True}),
            fqo_enabled=True, markout_enabled=True, clock=clock, mono=lambda: 1)
    return host


def set_quote(host, clock, bid, ask, *, feed=True, size=(40.0, 50.0)):
    t = clock.t
    host.latest_quote_by_inst["inst-up"] = (Decimal(str(bid)), Decimal(str(ask)))
    host.last_quote_update_ts_by_inst["inst-up"] = t
    host.last_quote_received_ts_by_inst["inst-up"] = t
    host.latest_quote_depth_by_inst["inst-up"] = (Decimal(str(size[0])), Decimal(str(size[1])))
    if feed:
        host._shadow_simulation_on_quote("inst-up", Decimal(str(bid)), Decimal(str(ask)), t)
        esr.hook_tick(host)
        if host.candidate_telemetry is not None:
            host.candidate_telemetry.sample(host, now=t)


ECON = SimpleNamespace(expected_net_usdc=Decimal("0.05"), expected_rebate_usdc=Decimal("0"),
                       expected_spread_capture_usdc=Decimal("0"), fee_equivalent_usdc=Decimal("0"))


def submit(host, clock, price, *, version=1, p_fair="0.76"):
    submit_maker_quote(host, instrument_id="inst-up", side="buy", limit_price=Decimal(str(price)), econ=ECON,
                       directional_snapshot={"p_fair": Decimal(p_fair), "planned_quote_ts": clock.t - 0.2},
                       target_version=version)


def walk(host, clock, ask_at, until, *, step=0.5, bid_at=None):
    while clock.t + step <= until + 1e-9:
        clock.t = round(clock.t + step, 6)
        a = ask_at(clock.t)
        set_quote(host, clock, bid_at(clock.t) if bid_at else round(a - 0.01, 4), a)


def journal(host, event_type):
    return [r for r in host.trade_db.orders if r["event_type"] == event_type]


def shadow(host, event_type=None):
    return [(e, p) for e, p in host.trade_db.strategy if e in SHADOW_EVENTS and (event_type is None or e == event_type)]


def records(host, kind):
    return [r for r in host.candidate_telemetry.sink.records if r.get("record_type") == kind]


def lifecycle(host, lifecycle_event=None):
    return [r for r in records(host, "LIFECYCLE")
            if lifecycle_event is None or r.get("lifecycle_event") == lifecycle_event]


def close_market(host, clock):
    clock.t = END + 1.0
    host.candidate_telemetry.sample(host, now=clock.t)


# ================================================================== G1
def test_g1_1_2_3_dry_run_submit_reaches_c_b_d_observers(clock):
    host = make_host(clock)
    submit(host, clock, "0.68")
    coid = journal(host, "ORDER_DRY_RUN_SUBMITTED")[0]["client_order_id"]
    assert SLUG in host.entry_shadow.attempts
    walk(host, clock, lambda t: 0.70 if t < T0 + 12 else 0.69, T0 + 45)
    kinds = Counter(e for e, _ in shadow(host))
    assert kinds["ENTRY_EARLY_CANCEL_SHADOW"] == 1
    assert kinds["ENTRY_CHASE_SHADOW"] == 1
    assert kinds["ENTRY_EDGE_SHADOW"] == len(D_HORIZONS_SEC)
    c = shadow(host, "ENTRY_EARLY_CANCEL_SHADOW")[0][1]
    assert c["c_rule_decision"] == "WOULD_CANCEL" and c["attempt_id"] == f"{SLUG}|{coid}"
    for _, p in shadow(host):
        assert p["execution_mode"] == "DRY_RUN" and p["evidence_tier"] == "SIMULATED_ENV"
        assert p["plumbing_revision"] == dp.PLUMBING_REVISION


def test_g1_2_b_would_chase_in_dry_run(clock):
    host = make_host(clock)
    host.side_decision_score = Decimal("0.42")
    submit(host, clock, "0.68")
    walk(host, clock, lambda t: 0.70 if t < T0 + 8 else 0.76, T0 + 45)
    b = shadow(host, "ENTRY_CHASE_SHADOW")[0][1]
    assert b["b_rule_decision"] == "WOULD_CHASE"


def test_g1_4_frozen_rule_modules_byte_identical_to_head_before():
    for rel in ("bot/entry_shadow.py", "bot/execution_selection_eval.py"):
        try:
            before = subprocess.run(["git", "-C", str(ROOT), "show", f"4ec677e:{rel}"], capture_output=True,
                                    check=True).stdout
        except Exception:
            pytest.skip("git history unavailable")
        assert hashlib.sha256(before).hexdigest() == hashlib.sha256((ROOT / rel).read_bytes()).hexdigest(), rel


class _StrictStrategy(PlumbHost):
    """Any execution side effect raises."""

    def _cancel_maker_order_key(self, *a, **k): raise AssertionError("observer cancelled an order")
    def _cancel_maker_order_side(self, *a, **k): raise AssertionError("observer cancelled an order")
    def _cancel_active_maker_orders(self, *a, **k): raise AssertionError("observer cancelled orders")
    def _record_shadow_simulated_entry(self, *a, **k): raise AssertionError("observer created a paper order")
    def _shadow_simulation_on_order_cancel(self, *a, **k): raise AssertionError("observer cancelled a paper order")
    def _submit_taker_exit_order(self, *a, **k): raise AssertionError("observer sold")


def test_g1_5_observer_cannot_touch_orders_or_simulation_state(clock):
    host = _StrictStrategy(_Journal())
    set_quote(host, clock, 0.69, 0.70, feed=False)
    host.entry_shadow = dro.build_entry_shadow(host, d=True, c=True, b=True)
    host.active_maker_orders = {"buy:inst-up": {"price": "0.68", "dry_run_client_order_id": "DRY-1"}}
    host._shadow_simulations_by_slug = {SLUG: {"status": "PENDING", "entry_price": 0.68}}
    before = copy.deepcopy((host.active_maker_orders, host._shadow_simulations_by_slug))
    events = [("ORDER_DRY_RUN_SUBMITTED", "DRY-1", {"instrument_id": "inst-up"}),
              ("ORDER_DRY_RUN_CANCELLED", "DRY-1", {"order_key": "buy:inst-up"}),
              ("ORDER_DRY_RUN_SUBMITTED", "DRY-2", {"instrument_id": "inst-up"}),
              ("SHADOW_SIM_ENTRY_FILLED", "shadow-sim:x:up",
               {"slug": SLUG, "dry_run_client_order_id": "DRY-2", "filled_ts": T0 + 1, "qty": 5.0})]
    for et, coid, payload in events:
        dro.observe_dry_run_order_event(host, event_type=et, client_order_id=coid, side="BUY", price=0.68, qty=5.0,
                                        reason="x", payload=payload)
    for i in range(1, 100):
        clock.t = T0 + i * 0.5
        host.last_quote_update_ts_by_inst["inst-up"] = clock.t
        esr.hook_tick(host)
    assert copy.deepcopy((host.active_maker_orders, host._shadow_simulations_by_slug)) == before
    assert host.entry_shadow.counters["exceptions"] == 0


@pytest.mark.parametrize("module", [dro, dp, ct])
def test_g1_5_plumbing_modules_have_no_execution_capability(module):
    tree = ast.parse(inspect.getsource(module))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (names & FORBIDDEN_CALLS)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("nautilus" in m or "order_submission" in m or "order_runtime" in m or "taker_exit" in m
                          or "py_clob_client" in m or "shadow_simulation" in m) for m in mods)


def test_g1_6_live_mode_ignores_dry_run_path_and_live_hook_unchanged(clock):
    host = make_host(clock, telemetry=False, dry=False)
    dro.observe_dry_run_order_event(host, event_type="ORDER_DRY_RUN_SUBMITTED", client_order_id="DRY-1", side="BUY",
                                    price=0.68, qty=5.0, reason=None, payload={"instrument_id": "inst-up"})
    assert host.entry_shadow.attempts == {}
    dry = make_host(clock, telemetry=False)
    dro.observe_dry_run_order_event(dry, event_type="ORDER_SUBMIT", client_order_id="LIVE-1", side="BUY",
                                    price=0.68, qty=5.0, reason=None, payload={"instrument_id": "inst-up"})
    assert dry.entry_shadow.attempts == {}
    # LIVE hook produces exactly the attempt the frozen tracker produced before the refactor.
    ref = EntryShadowTracker(emit=None, d_enabled=True, c_enabled=True, b_enabled=True)
    ref.on_buy_submit(coid="LIVE-1", market=SLUG, instrument="inst-up", side="UP", maker_price=Decimal("0.68"),
                      qty=5.0, place_ts=clock.t, sample=esr._read_sample(host, "inst-up", clock.t))
    esr.hook_buy_submit(host, order=SimpleNamespace(client_order_id="LIVE-1"), instrument_id="inst-up",
                        limit_price=Decimal("0.68"), qty=5.0)
    a, b = host.entry_shadow.attempts[SLUG], ref.attempts[SLUG]
    assert (a.attempt_id, a.side, a.place_ts, a.maker_price, a.qty, a.entry_score_signed, a.a0) == \
           (b.attempt_id, b.side, b.place_ts, b.maker_price, b.qty, b.entry_score_signed, b.a0)
    # The normalized-event path used for DRY-RUN reproduces the frozen LIVE hook's tracker call exactly.
    via = EntryShadowTracker(emit=None, d_enabled=True, c_enabled=True, b_enabled=True)
    dro.dispatch_submit(host, via, dro.normalize_live_submit(host, order=SimpleNamespace(client_order_id="LIVE-1"),
                                                             instrument_id="inst-up", limit_price=Decimal("0.68"),
                                                             qty=5.0, now=clock.t))
    c = via.attempts[SLUG]
    assert (c.attempt_id, c.side, c.place_ts, c.maker_price, c.qty, c.entry_score_signed, c.a0) == \
           (b.attempt_id, b.side, b.place_ts, b.maker_price, b.qty, b.entry_score_signed, b.a0)


def test_g1_6_live_order_intents_identical_with_and_without_dry_run_observer():
    from test_candidate_telemetry import _db_host_class, _run_decisions
    Host = _db_host_class()
    off, on = Host(), Host()
    off.candidate_telemetry = on.candidate_telemetry = None
    off.entry_shadow = None
    on.entry_shadow = dro.build_entry_shadow(SimpleNamespace(trade_db=SimpleNamespace(
        enqueue_strategy_event=lambda *a: True), run_id="r"), d=True, c=True, b=True)
    a, b = _run_decisions(off), _run_decisions(on)
    assert a[0] and a == b


def test_g1_7_flags_off_produce_no_observer_records(clock):
    host = make_host(clock, shadow=False)
    submit(host, clock, "0.68")
    walk(host, clock, lambda t: 0.69, T0 + 45)
    assert shadow(host) == []
    assert dro.build_entry_shadow(SimpleNamespace(), d=False, c=False, b=False) is None


@pytest.mark.parametrize("path,c_expected,b_expected", [
    (lambda t: 0.70, "WOULD_KEEP", "WOULD_NOT_CHASE"),                  # nothing triggers: still recorded
    (lambda t: 0.70 if t < T0 + 3 else 0.68, "NOT_APPLICABLE", "NOT_APPLICABLE"),   # filled before horizon
])
def test_g1_8_every_dry_run_attempt_has_c_and_b_evaluation_records(clock, path, c_expected, b_expected):
    host = make_host(clock)
    submit(host, clock, "0.68")
    walk(host, clock, path, T0 + 45)
    c = shadow(host, "ENTRY_EARLY_CANCEL_SHADOW")
    b = shadow(host, "ENTRY_CHASE_SHADOW")
    assert len(c) == 1 and len(b) == 1
    assert c[0][1]["c_rule_decision"] == c_expected and b[0][1]["b_rule_decision"] == b_expected
    counts = host.entry_shadow.observer_counts
    attempts = host.entry_shadow.counters["attempts"]
    assert counts["ENTRY_EARLY_CANCEL_SHADOW"] >= attempts and counts["ENTRY_CHASE_SHADOW"] >= attempts


def test_g1_8_requote_orders_link_to_one_attempt_with_one_evaluation(clock):
    host = make_host(clock)
    submit(host, clock, "0.66")
    clock.t += 3
    host._cancel_maker_order_key("buy:inst-up", reason="requote")
    clock.t += 1
    submit(host, clock, "0.67", version=2)
    walk(host, clock, lambda t: 0.70, T0 + 48)
    coids = [r["client_order_id"] for r in journal(host, "ORDER_DRY_RUN_SUBMITTED")]
    assert len(coids) == 2 and host.entry_shadow.counters["attempts"] == 1
    assert {p["attempt_id"] for _, p in shadow(host)} == {f"{SLUG}|{coids[0]}"}
    assert len(shadow(host, "ENTRY_EARLY_CANCEL_SHADOW")) == 1


def test_g1_9_contract_live_and_dry_run_normalized_events_share_one_structure(clock):
    host = make_host(clock, telemetry=False)
    live_host = make_host(clock, telemetry=False, dry=False)          # LIVE fixture vs DRY-RUN fixture
    live = dro.normalize_live_submit(live_host, order=SimpleNamespace(client_order_id="LIVE-1", venue_order_id="V1"),
                                     instrument_id="inst-up", limit_price=Decimal("0.68"), qty=5.0, now=clock.t)
    dry = dro.normalize_dry_run_submit(host, client_order_id="DRY-1", instrument_id="inst-up",
                                       limit_price=Decimal("0.68"), qty=5.0, now=clock.t)
    assert type(live) is type(dry)
    lv, dv = live.as_dict(), dry.as_dict()
    assert set(lv) == set(dv) == set(dro.NORMALIZED_EVENT_FIELDS)
    for key in ("event_kind", "market", "instrument", "side", "maker_price", "qty", "event_ts"):
        assert lv[key] == dv[key] or key == "client_order_id"
    assert (lv["execution_mode"], dv["execution_mode"]) == ("LIVE", "DRY_RUN")
    assert (lv["evidence_tier"], dv["evidence_tier"]) == ("LIVE_VENUE", "SIMULATED_ENV")
    # DRY-RUN cannot provide venue state: explicitly missing with a reason, never a fake value.
    assert dv["venue_order_id"] is None and dv["missing_reasons"]["venue_order_id"] == "DRY_RUN_NO_VENUE_ORDER"
    assert dv["request_to_ack_sec"] is None and "request_to_ack_sec" in dv["missing_reasons"]
    assert lv["missing_reasons"].get("venue_order_id") is None


# ================================================================== G2
def _fill_scenario(clock, *, requote=False, fill=True):
    host = make_host(clock)
    if requote:
        submit(host, clock, "0.66")
        clock.t += 3
        set_quote(host, clock, 0.69, 0.70)
        host._cancel_maker_order_key("buy:inst-up", reason="requote")
        clock.t += 1
        submit(host, clock, "0.68", version=2)
    else:
        submit(host, clock, "0.68")
    t_fill = clock.t + 5
    walk(host, clock, lambda t: 0.70 if (t < t_fill or not fill) else 0.68, clock.t + 40)
    return host


def test_g2_1_4_each_sim_fill_yields_exactly_one_simulated_fill_matching_journal(clock):
    host = _fill_scenario(clock)
    jf = journal(host, "SHADOW_SIM_ENTRY_FILLED")
    sf = lifecycle(host, "SIMULATED_FILL")
    assert len(jf) == 1 and len(sf) == 1
    fill, payload = sf[0], jf[0]["payload"]
    fa = [r for r in records(host, "CANDIDATE") if r["candidate_role"] == "FIRST_ACCEPTED"][0]
    submitted = journal(host, "ORDER_DRY_RUN_SUBMITTED")[0]
    assert fill["event_type"] == "ORDER_DRY_RUN_FILLED" and fill["terminal"] is True
    assert fill["candidate_id"] == fa["candidate_id"] and fill["run_id"] == "run_plumb"
    assert fill["client_order_id"] == submitted["client_order_id"] == payload["dry_run_client_order_id"]
    assert fill["price"] == jf[0]["price"] and fill["qty"] == jf[0]["qty"] == fill["cumulative_qty"]
    assert fill["fill_event_ts"] == payload["filled_ts"] and fill["wall_ts"] >= fill["fill_event_ts"]
    assert fill["submit_ts"] == pytest.approx(fa["decision_wall_ts"], abs=1e-6)
    assert (fill["simulation"], fill["source_event"], fill["execution_mode"], fill["evidence_tier"]) == \
           (True, "SHADOW_SIM_ENTRY_FILLED", "DRY_RUN", "SIMULATED_ENV")
    assert (fill["fill_seq"], fill["market_slug"], fill["candidate_side"]) == (1, SLUG, "UP")
    close_market(host, clock)
    path = records(host, "PATH_STATS")[0]
    assert path["canonical_fill_ts"] == fill["wall_ts"]


def test_g2_2_requote_fill_keeps_the_first_accepted_candidate_identity(clock):
    host = _fill_scenario(clock, requote=True)
    coids = [r["client_order_id"] for r in journal(host, "ORDER_DRY_RUN_SUBMITTED")]
    fa = [r for r in records(host, "CANDIDATE") if r["candidate_role"] == "FIRST_ACCEPTED"]
    sf = lifecycle(host, "SIMULATED_FILL")
    assert len(fa) == 1 and len(sf) == 1
    assert sf[0]["candidate_id"] == fa[0]["candidate_id"] and sf[0]["client_order_id"] == coids[1]
    assert sf[0]["is_canonical_order"] is False
    rq = lifecycle(host, "REQUOTED")
    assert len(rq) == 1 and rq[0]["client_order_id"] == coids[1] and rq[0]["replaces_client_order_id"] == coids[0]
    assert rq[0]["replaced_order_end"] == "REQUOTE_CANCEL" and rq[0]["terminal"] is False
    cancel = [r for r in records(host, "LIFECYCLE") if r["event_type"] == "ORDER_DRY_RUN_CANCELLED"]
    assert len(cancel) == 1 and cancel[0]["terminal"] is False
    close_market(host, clock)
    assert records(host, "PATH_STATS")[0]["canonical_fill_ts"] is None   # first quote itself never filled
    assert len([r for r in records(host, "LIFECYCLE") if r.get("terminal")]) == 1


def test_g2_3_repeated_quote_updates_and_replayed_fill_events_do_not_duplicate(clock):
    host = _fill_scenario(clock)
    walk(host, clock, lambda t: 0.67, clock.t + 20)        # many more touches after the fill
    fill_row = journal(host, "SHADOW_SIM_ENTRY_FILLED")[0]
    for _ in range(3):                                      # the same journal event replayed
        host.candidate_telemetry.observe_order_event(host, event_type="SHADOW_SIM_ENTRY_FILLED", side="UP",
                                                     price=fill_row["price"], qty=fill_row["qty"],
                                                     client_order_id=fill_row["client_order_id"], reason=None,
                                                     payload=fill_row["payload"])
    assert len(lifecycle(host, "SIMULATED_FILL")) == 1
    assert host.candidate_telemetry.counters["sim_fill_duplicates"] == 3


def test_g2_5_fill_anchored_markouts_at_fixed_horizons(clock):
    host = _fill_scenario(clock)
    walk(host, clock, lambda t: 0.69, clock.t + 35)
    fill = lifecycle(host, "SIMULATED_FILL")[0]
    mk = [r for r in records(host, "MARKOUT") if r["anchor"] == "SIMULATED_FILL"]
    assert sorted(r["horizon_sec"] for r in mk) == [1, 3, 5, 10, 15, 30]
    for r in mk:
        assert r["anchor_type"] == "SIMULATED_FILL" and r["anchor_ts"] == fill["wall_ts"]
        assert r["candidate_id"] == fill["candidate_id"] and r["client_order_id"] == fill["client_order_id"]
        assert r["sample_ts"] >= r["anchor_ts"] + r["horizon_sec"] and r["mid"] is not None
        assert (r["execution_mode"], r["evidence_tier"]) == ("DRY_RUN", "SIMULATED_ENV")


def test_g2_6_first_submit_markouts_unchanged(clock):
    host = _fill_scenario(clock)
    fs = [r for r in records(host, "MARKOUT") if r["anchor"] == "FIRST_SUBMIT"]
    fa = [r for r in records(host, "CANDIDATE") if r["candidate_role"] == "FIRST_ACCEPTED"][0]
    assert sorted(r["horizon_sec"] for r in fs) == [1, 3, 5, 10, 15, 30]
    for r in fs:
        assert OLD_MARKOUT_KEYS <= set(r) and r["anchor_type"] == "FIRST_SUBMIT"
        assert r["anchor_ts"] == pytest.approx(fa["decision_wall_ts"]) and r["reason"] != "MARKET_ENDED"


def test_g2_7_live_fill_semantics_unchanged(clock):
    s = make_host(clock, shadow=False, dry=False)
    rec = s.candidate_telemetry
    rec.observe_order_event(s, event_type="ORDER_SUBMIT", side="BUY", price=0.68, qty=5.0, client_order_id="L1",
                            reason=None, payload={"instrument_id": "inst-up", "p_fair": 0.76})
    clock.t += 2
    rec.observe_order_event(s, event_type="ORDER_FILLED", side="BUY", price=0.68, qty=5.0, client_order_id="L1",
                            reason=None, payload={"instrument_id": "inst-up"})
    rec.observe_order_event(s, event_type="SHADOW_SIM_ENTRY_FILLED", side="UP", price=0.68, qty=5.0,
                            client_order_id="shadow-sim:x", reason=None,
                            payload={"slug": SLUG, "dry_run_client_order_id": "L1", "filled_ts": clock.t})
    life = records(s, "LIFECYCLE")
    assert [r["event_type"] for r in life] == ["ORDER_FILLED"]
    assert "lifecycle_event" not in life[0] or life[0]["lifecycle_event"] is None
    clock.t += 40
    rec.sample(s, now=clock.t)
    anchors = Counter(r["anchor"] for r in records(s, "MARKOUT"))
    assert anchors == {"FIRST_SUBMIT": 6, "FILL": 6}


def _scripted_dry_run(clock, *, observers):
    clock.t = T0
    host = make_host(clock, shadow=observers, telemetry=observers)
    submit(host, clock, "0.66")
    walk(host, clock, lambda t: 0.70, T0 + 6)
    host._cancel_maker_order_key("buy:inst-up", reason="requote")
    clock.t += 0.5
    submit(host, clock, "0.68", version=2)
    walk(host, clock, lambda t: 0.70 if t < T0 + 15 else 0.68, T0 + 60)
    volatile = ("research_candidate_id", "position_lifecycle_id", "decision_trace")

    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if k not in volatile}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    rows = [clean({k: v for k, v in r.items() if k != "payload"}) for r in host.trade_db.orders]
    payloads = [clean(r["payload"]) for r in host.trade_db.orders]
    return rows, payloads, copy.deepcopy(host._shadow_simulations_by_slug), copy.deepcopy(host.active_maker_orders)


def test_g2_8_observers_on_vs_off_identical_dry_run_decisions_orders_and_paper_state(clock):
    off = _scripted_dry_run(clock, observers=False)
    on = _scripted_dry_run(clock, observers=True)
    assert off[0] and off == on
    assert [r["event_type"] for r in off[0]].count("SHADOW_SIM_ENTRY_FILLED") == 1


def test_g2_9_simulator_full_fill_only_and_explicit_partial_fill_sequence(clock):
    host = _fill_scenario(clock)
    jf = journal(host, "SHADOW_SIM_ENTRY_FILLED")[0]
    assert jf["qty"] == jf["payload"]["qty"]                # the simulator fills the whole order at once
    # A partial-fill-capable source would carry fill_seq: each new seq is recorded, cumulative qty grows,
    # the terminal state and the markout anchor stay on the first fill.
    rec = host.candidate_telemetry
    payload = dict(jf["payload"], fill_seq=2, qty=2.0)
    rec.observe_order_event(host, event_type="SHADOW_SIM_ENTRY_FILLED", side="UP", price=jf["price"], qty=2.0,
                            client_order_id=jf["client_order_id"], reason=None, payload=payload)
    sf = lifecycle(host, "SIMULATED_FILL")
    assert [r["fill_seq"] for r in sf] == [1, 2]
    assert sf[1]["cumulative_qty"] == pytest.approx(sf[0]["qty"] + 2.0) and sf[1]["terminal"] is False
    assert Counter(r["anchor"] for r in records(host, "MARKOUT"))["SIMULATED_FILL"] == 6


def test_g2_10_market_end_before_markout_horizon_is_null_with_reason(clock):
    clock.t = END - 30
    host = make_host(clock)
    submit(host, clock, "0.68")
    walk(host, clock, lambda t: 0.70 if t < END - 22 else 0.68, END - 1)
    host.current_market_slug = "btc-updown-15m-1791712800"
    clock.t = END + 40
    host.candidate_telemetry.sample(host, now=clock.t)
    mk = {r["horizon_sec"]: r for r in records(host, "MARKOUT") if r["anchor"] == "SIMULATED_FILL"}
    fill_ts = lifecycle(host, "SIMULATED_FILL")[0]["wall_ts"]
    for h, r in mk.items():
        if fill_ts + h > END:
            assert r["mid"] is None and r["reason"] == "MARKET_ENDED"
        else:
            assert r["reason"] != "MARKET_ENDED"
    assert any(r["reason"] == "MARKET_ENDED" for r in mk.values())


# ------------------------------------------------------------------ terminal states
def _terminal(host):
    return [r for r in records(host, "LIFECYCLE") if r.get("terminal")]


@pytest.mark.parametrize("scenario,expected,reason", [
    ("fill", "SIMULATED_FILL", None),
    ("terminal_cancel", "SIMULATED_CANCEL", "dry_run_live_lifecycle:risk:no_desired_quote"),
    ("open_at_end", "SIMULATED_EXPIRE", "MARKET_ENDED"),
    ("ttl_cancel", "SIMULATED_EXPIRE", "ORDER_TTL"),
    ("requote_fill", "SIMULATED_FILL", None),
    ("requote_no_fill", "SIMULATED_EXPIRE", "MARKET_ENDED"),
])
def test_terminal_state_reconstructable_and_unique(clock, scenario, expected, reason):
    host = make_host(clock)
    if scenario.startswith("requote"):
        submit(host, clock, "0.66")
        clock.t += 2
        host._cancel_maker_order_key("buy:inst-up", reason="requote")
        clock.t += 1
        submit(host, clock, "0.68", version=2)
    else:
        submit(host, clock, "0.68")
    fills = scenario in ("fill", "requote_fill")
    walk(host, clock, lambda t: 0.68 if fills and t > T0 + 8 else 0.70, T0 + 12)
    if scenario == "terminal_cancel":
        host._cancel_maker_order_key("buy:inst-up", reason="risk:no_desired_quote")
    if scenario == "ttl_cancel":
        host._cancel_maker_order_key("buy:inst-up", reason="ttl")
    close_market(host, clock)
    term = _terminal(host)
    assert len(term) == 1 and term[0]["lifecycle_event"] == expected
    assert term[0].get("terminal_reason") == reason
    fa = [r for r in records(host, "CANDIDATE") if r["candidate_role"] == "FIRST_ACCEPTED"][0]
    assert term[0]["candidate_id"] == fa["candidate_id"]
    close_market(host, clock)                               # idempotent: no second terminal
    assert len(_terminal(host)) == 1


def test_terminal_requote_then_rejected(clock):
    host = make_host(clock)
    rec = host.candidate_telemetry
    submit(host, clock, "0.66")
    clock.t += 2
    host._cancel_maker_order_key("buy:inst-up", reason="requote")
    clock.t += 1
    rec.observe_order_event(host, event_type="ORDER_DRY_RUN_SUBMITTED", side="BUY", price=0.68, qty=5.0,
                            client_order_id="DRY-R2", reason=None, payload={"instrument_id": "inst-up"})
    rec.observe_order_event(host, event_type="ORDER_REJECTED", side="BUY", price=0.68, qty=5.0,
                            client_order_id="DRY-R2", reason="post_only_cross", payload={"instrument_id": "inst-up"})
    close_market(host, clock)
    term = _terminal(host)
    assert len(term) == 1 and term[0]["lifecycle_event"] == "REJECTED"


def test_property_exactly_one_terminal_state_for_random_event_sequences(clock):
    rng = random.Random(20261013)
    for case in range(300):
        clock.t = T0
        host = make_host(clock, shadow=False)
        rec = host.candidate_telemetry
        submits = 0
        open_coid = None
        for step in range(rng.randint(1, 12)):
            clock.t += rng.uniform(0.2, 8.0)
            op = rng.choice(["submit", "cancel_requote", "cancel_risk", "cancel_ttl", "quote", "quote", "fill_touch",
                             "replay_fill", "expire", "reject"])
            if op == "submit" and open_coid is None:
                submit(host, clock, "0.66", version=submits + 1)
                submits += 1
                open_coid = journal(host, "ORDER_DRY_RUN_SUBMITTED")[-1]["client_order_id"]
            elif op.startswith("cancel") and "buy:inst-up" in host.active_maker_orders:
                host._cancel_maker_order_key("buy:inst-up", reason=op.split("_", 1)[1])
                open_coid = None
            elif op == "fill_touch":
                set_quote(host, clock, 0.65, 0.66)
                if journal(host, "SHADOW_SIM_ENTRY_FILLED"):
                    open_coid = None
            elif op == "replay_fill" and journal(host, "SHADOW_SIM_ENTRY_FILLED"):
                row = journal(host, "SHADOW_SIM_ENTRY_FILLED")[0]
                rec.observe_order_event(host, event_type=row["event_type"], side="UP", price=row["price"],
                                        qty=row["qty"], client_order_id=row["client_order_id"], reason=None,
                                        payload=row["payload"])
            elif op == "expire":
                set_quote(host, clock, 0.69, 0.70)
                clock.t += 30
                set_quote(host, clock, 0.69, 0.70)
            elif op == "reject" and open_coid is not None:
                rec.observe_order_event(host, event_type="ORDER_REJECTED", side="BUY", price=0.66, qty=5.0,
                                        client_order_id=open_coid, reason="x", payload={"instrument_id": "inst-up"})
                open_coid = None
            else:
                set_quote(host, clock, 0.69, 0.70)
        close_market(host, clock)
        close_market(host, clock)
        term = _terminal(host)
        accepted = submits > 0
        assert len(term) == (1 if accepted else 0), (case, [r["lifecycle_event"] for r in term])
        if accepted:
            fills = lifecycle(host, "SIMULATED_FILL")
            jfills = journal(host, "SHADOW_SIM_ENTRY_FILLED")
            assert len([f for f in fills if f["fill_seq"] == 1]) == len(jfills) <= 1
            assert (term[0]["lifecycle_event"] == "SIMULATED_FILL") == bool(jfills)


# ------------------------------------------------------------------ reconciliation (synthetic replay)
def test_reconciliation_journal_vs_telemetry_join_is_complete(clock):
    hosts = []
    for k, (requote, fill) in enumerate([(False, True), (True, True), (False, False), (True, False), (False, True)]):
        clock.t = T0
        h = _fill_scenario(clock, requote=requote, fill=fill)
        close_market(h, clock)
        hosts.append(h)
    for h in hosts:
        submitted = {r["client_order_id"] for r in journal(h, "ORDER_DRY_RUN_SUBMITTED")}
        jfill = {r["payload"]["dry_run_client_order_id"] for r in journal(h, "SHADOW_SIM_ENTRY_FILLED")}
        tfill = {r["client_order_id"] for r in lifecycle(h, "SIMULATED_FILL")}
        assert jfill == tfill and jfill <= submitted
        fa = [r for r in records(h, "CANDIDATE") if r["candidate_role"] == "FIRST_ACCEPTED"]
        assert len(fa) == 1 and len(_terminal(h)) == 1 and _terminal(h)[0]["candidate_id"] == fa[0]["candidate_id"]
        fill_mk = [r for r in records(h, "MARKOUT") if r["anchor"] == "SIMULATED_FILL"]
        assert len(fill_mk) == 6 * len(tfill)
        report = ct.reconcile_dry_run_fills(h.candidate_telemetry.sink.records, h.trade_db.orders)
        assert report["join_completeness"] == (1.0 if jfill else None) and report["unmatched_journal_fills"] == 0
        assert report["journal_fills"] == len(jfill) and report["field_mismatches"] == 0
        assert report["unmatched_telemetry_fills"] == 0 and report["accepted_without_terminal_closed"] == 0


def test_unmapped_sim_fill_is_reported_never_guessed(clock):
    host = make_host(clock, shadow=False)
    submit(host, clock, "0.68")
    host.candidate_telemetry.observe_order_event(
        host, event_type="SHADOW_SIM_ENTRY_FILLED", side="UP", price=0.68, qty=5.0, client_order_id="shadow-sim:x",
        reason=None, payload={"slug": SLUG, "filled_ts": clock.t, "qty": 5.0})       # no stable key
    assert lifecycle(host, "SIMULATED_FILL") == []
    assert host.candidate_telemetry.counters["sim_fill_unmapped"] == 1
    assert [r["lifecycle_event"] for r in records(host, "LIFECYCLE")] == ["SIMULATED_FILL_UNMAPPED"]


def test_offline_evaluator_reads_dry_run_fill_without_journal(clock, tmp_path):
    host = _fill_scenario(clock)
    close_market(host, clock)
    # The frozen comparator bootstrap needs >= 4 rows (block=4); replicate the market under 4 slugs.
    lines, slugs = [], [f"btc-updown-15m-{int(START) + 900 * k}" for k in range(4)]
    for k, slug in enumerate(slugs):
        for r in host.candidate_telemetry.sink.records:
            if r["record_type"] == "RUN_HEADER" and k:
                continue
            r = dict(r)
            for key in ("market_slug", "slug"):
                if r.get(key) == SLUG:
                    r[key] = slug
            lines.append(json.dumps(r) + "\n")
    path = tmp_path / "t.jsonl"
    path.write_text("".join(lines))
    spec = importlib.util.spec_from_file_location("off", ROOT / "scripts/entry_v3_offline_eval.py")
    off = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(off)
    tables = off.evaluate([path], {s: "UP" for s in slugs}, v1_labels={})
    assert len(tables["rows"]) == 4
    for row in tables["rows"]:
        assert row["current_filled"] is True and row["current_fill_price"] == pytest.approx(0.68)
        assert row["fqo"]["lower"] == ev.FILL and row["fqo"]["evidence"] == "OBSERVED_SAME"


# ------------------------------------------------------------------ population separation
def _header(**kw):
    base = {"record_type": "RUN_HEADER", "run_id": "r", "execution_mode": "DRY_RUN", "evidence_tier": "SIMULATED_ENV",
            "plumbing_revision": dp.PLUMBING_REVISION, "run_label": None,
            "policy_fingerprint": {"policy_fingerprint_sha256": "a" * 64}, "run_fingerprint": "f1"}
    base.update(kw)
    return base


@pytest.mark.parametrize("other", [
    {"execution_mode": "LIVE", "evidence_tier": "LIVE_VENUE"},
    {"plumbing_revision": None},
    {"policy_fingerprint": {"policy_fingerprint_sha256": "b" * 64}},
])
def test_mixed_population_is_refused(other):
    dp.check_population([_header()])
    with pytest.raises(dp.PopulationMergeRefused):
        dp.check_population([_header(), _header(run_id="r2", **other)])


def test_pre_plumbing_and_post_plumbing_headers_never_merge():
    pre = {"record_type": "RUN_HEADER", "run_id": "old", "run_fingerprint": "x"}
    assert dp.check_population([pre])["plumbing_revision"] == "PRE_PLUMBING"
    with pytest.raises(dp.PopulationMergeRefused):
        dp.check_population([pre, _header()])


def test_plumbing_smoke_runs_are_never_evaluable():
    with pytest.raises(dp.PopulationMergeRefused):
        dp.check_population([_header(run_label="PLUMBING_SMOKE")])
    assert dp.check_population([_header(run_label="PLUMBING_SMOKE")], allow_non_evaluable=True)
    assert dp.labelled_run_id("run_1_ab", "PLUMBING_SMOKE") == "PLUMBING_SMOKE_run_1_ab"
    assert dp.run_label_from_env({}) is None
    assert dp.run_label_from_env({"RESEARCH_RUN_LABEL": "PLUMBING_SMOKE"}) == "PLUMBING_SMOKE"
    with pytest.raises(ValueError):
        dp.run_label_from_env({"RESEARCH_RUN_LABEL": "bad label; rm"})


def test_offline_and_prereg_evaluators_refuse_mixed_modes(tmp_path):
    spec = importlib.util.spec_from_file_location("off", ROOT / "scripts/entry_v3_offline_eval.py")
    off = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(off)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(json.dumps(_header()) + "\n")
    b.write_text(json.dumps(_header(run_id="r2", execution_mode="LIVE", evidence_tier="LIVE_VENUE")) + "\n")
    with pytest.raises(dp.PopulationMergeRefused):
        off.evaluate([a, b], {}, v1_labels={})
    from bot.research import prereg_evaluator as pe
    with pytest.raises(dp.PopulationMergeRefused):
        pe.evaluate([], run_headers=[_header(), _header(run_id="r2", execution_mode="LIVE")], complete_days={})
    with pytest.raises(dp.PopulationMergeRefused):
        off.evaluate([a], {}, v1_labels={"DRY-1": {"c_decision": "WOULD_CANCEL", "execution_mode": "LIVE"}})


def test_run_header_and_policy_fingerprint_carry_plumbing_revision(clock):
    host = make_host(clock, shadow=False)
    h = records(host, "RUN_HEADER")[0]
    assert (h["plumbing_revision"], h["execution_mode"], h["evidence_tier"]) == \
           (dp.PLUMBING_REVISION, "DRY_RUN", "SIMULATED_ENV")
    from bot.research_startup_guard import build_policy_fingerprint
    fp = build_policy_fingerprint(SimpleNamespace(), profile_name="p", git={"revision": "x", "dirty": False})
    assert fp["plumbing_revision"] == dp.PLUMBING_REVISION
    body = {k: v for k, v in fp.items() if k != "policy_fingerprint_sha256"}
    assert fp["policy_fingerprint_sha256"] == hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    src = (ROOT / "bot/settings.py").read_text()
    assert "labelled_run_id" in src and "run_label_from_env" in src


# ================================================================== G3
def _candidate(host, role="FIRST_ACCEPTED"):
    return [r for r in records(host, "CANDIDATE") if r["candidate_role"] == role][0]


def test_g3_a_dry_run_accepted_p_fair_captured_from_the_decision_event(clock):
    host = make_host(clock, shadow=False)
    submit(host, clock, "0.68", p_fair="0.745")
    c = _candidate(host)
    assert c["production_p_fair"] == pytest.approx(0.745)
    assert c["production_p_fair_source"] == "DRY_RUN_PAPER_ORDER_EVENT"
    assert "production_p_fair" not in c["missing_reasons"]
    assert c["production_p_fair_asof_ts"] <= c["feature_cutoff_ts"]
    paper = journal(host, "SHADOW_SIM_ENTRY_CANDIDATE")[0]["payload"]
    assert paper["fair"] == pytest.approx(0.745) and paper["order_key"] == "buy:inst-up"
    assert "p_fair" not in journal(host, "ORDER_DRY_RUN_SUBMITTED")[0]["payload"]   # protected submit path untouched


def test_g3_a_paper_fair_is_consumed_once_and_requires_the_same_order_key(clock):
    host = make_host(clock, shadow=False)
    rec = host.candidate_telemetry
    rec.observe_order_event(host, event_type="SHADOW_SIM_ENTRY_CANDIDATE", side="UP", price=0.68, qty=5.0,
                            client_order_id="shadow-sim:x", reason=None,
                            payload={"slug": SLUG, "order_key": "buy:other", "fair": 0.7, "planned_quote_ts": clock.t})
    rec.observe_order_event(host, event_type="ORDER_DRY_RUN_SUBMITTED", side="BUY", price=0.68, qty=5.0,
                            client_order_id="D1", reason=None, payload={"instrument_id": "inst-up", "order_key": "buy:inst-up"})
    c = _candidate(host)
    assert c["production_p_fair"] is None
    assert c["missing_reasons"]["production_p_fair"] == "PRODUCTION_P_FAIR_NOT_IN_DECISION_EVENT"
    assert rec._pending_paper_entry == {}

    host2 = make_host(clock, dry=False, shadow=False)     # LIVE: ORDER_SUBMIT carries p_fair itself
    host2.candidate_telemetry.observe_order_event(host2, event_type="ORDER_SUBMIT", side="BUY", price=0.68, qty=5.0,
                                                  client_order_id="L1", reason=None,
                                                  payload={"instrument_id": "inst-up", "p_fair": 0.74})
    c2 = _candidate(host2)
    assert c2["production_p_fair"] == pytest.approx(0.74) and c2["production_p_fair_source"] == "DECISION_EVENT_PAYLOAD"


def test_g3_b_value_only_after_cutoff_is_not_captured(clock):
    host = make_host(clock, shadow=False)
    host.candidate_telemetry.observe_order_event(
        host, event_type="ORDER_DRY_RUN_SUBMITTED", side="BUY", price=0.68, qty=5.0, client_order_id="D1",
        reason=None, payload={"instrument_id": "inst-up", "p_fair": 0.75, "p_fair_asof_ts": clock.t + 5.0})
    c = _candidate(host)
    assert c["production_p_fair"] is None and c["production_p_fair_source"] is None
    assert c["missing_reasons"]["production_p_fair"] == "PRODUCTION_P_FAIR_AFTER_DECISION"


def test_g3_rejected_event_without_p_fair_has_explicit_reason_and_no_backfill(clock):
    host = make_host(clock, shadow=False)
    payload = {"instrument_id": "inst-up"}
    host.candidate_telemetry.observe_order_event(
        host, event_type="ORDER_SKIP_DIRECTIONAL_ENTRY_GATE", side="BUY", price=0.68, qty=None, client_order_id=None,
        reason="directional_entry_gate", payload=payload)
    c = _candidate(host, "FIRST_EVALUATION")
    assert c["production_p_fair"] is None
    assert c["missing_reasons"]["production_p_fair"] == "PRODUCTION_P_FAIR_NOT_IN_DECISION_EVENT"
    payload["p_fair"] = 0.9                                  # appears later: never written back
    submit(host, clock, "0.68")
    assert _candidate(host, "FIRST_EVALUATION")["production_p_fair"] is None


def test_g3_no_future_leakage_in_candidate_records(clock):
    host = _fill_scenario(clock)
    close_market(host, clock)
    for c in records(host, "CANDIDATE"):
        assert not [k for k in c if any(t in k.lower() for t in ct.FORBIDDEN_SNAPSHOT_FIELD_TOKENS)]
        if c.get("production_p_fair_asof_ts") is not None:
            assert c["production_p_fair_asof_ts"] <= c["feature_cutoff_ts"]


# ================================================================== compatibility
def test_old_readers_accept_new_records(clock, tmp_path):
    host = _fill_scenario(clock, requote=True)
    close_market(host, clock)
    path = tmp_path / "new.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in host.candidate_telemetry.sink.records))
    spec = importlib.util.spec_from_file_location("chk", ROOT / "scripts/check_candidate_telemetry.py")
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    rep = chk.check([path])
    assert rep["core_coverage_ok"] and rep["bad_schema"] == 0
    assert rep["simulated_fills"] == 1 and rep["dry_run_terminal_states"] == 1
    assert rep["accepted_closed_without_terminal"] == 0
    from bot.research.forward_monitor import monitor
    assert monitor([path])["lifecycle_join_rate"] == 1.0
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    life = [r for r in recs if r["record_type"] == "LIFECYCLE"]
    assert ev.funnel(recs, life, [])
    assert ct.SCHEMA_VERSION == "candidate_decision_v2"


def test_pre_baseline_frozen_input_still_reads():
    frozen = Path("/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/reports/"
                  "pre24h_entry_v3_baseline_20261013/inputs/candidate_v1_20261013.jsonl")
    if not frozen.exists():
        pytest.skip("frozen PRE input not present on this host")
    spec = importlib.util.spec_from_file_location("chk", ROOT / "scripts/check_candidate_telemetry.py")
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    rep = chk.check([frozen])
    assert rep["candidates"] == 497 and rep["simulated_fills"] == 0 and rep["population"]["plumbing_revision"] == \
        "PRE_PLUMBING"

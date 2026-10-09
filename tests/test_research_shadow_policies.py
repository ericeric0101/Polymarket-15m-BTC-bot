"""Candidate entry policy shadow and stop-loss shadow: research only, never order authority."""
import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from bot.research import candidate_policy, stop_shadow
from bot.research.candidate_policy import CandidatePolicyShadow, evaluate_candidate_v1
from bot.research.stop_shadow import StopCandidateShadow
from bot.shadow_simulation import ShadowSimulationMixin


class DB:
    def __init__(self):
        self.rows = []

    def enqueue_decision(self, **kw):
        self.rows.append(kw["payload"])
        return True


# --- candidate policy ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,price,tte,ok,fails", [
    (0.31, 0.80, 470, True, []),
    (-0.35, 0.76, 300, True, []),
    (0.29, 0.80, 470, False, ["score_below_0.30"]),
    (0.40, 0.74, 470, False, ["entry_price_below_0.75"]),
    (0.40, 0.80, 481, False, ["tte_above_480"]),
    (None, 0.80, 470, False, ["score_missing"]),
])
def test_candidate_v1_rules_are_the_frozen_ones(score, price, tte, ok, fails):
    out = evaluate_candidate_v1(score=score, entry_price=price, time_left_sec=tte)
    assert out["candidate_pass"] is ok and out["candidate_fail_reasons"] == fails


def test_candidate_shadow_writes_only_decision_transitions_with_policy_version():
    db = DB()
    shadow = CandidatePolicyShadow(db=db, run_id="r")
    kw = dict(slug="m", intended_side="UP", current_reason="eligible", score=0.4, entry_price=0.8, now_ts=1.0)
    for tte in (600, 590, 580):               # both policies unchanged across quote cycles -> one row
        shadow.observe(current_pass=True, time_left_sec=tte, **kw)
    shadow.observe(current_pass=True, time_left_sec=470, **kw)  # candidate flips to pass -> new row
    shadow.observe(current_pass=False, time_left_sec=460, **kw)  # current flips -> new row
    assert len(db.rows) == 3
    assert {r["policy_version"] for r in db.rows} == {"CANDIDATE_ENTRY_POLICY_V1"}
    assert [r["candidate_pass"] for r in db.rows] == [False, True, True]
    assert [r["current_pass"] for r in db.rows] == [True, True, False]


# --- stop shadow --------------------------------------------------------------------------------

def _position():
    return {"simulation_id": "sim", "slug": "m", "side": "UP", "entry_price": 0.80, "qty": 5.0, "filled_ts": 100.0}


def test_stop_candidates_are_event_driven_and_resolved_with_canonical_outcome():
    db = DB()
    shadow = StopCandidateShadow(db=db, run_id="r", hard_loss_usdc=2.0, hard_loss_min_hold_sec=60)
    pos = _position()
    common = dict(position=pos, ask=0.6, strike=100.0, time_left_sec=200, score=0.4)
    assert shadow.observe(bid=0.79, now_ts=110, twap=100.5, **common) == []           # favourable
    first = shadow.observe(bid=0.55, now_ts=120, twap=99.5, **common)                 # adverse cross
    assert [c["stop_reason"] for c in first] == ["ADVERSE_CROSS"]
    assert shadow.observe(bid=0.50, now_ts=130, twap=99.4, **common) == []           # same state, no row
    later = shadow.observe(bid=0.35, now_ts=170, twap=99.0, **common)                # persisted + hard loss
    assert sorted(c["stop_reason"] for c in later) == ["ADVERSE_CROSS_PERSIST_15S", "HARD_LOSS_EQUIVALENT"]
    assert later[0]["pnl_if_stop_now_usdc"] == pytest.approx(5 * (0.35 - 0.80))
    assert shadow.resolve(slug="m", outcome="UNKNOWN", settlement_ts=1000) == []     # never guessed
    resolved = shadow.resolve(slug="m", outcome="UP", settlement_ts=1000)
    assert len(resolved) == 3 and all(r["pnl_if_hold_usdc"] == pytest.approx(5 * 0.20) for r in resolved)
    assert {r["event_type"] for r in db.rows} == {"STOP_SHADOW_CANDIDATE", "STOP_SHADOW_RESOLUTION"}


def test_shadow_paths_cannot_reach_order_submission():
    forbidden = {"submit_order", "cancel_order", "modify_order", "order_factory", "_submit_taker_exit_order",
                 "submit_maker_quote", "_cancel_maker_order_side"}
    for module in (candidate_policy, stop_shadow):
        names = {n.attr for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(ast.parse(inspect.getsource(module))) if isinstance(n, ast.Name)}
        assert not names & forbidden, module.__name__

    class Host(ShadowSimulationMixin):
        current_market_end_timestamp = 1000.0
        market_strike_cache_by_slug = {"m": Decimal("100")}
        _polymarket_chainlink_twap_price = 99.0
        side_decision_score = 0.4

        def __init__(self):
            self.stop_candidate_shadow = StopCandidateShadow(db=DB(), run_id="r", hard_loss_usdc=2.0)

        def submit_order(self, *a, **k):
            raise AssertionError("shadow path reached submit_order")

        def cancel_order(self, *a, **k):
            raise AssertionError("shadow path reached cancel_order")

        def _is_dry_run_mode(self):
            return True

    host = Host()
    host._observe_stop_shadow(state=_position(), slug="m", bid=Decimal("0.3"), ask=Decimal("0.32"), now_ts=200.0)
    assert host.stop_candidate_shadow.db.rows  # observed, recorded, no order call


def test_entry_trace_hook_cannot_alter_the_production_decision():
    tree = ast.parse(Path("run_bot.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "observe" and isinstance(n.func.value, ast.Name) and n.func.value.id == "policy_shadow"]
    assert len(calls) == 1
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    node = calls[0]
    while not isinstance(node, ast.Try):
        node = parents[node]
        assert not isinstance(node, ast.Assign), "shadow result must not be assigned into decision state"
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in node.handlers)

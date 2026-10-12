"""Legacy annotate_decision clock-domain fix (hermetic).

Upstream source clocks (Polymarket CLOB, Binance, Chainlink) lead the host clock by
up to ~0.6 s.  Availability must be decided by LOCAL receipt/wall clocks only; the
upstream lead is diagnostic.  A future LOCAL receipt must still fail closed.
"""
import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.prediction_research_snapshot as prs
import bot.research.evidence as evidence
from bot.research.clocks import available_at, latest_evidence, source_clock_lead_sec, timestamp_domain
from bot.research.freshness import native_v2_exclusion_reason

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests/fixtures/legacy_clock_lead_cases.json").read_text())["cases"]


def _legacy_available_at(row, decision):
    """The pre-fix rule (bot/research/clocks.py at 08bcb32), kept only as a reference."""
    if row["snapshot_ts"] > decision:
        return False
    for k, v in row.items():
        if v is not None and (k.endswith(("_source_ts", "_received_ts", "_receive_ts", "_evidence_ts"))
                              or k in {"snapshot_ts", "fill_ts", "actual_stop_ts", "decision_ts"}) and v > decision:
            return False
    return True


def _snap(decision, *, age=0.05, lead=0.15, received_lag=0.02, pex=0.64):
    snap_ts = decision - age
    return {"snapshot_ts": snap_ts, "p_up_ex_market": 1 - pex, "p_down_ex_market": pex,
            "market_quote_up_source_ts": snap_ts + lead, "market_quote_up_received_ts": snap_ts - received_lag,
            "btc_source_ts": snap_ts + lead / 2, "btc_received_ts": snap_ts - received_lag}


@pytest.mark.parametrize("lead", [0.001, 0.15, 0.34, 0.63])
def test_upstream_source_ahead_of_local_clock_stays_available(lead):
    decision = 1_000.0
    row = _snap(decision, age=0.0005, lead=lead)
    assert row["market_quote_up_source_ts"] > decision        # source clock ahead of the host
    assert row["market_quote_up_received_ts"] <= decision     # received locally before the decision
    assert available_at(row, decision, max_age_sec=8) is True
    assert _legacy_available_at(row, decision) is False       # the defect being fixed
    assert source_clock_lead_sec(row, row["snapshot_ts"]) == pytest.approx(lead)


def test_future_local_receipt_fails_closed():
    decision = 1_000.0
    row = _snap(decision)
    row["market_quote_up_received_ts"] = decision + 0.01
    assert available_at(row, decision, max_age_sec=8) is False
    row = _snap(decision)
    row["snapshot_ts"] = decision + 0.001
    assert available_at(row, decision) is False
    row = _snap(decision)
    row["nested"] = {"p_ex_evidence_ts": decision + 0.5}
    assert available_at(row, decision) is False


def test_true_stale_local_data_fails_closed():
    decision = 1_000.0
    assert available_at(_snap(decision, age=8.5), decision, max_age_sec=8) is False
    assert latest_evidence([_snap(decision, age=9.0)], decision, max_age_sec=8) is None


def test_frozen_44h_clock_lead_cases_recover():
    assert len(CASES) == 184
    decision = 1_000.0
    for c in CASES:
        row = _snap(decision, age=c["snapshot_age_sec"], lead=c["source_clock_lead_sec"])
        assert _legacy_available_at(row, decision) is False
        assert available_at(row, decision, max_age_sec=8) is True


def test_timestamp_domain_classification():
    assert timestamp_domain("market_quote_up_source_ts") == "UPSTREAM_SOURCE"
    assert timestamp_domain("btc_received_ts") == "LOCAL_RECEIPT"
    assert timestamp_domain("snapshot_ts") == "LOCAL_WALL"
    assert timestamp_domain("p_ex_evidence_ts") == "LOCAL_WALL"
    assert timestamp_domain("decision_monotonic_ns") == "LOCAL_MONOTONIC"
    assert timestamp_domain("whatever") == "UNKNOWN"


class _Strategy:
    def __init__(self, snap, tick):
        self.run_id = "r"
        self.current_market_slug = "m"
        self.instrument_id = "inst"
        self.prediction_research_snapshotter = SimpleNamespace(_last_payload_by_slug={"m": snap})
        self.live_inventory_cost = {}
        self.last_quote_update_ts_by_inst = {"inst": tick} if tick is not None else {}

    def _instrument_key(self, inst):
        return str(inst)


def _annotate(snap, tick, event="ORDER_SUBMIT", now=1_000.0):
    out = evidence.annotate_decision(_Strategy(snap, tick), {"market_slug": "m"}, kind="EXECUTION_DECISION",
                                     instrument_id="inst", now_ts=now, source_event_type=event, client_order_id="c")
    return out["decision_trace"]


def test_annotate_decision_keeps_pex_and_propagates_local_decision_tick():
    now = 1_000.0
    tr = _annotate(_snap(now, age=0.0005, lead=0.34), tick=now - 0.003)
    assert tr["p_down_ex_market"] == 0.64
    assert tr["decision_ts"] == pytest.approx(now - 0.003) and tr["decision_ts_status"] == "INPUT_TICK_LOCAL"
    assert tr["evidence_clock_semantics"] == "LOCAL_RECEIPT_V2"
    assert tr["source_clock_lead_sec"] == pytest.approx(0.34)
    assert isinstance(tr["captured_monotonic_ns"], int)


@pytest.mark.parametrize("tick,event", [(None, "ORDER_SUBMIT"), (1_000.5, "ORDER_SUBMIT"), (999.9, "ORDER_FILLED")])
def test_decision_ts_stays_unknown_when_not_safely_known(tick, event):
    tr = _annotate(_snap(1_000.0), tick=tick, event=event)
    assert tr["decision_ts"] is None and tr["decision_ts_status"] == "UNKNOWN"


def test_freshness_v2_semantics_untouched():
    # Production freshness v2 lives in the snapshot builder and freshness module; neither
    # uses available_at, and both files are byte-identical to the pre-fix base (e42236f).
    tree = ast.parse(inspect.getsource(prs))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "available_at" not in names and "latest_evidence" not in names
    assert hashlib.sha256((ROOT / "bot/prediction_research_snapshot.py").read_bytes()).hexdigest() == \
        "050360b42d3afb76d1dd674cd7ed1fee8e0c2b34573e681961bf1c78c133826f"
    assert hashlib.sha256((ROOT / "bot/research/freshness.py").read_bytes()).hexdigest() == \
        "d98c041f355dc76444199f9d7bb5633d5fc7e45ac6ac91eebfd44edb4ccf4fae"
    assert native_v2_exclusion_reason({"freshness_clock_semantics_version": 2}) is None
    assert native_v2_exclusion_reason({"freshness_clock_semantics_version": 1}) == "LEGACY_FRESHNESS_CLOCK_V1"

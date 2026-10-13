"""Entry V3 candidate_decision_v2: atomic decision-time feature vector X_t (capture only, no model)."""
import ast
import dataclasses
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import bot.candidate_telemetry as ct
from test_candidate_telemetry import SLUG, START, _decide, _kinds, _recorder, _Strategy

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = START + 300.2  # recorder clock = decision wall time


def _rich_strategy(**kw):
    s = _Strategy(**kw)
    s.latest_quote_by_inst["up"] = (0.26, 0.27)
    s.last_quote_update_ts_by_inst["up"] = START + 299.9
    s._instrument_for_side = lambda side: "up" if getattr(side, "value", side) == "UP" else "down"
    s._polymarket_chainlink_price = 82940.0
    s._polymarket_chainlink_price_ts = START + 300.0          # local receipt
    s._polymarket_chainlink_event_ts_ms = (START + 300.4) * 1000  # upstream clock leads local: allowed
    s._binance_ws_price = 82950.0
    s._binance_ws_price_ts = START + 300.1
    s._binance_ws_price_source_ts = START + 300.5
    s._polymarket_chainlink_twap_price = 82945.0
    s._polymarket_chainlink_twap_price_ts = START + 299.0
    s._polymarket_chainlink_twap_observation_ts = START + 298.9
    s.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
    hist = deque(maxlen=2400)
    for i in range(0, 70):
        t = START + 230.2 + i          # receipts START+230.2 .. START+299.2
        hist.append((t + 0.3, 82900.0 + i, t))
    hist.append((START + 301.0, 99999.0, START + 300.9))  # received AFTER the decision: must be ignored
    s._prediction_btc_research_history = hist
    s.twap_forward_shadow = SimpleNamespace(latest=lambda slug: {"observed_ts": START + 299.5, "source_ts": START + 299.9,
                                                                 "projected_settlement_side_trend": "DOWN"})
    return s


def _snapshot(strategy):
    rec, sink, _ = _recorder(strategy, t=CUTOFF)
    _decide(rec, strategy)
    return _kinds(sink, "CANDIDATE")[0]


def test_schema_v2_and_atomic_single_cutoff():
    c = _snapshot(_rich_strategy())
    assert c["schema_version"] == ct.SCHEMA_VERSION == "candidate_decision_v2"
    assert c["feature_cutoff_ts"] == c["decision_wall_ts"] == CUTOFF
    assert c["feature_cutoff_basis"] == "DECISION_WALL_TS"
    assert c["decision_ts"] is not None and c["decision_ts"] <= c["feature_cutoff_ts"]
    assert c["decision_monotonic_ns"] == 123


def test_btc_reference_by_source_uses_local_receipt_not_upstream_clock():
    c = _snapshot(_rich_strategy())
    assert c["btc_chainlink_price"] == 82940.0 and c["btc_chainlink_receipt_ts"] == START + 300.0
    assert c["btc_chainlink_source_ts"] > c["feature_cutoff_ts"]  # upstream lead recorded, not rejected
    assert c["btc_binance_price"] == 82950.0 and c["btc_twap_price"] == 82945.0
    for prefix in ("btc_chainlink", "btc_binance", "btc_twap"):
        assert c[f"{prefix}_receipt_ts"] <= c["feature_cutoff_ts"]
        assert c[f"{prefix}_age_sec"] == c["feature_cutoff_ts"] - c[f"{prefix}_receipt_ts"]


def test_reference_received_after_decision_is_null_with_reason():
    s = _rich_strategy()
    s._binance_ws_price_ts = CUTOFF + 0.5
    c = _snapshot(s)
    assert c["btc_binance_price"] is None and c["missing_reasons"]["btc_binance"] == "RECEIPT_AFTER_DECISION"


def test_short_returns_only_from_receipts_at_or_before_decision():
    c = _snapshot(_rich_strategy())
    now_p = 82900.0 + 69          # last receipt <= cutoff (START+299.2), the 99999 tick is excluded
    assert abs(c["btc_ret_5s"] - (now_p / (82900.0 + 64) - 1)) < 1e-12
    assert abs(c["btc_ret_60s"] - (now_p / (82900.0 + 9) - 1)) < 1e-12
    assert c["btc_ret_source"] == "binance_ws_aggtrade_local_receipt"


def test_insufficient_history_has_explicit_reason():
    s = _rich_strategy()
    s._prediction_btc_research_history = deque([(START + 299.5, 82900.0, START + 299.2)])
    c = _snapshot(s)
    assert c["btc_ret_60s"] is None and c["missing_reasons"]["btc_returns"].startswith("RETURN_HISTORY_INSUFFICIENT")


def test_twap_state_book_other_side_and_alignment_with_strategy_state():
    s = _rich_strategy()
    c = _snapshot(s)
    assert c["twap_state_projected_side"] == "DOWN" and c["twap_state_observed_ts"] <= c["feature_cutoff_ts"]
    assert c["twap_minus_strike_usd"] == 82945.0 - 82956.85
    assert c["other_side_bid"] == 0.26 and c["other_side_ask"] == 0.27
    assert c["top_imbalance"] == (40.0 - 50.0) / 90.0
    assert c["best_bid"] == 0.73 and c["best_ask"] == 0.74 and c["side_score"] == -0.30
    assert c["tte_sec"] == START + 900 - c["decision_ts"]
    assert c["runtime_strike"] == 82956.85 and c["proposed_maker_price"] == 0.74 and c["proposed_qty"] == 5.5


def test_pex_missing_reason_is_explicit():
    c = _snapshot(_rich_strategy(snapshotter=False))
    assert c["raw_p_ex"] is None and c["pex_reason"] == "PEX_SNAPSHOTTER_ABSENT"
    c2 = _snapshot(_rich_strategy(snap_ts=CUTOFF + 1.0))
    assert c2["raw_p_ex"] is None and c2["pex_reason"] == "PEX_AFTER_DECISION"


def test_snapshot_contains_no_future_or_outcome_fields():
    names = [f.name for f in dataclasses.fields(ct.CandidateDecisionSnapshot)]
    for n in names:
        assert not any(tok in n for tok in ct.FORBIDDEN_SNAPSHOT_FIELD_TOKENS), n
    c = _snapshot(_rich_strategy())
    assert not any(tok in k for k in c for tok in ct.FORBIDDEN_SNAPSHOT_FIELD_TOKENS)


def test_minimal_state_never_raises_and_reports_reasons():
    c = _snapshot(_Strategy())  # no BTC feeds, no TWAP shadow, no other side
    for key in ("btc_chainlink", "btc_binance", "btc_twap", "btc_returns", "twap_state", "other_side_bbo"):
        assert key in c["missing_reasons"], key
    assert c["alignment_status"] == "ALIGNED"


def test_telemetry_never_imports_outcome_linker_or_evaluator():
    tree = ast.parse((ROOT / "bot/candidate_telemetry.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not mods & {"bot.research.candidate_labels", "bot.research.prereg_evaluator", "bot.execution_selection_eval"}

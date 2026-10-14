import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.enums import ActiveSide, MarketPhase
from bot.reference_basis import (
    BASIS_ADJUSTED_SOURCE,
    RollingBasisEstimator,
    adjust_binance_price,
    is_degraded_reference_source,
    reference_spot_provenance,
)
from bot.side_decision import SideDecisionMixin
from bot.taker_exit import TakerExitMixin
from run_bot import IntegratedBTCStrategy


def _primed(n=30, bps=10.0, start=1000.0):
    est = RollingBasisEstimator(min_pairs=30)
    for i in range(n):
        t = start + i
        assert est.observe(binance_price=100.0 * (1 + bps / 1e4), binance_ts=t, chainlink_price=100.0,
                           chainlink_ts=t, now_ts=t)
    return est


# --- estimator ------------------------------------------------------------------------------

def test_estimator_needs_min_pairs_and_returns_median():
    est = _primed(n=29)
    assert est.estimate(1029.0) == (None, 29)
    est = _primed(n=30)
    basis, pairs = est.estimate(1029.0)
    assert pairs == 30 and basis == pytest.approx(10.0)


def test_estimator_rejects_stale_skewed_and_duplicate_pairs():
    est = RollingBasisEstimator()
    # stale Binance
    assert not est.observe(binance_price=100, binance_ts=990, chainlink_price=100, chainlink_ts=999.5, now_ts=1000)
    # feeds too far apart
    assert not est.observe(binance_price=100, binance_ts=999.9, chainlink_price=100, chainlink_ts=998.5, now_ts=1000)
    assert est.observe(binance_price=100, binance_ts=999.9, chainlink_price=100, chainlink_ts=999.5, now_ts=1000)
    # same Chainlink update counted once
    assert not est.observe(binance_price=100, binance_ts=1000, chainlink_price=100, chainlink_ts=999.5, now_ts=1000.2)


def test_estimator_window_drops_old_pairs_and_never_looks_ahead():
    est = _primed(n=30, start=1000.0)
    assert est.estimate(1000.0 + 29 + 3600 + 1)[0] is None
    # Query before the pairs exist: none of them may be used.
    assert est.estimate(999.0) == (None, 0)


def test_adjust_binance_price_maps_onto_chainlink_level():
    assert adjust_binance_price(Decimal("100.1"), 10.0) == pytest.approx(Decimal("99.9999"), abs=Decimal("1e-9"))


# --- reference spot selection --------------------------------------------------------------

def _fallback_host(estimator):
    now = 2000.0
    return now, SimpleNamespace(
        _polymarket_chainlink_twap_price=Decimal("100.00"),
        _polymarket_chainlink_twap_price_ts=now - 20,
        _polymarket_chainlink_twap_observation_ts=now - 20,
        _polymarket_chainlink_twap_window_sec=60,
        polymarket_chainlink_twap_window_sec=60,
        _polymarket_chainlink_price=Decimal("100.00"),
        _polymarket_chainlink_price_ts=now - 20,
        _binance_ws_price=Decimal("100.10"),
        _binance_ws_price_ts=now - 0.5,
        latest_external_spot=None,
        latest_external_spot_source="",
        latest_external_spot_source_ts=0.0,
        last_external_spot=None,
        external_spot_history=[],
        _reference_basis_estimator=estimator,
        _reference_spot_basis_state={},
    )


def test_binance_fallback_is_basis_adjusted_when_estimate_available():
    now, host = _fallback_host(_primed(n=40, bps=10.0, start=2000.0 - 600))
    price, source, age = IntegratedBTCStrategy._capture_market_open_spot_detail(host, now_ts=now)
    assert source == BASIS_ADJUSTED_SOURCE
    assert price == pytest.approx(Decimal("100.10") * (1 - Decimal("0.001")), abs=Decimal("1e-9"))
    assert age == pytest.approx(0.5)
    assert host._reference_spot_basis_state["reference_basis_applied"] is True
    assert host._reference_spot_basis_state["reference_spot_raw_price"] == pytest.approx(100.10)


def test_binance_fallback_keeps_legacy_value_without_estimate():
    now, host = _fallback_host(RollingBasisEstimator())
    price, source, _ = IntegratedBTCStrategy._capture_market_open_spot_detail(host, now_ts=now)
    assert (price, source) == (Decimal("100.10"), "binance_ws")
    assert host._reference_spot_basis_state["reference_basis_applied"] is False
    assert host._reference_spot_basis_state["reference_basis_unavailable"] is True


def test_fresh_twap_is_never_adjusted_and_feeds_pairs():
    now, host = _fallback_host(RollingBasisEstimator(min_pairs=1))
    host._polymarket_chainlink_twap_observation_ts = now - 1
    host._polymarket_chainlink_price_ts = now - 0.3
    price, source, _ = IntegratedBTCStrategy._capture_market_open_spot_detail(host, now_ts=now)
    assert (price, source) == (Decimal("100.00"), "polymarket_chainlink_twap_60s_ws")
    basis, pairs = host._reference_basis_estimator.estimate(now)
    assert pairs == 1 and basis == pytest.approx(10.0)


# --- side decision -------------------------------------------------------------------------

@pytest.mark.parametrize("source,degraded", [
    ("binance_ws", True), (BASIS_ADJUSTED_SOURCE, True), ("coinbase_http", True),
    ("polymarket_chainlink_twap_60s_ws", False), ("polymarket_chainlink_ws", False), ("", False), (None, False),
])
def test_degraded_reference_classification(source, degraded):
    assert is_degraded_reference_source(source) is degraded


class _SideHost(SideDecisionMixin):
    def __init__(self, source):
        self._source = source
        self.bi_side_enabled = True
        self.bi_side_default_mode = "NONE"
        self.current_market_slug = "btc-updown-15m-1"
        self.market_strike_cache_by_slug = {"btc-updown-15m-1": Decimal("100")}
        self.market_strike_source_by_slug = {}
        self.current_market_end_timestamp = None
        self.active_side = ActiveSide.UP
        self.active_side_locked = True
        self.side_decision_ts = 0.0
        self.side_decision_score = Decimal("0.4")
        self.side_decision_reason = "previous"
        self.side_decision_inputs = {}
        self.side_decision_due_ts = 0.0
        self.side_pending_flip_side = ActiveSide.DOWN
        self.side_pending_flip_count = 1
        self.events = []

    def _capture_market_open_spot(self):
        return Decimal("100.5")

    def _capture_market_open_spot_detail(self, *, now_ts=None):
        return (Decimal("100.5"), self._source, 0.5)

    def _normalize_active_side(self, value):
        return ActiveSide.NONE if str(value).upper() == "NONE" else ActiveSide(str(value).upper())

    def _primary_instrument_for_market(self):
        return None

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))

    def _log_side_decision_skip_throttled(self, **kwargs):
        pass


def test_side_decision_skips_on_degraded_reference_and_keeps_side():
    host = _SideHost("binance_ws_basis_adjusted")
    side, score, reason, inputs = host._compute_side_decision_new(1000.0)
    assert reason == "spot_reference_degraded" and score == 0
    assert inputs["selected_spot_source"] == "binance_ws_basis_adjusted"

    asyncio.run(host._maybe_finalize_side_decision(1000.0, MarketPhase.ACTIVE))
    assert host.active_side == ActiveSide.UP
    assert host.side_pending_flip_side == ActiveSide.DOWN and host.side_pending_flip_count == 1
    assert [e for e, _ in host.events] == ["SIDE_DECISION_SKIPPED"]
    assert host.events[0][1]["reason"] == "spot_reference_degraded"


# --- exit provenance -----------------------------------------------------------------------

class _ExitHost(TakerExitMixin):
    def __init__(self):
        self._taker_exit_skip_log_ts_by_key = {}
        self.taker_exit_skip_log_interval_sec = 0.0
        self._reference_spot_basis_state = {"reference_basis_applied": True, "reference_basis_bps": 9.5,
                                            "reference_basis_pairs": 120}
        self.events = []

    def _capture_market_open_spot_detail(self, *, now_ts=None):
        return (Decimal("100"), BASIS_ADJUSTED_SOURCE, 0.4)

    def _db_strategy_event(self, event_type, payload):
        self.events.append((event_type, payload))


def test_exit_events_record_reference_spot_provenance_without_overriding_payload():
    host = _ExitHost()
    host._record_exit_policy_decision_throttled("inst", "tag", 1.0, {"reason": "hold", "reference_spot_source": "kept"})
    host._record_exit_audit_throttled("inst", "tag", 1.0, {"reason": "audit"})
    (e1, p1), (e2, p2) = host.events
    assert (e1, e2) == ("EXIT_POLICY_DECISION", "EXIT_AUDIT")
    assert p1["reference_spot_source"] == "kept"
    assert p2["reference_spot_source"] == BASIS_ADJUSTED_SOURCE
    assert p2["reference_spot_degraded"] is True
    assert p2["reference_basis_bps"] == 9.5 and p2["reference_basis_pairs"] == 120


def test_provenance_is_empty_for_hosts_without_reference_selection():
    assert reference_spot_provenance(SimpleNamespace()) == {}

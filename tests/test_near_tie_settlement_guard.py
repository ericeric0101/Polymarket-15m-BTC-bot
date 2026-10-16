"""Near-tie TWAP settlement guard (hermetic).

A 60s TWAP tick stamped before (or after) market end is not the official
end-of-market value; when its margin to the strike is small the side is left
UNKNOWN so startup Gamma reconciliation resolves it from the official result.
Evidence: the two HIGH_OFFICIAL_TWAP_CONFLICT markets in
data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv.
"""
import time
from decimal import Decimal

import pytest

from test_settlement_authority import Host

# (slug, last TWAP, strike, tick_ts - market_end, tick age at settlement); official outcome was UP for both.
CONFLICTS = [
    ("btc-updown-15m-1791039600", 84828.4032208896, 84828.57749907057, -4.0, 4.380977),
    ("btc-updown-15m-1791144000", 85406.90636769912, 85411.9999740108, -2.0, 2.496221),
]


def _settle(*, slug, twap, strike, tick_minus_end, age, qty=Decimal("0"), end_known=True):
    host = Host(twap=twap, spot=0.0, twap_age=age, qty=qty)
    host.current_market_slug = slug
    host.market_strike_cache_by_slug = {slug: Decimal(str(strike))}
    host._shadow_state = {}
    tick_ts = time.time() - age
    host._polymarket_chainlink_twap_observation_ts = tick_ts
    host.current_market_end_timestamp = (tick_ts - tick_minus_end) if end_known else None
    host.settle()
    return host, host.event("MARKET_SETTLEMENT")


@pytest.mark.parametrize("slug,twap,strike,tick_minus_end,age", CONFLICTS)
def test_observed_official_conflicts_are_not_labeled_from_pre_end_ticks(slug, twap, strike, tick_minus_end, age):
    host, settlement = _settle(slug=slug, twap=twap, strike=strike, tick_minus_end=tick_minus_end,
                               age=age, qty=Decimal("4"))
    # Pre-fix runtime labeled these DOWN; official resolution was UP.
    assert settlement["outcome"] == "UNKNOWN" and settlement["settlement_pending"] is True
    assert settlement["outcome_source"] == "unavailable"
    assert settlement["settlement_near_tie_unresolved"] is True
    assert settlement["settlement_reference_end_offset_sec"] == pytest.approx(abs(tick_minus_end))
    assert not [e for e, _ in host.events if e == "MARKET_CYCLE_PNL"]
    assert host.session_pnl == [] and host.regime == []


@pytest.mark.parametrize("slug,twap,strike,tick_minus_end,age", CONFLICTS)
def test_near_tie_tick_stamped_at_market_end_still_decides(slug, twap, strike, tick_minus_end, age):
    _, settlement = _settle(slug=slug, twap=twap, strike=strike, tick_minus_end=0.0, age=0.5)
    assert settlement["outcome"] == "DOWN" and settlement["outcome_source"] == "canonical_twap"
    assert settlement["settlement_near_tie_unresolved"] is False


@pytest.mark.parametrize("margin_bps,tick_minus_end,expected", [
    (0.8, -1.0, "UP"),        # band 0.5 bps at 1 s offset
    (0.8, -2.0, "UNKNOWN"),   # band 1.0 bps at 2 s offset
    (-1.2, -2.0, "DOWN"),
    (-1.9, -4.0, "UNKNOWN"),  # band 2.0 bps at 4 s offset
    (0.3, 3.0, "UNKNOWN"),    # post-end tick includes after-window prices
    (25.0, -4.0, "UP"),       # clear margin: unaffected
])
def test_near_tie_band_scales_with_tick_offset_from_market_end(margin_bps, tick_minus_end, expected):
    strike = 80000.0
    twap = strike * (1 + margin_bps / 1e4)
    _, settlement = _settle(slug="btc-updown-15m-1791478800", twap=twap, strike=strike,
                            tick_minus_end=tick_minus_end, age=max(0.5, 0.5 - tick_minus_end))
    assert settlement["outcome"] == expected
    assert settlement["settlement_reference_margin_bps"] == pytest.approx(margin_bps)


def test_unknown_market_end_uses_tick_age_as_offset():
    strike = 80000.0
    _, near = _settle(slug="btc-updown-15m-1791478800", twap=strike * (1 + 0.8 / 1e4), strike=strike,
                      tick_minus_end=0.0, age=3.0, end_known=False)
    assert near["outcome"] == "UNKNOWN"
    assert near["settlement_reference_end_offset_sec"] == pytest.approx(3.0, abs=0.1)
    _, clear = _settle(slug="btc-updown-15m-1791478800", twap=strike * (1 + 5 / 1e4), strike=strike,
                       tick_minus_end=0.0, age=3.0, end_known=False)
    assert clear["outcome"] == "UP"


def test_label_helper_exposes_guard_fields():
    from bot.lifecycle_runtime import _canonical_twap_shadow_label

    label = _canonical_twap_shadow_label(
        twap_price=100.0, source_ts=998.0, window_sec=60, strike=100.0,
        settlement_ts=1000.4, market_end_ts=1000.0)
    assert label["canonical"] is False and label["side"] is None
    assert label["near_tie_unresolved"] is True
    assert label["end_offset_sec"] == 2.0 and label["near_tie_band_bps"] == 1.0
    at_end = _canonical_twap_shadow_label(
        twap_price=100.0, source_ts=1000.0, window_sec=60, strike=100.0,
        settlement_ts=1000.4, market_end_ts=1000.0)
    assert at_end["canonical"] is True and at_end["side"] == "UP"  # exact tie at end settles UP

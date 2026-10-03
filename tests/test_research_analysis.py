from datetime import datetime, timezone

from scripts.research_analysis import (
    _contextual,
    market_context,
    normalized_event_threshold,
    select_catalog,
)


def test_market_context_uses_taipei_market_start_not_utc_observation_time():
    # 2026-10-02 16:00 UTC is Saturday 00:00 in Taipei.
    slug = "btc-updown-15m-1790956800"
    context = market_context(slug)
    assert context["session_regime"] == "WEEKEND"
    assert context["weekday_name"] == "Saturday"
    assert context["market_start_taipei"].endswith("+08:00")


def test_select_catalog_never_pools_regimes_when_filter_requested():
    catalog = [
        {"market_slug": "btc-updown-15m-1790956800", "run_id": "weekend"},
        {"market_slug": "btc-updown-15m-1790932500", "run_id": "weekday"},
    ]
    selected = select_catalog(catalog, markets=10, regime="weekend")
    assert [row["run_id"] for row in selected] == ["weekend"]
    assert selected[0]["session_regime"] == "WEEKEND"


def test_normalized_event_threshold_is_within_regime_and_not_absolute_threshold():
    assert normalized_event_threshold([0.01, 0.02, 0.03, 0.04, 0.05]) == 0.04
    assert normalized_event_threshold([]) is None


def test_event_context_uses_its_market_regime_not_mixed_batch_fallback():
    contexts = {"btc-updown-15m-1790956800": market_context("btc-updown-15m-1790956800")}
    row = _contextual({"market_slug": "btc-updown-15m-1790956800", "value": 1}, contexts, batch_regime="MIXED")
    assert row["session_regime"] == "WEEKEND"

from types import SimpleNamespace

from bot.market_runtime import market_pair_instruments_by_slug
from bot.spot_pricer import SpotPricerMixin
from bot.launcher import request_auto_rollover_stop


def _instrument(instrument_id, outcome):
    return SimpleNamespace(id=instrument_id, info={"outcome": outcome})


def test_market_pair_map_includes_pre_warmed_next_market_ids():
    candidates = [
        {"slug": "market-old", "instrument": _instrument("old-up", "Up")},
        {"slug": "market-next", "instrument": _instrument("next-up", "Up")},
        {"slug": "market-next", "instrument": _instrument("next-down", "Down")},
    ]

    mapping = market_pair_instruments_by_slug(
        candidates,
        extract_outcome=lambda inst: inst.info["outcome"],
    )

    assert mapping["market-next"] == {"UP": "next-up", "DOWN": "next-down"}


def test_research_bbo_selection_uses_target_market_pair_not_current_pair():
    host = SimpleNamespace(
        current_up_instrument_id="old-up",
        current_down_instrument_id="old-down",
        research_market_instruments_by_slug={
            "market-next": {"UP": "next-up", "DOWN": "next-down"},
        },
    )

    assert SpotPricerMixin._research_market_quote_instruments(
        host, slug="market-next", runtime_slug="market-old",
    ) == ("next-up", "next-down")


def test_research_bbo_selection_never_falls_back_to_old_market_pair():
    host = SimpleNamespace(
        current_up_instrument_id="old-up",
        current_down_instrument_id="old-down",
        research_market_instruments_by_slug={},
    )

    assert SpotPricerMixin._research_market_quote_instruments(
        host, slug="market-next", runtime_slug="market-old",
    ) is None


def test_auto_rollover_stop_resolves_callback_from_node_strategy():
    stopped = []
    node = SimpleNamespace(
        trader=SimpleNamespace(strategies=lambda: [
            SimpleNamespace(_request_node_stop_callback=lambda: stopped.append("stop")),
        ]),
    )

    assert request_auto_rollover_stop(node) is True
    assert stopped == ["stop"]


def test_auto_rollover_stop_has_threadsafe_node_fallback_without_strategy_hook():
    stopped = []
    node = SimpleNamespace(
        trader=SimpleNamespace(strategies=lambda: []),
        kernel=SimpleNamespace(loop=None),
        stop=lambda: stopped.append("node-stop"),
    )

    assert request_auto_rollover_stop(node) is True
    assert stopped == ["node-stop"]

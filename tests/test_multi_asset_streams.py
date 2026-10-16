"""Multi-asset / 5m extension of the recorder and the TWAP-z executor."""
import json
import sys
from pathlib import Path

from bot.research.price_recorder import (
    MARKET_STREAMS, alt_source_name, market_tokens_for, parse_rtds_chainlink, slug_duration_sec, stream_slugs,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_slug_duration_and_stream_slugs():
    assert slug_duration_sec("eth-updown-5m-1792116000") == 300
    assert slug_duration_sec("btc-updown-15m-1792116000") == 900
    slugs = stream_slugs(1792116000 + 301)
    assert len(slugs) == 2 * len(MARKET_STREAMS) == 16
    assert "btc-updown-15m-1792116000" in slugs and "btc-updown-15m-1792116900" in slugs
    assert "xrp-updown-5m-1792116300" in slugs and "xrp-updown-5m-1792116600" in slugs


def test_market_tokens_for_uses_market_length():
    m = {"version": "v1", "conditionId": "0xabc", "outcomes": json.dumps(["Up", "Down"]), "clobTokenIds": json.dumps(["1", "2"])}
    meta = market_tokens_for(["sol-updown-5m-1792116000"], lambda s: m)
    assert {v["end_ts"] for v in meta.values()} == {1792116300.0}


def test_parse_rtds_spot_twap_update_and_snapshot():
    spot = {"topic": "crypto_prices_chainlink", "payload": {"symbol": "eth/usd", "timestamp": 1792116076000,
            "full_accuracy_value": "2430676602273001000000", "value": 2430.67}}
    assert parse_rtds_chainlink(spot) == [("spot", "eth", 2430.676602273001, 1792116076000)]
    upd = {"topic": "crypto_prices_twap_sixty", "type": "update",
           "payload": {"symbol": "sol/usd", "timestamp": 1792116020000, "value": 105.0}}
    assert parse_rtds_chainlink(upd) == [("twap", "sol", 105.0, 1792116020000)]
    snap = {"topic": "crypto_prices_twap_sixty", "type": "subscribe", "payload": {"symbol": "xrp/usd", "data": [
        {"full_accuracy_value": "1329974472052685568", "timestamp": 1792116017000},
        {"value": 1.33, "timestamp": 1792116018000}]}}
    out = parse_rtds_chainlink(snap)
    assert [o[:2] for o in out] == [("twap", "xrp")] * 2 and out[1][2:] == (1.33, 1792116018000)
    assert parse_rtds_chainlink({"topic": "other", "payload": {}}) == []
    assert alt_source_name("twap", "eth") == "chainlink_twap60:eth"


def test_executor_next_checkpoint_groups_streams_by_end():
    import twap_z_executor as ex
    t, h, slugs = ex.next_checkpoint(1792116900 - 56)        # 15m and 5m markets both end at ...900
    assert (t, h) == (1792116900 - 55, 55)
    assert "btc-updown-15m-1792116000" in slugs and "btc-updown-5m-1792116600" in slugs and len(slugs) == 8
    t, h, slugs = ex.next_checkpoint(1792116600 - 56)        # only 5m markets end at ...600
    assert len(slugs) == 4 and all("-5m-" in s for s in slugs)


def test_evaluate_uses_5m_market_end():
    from bot.research.twap_z_executor import LiveBookSampler, LiveChainlink, evaluate_checkpoint
    live = LiveChainlink()
    start, end = 1792116000, 1792116300
    for s in range(start - 400, end - 20):
        live.observe("spot", 100.0 + (s % 7) * 0.001, s + 0.3, s * 1000)
    live.observe("twap", 100.0, start + 1, start * 1000)
    d = evaluate_checkpoint(chainlink=live, books=LiveBookSampler(), slug=f"eth-updown-5m-{start}", checkpoint=30,
                            scheduled_ts=end - 30, now=end - 29.9, feed_ok={}, already_selected=False)
    assert d.strike == 100.0 and d.remaining_final_window_sec == 28   # window [end-62, end-3]; spot seen to end-31


def test_book_sampler_prefix_reset_keeps_other_sockets_books():
    from bot.research.price_recorder import BookSampler
    s = BookSampler()
    s.set_tokens({"b": {"slug": "btc-updown-5m-1", "outcome": "Up", "end_ts": 9e9},
                  "e": {"slug": "eth-updown-5m-1", "outcome": "Up", "end_ts": 9e9}})
    for tok in ("b", "e"):
        s.apply({"event_type": "book", "asset_id": tok, "bids": [{"price": "0.4", "size": "1"}],
                 "asks": [{"price": "0.5", "size": "1"}]}, 1.0)
    assert s.tokens("eth-") == ["e"]
    s.reset("eth-")
    assert {r["token_id"] for r in s.sample(2.0)} == {"b"}

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


def test_book_store_partial_flush_never_drops_a_busy_hour(tmp_path):
    from bot.research.price_recorder import HourlyParquetStore, book_schema
    clock = {"t": 1792119600.0}                      # 03:00:00Z
    store = HourlyParquetStore(tmp_path, max_buffer_rows=1000, partial_flush_sec=600, schema=book_schema(),
                               count_key="outcome", sort_cols=("ts_sec", "market_slug", "outcome"),
                               free_disk_provider=lambda p: type("U", (), {"free": 1 << 50})(),
                               clock=lambda: clock["t"])
    for i in range(3600):                            # one row per second for a whole hour
        clock["t"] = 1792119600.0 + i
        store.add({"ts_sec": int(clock["t"]), "market_slug": "btc-updown-5m-1", "outcome": "Up",
                   "token_id": "t", "best_bid": 0.4, "best_ask": 0.5, "bid_size": 1.0, "ask_size": 1.0,
                   "n_updates": 1, "last_update_received_ms": 1, "last_event_ms": 1})
        store.flush()
    store.flush(include_current_hour=True)
    assert store.stats.rows_dropped_buffer_full == 0
    assert store.stats.rows_written == 3600 and store.stats.files_written >= 6


def test_write_hour_file_never_overwrites_an_existing_part(tmp_path, monkeypatch):
    import os
    from bot.research import price_recorder as pr
    rows = [{"source": "s", "ts_sec": 1, "close": 1.0, "high": 1.0, "low": 1.0, "n_updates": 1,
             "last_source_ms": 1, "last_received_ms": 1}]
    first = pr.write_hour_file(tmp_path, "2026-10-17", "03", rows)
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self.name == "03.parquet" else real_exists(self))
    second = pr.write_hour_file(tmp_path, "2026-10-17", "03", rows)   # simulates a racing second process
    assert first.name == "03.parquet" and second.name == "03_2.parquet"
    assert len(list((tmp_path / "2026-10-17").glob("*.parquet"))) == 2


def test_book_stream_prefix_and_feed_names():
    from bot.research.price_recorder import book_feed_name, book_stream_prefix
    assert book_stream_prefix("btc-updown-5m-1792116000") == "btc-updown-5m-"
    assert book_feed_name("btc-updown-15m-") == "polymarket_book"
    assert book_feed_name("eth-updown-5m-") == "polymarket_book_eth_5m"

"""Live executor core must reproduce the recorder-shadow (plan_v2) mechanics exactly."""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.research.twap_z_executor import (
    LiveBookSampler, LiveChainlink, evaluate_checkpoint, CHECKPOINTS, YR3,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reports" / "twap_z_forward_shadow_20261015"))
from recorder_mechanics import ChainlinkSeries  # noqa: E402

START = 1_792_000_800          # multiple of 900
END = START + 900


def _synthetic(seed=1, gaps=(), lag=0.4):
    rng = np.random.default_rng(seed)
    secs = np.arange(START - 400, END + 5)
    px = 100_000 * np.exp(np.cumsum(rng.normal(0, 2e-5, len(secs))))
    rows, live = [], LiveChainlink()
    for s, p in zip(secs, px):
        if int(s) in gaps:
            continue
        rows.append({"source": "chainlink_spot", "close": p, "last_source_ms": s * 1000 + 100,
                     "last_received_ms": (s + lag) * 1000})
        live.observe("spot", p, s + lag, s * 1000 + 100)
    for s in (START, END):
        tw = float(np.mean(px[(secs >= s - 62) & (secs <= s - 3)]))
        rows.append({"source": "chainlink_twap60", "close": tw, "last_source_ms": s * 1000,
                     "last_received_ms": (s + 1.0) * 1000})
        live.observe("twap", tw, s + 1.0, s * 1000)
    return ChainlinkSeries(pd.DataFrame(rows), window_offset=-62), live


@pytest.mark.parametrize("gaps", [(), (END - 50, END - 49), tuple(range(END - 40, END - 35))])
def test_mechanics_match_recorder_series(gaps):
    cs, live = _synthetic(gaps=set(gaps))
    for h in CHECKPOINTS:
        t = END - h + 0.05
        assert live.strike(START, t) == pytest.approx(cs.strike(START, t))
        a = live.required_future_avg(END, cs.strike(START, t), t)
        b = cs.required_future_avg(END, cs.strike(START, t), t)
        assert a[1:] == b[1:]
        assert (a[0] is None) == (b[0] is None)
        if a[0] is not None:
            assert a[0] == pytest.approx(b[0], rel=1e-12)
        assert live.sigma(t) == pytest.approx(cs.sigma(t), rel=1e-9)
        assert live.spot_asof(t)[0] == pytest.approx(cs.spot_asof(t))


def test_strike_not_used_before_received():
    _cs, live = _synthetic()
    assert live.strike(START, START + 0.5) is None
    assert live.strike(START, START + 1.0) is not None


def _books(up=(0.40, 0.42), down=(0.58, 0.60)):
    b = LiveBookSampler()
    b.set_tokens({"U": {"slug": f"btc-updown-15m-{START}", "outcome": "Up", "end_ts": END},
                  "D": {"slug": f"btc-updown-15m-{START}", "outcome": "Down", "end_ts": END}})
    for tok, (bid, ask) in (("U", up), ("D", down)):
        b.apply({"event_type": "book", "asset_id": tok, "bids": [{"price": str(bid), "size": "10"}],
                 "asks": [{"price": str(ask), "size": "7"}]}, END - 100)
    return b


def _eval(live, books, h=30, selected=False, now=None):
    t = END - h
    return evaluate_checkpoint(chainlink=live, books=books, slug=f"btc-updown-15m-{START}", checkpoint=h,
                               scheduled_ts=t, now=now if now is not None else t + 0.01,
                               feed_ok={"polymarket_book": True}, already_selected=selected)


def test_z_formula_and_side_selection():
    _cs, live = _synthetic()
    d = _eval(live, _books())
    assert d.eligible and d.z is not None
    expect = (d.spot - d.required_future_avg_to_flip) / (
        d.spot * d.sigma * math.sqrt(d.remaining_final_window_sec / YR3))
    assert d.z == pytest.approx(expect)
    assert d.side == ("up" if d.z > 0 else "down")
    assert d.mid == pytest.approx(0.41)
    if abs(d.z) >= 2:
        assert d.action == "WOULD_SUBMIT"
        assert d.ask == (0.42 if d.side == "up" else 0.60)
    else:
        assert "Z_BELOW_THRESHOLD" in d.reasons and d.action == "SKIP"


def test_mid_out_of_range_skips():
    _cs, live = _synthetic()
    d = _eval(live, _books(up=(0.95, 0.96), down=(0.04, 0.05)))
    assert "MID_OUT_OF_RANGE" in d.reasons and d.action == "SKIP"


def test_fail_closed_on_stale_feeds_and_already_selected():
    _cs, live = _synthetic()
    books = _books()
    stale = _eval(live, books, h=5, now=END + 30)   # spot/twap far older than 3 s
    assert "SPOT_STALE" in stale.reasons and stale.action in ("SKIP", "RULE_MATCH_FAIL_CLOSED")
    down = evaluate_checkpoint(chainlink=live, books=books, slug=f"btc-updown-15m-{START}", checkpoint=30,
                               scheduled_ts=END - 30, now=END - 29.99, feed_ok={"polymarket_book": False},
                               already_selected=False)
    assert "FEED_DOWN:polymarket_book" in down.reasons and down.action != "WOULD_SUBMIT"
    assert _eval(live, books, selected=True).action == "ALREADY_SELECTED"

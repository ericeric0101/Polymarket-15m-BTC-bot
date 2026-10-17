import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_DIR = Path(__file__).resolve().parents[1] / "reports" / "twap_z_forward_shadow_20261015"
sys.path.insert(0, str(_DIR))
import evaluate_v2 as ev2  # noqa: E402
from recorder_mechanics import ChainlinkSeries, validate_window  # noqa: E402

START = 1_792_027_800  # 2026-10-15T01:30:00Z, inside the v2 population
END = START + 900


def _prices(spot_by_sec: dict[int, float], twap_by_sec: dict[int, float], lag_ms: int = 900) -> pd.DataFrame:
    rows = []
    for s, px in spot_by_sec.items():
        rows.append(("chainlink_spot", s, px, s * 1000, s * 1000 + lag_ms))
    for s, px in twap_by_sec.items():
        rows.append(("chainlink_twap60", s, px, s * 1000, s * 1000 + lag_ms))
    return pd.DataFrame(rows, columns=["source", "ts_sec", "close", "last_source_ms", "last_received_ms"])


def _twap(spot, offset):
    return {s: float(np.mean([spot[k] for k in range(s + offset, s + offset + 60)]))
            for s in spot if all(k in spot for k in range(s + offset, s + offset + 60))}


def test_validate_window_recovers_true_offset():
    rng = np.random.default_rng(1)
    secs = range(START - 400, START)
    spot = dict(zip(secs, 100 + np.cumsum(rng.normal(0, .02, len(secs)))))
    res = validate_window(_prices(spot, _twap(spot, -59)), offsets=range(-61, 0))
    best = res.sort_values("median_abs_bps").iloc[0]
    assert best.window_offset == -59 and best.median_abs_bps < 1e-9


def test_strike_and_required_average_are_as_of_and_fill_small_gaps():
    spot = {s: 100.0 for s in range(START - 120, END)}
    spot.update({s: 101.0 for s in range(END - 59, END - 20)})       # first 39 window seconds at 101
    prices = _prices(spot, {START: 99.5})
    cs = ChainlinkSeries(prices, window_offset=-59)                    # window = [END-59, END]
    assert cs.strike(START, START + 0.5) is None                       # received at START+0.9
    assert cs.strike(START, START + 1.0) == 99.5
    t = END - 20 + 0.95                                                 # last received source second = END-20
    need, rem, missing = cs.required_future_avg(END, 100.0, t)
    observed = 39 * 101.0 + 1 * 100.0                                   # END-59..END-21 at 101, END-20 at 100
    assert rem == 20 and missing == 0
    assert need == pytest.approx((60 * 100.0 - observed) / 20)
    # Two missing observed seconds are forward-filled; four are too many.
    gap = {k: v for k, v in spot.items() if k not in (END - 30, END - 31)}
    need2, _, missing2 = ChainlinkSeries(_prices(gap, {START: 99.5}), window_offset=-59).required_future_avg(END, 100.0, t)
    assert missing2 == 2 and need2 == pytest.approx(need)
    gap4 = {k: v for k, v in spot.items() if k not in range(END - 34, END - 30)}
    assert ChainlinkSeries(_prices(gap4, {}), window_offset=-59).required_future_avg(END, 100.0, t)[0] is None


def test_sigma_annualises_one_second_log_returns():
    rng = np.random.default_rng(7)
    r = rng.normal(0, 1e-4, 400)
    secs = range(START - 400, START)
    spot = dict(zip(secs, 100 * np.exp(np.cumsum(r))))
    cs = ChainlinkSeries(_prices(spot, {}), window_offset=-59)
    sig = cs.sigma(START - 1 + 0.95)
    assert sig == pytest.approx(np.std(r[-300:], ddof=1) * np.sqrt(365 * 86400), rel=0.05)
    assert ChainlinkSeries(_prices(dict(list(spot.items())[:100]), {}), window_offset=-59).sigma(START) is None


def _books(rows, lead_ms=10):
    """Aligned-recorder rows: each row is read `lead_ms` before its second (amendment A1.1)."""
    b = pd.DataFrame(rows, columns=["ts_sec", "market_slug", "outcome", "best_bid", "best_ask"])
    b["sample_ms"] = b.ts_sec * 1000 - lead_ms
    b["last_update_received_ms"] = b.ts_sec * 1000 - 500
    return b


def test_v2_grid_builds_eligible_rows_and_selects_by_chainlink_z(monkeypatch):
    monkeypatch.setattr(ev2, "POPULATION_START", START)
    rng = np.random.default_rng(3)
    secs = range(START - 400, END + 1)
    spot = dict(zip(secs, 100 + np.cumsum(rng.normal(0, .002, len(secs)))))
    spot.update({s: spot[s] + 0.5 for s in range(END - 59, END + 1)})  # final window well above strike
    prices = _prices(spot, {START: 100.0, END: 100.5})
    slug = f"btc-updown-15m-{START}"
    books = _books([(s, slug, o, b, a) for s in range(END - 60, END + 1)
                    for o, b, a in (("Up", .55, .56), ("Down", .44, .45))])
    cs = ChainlinkSeries(prices, window_offset=-59)
    d = ev2.build_grid(prices, books, cs, now_ts=END + 3600)
    assert len(d) == len(ev2.v1.CHECKPOINTS) and d.eligible.all()
    assert (d.zc > 2).all() and d.mid.eq(.555).all()
    g = ev2.v1.select(d, "zc", .10)
    assert len(g) == 1 and g.iloc[0].checkpoint == 55 and g.iloc[0].side == "up" and g.iloc[0].ask == .56
    s = ev2.v1.settle(g, {slug: "UP"})
    s["net_delay1"] = ev2.delayed_net(s, ev2.BookIndex(books), 1)
    assert s.net_delay1.iloc[0] == pytest.approx(1 - .56 - .07 * .56 * .44)
    assert ev2.label_agreement(d, cs, {slug: "UP"}) == {"markets": 1, "agree": 1, "rate": 1.0}


def test_book_index_rejects_stale_rows():
    slug = f"btc-updown-15m-{START}"
    bx = ev2.BookIndex(_books([(END - 30, slug, "Up", .5, .51)]))
    assert bx.asof(slug, "Up", END - 28) is not None
    assert bx.asof(slug, "Up", END - 26) is None              # older than 3 s
    assert bx.asof(slug, "Down", END - 28) is None
    assert bx.asof(slug, "Up", END - 26, not_before=END - 31) is not None


def test_lock_policy_finds_first_fully_observed_second():
    # Window for END is [END-62, END-3]; spot received 0.9 s after each source second,
    # so the last window second (END-3) is known from END-2.1 -> first lock check at T-2.
    spot = {s: 100.0 for s in range(START - 100, END + 1)}
    spot.update({s: 100.2 for s in range(END - 62, END - 2)})          # settles 20 bps above strike
    prices = _prices(spot, {START: 100.0})
    slug = f"btc-updown-15m-{START}"
    books = _books([(s, slug, o, b, a) for s in range(END - 20, END + 1)
                    for o, b, a in (("Up", .97, .98), ("Down", .02, .03))])
    cs = ChainlinkSeries(prices, window_offset=ev2.WINDOW_OFFSET)
    r = ev2.lock_policy([slug], cs, ev2.BookIndex(books), {slug: "UP"})
    (row,) = r.to_dict("records")
    assert row["seconds_before_end"] == 2 and row["side"] == "Up"
    assert row["margin_bps"] == pytest.approx(20.0) and row["eligible"] and row["win"] == 1
    assert row["net"] == pytest.approx(1 - .98 - .07 * .98 * .02)


def test_book_index_is_causal_amendment_a1():
    slug = f"btc-updown-15m-{START}"
    t = END - 30
    legacy = pd.DataFrame([(t - 1, slug, "Up", .40, .41, None, (t - 1) * 1000 - 200),
                           (t, slug, "Up", .50, .51, None, t * 1000 + 400)],   # changed 0.4 s AFTER t
                          columns=["ts_sec", "market_slug", "outcome", "best_bid", "best_ask",
                                   "sample_ms", "last_update_received_ms"])
    assert ev2.BookIndex(legacy).asof(slug, "Up", t).best_ask == .41      # falls back to the causal row
    aligned = legacy.assign(sample_ms=[(t - 1) * 1000 - 10, t * 1000 - 10])
    assert ev2.BookIndex(aligned).asof(slug, "Up", t).best_ask == .51     # read 10 ms before t: usable

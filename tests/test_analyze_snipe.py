"""Stale-quote analysis on a synthetic ms_recorder tape: replay, jumps, stale time, snipes, capture."""
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "analyze_snipe", Path(__file__).resolve().parents[1] / "reports/latency_snipe_20261017/analyze_snipe.py")
an = importlib.util.module_from_spec(spec)
spec.loader.exec_module(an)

START = 1792249200                     # market start (s); end = +300
T = (START + 100) * 1000.0             # jump time (ms)
UP, DOWN = "tu", "td"


def _write(d: Path, rows):
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "20261017T15.jsonl.gz", "wt") as fh:
        for t_ms, obj in rows:
            fh.write(f"{int(t_ms * 1e6)}\t{json.dumps(obj)}\n")


def _bt(t, mid):
    return t, {"stream": "btcusdt@bookTicker", "data": {"b": str(mid - 0.5), "a": str(mid + 0.5)}}


def _tape(tmp_path: Path, snipe_at=None, requote_at=T + 300.0):
    (tmp_path / "markets.jsonl").write_text(json.dumps(
        {"slug": f"btc-updown-5m-{START}", "asset": "btc", "duration": 300, "start_ts": START,
         "end_ts": START + 300, "tokens": {UP: "Up", DOWN: "Down"}}) + "\n")
    _write(tmp_path / "binance", [_bt(T - 2000, 100000.0), _bt(T - 500, 100000.0), _bt(T, 100050.0),  # +5 bps
                                  _bt(T + 100, 100050.0)])
    pm = [(T - 10000, [{"event_type": "book", "asset_id": UP, "bids": [{"price": "0.50", "size": "100"}],
                        "asks": [{"price": "0.52", "size": "20"}], "timestamp": str(int(T - 10010))},
                       {"event_type": "book", "asset_id": DOWN, "bids": [{"price": "0.48", "size": "100"}],
                        "asks": [{"price": "0.50", "size": "100"}], "timestamp": str(int(T - 10010))}])]
    if snipe_at is not None:
        pm.append((snipe_at, {"event_type": "last_trade_price", "asset_id": UP, "price": "0.52", "size": "20",
                              "side": "BUY", "timestamp": str(int(snipe_at - 10))}))
    pm.append((requote_at, {"event_type": "price_change", "timestamp": str(int(requote_at - 10)), "price_changes": [
        {"asset_id": UP, "price": "0.52", "size": "0", "side": "SELL"},
        {"asset_id": UP, "price": "0.58", "size": "50", "side": "SELL"},
        {"asset_id": UP, "price": "0.56", "size": "50", "side": "BUY"}]}))
    _write(tmp_path / "pm_btc_5m", pm)
    meta = an.load_markets(tmp_path)
    bn, _ = an.load_binance(tmp_path)
    best, trades, lat = an.replay_pm(tmp_path, meta)
    return meta, bn, best, trades, lat


def test_jump_detection_and_refractory():
    import pandas as pd
    df = pd.DataFrame({"t": [0, 500, 1000, 1500, 5000], "mid": [100.0, 100.0, 100.05, 100.1, 100.2]})
    jumps = an.find_jumps(df, 3.0, w_ms=1000, refractory_ms=3000)
    assert [j[0] for j in jumps] == [1000.0, 5000.0] and jumps[0][1] == 1


def test_stale_time_capture_and_markout(tmp_path):
    meta, bn, best, trades, lat = _tape(tmp_path)
    d_pm = sorted(lat)[len(lat) // 2]
    assert d_pm == pytest.approx(10.0)
    ev = an.evaluate({"btc": an.find_jumps(bn["btc"], 3.0)}, best, trades, meta, d_pm)
    (r,) = ev.to_dict("records")
    assert r["outcome"] == "Up" and r["a0"] == 0.52 and r["q0"] == 20
    assert r["stale_ms"] == pytest.approx(300.0) and not r["pm_led"] and r["n_snipes"] == 0
    # level removed at engine time T+290: arrive at +100 captured, +370 not
    assert r["cap_100"] and r["cap_200"] and not r["cap_370"]
    fee = 0.07 * 0.52 * 0.48
    assert r["net_5s"] == pytest.approx(0.57 - 0.52 - fee)


def test_faster_sniper_takes_the_level(tmp_path):
    meta, bn, best, trades, lat = _tape(tmp_path, snipe_at=T + 40.0)
    ev = an.evaluate({"btc": an.find_jumps(bn["btc"], 3.0)}, best, trades, meta, 10.0)
    (r,) = ev.to_dict("records")
    assert r["first_snipe_ms"] == pytest.approx(40.0)
    assert r["cap_10"] and not r["cap_50"]          # sniper at engine T+30 ate all 20 shares


def test_pm_moving_first_is_flagged(tmp_path):
    meta, bn, best, trades, lat = _tape(tmp_path, requote_at=T - 200.0)
    ev = an.evaluate({"btc": an.find_jumps(bn["btc"], 3.0)}, best, trades, meta, 10.0)
    (r,) = ev.to_dict("records")
    assert r["pm_led"] and r["a0"] == 0.58

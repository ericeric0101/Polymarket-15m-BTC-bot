import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "reports" / "twap_z_forward_shadow_20261015" / "evaluate.py"
_spec = importlib.util.spec_from_file_location("twap_z_forward_shadow_evaluate", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

START = 1_792_000_800.0  # a 15-minute boundary after the population start
SLUG = f"btc-updown-15m-{int(START)}"


def _paths(rows):
    base = dict(market_slug=SLUG, run_id="r", market_start_ts=START, snapshot_trigger="periodic", official_twap=100.0,
                strike=100.0, twap_fresh=True, twap_age_sec=1.0, btc_spot=100.2, btc_fresh=True,
                market_mid_fresh=True, sigma_ex_market=0.3, sigma_ex_market_fresh=True,
                required_move_mode="EXACT_FINAL_WINDOW_BOUNDARY", remaining_final_window_sec=20.0, time_left_sec=20.0,
                best_bid_down=np.nan, best_ask_down=np.nan)
    x = pd.DataFrame([{**base, **r} for r in rows])
    x["priority"] = 1
    x["start"] = START
    return x


def _rec(rows):
    r = pd.DataFrame(rows, columns=["source", "ts_sec", "close", "last_received_ms"])
    r["t"] = r.last_received_ms / 1000.0
    return r.sort_values("t")


def test_zc_uses_chainlink_asof_without_lookahead_and_selects_first_row():
    t45, t40 = START + 855, START + 860
    x = _paths([
        dict(snapshot_ts=t45 - 1, best_bid_up=.30, best_ask_up=.31, required_future_avg_to_flip=99.99),
        dict(snapshot_ts=t40 - 1, best_bid_up=.40, best_ask_up=.41, required_future_avg_to_flip=99.99),
    ])
    sd = 100.0 * .3 * np.sqrt(20 / ev.YR3)
    rec = _rec([
        ("chainlink_spot", int(t45) - 1, 99.99 + 3 * sd, (t45 - 1) * 1000),  # z = +3 at T-45
        ("chainlink_spot", int(t45) + 1, 50.0, (t45 + 0.5) * 1000),        # after T-45: must not be used there
    ])
    d = ev.add_z(ev.build_grid(x), rec)
    row45 = d[d.checkpoint.eq(45)].iloc[0]
    spot = 99.99 + 3 * sd
    assert row45.zc == pytest.approx((spot - 99.99) / (spot * .3 * np.sqrt(20 / ev.YR3)), rel=1e-9)
    assert row45.zc > 2.99
    g = ev.select(d, "zc", .10)
    assert len(g) == 1 and g.iloc[0].checkpoint == 45 and g.iloc[0].side == "up" and g.iloc[0].ask == .31


def test_rolling_basis_needs_30_prior_pairs_and_uses_median():
    rows = []
    for i in range(40):
        ts = int(START) + i
        rows += [("binance_btcusdt", ts, 100.1, ts * 1000 + 100), ("chainlink_spot", ts, 100.0, ts * 1000 + 200)]
    rec = _rec(rows)
    basis, cnt = ev.rolling_basis(rec, np.array([START + 20.5, START + 40.5]))
    assert np.isnan(basis[0]) and cnt[0] == 21  # pairs ts 0..20 completed before t
    assert basis[1] == pytest.approx(10.0) and cnt[1] == 40


def test_settle_and_verdict_rules():
    g = pd.DataFrame({"market_slug": [f"m{i}" for i in range(4)], "side": ["up", "down", "up", "down"],
                      "ask": [.6, .6, .6, .6], "start": [START, START, START + 86400, START + 86400]})
    labels = {"m0": "UP", "m1": "DOWN", "m2": "UP", "m3": "UNRESOLVED"}
    s = ev.settle(g, labels)
    assert list(s.market_slug) == ["m0", "m1", "m2"] and s.win.tolist() == [1, 1, 1]
    s["net_delay1"] = s.net
    v = ev.verdict(s)
    assert v["n"] == 3 and v["mean_net"] > 0
    # 2 of 3 markets on one day -> day concentration check fails -> not PASS
    assert v["checks"]["no_day_over_50pct"] is False and v["verdict"] == "INCONCLUSIVE"
    s["win"], s["net"] = 0, -0.6
    assert ev.verdict(s)["verdict"] == "FAIL"


def test_stopping_rule_withholds_pnl(tmp_path, monkeypatch):
    exports = tmp_path / "exp"
    (exports / "manifests").mkdir(parents=True)
    day = pd.to_datetime(START, unit="s", utc=True).date().isoformat()
    (exports / "manifests" / f"P_{day}.json").write_text(json.dumps({"date_utc": day, "complete_day": True}))
    (exports / "P_paths" / day).mkdir(parents=True)
    x = _paths([dict(snapshot_ts=START + 854, best_bid_up=.30, best_ask_up=.31, required_future_avg_to_flip=99.0)])
    x.drop(columns=["priority", "start"]).to_parquet(exports / "P_paths" / day / "paths_native_v2.parquet")
    monkeypatch.setattr(ev, "O", tmp_path / "report")
    monkeypatch.setattr(ev, "POPULATION_START", START - 60)
    ev.main(["--exports-root", str(exports), "--recorder-root", str(tmp_path / "rec")])
    run = next((tmp_path / "report" / "runs").iterdir())
    status = json.loads((run / "status.json").read_text())
    assert status["analysis_allowed"] is False and status["status"].startswith("COVERAGE_ONLY")
    assert not (run / "candidates.csv").exists() and not (run / "verdict.json").exists()

import importlib.util
import json
import pathlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sports_odds_analyze.py"
_spec = importlib.util.spec_from_file_location("sports_odds_analyze", _PATH)
an = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(an)

T0 = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
KICK = T0 + timedelta(days=3)


def _row(scan, token="t1", fair=0.50, bid=0.47, ask=0.48, kind="baseline", event="e1", **kw):
    r = {"scan_utc": scan.isoformat(), "kind": kind, "league": "nhl", "event_id": event, "event": "A vs. B",
         "slug": "s", "start_utc": KICK.isoformat(), "hours_to_start": 0, "token": "A", "token_id": token,
         "market_id": "m1", "book_outcome": "A", "side": "outcome", "fair": fair, "bid": bid, "ask": ask,
         "mid": (bid + ask) / 2, "ask_size": 100, "fee_rate": 0.0, "fee_exponent": 1.0,
         "accepting_orders": True, "has_book": True, "taker_edge": 0, "maker_edge": 0, "plus_ev_shares": 0,
         "plus_ev_usdc": 50.0, "fair_minus_mid": 0, "suspect": False, "match_level": "exact",
         "home_away_consistent": True, "overround": 1.03, "odds_age_min": 1, "odds_stale": False,
         "pm_volume": 0, "book_event_id": "b1", "raw_odds": "{}"}
    r.update(kw)
    return r


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(an, "OUT_ROOT", tmp_path)

    def write(rows, name="000000Z_tokens.csv"):
        d = tmp_path / "scans" / "2026-10-20"
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(d / name, index=False)
    return write


def test_entry_is_first_qualifying_baseline_and_close_is_last_in_window(store):
    rows = [
        _row(KICK - timedelta(hours=50), fair=0.50, ask=0.495),                  # edge 0.005 < theta
        _row(KICK - timedelta(hours=30), fair=0.52, ask=0.50, bid=0.49),         # entry: edge 0.02
        _row(KICK - timedelta(hours=10), fair=0.56, ask=0.50, bid=0.49),         # later, ignored
        _row(KICK - timedelta(minutes=25), fair=0.53, bid=0.52, ask=0.53, kind="closing"),
        _row(KICK - timedelta(minutes=5), fair=0.54, bid=0.53, ask=0.54, kind="closing"),   # close
        _row(KICK - timedelta(seconds=30), fair=0.90, bid=0.89, ask=0.90, kind="closing"),  # < 1 min
    ]
    store(rows)
    df = an.load_rows(T0, T0 + timedelta(days=14))
    ent = an.entries(df, 0.01)
    assert len(ent) == 1 and ent.iloc[0]["bucket"] == "B" and ent.iloc[0]["ask"] == 0.50
    close = an.closing_rows(df)
    assert close.loc["t1", "fair"] == 0.54
    t = an.clv_table(df, close, 0.01, an.fee_main)
    assert t["frame"]["clv"].iloc[0] == pytest.approx(0.54 - 0.50)
    assert t["frame"]["rt"].iloc[0] == pytest.approx(0.53 - 0.50)


def test_invalid_rows_and_out_of_range_events_are_excluded(store):
    early = _row(T0 + timedelta(hours=1), token="t2")
    early["start_utc"] = (T0 + timedelta(hours=12)).isoformat()          # starts before T0 + 1d
    store([_row(KICK - timedelta(hours=30), suspect=True, fair=0.6, ask=0.5), early,
           _row(KICK - timedelta(hours=30), token="t3", fair=0.6, ask=0.5, accepting_orders=False)])
    df = an.load_rows(T0, T0 + timedelta(days=14))
    assert set(df["token_id"]) == {"t1", "t3"} and not df["valid"].any()
    assert len(an.entries(df, 0.01)) == 0


def test_fees():
    assert an.fee_main(0.5, 0.03, 1.0) == pytest.approx(0.0075)
    assert an.fee_alt(0.5, 0.03, 1.0) == pytest.approx(0.00375)


def test_cluster_bootstrap_resamples_events():
    vals = np.array([1.0, 1.0, 1.0, -1.0])
    m, lo, hi = an.cluster_bootstrap(vals, np.array(["a", "a", "a", "b"]), n=2000)
    assert m == pytest.approx(0.5)
    assert lo == pytest.approx(-1.0) and hi == pytest.approx(1.0)   # only 2 clusters


def test_cluster_logit_recovers_coefficients():
    rng = np.random.default_rng(0)
    n = 4000
    x = rng.normal(size=n)
    y = (rng.random(n) < 1 / (1 + np.exp(-(0.3 + 1.5 * x)))).astype(float)
    beta, se = an.cluster_logit(y, np.column_stack([np.ones(n), x]), np.arange(n) // 2)
    assert beta[1] == pytest.approx(1.5, abs=0.15) and se[1] < 0.1


def test_blind_mode_does_not_read_resolutions(store, monkeypatch, capsys):
    store([_row(KICK - timedelta(hours=30), fair=0.52, ask=0.50)])
    (an.OUT_ROOT / "resolutions.jsonl").write_text(json.dumps({"market_id": "m1"}) + "\n")
    monkeypatch.setattr(an, "load_resolutions", lambda: (_ for _ in ()).throw(AssertionError("read")))
    monkeypatch.setattr(an, "closing_rows", lambda df: (_ for _ in ()).throw(AssertionError("read")))
    assert an.main(["--t0", T0.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "H2 entries (theta=0.01): 1" in out and "blind" in out


def test_unblind_report_runs_end_to_end(store, capsys):
    rows = []
    for i in range(30):
        ev, tok = f"e{i}", f"t{i}"
        rows += [_row(KICK - timedelta(hours=12), token=tok, event=ev, fair=0.52, ask=0.50, bid=0.49),
                 _row(KICK - timedelta(minutes=10), token=tok, event=ev, fair=0.53, bid=0.51, ask=0.52,
                      kind="closing")]
    store(rows)
    res = [{"market_id": f"m{i}", "closed": True, "token_values": {f"t{i}": float(i % 2)}} for i in range(30)]
    for r, i in zip(rows[::2], range(30)):
        r["market_id"] = f"m{i}"
    store(rows)
    (an.OUT_ROOT / "resolutions.jsonl").write_text("\n".join(json.dumps(r) for r in res) + "\n")
    assert an.main(["--t0", T0.isoformat(), "--unblind"]) == 0
    out = capsys.readouterr().out
    assert "H2 CLV per share: +0.0300" in out and "PASS" in out
    assert "H1 rows: 30" in out and "hold to settlement: n=30" in out

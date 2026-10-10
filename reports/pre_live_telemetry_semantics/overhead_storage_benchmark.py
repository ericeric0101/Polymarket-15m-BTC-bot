"""Overhead + storage measurement for snapshot schema v2 (read-only; no DB writes)."""
import json, sqlite3, statistics, sys, time
from collections import deque
from decimal import Decimal
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import bot.prediction_research_snapshot as m
from bot.stop_timing_telemetry import StopTimingTelemetry

ROOT = sys.argv[1]
RUN = "run_1791602735_398a83d0"
r = sqlite3.connect(f"file:{ROOT}/data/research/twap_forward_shadow.db?mode=ro&immutable=1", uri=True)
sizes = [len(p) for (p,) in r.execute("select payload_json from lead_lag_decisions where run_id=? and payload_json like '%PREDICTION_RESEARCH_SNAPSHOT%'", (RUN,))]
real = json.loads(r.execute("select payload_json from lead_lag_decisions where run_id=? and slug='btc-updown-15m-1791606600' and payload_json like '%PREDICTION_RESEARCH_SNAPSHOT%' limit 1 offset 300", (RUN,)).fetchone()[0])

now = 1791606900.1234567
class DB:
    def enqueue_decision(self, **k): return True
def strat(held):
    inv = {"up": {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.67"), "entry_bid_at_fill": Decimal("0.66"),
                  "entry_ask_at_fill": Decimal("0.67"), "entry_quote_age_at_fill_sec": 0.41234567,
                  "entry_bid_size_at_fill": Decimal("120.5")}} if held else {}
    return SimpleNamespace(
        current_market_slug="btc-updown-15m-1791606600", current_market_end_timestamp=1791607500,
        market_start_ts_by_slug={}, market_strike_cache_by_slug={"btc-updown-15m-1791606600": 121034.56789012},
        _polymarket_chainlink_twap_price=121040.12345678, _polymarket_chainlink_twap_observation_ts=now - 0.81,
        _polymarket_chainlink_twap_price_ts=now - 0.43217897,
        _polymarket_chainlink_price=121041.98765432, _polymarket_chainlink_price_ts=now - 0.31234567,
        _polymarket_chainlink_price_observation_ts=now - 1.0,
        _binance_ws_price_source_ts=now - 0.2, _binance_ws_price_ts=now - 0.15,
        _prediction_btc_research_history=deque([(now - 70 + i * 0.5, 121150.12 + i * 0.01, now - 70 + i * 0.5 + 0.05) for i in range(140)]),
        _RAW_SPOT_FRESHNESS_SEC=10.0, quote_max_delivery_delay_sec=2.0,
        last_quote_source_ts_by_inst={"up": now - 0.4, "down": now - 0.4},
        last_quote_received_ts_by_inst={"up": now - 0.35, "down": now - 0.35},
        last_quote_update_ts_by_inst={"up": now - 0.35, "down": now - 0.35},
        latest_quote_by_inst={"up": (Decimal("0.62"), Decimal("0.63")), "down": (Decimal("0.37"), Decimal("0.38"))},
        latest_quote_depth_by_inst={"up": (Decimal("120.5"), Decimal("40")), "down": (Decimal("88"), Decimal("12"))},
        live_inventory_cost=inv, _research_market_quote_instruments=lambda **_: ("up", "down"),
        _settlement_probability_shadow_inputs=lambda **_: {"path_spot_source": "binance_ws", "p_up_ex_market": .7,
            "sigma_ex_market_fresh": True, "sigma_ex_market_age_sec": .2, "required_move_sigma": 0.81234567})

def run(held, legacy, n=4000):
    orig_f, orig_c = m._early_warning_fields, m.PredictionResearchSnapshotter.__dict__["_early_warning_context"]
    if legacy:
        m._early_warning_fields = lambda *a, **k: {}
        m.PredictionResearchSnapshotter._early_warning_context = staticmethod(lambda *a, **k: {})
    try:
        snap = m.PredictionResearchSnapshotter(db=DB(), run_id=RUN)
        s = strat(held); out = []; row = None
        for i in range(n):
            t0 = time.perf_counter_ns()
            row = snap.capture(s, now_ts=now + i * 1e-6, trigger="entry_decision", force=True)
            out.append(time.perf_counter_ns() - t0)
        return out, row
    finally:
        m._early_warning_fields, m.PredictionResearchSnapshotter._early_warning_context = orig_f, orig_c

def pct(xs, q): xs = sorted(xs); return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]
res = {}
for held in (False, True):
    # interleave to reduce drift
    leg, new = [], []
    for _ in range(5):
        a, rl = run(held, True, 2000); b, rn = run(held, False, 2000); leg += a; new += b
    added_keys = {k: v for k, v in rn.items() if k not in rl}
    research_version = {"research_schema_version": 1}
    bytes_added = len(json.dumps({**rn, **research_version}, ensure_ascii=False)) - len(json.dumps({**rl, **research_version}, ensure_ascii=False))
    res["held" if held else "flat"] = {
        "legacy_capture_us_p50": pct(leg, .5) / 1000, "legacy_capture_us_p99": pct(leg, .99) / 1000,
        "v2_capture_us_p50": pct(new, .5) / 1000, "v2_capture_us_p99": pct(new, .99) / 1000,
        "added_us_p50": (pct(new, .5) - pct(leg, .5)) / 1000, "added_us_p99": (pct(new, .99) - pct(leg, .99)) / 1000,
        "bytes_added": bytes_added, "added_keys": sorted(added_keys)}
# STOP_TIMING observe cost (transition rows only, per protective evaluation)
sys.path.insert(0, f"{ROOT}/tests")
from test_stop_timing_early_warning_fields import _obs as st_obs
from test_stop_timing_telemetry import END
tel = StopTimingTelemetry(emit=lambda *a: True, run_id="r")
xs = []
for i in range(3000):
    t0 = time.perf_counter_ns(); st_obs(tel, END - 590 + i * 0.01); xs.append(time.perf_counter_ns() - t0)
res["stop_timing_observe_us_p50"] = pct(xs, .5) / 1000
res["stop_timing_observe_us_p99"] = pct(xs, .99) / 1000
res["dry_run_snapshot_rows"] = len(sizes)
res["dry_run_payload_bytes_mean"] = statistics.mean(sizes)
res["dry_run_payload_bytes_p50"] = statistics.median(sizes)
print(json.dumps(res, indent=1))

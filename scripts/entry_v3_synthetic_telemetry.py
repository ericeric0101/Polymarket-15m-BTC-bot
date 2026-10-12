"""Synthetic candidate telemetry produced by the REAL recorder (tool validation only; not research data).

Usage: python -I scripts/entry_v3_synthetic_telemetry.py <out_dir> [n_markets] [seed]
Writes candidate JSONL + synthetic_outcomes.json.  Nothing here is LIVE data.
"""
from __future__ import annotations

import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot.candidate_telemetry as ct  # noqa: E402


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _FakeStrategy:
    def __init__(self, slug, start):
        self.run_id = "run_synthetic"
        self.runtime_git_revision = "synthetic"
        self.current_market_slug = slug
        self.current_market_end_timestamp = start + 900
        self.latest_quote_by_inst = {}
        self.last_quote_update_ts_by_inst = {}
        self.latest_quote_depth_by_inst = {}
        self.side_decision_score = Decimal("0.3")
        self.market_strike_cache_by_slug = {slug: 80000.0}
        self.latest_external_spot = 80010.0
        self.strike_finality = None
        self.absolute_max_loss_enabled = False
        self.prediction_research_snapshotter = SimpleNamespace(_last_payload_by_slug={})

    def _instrument_key(self, inst): return str(inst)
    def _normalize_instrument_id(self, inst): return inst
    def _side_for_instrument_id(self, inst): return SimpleNamespace(value=str(inst).upper())
    def _extract_token_id_from_instrument(self, inst): return f"tok-{inst}"


def generate(out_dir, n_markets=40, seed=7):
    out_dir = Path(out_dir)
    rng = random.Random(seed)
    sink = ct.JsonlSink(out_dir, threaded=False, disk_free_fn=lambda p: 10 ** 15)
    outcomes = {}
    t0 = 1_800_000_000.0
    first = None
    rec = None
    for i in range(n_markets):
        start = t0 + i * 900
        slug = f"btc-updown-15m-{int(start)}"
        s = _FakeStrategy(slug, start)
        clock = _Clock(start + 300)
        if rec is None:
            rec = ct.CandidateTelemetryRecorder(sink=sink, run_header=ct.build_run_header(s, {"SYNTHETIC": True}),
                                                fqo_enabled=True, markout_enabled=True, clock=clock, mono=lambda: 0)
            first = clock
        rec.clock = clock
        side = rng.choice(["up", "down"])
        ask = round(rng.uniform(0.60, 0.85), 2)
        s.latest_quote_by_inst[side] = (round(ask - 0.01, 2), ask)
        s.latest_quote_depth_by_inst[side] = (50.0, 50.0)
        s.last_quote_update_ts_by_inst[side] = clock.t
        s.prediction_research_snapshotter._last_payload_by_slug[slug] = {
            "snapshot_ts": clock.t - 0.4, "p_up_ex_market": rng.uniform(0.3, 0.7), "p_down_ex_market": None,
            "required_move_sigma": 0.3}
        snap = s.prediction_research_snapshotter._last_payload_by_slug[slug]
        snap["p_down_ex_market"] = 1 - snap["p_up_ex_market"]
        kw = dict(side="BUY", qty=5.5, payload={"instrument_id": side, "p_fair": ask - 0.005})
        if rng.random() < 0.3:
            rec.observe_order_event(s, event_type="ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW", price=ask - 0.01,
                                    client_order_id=None, reason="first_entry_too_early", **kw)
        accepted = rng.random() < 0.8
        q = round(ask - 0.01, 2)
        if accepted:
            rec.observe_order_event(s, event_type="ORDER_SUBMIT", price=q, client_order_id=f"c{i}a", reason=None, **kw)
            filled = False
            requoted = rng.random() < 0.5
            for k in range(60):
                clock.t += 1
                ask = round(min(0.97, max(0.02, ask + rng.choice([-0.01, 0, 0, 0.01]))), 2)
                s.latest_quote_by_inst[side] = (round(ask - 0.01, 2), ask)
                s.last_quote_update_ts_by_inst[side] = clock.t
                if requoted and k == 5 and not filled:
                    rec.observe_order_event(s, event_type="ORDER_SUBMIT", price=q + 0.02, client_order_id=f"c{i}b",
                                            reason=None, **kw)
                rec.sample(s)
                if not filled and ask <= q - 0.01 and rng.random() < 0.7:
                    filled = True
                    coid = f"c{i}b" if requoted and k > 5 else f"c{i}a"
                    rec.observe_order_event(s, event_type="ORDER_FILLED", side="BUY", price=q, qty=5.5,
                                            client_order_id=coid, reason=None, payload={})
            rec.close_market(slug, clock.t)
        else:
            rec.observe_order_event(s, event_type="ORDER_OBSERVE_BUY_BLOCKED", price=q, client_order_id=None,
                                    reason="reduce_only", **kw)
        outcomes[slug] = rng.choice(["UP", "DOWN"])
    sink.flush()
    jsonl = sorted(out_dir.glob("candidate_v1_*.jsonl"))[0]
    (out_dir / "synthetic_outcomes.json").write_text(json.dumps(outcomes, indent=1))
    return jsonl, outcomes


if __name__ == "__main__":
    j, o = generate(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40, int(sys.argv[3]) if len(sys.argv) > 3 else 7)
    print(j, len(o))

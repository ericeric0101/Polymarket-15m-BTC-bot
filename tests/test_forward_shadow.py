from bot.forward_shadow import ForwardShadowExperiment


class ResearchDB:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def enqueue_decision(self, **row):
        if self.fail:
            raise RuntimeError("research queue unavailable")
        self.events.append(row["payload"])


def quote(
    exp,
    *,
    now,
    spot=100070,
    strike=100000,
    bid=.49,
    ask=.50,
    bid_size=100,
    ask_size=20,
    asks=None,
    side="UP",
    signal_score=.3,
    fair_probability=.6,
    spot_leader=None,
):
    if spot_leader == "DOWN":
        spot = strike - 100
    elif spot_leader == "UP":
        spot = strike + 100
    return exp.on_quote(
        slug="btc-updown-15m-1000000000", start_ts=1_000_000_000,
        end_ts=1_000_000_900, now_ts=now, instrument_id="up-token" if side == "UP" else "down-token",
        side=side, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        bid_levels=[(bid, bid_size), (max(.01, bid-.01), 100)],
        ask_levels=asks or [(ask, 2), (ask + .01, 10)],
        reference_spot=spot, reference_ts=now, reference_source="polymarket_chainlink_twap_60s_ws",
        strike=strike, strike_ts=1_000_000_000 if strike is not None else None,
        strike_source="canonical_open" if strike is not None else "",
        signal_inputs={"composite_score": signal_score, "btc_trend": .2, "confidence": .3},
        signal_ts=now, fair_probability=fair_probability,
        economics={"gross_probability_edge_ps": .1, "net_directional_edge_ps": None,
                   "robust_net_usdc": None, "fee_estimate": None, "execution_penalty": None},
        live_snapshot={"instrument_for_side": {"UP": "up-token", "DOWN": "down-token"},
                       "production_side": "UP"},
    )


def events(db, kind):
    return [e for e in db.events if e.get("event_type") == kind]


def test_120_second_candidates_thresholds_stable_identity_and_config_overlap():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    assert quote(exp, now=1_000_000_119.9) == 0
    assert quote(exp, now=1_000_000_120, spot=100050) == 3  # strict >5: 120/5 is NONE
    assert quote(exp, now=1_000_000_121, spot=100070) == 0
    rows = events(db, "SHADOW_ENTRY_CANDIDATE")
    assert [r["entry_config"] for r in rows] == ["120_0", "120_2", "120_5"]
    assert [r["candidate_side"] for r in rows] == ["UP", "UP", None]
    assert rows[0]["candidate_id"] == "btc-updown-15m-1000000000|120_0"
    assert rows[0]["signal_source"] == "canonical_chainlink_twap_vs_official_market_strike"
    assert rows[0]["live_comparator"]["production_side"] == "UP"


def test_canonical_inputs_can_arrive_during_bounded_capture_grace():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test", canonical_wait_sec=15)

    assert quote(exp, now=1_000_000_120, spot=None, strike=None) == 0
    assert events(db, "SHADOW_ENTRY_CANDIDATE") == []
    assert exp._attempted == set()

    assert quote(exp, now=1_000_000_123, spot=100070, strike=100000) == 3
    rows = events(db, "SHADOW_ENTRY_CANDIDATE")
    assert len(rows) == 3
    assert all(row["signal_status"] != "observation_unavailable" for row in rows)
    assert all(row["schedule_lateness_sec"] == 3 for row in rows)


def test_canonical_inputs_timeout_records_unavailable_once():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test", canonical_wait_sec=15)

    assert quote(exp, now=1_000_000_134, spot=None, strike=None) == 0
    assert events(db, "SHADOW_ENTRY_CANDIDATE") == []
    assert quote(exp, now=1_000_000_136, spot=None, strike=None) == 3
    assert quote(exp, now=1_000_000_140, spot=100070, strike=100000) == 0

    rows = events(db, "SHADOW_ENTRY_CANDIDATE")
    assert len(rows) == 3
    assert all(row["signal_status"] == "observation_unavailable" for row in rows)
    assert all(row["canonical_wait_timed_out"] is True for row in rows)


def test_exact_zero_is_none_and_weekend_is_shadow_only():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120, spot=100000)
    rows = events(db, "SHADOW_ENTRY_CANDIDATE")
    assert len(rows) == 3
    assert all(row["candidate_side"] is None for row in rows)
    assert all(row["exact_zero_tie"] for row in rows)
    assert all(row["experiment_class"] == "WEEKEND_SHADOW_ONLY" for row in rows)


def test_top_ask_and_depth_weighted_entry_and_insufficient_top_depth():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120, ask_size=2, asks=[(.50, 2), (.51, 10)])
    entry = next(row for row in events(db, "SHADOW_ENTRY_CANDIDATE") if row["entry_config"] == "120_0")
    assert entry["entry_top_ask"] == .5
    assert entry["research_shares"] == 10
    assert entry["top_level_fillable"] is False
    assert entry["depth_weighted_entry_status"] == "filled"
    assert entry["depth_weighted_entry_price"] == .508
    assert {s["entry_variant"] for s in exp._candidates.values()} == {"ENTRY_DEPTH_WEIGHTED_5USD"}

    other_db = ResearchDB()
    other = ForwardShadowExperiment(db=other_db, run_id="other")
    quote(other, now=1_000_000_120, ask_size=2, asks=[(.50, 2), (.51, 3)])
    assert not other._candidates  # neither the top nor complete $5 depth is fillable
    candidate = next(row for row in events(other_db, "SHADOW_ENTRY_CANDIDATE") if row["entry_config"] == "120_0")
    assert candidate["top_level_fillable"] is False
    assert candidate["depth_weighted_entry_status"] == "insufficient_depth"


def test_bid_marks_mfe_mae_tp20_trails_and_recovery_are_observational():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120)
    quote(exp, now=1_000_000_122, bid=.60, ask=.61)
    quote(exp, now=1_000_000_124, bid=.70, ask=.71)
    quote(exp, now=1_000_000_126, bid=.67, ask=.68)
    quote(exp, now=1_000_000_128, bid=.64, ask=.65)
    exit_rows = events(db, "SHADOW_EXIT")
    top = [row for row in exit_rows if row["entry_variant"] == "ENTRY_TOP_ASK"]
    assert {row["exit_policy"] for row in top} == {"TP20", "TRAIL5", "TRAIL10", "COMBINED180", "COMBINED300"}
    assert all(any(abs(row["exit_best_bid"] - price) < 1e-9 for price in (.60, .67, .70, .64)) for row in top)
    marks = events(db, "SHADOW_POSITION_MARK")
    assert all("mark_return_pct" in row and row["mark_pnl_usdc"] == row["mark_return_pct"] * 5 for row in marks)
    assert any(row["mfe_bid_pct"] > .3 and row["mae_bid_pct"] < 0 for row in marks)
    exp.on_settlement(slug="btc-updown-15m-1000000000", outcome="UP", settlement_ts=1_000_000_900,
                      settlement_source="polymarket_chainlink_twap_60s_ws")
    settlements = events(db, "SHADOW_SETTLEMENT")
    assert settlements and all(r["settlement_status"] == "observed" for r in settlements)
    assert all(r["pnl_usdc"] is not None for r in settlements)


def test_noncanonical_settlement_keeps_outcome_and_pnl_unknown():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120)
    exp.on_settlement(slug="btc-updown-15m-1000000000", outcome="UNKNOWN", settlement_ts=1_000_000_900,
                      settlement_source="binance_fallback")
    row = events(db, "SHADOW_SETTLEMENT")[0]
    assert row["outcome"] is None
    assert row["pnl_usdc"] is None
    assert row["settlement_status"] == "canonical_settlement_unavailable"


def test_research_db_failure_does_not_escape_or_change_any_live_authority():
    db = ResearchDB(fail=True)
    exp = ForwardShadowExperiment(db=db, run_id="test")
    assert quote(exp, now=1_000_000_120) == 3
    assert exp.counters["write_errors"] >= 9  # includes hourly capture-health event
    assert exp._candidates  # in-memory research calculations proceed without affecting caller


def test_research_queue_drop_is_counted_but_never_raises():
    class FullQueueDB:
        def enqueue_decision(self, **_kwargs):
            return False

        def research_health(self):
            return {"queue_drops": 9, "write_errors": 0}

    exp = ForwardShadowExperiment(db=FullQueueDB(), run_id="test")
    assert quote(exp, now=1_000_000_120) == 3
    assert exp.counters["queue_drops"] >= 3


def test_mark_snapshots_are_rate_limited_and_count_suppressed_updates():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120)
    before = len(events(db, "SHADOW_POSITION_MARK"))
    quote(exp, now=1_000_000_120.1, bid=.48, ask=.49)
    after = len(events(db, "SHADOW_POSITION_MARK"))
    assert after == before
    assert len(events(db, "SHADOW_BBO_SNAPSHOT")) == 1
    quote(exp, now=1_000_000_120.3, bid=.47, ask=.48)
    assert len(events(db, "SHADOW_BBO_SNAPSHOT")) == 1
    assert len(events(db, "SHADOW_BBO_MATERIAL_CHANGE")) == 0
    assert exp.counters["events_suppressed"] > 0


def test_exit_top_quote_is_not_counted_as_full_fill_when_top_depth_is_short():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120)
    quote(exp, now=1_000_000_122, bid=.60, ask=.61, bid_size=1)
    exits = [row for row in events(db, "SHADOW_EXIT")
             if row["entry_config"] == "120_0" and row["entry_variant"] == "ENTRY_TOP_ASK" and row["exit_policy"] == "TP20"]
    assert exits
    assert exits[0]["top_exit_fillable"] is False
    assert exits[0]["pnl_usdc"] is None
    assert exits[0]["depth_weighted_pnl_usdc"] is not None


def test_combined_shadow_requires_two_independent_thesis_weakening_components():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120, fair_probability=.60)
    # A 12% drawdown plus signal reversal alone is only one independent thesis component.
    quote(exp, now=1_000_000_122, bid=.44, ask=.45, signal_score=-.3,
          fair_probability=.60, spot_leader="UP")
    assert not [row for row in events(db, "SHADOW_EXIT") if row["exit_policy"].startswith("COMBINED")]
    # Fair deterioration is a second independent component, so both combined policies exit.
    quote(exp, now=1_000_000_124, bid=.44, ask=.45, signal_score=-.3,
          fair_probability=.50, spot_leader="UP")
    exits = [row for row in events(db, "SHADOW_EXIT") if row["exit_policy"].startswith("COMBINED")]
    assert {row["exit_policy"] for row in exits} == {"COMBINED180", "COMBINED300"}
    assert all(row["exit_reason"] == "thesis_weakening_loss" for row in exits)
    assert all(row["thesis_weakening_count"] == 2 for row in exits)


def test_combined_shadow_hard_two_dollar_breaker_does_not_require_thesis():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120)
    quote(exp, now=1_000_000_122, bid=.29, ask=.30)
    exits = [row for row in events(db, "SHADOW_EXIT") if row["exit_policy"].startswith("COMBINED")]
    assert exits
    assert all(row["exit_reason"] == "hard_max_loss_2usdc" for row in exits)


def test_combined_shadow_no_progress_needs_time_and_two_thesis_components():
    db = ResearchDB()
    exp = ForwardShadowExperiment(db=db, run_id="test")
    quote(exp, now=1_000_000_120, fair_probability=.60)
    quote(exp, now=1_000_000_301, bid=.49, ask=.50, signal_score=-.3,
          fair_probability=.50, spot_leader="UP")
    exits = [row for row in events(db, "SHADOW_EXIT") if row["exit_policy"].startswith("COMBINED")]
    assert {row["exit_policy"] for row in exits} == {"COMBINED180"}
    assert exits[0]["exit_reason"] == "no_progress_180s_with_thesis_weakening"


def test_optional_five_second_sampling_preserves_quote_driven_extrema_and_exits():
    db = ResearchDB(); exp = ForwardShadowExperiment(db=db, run_id='test')
    quote(exp, now=1_000_000_120)
    marks = len(events(db, 'SHADOW_POSITION_MARK'))
    for second in (121, 122, 123, 124):
        quote(exp, now=1_000_000_000 + second)
    assert len(events(db, 'SHADOW_POSITION_MARK')) == marks
    assert len(events(db, 'SHADOW_BBO_SNAPSHOT')) == 1
    quote(exp, now=1_000_000_125)
    assert len(events(db, 'SHADOW_POSITION_MARK')) > marks
    assert len(events(db, 'SHADOW_BBO_SNAPSHOT')) == 2
    # TP can trigger between sampled BBO rows; its exact event is retained.
    quote(exp, now=1_000_000_126, bid=.60, ask=.61)
    assert any(r['exit_policy'] == 'TP20' for r in events(db, 'SHADOW_EXIT'))
    assert len(events(db, 'SHADOW_BBO_SNAPSHOT')) == 2
    assert any(r['mfe_bid_pct'] > .19 for r in events(db, 'SHADOW_POSITION_MARK'))
    assert all(r['capture_policy_version'] == 2 for r in events(db, 'SHADOW_POSITION_MARK'))

from monitoring.trade_journal_db import TradeJournalDB


def _fill(db, *, slug, order_id, side, price, qty, fee=0.0):
    db.log_order_event(
        run_id="run",
        event_type="ORDER_FILLED",
        client_order_id=order_id,
        side=side,
        price=price,
        qty=qty,
        payload={"slug": slug, "effective_fee_usdc": fee},
    )


def test_market_guard_counts_survive_restart_and_ignore_partial_fill_rows(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    _fill(db, slug=slug, order_id="buy-1", side="BUY", price=0.7, qty=2)
    _fill(db, slug=slug, order_id="buy-1", side="BUY", price=0.7, qty=3)
    db.log_order_event(
        run_id="run",
        event_type="ORDER_TAKER_EXIT_SUBMIT",
        client_order_id="exit-1",
        side="SELL",
        reason="invalidation_recovery",
        payload={"slug": slug},
    )
    _fill(db, slug=slug, order_id="exit-1", side="SELL", price=0.4, qty=5)

    assert db.load_market_guard_counts(slug) == {
        "buy_count": 1,
        "protective_exit_count": 1,
    }


def test_strong_directional_regime_calibration_uses_one_first_observation_per_market(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for index, (candidate, outcome) in enumerate((("UP", "UP"), ("DOWN", "UP"), ("UP", "UP"))):
        slug = f"btc-updown-{index}"
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": f"BUY_{candidate}",
                "main_score": 0.40,
                "main_side_locked": True,
                "spot_minus_strike": 20 if candidate == "UP" else -20,
                "time_left_sec": 480,
            },
        )
        # A later flip must not create another sample for this market.
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": f"BUY_{outcome}",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 20 if outcome == "UP" else -20,
                "time_left_sec": 480,
            },
        )
        db.log_strategy_event("run", "MARKET_SETTLEMENT", {"slug": slug, "outcome": outcome})

    calibrations = db.load_strong_directional_regime_calibrations(
        lookback_hours=168,
        min_score_abs=0.35,
        min_samples=3,
    )

    calibration = calibrations["10_30"]
    assert calibration["sample_count"] == 3
    assert calibration["wins"] == 2
    assert calibration["win_probability"] == 2 / 3


def test_strong_directional_regime_calibration_selects_first_eligible_60_plus_observation(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for index in range(3):
        slug = f"btc-updown-60-plus-{index}"
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": "BUY_UP",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 80,
                "time_left_sec": 700,
            },
        )
        db.log_strategy_event(
            "run", "LIVE_SIGNAL_COMPARE",
            {
                "slug": slug,
                "main_candidate_side": "BUY_UP",
                "main_score": 0.50,
                "main_side_locked": True,
                "spot_minus_strike": 80,
                "time_left_sec": 480,
            },
        )
        db.log_strategy_event("run", "MARKET_SETTLEMENT", {"slug": slug, "outcome": "UP"})

    calibrations = db.load_strong_directional_regime_calibrations(
        lookback_hours=168,
        min_score_abs=0.35,
        min_samples=3,
    )

    calibration = calibrations["60_plus"]
    assert calibration["sample_count"] == 3
    assert calibration["wins"] == 3


def test_reconcile_redeem_cycle_rebuilds_missing_pnl_with_buy_fees(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    _fill(db, slug=slug, order_id="buy-down", side="BUY", price=0.72, qty=5.4, fee=0.02)
    _fill(db, slug=slug, order_id="sell-down", side="SELL", price=0.38, qty=5.3, fee=0.03)
    _fill(db, slug=slug, order_id="buy-up", side="BUY", price=0.73, qty=5.4)

    reconciled = db.reconcile_redeem_cycle(slug, 5.4)

    assert reconciled is not None
    assert round(reconciled["buy_cost_usdc"], 3) == 7.85
    assert round(reconciled["sell_proceeds_usdc"], 3) == 1.984
    assert round(reconciled["cycle_combined_pnl_usdc"], 3) == -0.466
    db.log_strategy_event(
        "run",
        "MARKET_CYCLE_PNL",
        {"slug": slug, "cycle_combined_pnl_usdc": reconciled["cycle_combined_pnl_usdc"]},
    )
    assert db.reconcile_redeem_cycle(slug, 5.4)["wrote_cycle_pnl"] is False


def test_reconcile_redeem_cycle_updates_existing_cycle_without_duplicate(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-existing"
    _fill(db, slug=slug, order_id="buy", side="BUY", price=0.70, qty=10, fee=0.10)
    db.log_strategy_event(
        "run",
        "MARKET_SETTLEMENT",
        {"slug": slug, "inventory_shares": 9.9, "inventory_cost_usdc": 7.1, "redeem_value_usdc": 0.0},
    )
    db.log_strategy_event(
        "run",
        "MARKET_CYCLE_PNL",
        {"slug": slug, "cycle_combined_pnl_usdc": -7.0},
    )

    reconciled = db.reconcile_redeem_cycle(slug, 9.9, tx_hash="0xtx", condition_id="0xcondition")

    assert reconciled is not None
    assert reconciled["wrote_cycle_pnl"] is False
    assert round(reconciled["cycle_combined_pnl_usdc"], 6) == 2.8
    with db._connect() as conn:
        pnl_rows = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='MARKET_CYCLE_PNL'"
        ).fetchall()
        settlement = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='MARKET_SETTLEMENT'"
        ).fetchone()
    assert len(pnl_rows) == 1
    assert '"cycle_pnl_reconciled_source": "onchain_redeem"' in pnl_rows[0][0]
    assert '"redeem_value_usdc": 9.9' in settlement[0]

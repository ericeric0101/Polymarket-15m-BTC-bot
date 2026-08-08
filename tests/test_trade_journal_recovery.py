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


def test_reconcile_redeem_cycle_rebuilds_only_missing_pnl(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    slug = "btc-updown-15m-test"
    _fill(db, slug=slug, order_id="buy-down", side="BUY", price=0.72, qty=5.4)
    _fill(db, slug=slug, order_id="sell-down", side="SELL", price=0.38, qty=5.3, fee=0.03)
    _fill(db, slug=slug, order_id="buy-up", side="BUY", price=0.73, qty=5.4)

    reconciled = db.reconcile_redeem_cycle(slug, 5.4)

    assert reconciled is not None
    assert round(reconciled["buy_cost_usdc"], 3) == 7.83
    assert round(reconciled["sell_proceeds_usdc"], 3) == 1.984
    assert round(reconciled["cycle_combined_pnl_usdc"], 3) == -0.446
    db.log_strategy_event(
        "run",
        "MARKET_CYCLE_PNL",
        {"slug": slug, "cycle_combined_pnl_usdc": reconciled["cycle_combined_pnl_usdc"]},
    )
    assert db.reconcile_redeem_cycle(slug, 5.4) is None

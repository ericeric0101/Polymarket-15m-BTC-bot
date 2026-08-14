from decimal import Decimal

from bot.exit_audit import build_invalidation_exit_audit


def test_invalidation_exit_audit_records_existing_gate_evidence() -> None:
    payload = build_invalidation_exit_audit(
        slug="btc-updown-15m-1",
        instrument_id="up-token",
        time_left_sec=420.0,
        best_bid=Decimal("0.41"),
        best_ask=Decimal("0.42"),
        qty=Decimal("10"),
        sellable_qty=Decimal("9.99"),
        avg_entry=Decimal("0.69"),
        hold_sec=180.0,
        locked_side_invalidated=True,
        twap_confirms_adverse=True,
        twap_fresh=True,
        recovery_candidate=False,
        recovery_ratio=Decimal("0.5942028985"),
        min_recovery_ratio=Decimal("0.50"),
        min_hold_sec=120.0,
        max_time_left_sec=720.0,
        min_bid=Decimal("0.15"),
        disable_if_bid_below=Decimal("0.10"),
        pending_exit=False,
        block_reason="recovery_ratio_below_min",
    )

    assert payload["locked_side_invalidated"] is True
    assert payload["twap_confirms_adverse"] is True
    assert payload["gross_recovery"] == 4.0959
    assert payload["cost_basis"] == 6.8931
    assert payload["block_reason"] == "recovery_ratio_below_min"

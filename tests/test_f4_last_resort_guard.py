"""
F-4 regression test: `true_last_resort` must NOT bypass thesis quality.

Tests three scenarios:
1. Thesis-good position in final 15s → must NOT allow loss sell
2. Thesis-bad position in final 15s → MUST allow loss sell  
3. Absolute last resort (60s window) with thesis-bad → MUST allow loss sell
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.quote_service import compute_loss_sell_policy


def _base_kwargs():
    """Common kwargs for a thesis-healthy, non-adverse position."""
    return dict(
        thesis_weakened=False,
        offside_confirmed=False,
        confirmed_adverse_exit_active=False,
        spot_still_supports_position=True,
        stop_loss_pending_active=False,
        stop_loss_regime_armed=False,
        decision_phase="HOLD",
        decision_regime="TREND",
        hold_sec=120.0,
        loss_sell_min_hold_sec=90.0,
        emergency_window=False,
        time_left_sec=10.0,  # within true_last_resort (< 15s)
        absolute_last_resort_sec=60.0,
        true_last_resort_sec=15.0,
    )


def test_thesis_good_in_true_last_resort_does_NOT_loss_sell():
    """
    Scenario: position has a good thesis (HOLD phase, no thesis_bad signals),
    and we are in the last 10 seconds. The bot must NOT force-sell.
    """
    kwargs = _base_kwargs()
    # thesis_good = True (not thesis_bad, phase=HOLD)
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert not allow, (
        f"F-4 FAILED: thesis-good position was loss-sold in true_last_resort. "
        f"reason={reason}"
    )
    print("PASS: thesis-good position NOT loss-sold in true_last_resort")


def test_thesis_bad_in_true_last_resort_DOES_loss_sell():
    """
    Scenario: thesis is weakened and we are in the last 10 seconds.
    The bot MUST allow loss-selling.
    """
    kwargs = _base_kwargs()
    kwargs["thesis_weakened"] = True 
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert allow, (
        "F-4 FAILED: thesis-bad position was NOT loss-sold in true_last_resort."
    )
    assert "last_resort" in reason or "exit" in reason or "forced" in reason or "adverse" in reason, (
        f"F-4 WARNING: unexpected reason={reason}"
    )
    print(f"PASS: thesis-bad position loss-sold in true_last_resort, reason={reason}")


def test_absolute_last_resort_thesis_bad_still_works():
    """
    Scenario: within the absolute_last_resort window (60s) but outside
    true_last_resort (15s), with thesis_bad. Must still allow loss sell.
    """
    kwargs = _base_kwargs()
    kwargs["time_left_sec"] = 40.0  # inside 60s, outside 15s
    kwargs["thesis_weakened"] = True
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert allow, (
        "F-4 FAILED: thesis-bad position not loss-sold in absolute_last_resort window"
    )
    print(f"PASS: thesis-bad position loss-sold in absolute_last_resort, reason={reason}")


def test_thesis_good_in_absolute_last_resort_does_NOT_loss_sell():
    """
    Scenario: within 60s window but thesis is good. Must NOT loss-sell.
    (Existing behavior, should still pass after our change.)
    """
    kwargs = _base_kwargs()
    kwargs["time_left_sec"] = 40.0  # inside 60s, outside 15s
    # thesis_good = True
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert not allow, (
        f"F-4 FAILED: thesis-good position loss-sold in absolute_last_resort. "
        f"reason={reason}"
    )
    print("PASS: thesis-good position NOT loss-sold in absolute_last_resort window")


def test_thesis_good_PROBE_phase_in_true_last_resort():
    """
    Scenario: phase=PROBE (also thesis_good). Must NOT loss-sell in last 15s.
    """
    kwargs = _base_kwargs()
    kwargs["decision_phase"] = "PROBE"
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert not allow, (
        f"F-4 FAILED: thesis-good PROBE position loss-sold in true_last_resort. "
        f"reason={reason}"
    )
    print("PASS: thesis-good PROBE position NOT loss-sold in true_last_resort")


def test_exit_phase_overrides_true_last_resort():
    """
    Scenario: decision_phase=EXIT (urgent override). Even with time_left=10s,
    the EXIT phase should ALREADY trigger loss-sell via allow_regime_loss_sell,
    not via the last resort path. This is unaffected by F-4.
    """
    kwargs = _base_kwargs()
    kwargs["decision_phase"] = "EXIT"
    allow, reason = compute_loss_sell_policy(**kwargs)
    assert allow, "EXIT phase must always allow loss sell"
    print(f"PASS: EXIT phase correctly overrides, reason={reason}")


if __name__ == "__main__":
    test_thesis_good_in_true_last_resort_does_NOT_loss_sell()
    test_thesis_bad_in_true_last_resort_DOES_loss_sell()
    test_absolute_last_resort_thesis_bad_still_works()
    test_thesis_good_in_absolute_last_resort_does_NOT_loss_sell()
    test_thesis_good_PROBE_phase_in_true_last_resort()
    test_exit_phase_overrides_true_last_resort()
    print("\n✅ All F-4 tests passed.")

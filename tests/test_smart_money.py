import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.smart_money import (
    SmartMoneyConfig,
    SmartMoneyTracker,
    _TradeEvent,
    apply_smart_money_adjustment,
    extract_condition_id_from_instrument_id,
    extract_token_id_from_instrument_id,
)


def _trade(wallet: str, direction: str, cash: float, ts: int = 1000) -> _TradeEvent:
    return _TradeEvent(
        proxy_wallet=wallet,
        side="BUY",
        direction=direction,
        asset="",
        size=cash,
        price=1.0,
        usdc_size=cash,
        timestamp=ts,
        transaction_hash=f"{wallet}-{direction}-{cash}",
    )


def test_extract_condition_and_token_from_instrument_id():
    condition = "0x" + ("a" * 64)
    token = "12345678901234567890"
    inst = f"{condition}-{token}.POLYMARKET"

    assert extract_condition_id_from_instrument_id(inst) == condition
    assert extract_token_id_from_instrument_id(inst) == token


def test_smart_money_supports_active_side():
    tracker = SmartMoneyTracker(
        SmartMoneyConfig(
            entry_threshold=Decimal("0.60"),
            min_directional_wallets=2,
            directional_min_cash=10,
            min_wallet_trades=1,
            shadow_enabled=True,
        )
    )

    signal = tracker._compute_signal(
        recent=[
            _trade("0x1", "UP", 30),
            _trade("0x2", "UP", 25),
            _trade("0x3", "DOWN", 10),
        ],
        hedgers=set(),
        active_side="UP",
        cache_age_sec=1.0,
        last_error="",
    )

    assert signal.state == "support"
    assert signal.direction == "UP"
    assert signal.action == "observe"


def test_smart_money_conflict_reduces_size_when_enabled():
    config = SmartMoneyConfig(
        enabled=True,
        entry_threshold=Decimal("0.60"),
        min_directional_wallets=2,
        directional_min_cash=10,
        min_wallet_trades=1,
        conflict_size_multiplier=Decimal("0.25"),
    )
    tracker = SmartMoneyTracker(config)

    signal = tracker._compute_signal(
        recent=[
            _trade("0x1", "DOWN", 30),
            _trade("0x2", "DOWN", 25),
            _trade("0x3", "UP", 10),
        ],
        hedgers=set(),
        active_side="UP",
        cache_age_sec=1.0,
        last_error="",
    )
    desired = {"should_quote": True, "size_multiplier": Decimal("1")}

    adjusted = apply_smart_money_adjustment(
        desired_entry=desired,
        side="buy",
        signal=signal,
        config=config,
    )

    assert signal.state == "conflict"
    assert adjusted["should_quote"] is True
    assert adjusted["size_multiplier"] == Decimal("0.25")


def test_hedger_wallet_is_excluded_from_flow():
    tracker = SmartMoneyTracker(
        SmartMoneyConfig(
            entry_threshold=Decimal("0.60"),
            min_directional_wallets=1,
            directional_min_cash=10,
            min_wallet_trades=1,
            shadow_enabled=True,
        )
    )

    signal = tracker._compute_signal(
        recent=[
            _trade("0xhedge", "UP", 100),
            _trade("0x2", "DOWN", 20),
        ],
        hedgers={"0xhedge"},
        active_side="UP",
        cache_age_sec=1.0,
        last_error="",
    )

    assert signal.direction == "DOWN"
    assert signal.features["label_counts"]["HEDGER"] == 1

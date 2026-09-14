import pytest

from scripts.execution_path_penalty_report import execution_path, size_bucket, summarize


def test_execution_path_classifies_fast_follow_and_taker_exit_before_liquidity():
    assert execution_path("BTC-15M-FAST-FOLLOW-BUY-1", "BUY", "taker") == "fast_follow_fok_buy"
    assert execution_path("BTC-15M-TAKER-EXIT-1", "SELL", "taker") == "taker_exit_sell"
    assert execution_path("maker", "BUY", "maker") == "maker_buy"


def test_execution_path_size_and_adverse_statistics_are_explicit():
    assert size_bucket(5.5) == "5_5_shares"
    assert size_bucket(10.0) == "10_shares"
    stats = summarize([0.02, -0.01, -0.50])
    assert stats["n"] == 3
    assert stats["raw_adverse_ps"] == pytest.approx(0.17)
    assert stats["winsorized_adverse_ps"] <= stats["raw_adverse_ps"]

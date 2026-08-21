from pathlib import Path
from decimal import Decimal

from bot.app_config import AppConfig
from scripts.inspect_env_contract import OPERATOR_KEYS, _code_keys, _keys


def test_operator_template_has_no_duplicate_or_empty_keys():
    assert len(OPERATOR_KEYS) == 54
    assert all(key and key.upper() == key for key in OPERATOR_KEYS)
    template = Path(__file__).parents[1] / "config" / "operator.env.example"
    assert _keys(template) == OPERATOR_KEYS


def test_env_key_reader_ignores_values_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# credential=hidden\nKNOWN=value\nBAD-KEY=value\n", encoding="utf-8")

    assert _keys(path) == {"KNOWN"}


def test_code_key_scan_includes_core_runtime_configuration():
    keys = _code_keys(Path(__file__).parents[1])

    assert "ENTRY_MIN_ROBUST_NET_USDC" in keys
    assert "TWAP_DEGRADED_BLOCK_NEW_ENTRIES" in keys


def test_first_entry_threshold_is_never_looser_than_general_entry_threshold(monkeypatch):
    monkeypatch.setenv("ENTRY_SCORE_MIN", "0.20")
    monkeypatch.setenv("FIRST_ENTRY_SCORE_MIN", "0.22")

    config = AppConfig.from_env(enable_terminal_dashboard=False)

    assert config.side.directional_entry_min_score_abs_new == Decimal("0.20")
    assert config.side.directional_first_entry_min_score_abs_new == Decimal("0.22")


def test_market_entry_budget_is_hard_limited_to_one_successful_buy(monkeypatch):
    monkeypatch.setenv("MARKET_MAX_BUY_EVENTS_PER_MARKET", "2")

    config = AppConfig.from_env(enable_terminal_dashboard=False)

    assert config.exit.market_max_buy_events_per_market == 1

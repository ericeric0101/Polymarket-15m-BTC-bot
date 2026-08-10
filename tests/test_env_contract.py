from pathlib import Path

from scripts.inspect_env_contract import OPERATOR_KEYS, _code_keys, _keys


def test_operator_template_has_no_duplicate_or_empty_keys():
    assert len(OPERATOR_KEYS) == 66
    assert all(key and key.upper() == key for key in OPERATOR_KEYS)


def test_env_key_reader_ignores_values_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# credential=hidden\nKNOWN=value\nBAD-KEY=value\n", encoding="utf-8")

    assert _keys(path) == {"KNOWN"}


def test_code_key_scan_includes_core_runtime_configuration():
    keys = _code_keys(Path(__file__).parents[1])

    assert "MAKER_MIN_EXPECTED_NET_USDC" in keys
    assert "TWAP_DEGRADED_BLOCK_NEW_ENTRIES" in keys

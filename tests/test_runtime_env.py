from __future__ import annotations

from pathlib import Path

import pytest

from bot.runtime_env import SENSITIVE_ENV_KEYS, load_runtime_env
from scripts.inspect_env_contract import _keys
from scripts.migrate_env_to_profile import migrate


def test_profile_loads_before_local_canonical_overrides(tmp_path):
    root = tmp_path
    profile_dir = root / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "test-profile.env").write_text(
        "DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW=0.20\n"
        "MAKER_FIXED_SHARES=8\n"
        "MAKER_MIN_MINUTES_TO_CLOSE=3\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "STRATEGY_PROFILE=test-profile\n"
        "ENTRY_SCORE_MIN=0.25\n"
        "MARKET_TARGET_SHARES=10\n"
        "ENTRY_MIN_TIME_LEFT_SEC=60\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    path = load_runtime_env(repo_root=root, environ=environ)

    assert path == profile_dir / "test-profile.env"
    assert environ["DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW"] == "0.25"
    assert environ["MAKER_FIXED_SHARES"] == "10"
    assert environ["MAKER_MIN_MINUTES_TO_CLOSE"] == "1.0"


def test_shell_legacy_override_remains_highest_priority(tmp_path):
    profile_dir = tmp_path / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "test-profile.env").write_text(
        "MAKER_FIXED_SHARES=8\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "STRATEGY_PROFILE=test-profile\nMARKET_TARGET_SHARES=10\n",
        encoding="utf-8",
    )
    environ = {"MAKER_FIXED_SHARES": "6"}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["MAKER_FIXED_SHARES"] == "6"


def test_invalid_profile_name_is_rejected(tmp_path):
    (tmp_path / ".env").write_text("STRATEGY_PROFILE=../../bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid STRATEGY_PROFILE"):
        load_runtime_env(repo_root=tmp_path, environ={})


def test_migration_keeps_secrets_local_and_maps_operator_names(tmp_path):
    source = tmp_path / ".env"
    source.write_text(
        "POLYMARKET_PK=secret\n"
        "MAKER_FIXED_SHARES=10\n"
        "MAKER_MIN_MINUTES_TO_CLOSE=1\n"
        "DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW=0.20\n"
        "MAKER_ORDER_TTL_SEC=25\n"
        "MAKER_HALF_SPREAD=0.012\n",
        encoding="utf-8",
    )

    core, profile = migrate(source=source, profile=tmp_path / "btc15_twap_v3.env")

    assert core["POLYMARKET_PK"] == "secret"
    assert core["MARKET_TARGET_SHARES"] == "10"
    assert core["ENTRY_MIN_TIME_LEFT_SEC"] == "60.0"
    assert core["ENTRY_SCORE_MIN"] == "0.20"
    assert core["ORDER_TTL_SEC"] == "25"
    assert "POLYMARKET_PK" not in profile
    assert profile["MAKER_HALF_SPREAD"] == "0.012"


def test_versioned_profile_has_no_sensitive_keys():
    profile = Path(__file__).parents[1] / "config" / "profiles" / "btc15_twap_v3.env"

    assert _keys(profile).isdisjoint(SENSITIVE_ENV_KEYS)

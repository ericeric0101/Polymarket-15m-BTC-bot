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
        "ENTRY_SCORE_MIN=0.20\n"
        "MARKET_TARGET_SHARES=8\n"
        "ENTRY_MIN_TIME_LEFT_SEC=180\n",
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
    assert environ["ENTRY_SCORE_MIN"] == "0.25"
    assert environ["MARKET_TARGET_SHARES"] == "10"
    assert environ["ENTRY_MIN_TIME_LEFT_SEC"] == "60"


def test_shell_canonical_override_remains_highest_priority(tmp_path):
    profile_dir = tmp_path / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "test-profile.env").write_text(
        "MARKET_TARGET_SHARES=8\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "STRATEGY_PROFILE=test-profile\nMARKET_TARGET_SHARES=10\n",
        encoding="utf-8",
    )
    environ = {"MARKET_TARGET_SHARES": "6"}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["MARKET_TARGET_SHARES"] == "6"


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
        "MAKER_POST_ONLY=1\n"
        "MAKER_ORDER_TTL_SEC=25\n"
        "MAKER_REQUOTE_MIN_AGE_SEC=7\n"
        "REQUOTE_HYSTERESIS_TICKS=2\n"
        "MAKER_MAX_INVENTORY_SHARES=10\n"
        "MAKER_HALF_SPREAD=0.012\n",
        encoding="utf-8",
    )

    core, profile = migrate(source=source, profile=tmp_path / "btc15_twap_v3.env")

    assert core["POLYMARKET_PK"] == "secret"
    assert core["MARKET_TARGET_SHARES"] == "10"
    assert core["ENTRY_MIN_TIME_LEFT_SEC"] == "60.0"
    assert core["ENTRY_SCORE_MIN"] == "0.20"
    assert core["ORDER_POST_ONLY"] == "1"
    assert core["ORDER_TTL_SEC"] == "25"
    assert core["ORDER_REQUOTE_MIN_AGE_SEC"] == "7"
    assert core["ORDER_REQUOTE_HYSTERESIS_TICKS"] == "2"
    assert core["MARKET_MAX_POSITION_SHARES"] == "10"
    assert "POLYMARKET_PK" not in profile
    assert profile["MAKER_HALF_SPREAD"] == "0.012"


def test_runtime_env_does_not_recreate_removed_lifecycle_aliases(tmp_path):
    profile_dir = tmp_path / "config" / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "btc15_twap_v3.env").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "ORDER_POST_ONLY=1\n"
        "ORDER_TTL_SEC=21\n"
        "ORDER_REQUOTE_MIN_AGE_SEC=8\n"
        "ORDER_REQUOTE_HYSTERESIS_TICKS=3\n"
        "MARKET_MAX_POSITION_SHARES=10\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["ORDER_POST_ONLY"] == "1"
    assert environ["ORDER_TTL_SEC"] == "21"
    assert environ["ORDER_REQUOTE_MIN_AGE_SEC"] == "8"
    assert environ["ORDER_REQUOTE_HYSTERESIS_TICKS"] == "3"
    assert environ["MARKET_MAX_POSITION_SHARES"] == "10"
    assert not {
        "MAKER_POST_ONLY",
        "MAKER_ORDER_TTL_SEC",
        "MAKER_REQUOTE_MIN_AGE_SEC",
        "REQUOTE_HYSTERESIS_TICKS",
        "MAKER_MAX_INVENTORY_SHARES",
        "MAX_LOCKED_SIDE_POSITION",
    } & environ.keys()


def test_versioned_profile_has_no_sensitive_keys():
    profile = Path(__file__).parents[1] / "config" / "profiles" / "btc15_twap_v3.env"

    assert _keys(profile).isdisjoint(SENSITIVE_ENV_KEYS)

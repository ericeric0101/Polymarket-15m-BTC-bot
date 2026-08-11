#!/usr/bin/env python3
"""Migrate a legacy full .env into a tracked profile plus a small local .env.

Only non-sensitive advanced settings are written to the profile. The command
never prints values. Run with --apply to perform the migration.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.runtime_env import (
    CANONICAL_TO_LEGACY,
    CORE_ENV_KEYS,
    SENSITIVE_ENV_KEYS,
)

LEGACY_TO_CANONICAL = {legacy: canonical for canonical, legacy in CANONICAL_TO_LEGACY.items() if canonical != legacy}


def _canonical_value(values: dict[str, str | None], key: str) -> str | None:
    if key in values:
        return values[key]
    legacy = CANONICAL_TO_LEGACY.get(key)
    if legacy is not None:
        return values.get(legacy)
    if key == "ENTRY_MIN_TIME_LEFT_SEC":
        minutes = values.get("MAKER_MIN_MINUTES_TO_CLOSE")
        return str(float(minutes) * 60.0) if minutes is not None else None
    if key == "EXTERNAL_CONFLICT_ACTION":
        return "skip" if values.get("EXTERNAL_ENTRY_CONFIRMATION_SKIP_STRONG_CONFLICT", "1") == "1" else "size_down"
    if key == "EXECUTION_COST_MODE":
        return "empirical_markout" if values.get("MAKER_EXECUTION_EMPIRICAL_MARKOUT_ENABLE", "1") == "1" else "book_proxy"
    if key == "HIGH_PRICE_TARGET_SHARES":
        target = values.get("MAKER_FIXED_SHARES")
        multiplier = values.get("MAKER_HIGH_ENTRY_PRICE_SIZE_ADJUST_MULTIPLIER")
        if target is not None and multiplier is not None:
            return str(float(target) * float(multiplier))
    if key == "MARKET_MAX_POSITION_SHARES":
        return values.get("MAKER_MAX_INVENTORY_SHARES")
    return None


def _render(assignments: dict[str, str | None]) -> str:
    return "\n".join(f"{key}={value or ''}" for key, value in assignments.items()) + "\n"


def migrate(*, source: Path, profile: Path) -> tuple[dict[str, str | None], dict[str, str | None]]:
    values = dict(dotenv_values(source))
    profile_values = {
        key: value
        for key, value in values.items()
        if key
        and key not in CORE_ENV_KEYS
        and key not in SENSITIVE_ENV_KEYS
        and key not in LEGACY_TO_CANONICAL
        and key not in {"MAKER_MIN_MINUTES_TO_CLOSE", "MAKER_MAX_INVENTORY_SHARES", "MAX_LOCKED_SIDE_POSITION"}
    }
    core_values: dict[str, str | None] = {"STRATEGY_PROFILE": profile.stem}
    for key in sorted(CORE_ENV_KEYS - {"STRATEGY_PROFILE"}):
        value = _canonical_value(values, key)
        if value is not None:
            core_values[key] = value
    for key in sorted(SENSITIVE_ENV_KEYS):
        if key in values:
            core_values[key] = values[key]
    return core_values, profile_values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--profile", default="btc15_twap_v3")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = (REPO_ROOT / args.env).resolve()
    profile = REPO_ROOT / "config" / "profiles" / f"{args.profile}.env"
    if not source.is_file():
        raise SystemExit(f"environment file not found: {source}")
    core, advanced = migrate(source=source, profile=profile)
    print(f"core_keys={len(core)} profile_keys={len(advanced)} sensitive_keys={len(set(core) & SENSITIVE_ENV_KEYS)}")
    if not args.apply:
        return 0
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(_render(dict(sorted(advanced.items()))), encoding="utf-8")
    source.write_text(_render(core), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

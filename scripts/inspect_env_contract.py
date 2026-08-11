#!/usr/bin/env python3
"""Classify an environment file into operator, advanced, and unknown keys.

The tool deliberately reads only key names. It never prints values, so it is
safe to run against a local file containing wallet credentials.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.runtime_env import CORE_ENV_KEYS, SENSITIVE_ENV_KEYS

OPERATOR_KEYS = CORE_ENV_KEYS


def _keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, _ = line.partition("=")
        if key and key.replace("_", "").isalnum() and key.upper() == key:
            keys.add(key)
    return keys


def _code_keys(repo_root: Path) -> set[str]:
    """Find configured environment keys without importing the live bot."""
    pattern = re.compile(
        r"(?:_env_(?:bool|bool_inverted|decimal|float|int|str)|os\.getenv)\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    )
    keys: set[str] = set()
    for path in (repo_root / "bot").rglob("*.py"):
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    for path in (repo_root / "scripts").glob("*.py"):
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    for path in (repo_root / "run_bot.py", repo_root / "dashboard.py"):
        if path.exists():
            keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def _profile_keys(repo_root: Path) -> set[str]:
    profile_dir = repo_root / "config" / "profiles"
    if not profile_dir.is_dir():
        return set()
    keys: set[str] = set()
    for path in profile_dir.glob("*.env"):
        keys.update(_keys(path))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="environment file to inspect")
    parser.add_argument(
        "--known", default=".env.example", help="full known-key reference file"
    )
    parser.add_argument("--list", action="store_true", help="print advanced and unknown key names")
    parser.add_argument("--strict", action="store_true", help="return nonzero for unknown keys")
    args = parser.parse_args()
    env_path = Path(args.env)
    known_path = Path(args.known)
    if not env_path.exists():
        raise SystemExit(f"environment file not found: {env_path}")
    if not known_path.exists():
        raise SystemExit(f"known-key reference not found: {known_path}")

    keys = _keys(env_path)
    repo_root = Path(__file__).parents[1]
    known_keys = (
        _keys(known_path)
        | _code_keys(repo_root)
        | _profile_keys(repo_root)
        | OPERATOR_KEYS
        | SENSITIVE_ENV_KEYS
    )
    operator = keys & OPERATOR_KEYS
    local_only = keys & SENSITIVE_ENV_KEYS - OPERATOR_KEYS
    advanced = (keys & known_keys) - OPERATOR_KEYS - SENSITIVE_ENV_KEYS
    unknown = keys - known_keys
    print(
        f"total_keys={len(keys)} operator_keys={len(operator)} "
        f"local_only_keys={len(local_only)} advanced_keys={len(advanced)} unknown_keys={len(unknown)}"
    )
    if args.list and local_only:
        print("local_only_keys=" + ",".join(sorted(local_only)))
    if args.list and advanced:
        print("advanced_keys=" + ",".join(sorted(advanced)))
    if args.list and unknown:
        print("unknown_keys=" + ",".join(sorted(unknown)))
    return 1 if args.strict and unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Immutable, secret-safe metadata for reproducible process runs."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_CONFIG_SECTIONS = ("maker", "side", "exit", "risk", "market_data", "operations")
_SENSITIVE_KEY_PARTS = ("secret", "private", "password", "passphrase", "token", "credential", "api_key", "apikey", "wallet", "seed")
_SCHEMA_VERSIONS = {
    "prediction_schema_version": 1,
    "research_schema_version": 1,
    "telemetry_schema_version": 1,
    "lifecycle_schema_version": 1,
}
PREDICTION_SCHEMA_VERSION = _SCHEMA_VERSIONS["prediction_schema_version"]
RESEARCH_SCHEMA_VERSION = _SCHEMA_VERSIONS["research_schema_version"]
LIFECYCLE_SCHEMA_VERSION = _SCHEMA_VERSIONS["lifecycle_schema_version"]


def _safe(value: Any, key: str = "") -> Any:
    normalized_key = key.lower()
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS) or normalized_key.endswith("_path"):
        return None
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {
            str(name): cleaned
            for name, raw in sorted(value.items(), key=lambda item: str(item[0]))
            if (cleaned := _safe(raw, str(name))) is not None
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if hasattr(value, "as_tuple") and value.__class__.__name__ == "Decimal":
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    return str(value)


def safe_strategy_config(config: Any) -> dict[str, Any]:
    """Return only strategy/research sections, with credential-like fields removed."""
    raw = dataclasses.asdict(config) if dataclasses.is_dataclass(config) else dict(config or {})
    selected = {name: raw[name] for name in _CONFIG_SECTIONS if name in raw}
    return _safe(selected)


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args], capture_output=True,
                text=True, timeout=3.0, check=True,
            )
            return result.stdout.strip()
        except Exception:
            return None

    commit = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    status = git("status", "--porcelain")
    if status is None:
        return {
            "git_commit": commit, "git_branch": branch or None, "git_dirty": None,
            "git_dirty_tracked": None, "untracked_file_count": None, "untracked_runtime_files": [],
            "dirty_diff_hash": None, "dirty_diff_hash_scope": _DIRTY_SCOPE, "dirty_diff_archive": None,
        }
    lines = [line for line in status.splitlines() if line.strip()]
    tracked_dirty = any(not line.startswith("??") for line in lines)
    untracked = [line[3:].strip() for line in lines if line.startswith("??")]
    # `git status` collapses untracked directories; list files explicitly so a
    # new module inside an untracked package is still part of the runtime hash.
    untracked_files = (git("ls-files", "--others", "--exclude-standard") or "").splitlines()
    runtime_files = sorted(path for path in untracked_files if _is_runtime_source(path))
    payload = b""
    if tracked_dirty:
        try:
            payload += subprocess.run(
                ["git", "-C", str(repo_root), "diff", "HEAD", "--binary"],
                capture_output=True, timeout=5.0, check=True,
            ).stdout
        except Exception:
            payload = b""
    for path in runtime_files:
        try:
            payload += b"\0untracked:" + path.encode() + b"\0" + (repo_root / path).read_bytes()
        except OSError:
            payload += b"\0untracked-unreadable:" + path.encode()
    diff_hash = hashlib.sha256(payload).hexdigest() if payload else None
    return {
        "git_commit": commit,
        "git_branch": branch or None,
        # Untracked research reports do not change the running code; only
        # tracked edits or untracked runtime sources make a run irreproducible.
        "git_dirty": bool(tracked_dirty or runtime_files),
        "git_dirty_tracked": tracked_dirty,
        "untracked_file_count": len(untracked),
        "untracked_runtime_files": runtime_files[:50],
        "dirty_diff_hash": diff_hash,
        "dirty_diff_hash_scope": _DIRTY_SCOPE,
        "dirty_diff_archive": _archive_dirty_payload(repo_root, diff_hash, payload),
    }


_DIRTY_SCOPE = "tracked_diff_plus_untracked_runtime_sources"
_RUNTIME_SOURCE_DIRS = ("bot/", "execution/", "monitoring/", "core/", "config/", "py_clob_client/")
_RUNTIME_SOURCE_FILES = ("run_bot.py",)
_MAX_ARCHIVED_DIFF_BYTES = 5 * 1024 * 1024


def _is_runtime_source(path: str) -> bool:
    if path in _RUNTIME_SOURCE_FILES:
        return True
    return path.startswith(_RUNTIME_SOURCE_DIRS) and path.endswith((".py", ".pyx", ".json", ".env"))


def _archive_dirty_payload(repo_root: Path, diff_hash: str | None, payload: bytes) -> str | None:
    """Content-addressed, size-bounded copy of the exact uncommitted runtime code."""
    if not diff_hash or len(payload) > _MAX_ARCHIVED_DIFF_BYTES:
        return None
    try:
        target = repo_root / "logs" / "run_diffs" / f"{diff_hash}.patch"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        return str(target.relative_to(repo_root))
    except OSError:
        return None


def build_run_manifest(
    *, run_id: str, config: Any, mode: str, test_mode: bool, maker_mode: bool,
    repo_root: str | Path | None = None, trade_journal_schema_version: int | None = None,
    strategy_profile: str | None = None,
) -> dict[str, Any]:
    """Build a single safe manifest at process startup; failures degrade to unknown metadata."""
    now = datetime.now(timezone.utc)
    safe_config = safe_strategy_config(config)
    raw_profile = getattr(config, "strategy_profile", None)
    if isinstance(config, dict):
        raw_profile = config.get("strategy_profile")
    config_bytes = json.dumps(safe_config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        git = _git_metadata(root)
    except Exception:
        git = {
            "git_commit": None, "git_branch": None, "git_dirty": None,
            "git_dirty_tracked": None, "untracked_file_count": None, "untracked_runtime_files": [],
            "dirty_diff_hash": None, "dirty_diff_hash_scope": _DIRTY_SCOPE, "dirty_diff_archive": None,
        }
    guard_mode = str((safe_config.get("operations") or {}).get("session_pnl_guard_mode") or "legacy")
    return {
        "manifest_schema_version": 1,
        "run_id": str(run_id),
        "started_at_utc": now.isoformat().replace("+00:00", "Z"),
        "started_at_taipei": now.astimezone(ZoneInfo("Asia/Taipei")).isoformat(),
        **git,
        "strategy_profile": strategy_profile or raw_profile or os.getenv("STRATEGY_PROFILE") or "UNKNOWN",
        "session_guard_mode": guard_mode,
        "execution_mode": str(mode),
        "test_dry_run": bool(test_mode),
        "maker_mode": bool(maker_mode),
        "config_hash": hashlib.sha256(config_bytes).hexdigest(),
        "config_hash_scope": "explicit_safe_section_allowlist_only",
        "config_hash_sections": list(_CONFIG_SECTIONS),
        "safe_config": safe_config,
        "schema_versions": {
            **_SCHEMA_VERSIONS,
            "trade_journal_schema_version": trade_journal_schema_version,
            "session_guard_version": 2 if guard_mode in {"target_scaled_v2", "shadow_target_scaled_v2"} else 1,
        },
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "hostname": socket.gethostname(),
        "process_id": os.getpid(),
        "runtime_version": sys.version.split()[0],
    }


def process_identity() -> dict[str, Any]:
    """One identity per imported Python launcher process; no disk reads."""
    return dict(_PROCESS_IDENTITY)


_PROCESS_STARTED_AT = time.time()
_PROCESS_IDENTITY = {
    "process_instance_id": f"process_{int(_PROCESS_STARTED_AT)}_{os.getpid()}_{uuid.uuid4().hex[:8]}",
    "pid": os.getpid(), "process_started_at": _PROCESS_STARTED_AT,
}

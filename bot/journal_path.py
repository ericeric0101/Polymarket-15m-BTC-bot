"""Canonical trade-journal location and the cross-process writer lock.

One resolver for every reader and writer (bot, dashboard, research and
maintenance scripts): explicit path > shell env > ``.env`` > strategy profile
> ``DEFAULT_TRADE_DB_PATH``, the same precedence ``bot.runtime_env`` applies to
the bot. A relative path is anchored at the repository root, never the CWD.

The writer lock is a host-local advisory ``flock`` next to the journal. The bot
holds it for its process lifetime in LIVE and DRY-RUN alike, so two bot
processes can never write the same journal. Destructive maintenance tools take
the same lock (and check the LIVE process lock) for their whole run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from bot.process_lock import ProcessLock
from bot.runtime_env import PROJECT_ROOT, load_runtime_env

DEFAULT_TRADE_DB_PATH = "./data/trading/trade_journal.db"
DEFAULT_LIVE_PROCESS_LOCK_PATH = "/tmp/polymarket-btc-15m-live.lock"
WRITER_LOCK_SUFFIX = ".writer.lock"


def anchor_repo_path(raw: str | Path, *, repo_root: Optional[Path] = None) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (repo_root or PROJECT_ROOT) / path
    return Path(os.path.normpath(path))


def resolved_runtime_env(*, repo_root: Optional[Path] = None,
                         environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The bot's effective environment, computed on a copy (os.environ untouched)."""
    env = dict(os.environ if environ is None else environ)
    load_runtime_env(repo_root=repo_root, environ=env)
    return env


def resolve_trade_db_path(explicit: Optional[str | Path] = None, *, repo_root: Optional[Path] = None,
                          environ: Optional[Mapping[str, str]] = None) -> Path:
    if explicit:
        return anchor_repo_path(explicit, repo_root=repo_root)
    env = resolved_runtime_env(repo_root=repo_root, environ=environ)
    raw = str(env.get("TRADE_DB_PATH") or "").strip() or DEFAULT_TRADE_DB_PATH
    return anchor_repo_path(raw, repo_root=repo_root)


def resolve_live_process_lock_path(*, repo_root: Optional[Path] = None,
                                   environ: Optional[Mapping[str, str]] = None) -> Path:
    env = resolved_runtime_env(repo_root=repo_root, environ=environ)
    raw = str(env.get("LIVE_PROCESS_LOCK_PATH") or "").strip() or DEFAULT_LIVE_PROCESS_LOCK_PATH
    return anchor_repo_path(raw, repo_root=repo_root)


def journal_writer_lock_path(journal_path: str | Path) -> Path:
    path = Path(journal_path)
    return path.with_name(path.name + WRITER_LOCK_SUFFIX)


# Held for the process lifetime; node rebuilds in the same process re-use it.
_PROCESS_WRITER_LOCKS: Dict[str, ProcessLock] = {}


def acquire_journal_writer_lock(journal_path: str | Path) -> bool:
    key = str(journal_writer_lock_path(journal_path))
    if key in _PROCESS_WRITER_LOCKS:
        return True
    lock = ProcessLock(key)
    if not lock.acquire():
        return False
    _PROCESS_WRITER_LOCKS[key] = lock
    return True


def release_journal_writer_lock(journal_path: str | Path) -> None:
    lock = _PROCESS_WRITER_LOCKS.pop(str(journal_writer_lock_path(journal_path)), None)
    if lock is not None:
        lock.release()


class MaintenanceLockError(RuntimeError):
    pass


class BotStoppedGuard:
    """Hold the journal writer lock (+ verify the LIVE lock is free) for a destructive run."""

    def __init__(self, journal_path: str | Path, live_lock_path: str | Path) -> None:
        self.journal_path = Path(journal_path)
        self.live_lock_path = Path(live_lock_path)
        self._held: List[ProcessLock] = []

    def __enter__(self) -> "BotStoppedGuard":
        live = ProcessLock(self.live_lock_path)
        if not live.acquire():
            raise MaintenanceLockError(f"LIVE bot process lock is held: {self.live_lock_path}")
        writer = ProcessLock(journal_writer_lock_path(self.journal_path))
        if not writer.acquire():
            live.release()
            raise MaintenanceLockError(
                f"Trade-journal writer lock is held (bot running in LIVE or DRY-RUN): "
                f"{journal_writer_lock_path(self.journal_path)}"
            )
        self._held = [writer, live]
        return self

    def __exit__(self, *_exc) -> None:
        for lock in self._held:
            lock.release()
        self._held = []


class JournalWriterGuard:
    """Hold only the journal writer lock.

    For a child spawned by the launcher's final-exit hook: the parent has
    already released the writer lock but still owns the LIVE process lock, and
    no new bot can start without the writer lock this guard holds.
    """

    def __init__(self, journal_path: str | Path) -> None:
        self.journal_path = Path(journal_path)
        self._lock: Optional[ProcessLock] = None

    def __enter__(self) -> "JournalWriterGuard":
        lock = ProcessLock(journal_writer_lock_path(self.journal_path))
        if not lock.acquire():
            raise MaintenanceLockError(
                f"Trade-journal writer lock is held (bot running in LIVE or DRY-RUN): "
                f"{journal_writer_lock_path(self.journal_path)}"
            )
        self._lock = lock
        return self

    def __exit__(self, *_exc) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None


def require_bot_stopped(*, journal_path: Optional[str | Path] = None, repo_root: Optional[Path] = None,
                        environ: Optional[Mapping[str, str]] = None) -> BotStoppedGuard:
    """Context manager for destructive maintenance; raises MaintenanceLockError if the bot runs."""
    journal = Path(journal_path) if journal_path else resolve_trade_db_path(repo_root=repo_root, environ=environ)
    return BotStoppedGuard(journal, resolve_live_process_lock_path(repo_root=repo_root, environ=environ))

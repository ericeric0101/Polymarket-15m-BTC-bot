"""Runtime settlement confirmation: journal official outcome, venue cash and redeem cash.

A daemon worker, separate from quoting and order handling. Each cycle it
reads the journal read-only, finds ended markets the bot traded that still
lack evidence, and appends evidence rows (``bot.settlement_evidence``) through
the strategy's own journal writer:

* official outcome (Gamma) once per market, retried until resolved;
* the venue's TRADE cash legs for those markets (missed fills, real fees);
* REDEEM cash for those markets.

It never edits or adds MARKET_CYCLE_PNL and never touches the regime guard
or any order state: effective PnL is projected from the evidence by
``monitoring.pnl_attribution``. The optional ``after_cycle`` hook lets the
strategy apply its idempotent, open-session-only guard correction
(``StrategyDBRuntimeMixin._apply_settlement_evidence_session_corrections``). Restart-safe: already journaled
evidence keys are re-read every cycle, so retries, duplicate API pages and
restarts cannot write the same fact twice. Every network/journal error is
logged and retried next cycle; nothing propagates into the caller.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from loguru import logger

from bot.settlement_evidence import (
    OUTCOME_EVENT,
    REDEEM_EVENT,
    TRADE_EVENT,
    fetch_activity,
    fetch_gamma_outcome,
    journaled_evidence_keys,
    outcome_evidence_key,
    redeem_evidence_from_activity,
    trade_evidence_from_activity,
)

MARKET_DURATION_SEC = 900


def _slug_start(slug: str) -> Optional[int]:
    try:
        return int(str(slug).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def resolve_wallet_address(environ: Any) -> str:
    """Public wallet address only (same fallback order as the launcher)."""
    for key in ("POLYMARKET_FUNDER", "POLYMARKET_WALLET_ADDRESS", "WALLET_ADDRESS"):
        value = str(environ.get(key) or "").strip().lower()
        if value:
            return value
    return ""


class SettlementConfirmationWorker:
    def __init__(
        self,
        *,
        journal_path: str | Path,
        write_event: Callable[[str, Dict[str, Any]], Any],
        user_address: str,
        client_factory: Optional[Callable[[], Any]] = None,
        interval_sec: float = 120.0,
        grace_sec: float = 60.0,
        lookback_sec: float = 7 * 86400.0,
        max_outcome_fetches_per_cycle: int = 20,
        now_fn: Callable[[], float] = time.time,
        after_cycle: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.journal_path = Path(journal_path)
        self.write_event = write_event
        self.user_address = str(user_address or "").strip().lower()
        self.client_factory = client_factory or self._default_client
        self.interval_sec = max(10.0, float(interval_sec))
        self.grace_sec = max(0.0, float(grace_sec))
        self.lookback_sec = max(MARKET_DURATION_SEC, float(lookback_sec))
        self.max_outcome_fetches = max(1, int(max_outcome_fetches_per_cycle))
        self.now_fn = now_fn
        self.after_cycle = after_cycle
        self.last_stats: Dict[str, Any] = {}

    @staticmethod
    def _default_client() -> Any:
        import httpx
        return httpx.Client(timeout=15.0)

    # -- journal (read-only) ---------------------------------------------------------------

    def _journal_state(self) -> tuple[Dict[str, Set[str]], Set[str], Dict[str, Dict[str, str]]]:
        """(tokens by traded slug, evidence keys, token map by slug from outcome evidence)."""
        uri = f"file:{self.journal_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            tokens: Dict[str, Set[str]] = {}
            for slug, token in conn.execute(
                """SELECT json_extract(payload_json, '$.slug'), token_id FROM order_events
                   WHERE event_type='ORDER_FILLED'"""
            ):
                if slug:
                    tokens.setdefault(str(slug), set()).add(str(token or ""))
            keys = journaled_evidence_keys(conn)
            token_maps: Dict[str, Dict[str, str]] = {}
            for slug, raw in conn.execute(
                """SELECT json_extract(payload_json, '$.slug'), json_extract(payload_json, '$.token_outcomes')
                   FROM strategy_events WHERE event_type=?""",
                (OUTCOME_EVENT,),
            ):
                if slug and raw:
                    try:
                        token_maps[str(slug)] = dict(json.loads(raw))
                    except (TypeError, ValueError):
                        continue
            return tokens, keys, token_maps
        finally:
            conn.close()

    def _write(self, event_type: str, payload: Dict[str, Any], keys: Set[str]) -> bool:
        key = str(payload.get("evidence_key") or "")
        if not key or key in keys:
            return False
        try:
            ok = self.write_event(event_type, payload)
        except Exception as exc:
            logger.warning(f"Settlement confirmation write failed key={key}: {exc}")
            return False
        if ok is not False:
            keys.add(key)
            return True
        return False

    # -- one cycle ---------------------------------------------------------------------------

    def cycle(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {"outcomes_written": 0, "trades_written": 0, "redeems_written": 0,
                                 "outcome_pending": 0, "errors": []}
        now = float(self.now_fn())
        try:
            tokens_by_slug, keys, token_maps = self._journal_state()
        except Exception as exc:
            stats["errors"].append(f"journal_read: {exc}")
            self.last_stats = stats
            return stats
        candidates = []
        for slug in tokens_by_slug:
            start = _slug_start(slug)
            if start is None:
                continue
            end = start + MARKET_DURATION_SEC
            if end + self.grace_sec <= now and end >= now - self.lookback_sec:
                candidates.append(slug)
        if not candidates:
            self.last_stats = stats
            return stats
        try:
            client = self.client_factory()
        except Exception as exc:
            stats["errors"].append(f"client: {exc}")
            self.last_stats = stats
            return stats
        try:
            missing = [s for s in sorted(candidates) if outcome_evidence_key(s) not in keys]
            stats["outcome_pending"] = len(missing)
            for slug in missing[: self.max_outcome_fetches]:
                outcome = fetch_gamma_outcome(client, slug)
                if outcome.get("error"):
                    stats["errors"].append(f"gamma {slug}: {outcome['error']}")
                elif outcome.get("resolved"):
                    if self._write(OUTCOME_EVENT, outcome, keys):
                        stats["outcomes_written"] += 1
                        token_maps[slug] = dict(outcome.get("token_outcomes") or {})
            if self.user_address:
                oldest = min(_slug_start(s) or now for s in candidates) - 3600
                wanted_tokens = {t for s in candidates for t in tokens_by_slug.get(s, set()) | set(token_maps.get(s, {}))}
                wanted_slugs = set(candidates)
                stats["trades_written"] = self._sync_activity(
                    client, "TRADE", trade_evidence_from_activity, TRADE_EVENT, oldest,
                    wanted_tokens, wanted_slugs, keys, stats,
                )
                stats["redeems_written"] = self._sync_activity(
                    client, "REDEEM", redeem_evidence_from_activity, REDEEM_EVENT, oldest,
                    wanted_tokens, wanted_slugs, keys, stats,
                )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self.last_stats = stats
        return stats

    def _sync_activity(self, client: Any, activity_type: str, parse: Callable[[Iterable[Any]], List[Dict[str, Any]]],
                       event_type: str, oldest_ts: float, wanted_tokens: Set[str], wanted_slugs: Set[str],
                       keys: Set[str], stats: Dict[str, Any]) -> int:
        result = fetch_activity(client, self.user_address, activity_type, stop_before_ts=int(oldest_ts))
        if result.get("error"):
            stats["errors"].append(f"{activity_type.lower()}_activity: {result['error']}")
        written = 0
        for item in parse(result.get("rows") or []):
            if item.get("token_id") in wanted_tokens or item.get("slug") in wanted_slugs:
                written += int(self._write(event_type, item, keys))
        return written

    def run(self, stop_event: Any) -> None:
        """Thread target: one cycle per interval until ``stop_event`` is set."""
        while not stop_event.is_set():
            try:
                stats = self.cycle()
                if self.after_cycle is not None:
                    try:
                        stats["session_correction"] = self.after_cycle()
                    except Exception as exc:
                        stats["errors"].append(f"session_correction: {exc}")
                correction = stats.get("session_correction") or {}
                if any(stats.get(k) for k in ("outcomes_written", "trades_written", "redeems_written")) \
                        or correction.get("applied") or stats["errors"]:
                    logger.info(f"Settlement confirmation cycle: {stats}")
            except Exception as exc:  # defensive: the worker must never die silently
                logger.warning(f"Settlement confirmation cycle failed: {exc}")
            stop_event.wait(self.interval_sec)

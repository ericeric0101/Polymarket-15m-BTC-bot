"""Append-only settlement evidence: official outcome and attributable redeem cash.

Two different facts, never conflated:

* ``MARKET_OUTCOME_CONFIRMED`` -- Polymarket's official resolution for a market
  (Gamma ``/events/slug``): winner, token -> outcome map and condition id.
  It confirms *which side won*; it says nothing about cash received.
* ``REDEEM_CASH_CONFIRMED`` -- one Data API ``REDEEM`` activity row: USDC
  actually paid for one (transaction, condition, token). It confirms *cash*.
* ``VENUE_TRADE_CONFIRMED`` -- one Data API ``TRADE`` activity row: the venue's
  own cash leg (``usdc_size``, fees included) and share size for a fill. The
  journal has missed fills (resting take-profit SELLs) and booked some at
  price 0, so venue trades are the cash authority when present.

Rows are evidence, not PnL. ``monitoring.pnl_attribution`` projects effective
PnL from fills + this evidence, so no consumer ever sums a second PnL row.
Every payload carries a stable ``evidence_key``; writers skip keys that are
already journaled, and the projection dedupes by key, so retries, restarts
and duplicate API responses can never count the same cash twice.

Network helpers take an injected client and never raise into a caller's hot
path; pure parsers are separate so tests need no network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from bot.polymarket_data_api import DATA_API_V2_BASE_URL, v2_next_cursor, v2_rows

OUTCOME_EVENT = "MARKET_OUTCOME_CONFIRMED"
REDEEM_EVENT = "REDEEM_CASH_CONFIRMED"
TRADE_EVENT = "VENUE_TRADE_CONFIRMED"
EVIDENCE_EVENTS = (OUTCOME_EVENT, REDEEM_EVENT, TRADE_EVENT)
EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_GAMMA_API = "https://gamma-api.polymarket.com"
OUTCOMES = ("UP", "DOWN")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _norm_hex(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text.startswith("0x") or not text else f"0x{text}"


def outcome_evidence_key(slug: str) -> str:
    return f"outcome:{slug}"


def redeem_evidence_key(tx_hash: str, condition_id: str, token_id: str) -> str:
    return f"redeem:{_norm_hex(tx_hash)}:{_norm_hex(condition_id)}:{str(token_id or '')}"


def trade_evidence_key(tx_hash: str, token_id: str, side: str, size: float, price: float) -> str:
    # One transaction can match several orders; size+price keep the legs distinct.
    return f"trade:{_norm_hex(tx_hash)}:{str(token_id or '')}:{side}:{size:.6f}:{price:.6f}"


def evidence_event_type(payload: Dict[str, Any]) -> str:
    key = str(payload.get("evidence_key") or "")
    if key.startswith("outcome:"):
        return OUTCOME_EVENT
    if key.startswith("trade:"):
        return TRADE_EVENT
    return REDEEM_EVENT


# --- official outcome (Gamma) ------------------------------------------------------------

def parse_gamma_event(slug: str, event: Any, *, source_url: str = "", fetched_at: Optional[str] = None) -> Dict[str, Any]:
    """Pure: Gamma event JSON -> outcome evidence payload (``resolved`` may be False)."""
    market = ((event or {}).get("markets") or [{}])[0] if isinstance(event, dict) else {}
    outcomes = [str(o).strip().upper() for o in _json_list(market.get("outcomes"))]
    prices = _json_list(market.get("outcomePrices"))
    token_ids = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    token_outcomes = {
        token: outcome for token, outcome in zip(token_ids, outcomes) if token and outcome in OUTCOMES
    }
    winners = []
    for outcome, price in zip(outcomes, prices):
        try:
            if float(price) > 0.99:
                winners.append(outcome)
        except (TypeError, ValueError):
            continue
    closed = bool(market.get("closed"))
    uma = str(market.get("umaResolutionStatus") or "").strip().lower()
    official = winners[0] if len(winners) == 1 and winners[0] in OUTCOMES else "UNRESOLVED"
    resolved = closed and official in OUTCOMES and uma in ("resolved", "")
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_key": outcome_evidence_key(slug),
        "slug": slug,
        "condition_id": _norm_hex(market.get("conditionId")) if market.get("conditionId") else "",
        "official_outcome": official,
        "resolved": bool(resolved),
        "token_outcomes": token_outcomes,
        "closed": closed,
        "uma_resolution_status": uma,
        "outcome_prices": [str(p) for p in prices],
        "source": "gamma_events_slug",
        "source_url": source_url,
        "fetched_at": fetched_at or _utc_now_iso(),
    }


def fetch_gamma_outcome(client: Any, slug: str, *, base: str = DEFAULT_GAMMA_API) -> Dict[str, Any]:
    """Network: never raises; failures come back with ``resolved`` False and an error."""
    url = f"{base.rstrip('/')}/events/slug/{quote(slug, safe='')}"
    try:
        response = client.get(url)
        if response.status_code != 200:
            return {"slug": slug, "resolved": False, "error": f"http_{response.status_code}", "source_url": url}
        return parse_gamma_event(slug, response.json(), source_url=url)
    except Exception as exc:  # network/JSON errors are data, not crashes
        return {"slug": slug, "resolved": False, "error": f"{type(exc).__name__}: {exc}"[:200], "source_url": url}


# --- attributable redeem cash (Data API activity) ----------------------------------------

def parse_redeem_activity(row: Any, *, fetched_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pure: one Data API activity row -> redeem evidence, or None if not attributable.

    Requires the transaction hash, condition id and a numeric ``usdc_size``.
    Token quantity alone (``size``) is never treated as cash.
    """
    if not isinstance(row, dict) or str(row.get("type") or "").upper() != "REDEEM":
        return None
    tx_hash = row.get("transaction_hash") or row.get("transactionHash")
    condition_id = row.get("condition_id") or row.get("conditionId")
    cash = row.get("usdc_size", row.get("usdcSize"))
    if not tx_hash or not condition_id or cash is None:
        return None
    try:
        cash_value = float(cash)
        shares = float(row.get("size") or 0.0)
    except (TypeError, ValueError):
        return None
    token_id = str(row.get("token_id") or row.get("asset") or "")
    slug = str(row.get("slug") or "")
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_key": redeem_evidence_key(str(tx_hash), str(condition_id), token_id),
        "slug": slug,
        "condition_id": _norm_hex(condition_id),
        "tx_hash": _norm_hex(tx_hash),
        "token_id": token_id,
        "outcome": str(row.get("outcome") or "").strip().upper(),
        "outcome_index": row.get("outcome_index", row.get("outcomeIndex")),
        "redeemed_shares": shares,
        "redeem_cash_usdc": cash_value,
        "activity_timestamp": row.get("timestamp"),
        "source": "polymarket_data_api_activity",
        "fetched_at": fetched_at or _utc_now_iso(),
    }


def parse_trade_activity(row: Any, *, fetched_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pure: one Data API TRADE row -> venue trade evidence, or None if not attributable."""
    if not isinstance(row, dict) or str(row.get("type") or "").upper() != "TRADE":
        return None
    tx_hash = row.get("transaction_hash") or row.get("transactionHash")
    side = str(row.get("side") or "").upper()
    token_id = str(row.get("token_id") or row.get("asset") or "")
    if not tx_hash or side not in ("BUY", "SELL") or not token_id:
        return None
    try:
        size = float(row.get("size"))
        price = float(row.get("price"))
        cash = float(row.get("usdc_size", row.get("usdcSize")))
    except (TypeError, ValueError):
        return None
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_key": trade_evidence_key(str(tx_hash), token_id, side, size, price),
        "slug": str(row.get("slug") or ""),
        "condition_id": _norm_hex(row.get("condition_id")) if row.get("condition_id") else "",
        "tx_hash": _norm_hex(tx_hash),
        "token_id": token_id,
        "side": side,
        "size": size,
        "price": price,
        "usdc_size": cash,
        "outcome": str(row.get("outcome") or "").strip().upper(),
        "activity_timestamp": row.get("timestamp"),
        "source": "polymarket_data_api_activity",
        "fetched_at": fetched_at or _utc_now_iso(),
    }


def trade_evidence_from_activity(rows: Iterable[Any], *, fetched_at: Optional[str] = None) -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        parsed = parse_trade_activity(row, fetched_at=fetched_at)
        if parsed is not None and parsed["evidence_key"] not in out:
            out[parsed["evidence_key"]] = parsed
    return list(out.values())


def fetch_activity(client: Any, user: str, activity_type: str, *, page_limit: int = 500, max_pages: int = 40,
                   stop_before_ts: Optional[int] = None, base: str = DATA_API_V2_BASE_URL) -> Dict[str, Any]:
    """Network: one activity type (newest first) via cursor pagination; never raises."""
    return fetch_redeem_activity(client, user, page_limit=page_limit, max_pages=max_pages,
                                 stop_before_ts=stop_before_ts, base=base, activity_type=activity_type)


def fetch_redeem_activity(client: Any, user: str, *, page_limit: int = 500, max_pages: int = 40,
                          stop_before_ts: Optional[int] = None,
                          base: str = DATA_API_V2_BASE_URL, activity_type: str = "REDEEM") -> Dict[str, Any]:
    """Network: all activity of one type (default REDEEM), newest first, via cursor pagination; never raises.

    ``stop_before_ts`` ends paging once rows are older than that epoch second.
    Returns ``{"rows": [...], "complete": bool, "error": str|None, "pages": n}``.
    """
    rows: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {
        "user": user, "type": activity_type, "limit": max(1, min(int(page_limit), 500)),
        "sort_by": "TIMESTAMP", "sort_direction": "DESC",
    }
    pages = 0
    try:
        while pages < max_pages:
            response = client.get(f"{base.rstrip('/')}/activity", params=params)
            pages += 1
            if response.status_code != 200:
                return {"rows": rows, "complete": False, "error": f"http_{response.status_code}", "pages": pages}
            body = response.json()
            page = v2_rows(body)
            rows.extend(page)
            if stop_before_ts is not None and page and all(
                int(r.get("timestamp") or 0) < int(stop_before_ts) for r in page[-1:]
            ):
                return {"rows": rows, "complete": True, "error": None, "pages": pages}
            cursor = v2_next_cursor(body)
            if not cursor or not page:
                return {"rows": rows, "complete": True, "error": None, "pages": pages}
            # Keep the filter on every page; parse_redeem_activity still drops
            # any non-REDEEM row a page might return.
            params = {**params, "cursor": cursor}
        return {"rows": rows, "complete": False, "error": "max_pages_reached", "pages": pages}
    except Exception as exc:
        return {"rows": rows, "complete": False, "error": f"{type(exc).__name__}: {exc}"[:200], "pages": pages}


def redeem_evidence_from_activity(rows: Iterable[Any], *, fetched_at: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse + dedupe by evidence key (a page boundary can repeat a row)."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        parsed = parse_redeem_activity(row, fetched_at=fetched_at)
        if parsed is not None and parsed["evidence_key"] not in out:
            out[parsed["evidence_key"]] = parsed
    return list(out.values())


# --- journal read helpers (shared by runtime worker and backfill tool) ----------------------

def journaled_evidence_keys(conn: Any) -> set:
    """Evidence keys already present in ``strategy_events`` (read-only)."""
    keys = set()
    for (key,) in conn.execute(
        "SELECT json_extract(payload_json, '$.evidence_key') FROM strategy_events "
        f"WHERE event_type IN ({','.join('?' * len(EVIDENCE_EVENTS))})",
        EVIDENCE_EVENTS,
    ):
        if key:
            keys.add(str(key))
    return keys

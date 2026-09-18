"""Small compatibility-free helpers for Polymarket's Data API v2 contract."""
from __future__ import annotations

from typing import Any


DATA_API_V2_BASE_URL = "https://data-api.polymarket.com/v2"


def v2_rows(payload: Any) -> list[dict[str, Any]]:
    """Return a v2 response page's rows, treating documented misses as empty."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def v2_next_cursor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        return None
    cursor = pagination.get("next_cursor")
    return str(cursor) if cursor else None

"""Collision-free client order ids shared by every strategy submit path."""
from __future__ import annotations

import time
import uuid

from nautilus_trader.model.identifiers import ClientOrderId

CLIENT_ORDER_ID_PREFIX = "BTC-15M-"


def new_client_order_id(kind: str) -> ClientOrderId:
    """Return ``BTC-15M-<KIND>-<epoch ms>-<12 hex>``.

    The millisecond component keeps ids sortable and human-readable; the random
    suffix makes two submits in the same millisecond (or across a restart that
    reuses a clock reading) distinct. The id is persisted with the order intent
    before submission, so it is the durable key linking intent, venue order and
    fill across a restart.
    """
    kind = str(kind).strip().upper()
    return ClientOrderId(f"{CLIENT_ORDER_ID_PREFIX}{kind}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}")


def is_strategy_client_order_id(client_order_id: object) -> bool:
    return str(client_order_id or "").startswith(CLIENT_ORDER_ID_PREFIX)

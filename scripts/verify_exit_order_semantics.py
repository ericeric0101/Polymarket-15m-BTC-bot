#!/usr/bin/env python3
"""Inspect the exact TIF semantics of the installed Polymarket adapter."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.polymarket_exit_capability import inspect_exit_order_capability


def main() -> int:
    result = inspect_exit_order_capability()
    print("requested_tif=" + result.requested_tif)
    print("limit_IOC_adapter_order_type=" + result.limit_ioc_order_type)
    print("market_adapter_order_type=" + result.market_order_type)
    print("market_orders_allow_partial_fill=" + str(result.market_orders_allow_partial_fill).lower())
    print("evidence=" + result.evidence)
    return 0 if result.market_order_type != "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inspect the exact TIF semantics of the installed Polymarket adapter."""
from __future__ import annotations

import sys
import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.polymarket_exit_capability import inspect_exit_order_capability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output")
    parser.add_argument("--markdown-output")
    args = parser.parse_args()
    result = inspect_exit_order_capability()
    print("requested_tif=" + result.requested_tif)
    print("limit_IOC_adapter_order_type=" + result.limit_ioc_order_type)
    print("market_adapter_order_type=" + result.market_order_type)
    print("market_orders_allow_partial_fill=" + str(result.market_orders_allow_partial_fill).lower())
    print("insufficient_depth_can_reject_entire_order=" + str(result.insufficient_depth_can_reject_entire_order).lower())
    print("limit_ioc_uses_adapter_tif_converter=" + str(result.limit_ioc_uses_adapter_tif_converter).lower())
    print("price_bounded_limit_ioc_avoids_fok_requirement=" + str(result.price_bounded_limit_ioc_avoids_fok_requirement).lower())
    print("aggressive_partial_exit_proven=" + str(result.aggressive_partial_exit_proven).lower())
    print("verdict=" + result.verdict)
    print("evidence=" + result.evidence)
    payload = result.as_dict()
    if args.json_output:
        path = Path(args.json_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        path = Path(args.markdown_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Aggressive SELL Adapter Capability", "",
                 f"- Verdict: **{result.verdict}**",
                 f"- Requested strategy TIF: `{result.requested_tif}`",
                 f"- Limit IOC conversion: `{result.limit_ioc_order_type}`",
                 f"- Generic market path venue type: `{result.market_order_type}`",
                 f"- Partial fill supported on market path: `{str(result.market_orders_allow_partial_fill).lower()}`",
                 f"- Insufficient visible/executable depth can reject the entire order: `{str(result.insufficient_depth_can_reject_entire_order).lower()}`",
                 f"- Limit IOC uses the installed adapter TIF converter: `{str(result.limit_ioc_uses_adapter_tif_converter).lower()}`",
                 f"- Price-bounded limit IOC avoids the FOK full-depth requirement: `{str(result.price_bounded_limit_ioc_avoids_fok_requirement).lower()}`",
                 "", "## Conclusion", "",
                 "The installed adapter does **not** prove safe partial aggressive SELL execution. "
                 "Its generic market path is venue-effective FOK, so insufficient depth may reject the whole order. "
                 "A limit IOC request converts to FAK, but this static capability check is not a live venue fill proof. "
                 "Live activation remains blocked pending adapter-level integration evidence for cancel, partial fill, residual inventory, and retry handling.",
                 "", "## Evidence", "", result.evidence, ""]
        path.write_text("\n".join(lines), encoding="utf-8")
    return 0 if result.market_order_type != "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())

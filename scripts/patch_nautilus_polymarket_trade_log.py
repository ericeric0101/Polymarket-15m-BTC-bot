#!/usr/bin/env python3
"""
Patch Nautilus Polymarket execution client trade log verbosity:
- replace full PolymarketUserTrade object dumps with compact one-line summaries.

Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "compact_trade_log_summary_v1"


def find_target_file(project_root: Path) -> Path:
    candidates = sorted(project_root.glob("venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/execution.py"))
    if not candidates:
        raise FileNotFoundError("Cannot find Nautilus Polymarket execution.py inside venv.")
    return candidates[-1]


def apply_patch_to_text(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False

    old = (
        "        trade_id = TradeId(msg.id)\n"
        "        trade_str = f\"Trade {trade_id}\"\n"
        "        log_msg = f\"{trade_str} {msg.status.value}: {msg}\"\n"
    )
    new = (
        "        trade_id = TradeId(msg.id)\n"
        "        trade_str = f\"Trade {trade_id}\"\n"
        f"        # {MARKER}\n"
        "        log_msg = (\n"
        "            f\"{trade_str} {msg.status.value}: market={msg.market} asset={msg.asset_id} \"\n"
        "            f\"side={msg.side.value} liq={msg.trader_side.value} price={msg.price} size={msg.size}\"\n"
        "        )\n"
    )

    if old not in text:
        return text, False
    patched = text.replace(old, new, 1)
    return patched, patched != text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Only check patch status.")
    parser.add_argument("--quiet", action="store_true", help="Minimal output.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    target = find_target_file(project_root)
    original = target.read_text(encoding="utf-8")
    patched, changed = apply_patch_to_text(original)

    already = MARKER in original
    if args.check:
        if not args.quiet:
            print(f"target={target}")
            print("status=patched" if already else "status=not_patched")
        return 0 if already else 1

    if changed:
        target.write_text(patched, encoding="utf-8")
        if not args.quiet:
            print(f"patched {target}")
    else:
        if not args.quiet:
            print(f"already patched {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


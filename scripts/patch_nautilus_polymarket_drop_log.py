#!/usr/bin/env python3
"""
Patch Nautilus Polymarket data client warning spam:
- throttle repeated "Dropping QuoteTick ..." warnings to once per 30s per instrument+reason.

Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


MARKER = "_log_drop_quote_warning_throttled"


def find_target_file(project_root: Path) -> Path:
    candidates = sorted(project_root.glob("venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/data.py"))
    if not candidates:
        raise FileNotFoundError("Cannot find Nautilus Polymarket data.py inside venv.")
    return candidates[-1]


def apply_patch_to_text(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False

    out = text

    # 1) import time
    if "import time\n" not in out:
        out = out.replace("import asyncio\n", "import asyncio\nimport time\n", 1)

    # 2) add cache fields
    anchor_cache = "        self._local_books: dict[InstrumentId, OrderBook] = {}\n"
    if anchor_cache in out and "_drop_quote_warn_last_ts" not in out:
        out = out.replace(
            anchor_cache,
            anchor_cache
            + "        self._drop_quote_warn_last_ts: dict[str, float] = {}\n"
            + "        self._drop_quote_warn_throttle_sec: float = 30.0\n",
            1,
        )

    # 3) helper method
    anchor_connect = "    async def _connect(self) -> None:\n"
    if anchor_connect in out and MARKER not in out:
        helper = (
            "    def _log_drop_quote_warning_throttled(self, instrument_id: InstrumentId, reason: str) -> None:\n"
            "        key = f\"{instrument_id}:{reason}\"\n"
            "        now_ts = time.time()\n"
            "        last_ts = self._drop_quote_warn_last_ts.get(key, 0.0)\n"
            "        if now_ts - last_ts < self._drop_quote_warn_throttle_sec:\n"
            "            return\n"
            "        self._drop_quote_warn_last_ts[key] = now_ts\n"
            "        self._log.warning(f\"Dropping QuoteTick for {instrument_id}: {reason}\")\n\n"
        )
        out = out.replace(anchor_connect, helper + anchor_connect, 1)

    # 4) replace warning #1 (snapshot)
    old_block_1 = (
        "            if quote is None:\n"
        "                self._log.warning(\n"
        "                    f\"Dropping QuoteTick for {instrument.id}: missing bid or ask prices in snapshot\",\n"
        "                )\n"
        "                return\n"
    )
    new_block_1 = (
        "            if quote is None:\n"
        "                self._log_drop_quote_warning_throttled(\n"
        "                    instrument.id,\n"
        "                    \"missing bid or ask prices in snapshot\",\n"
        "                )\n"
        "                return\n"
    )
    if old_block_1 in out:
        out = out.replace(old_block_1, new_block_1, 1)

    # 5) replace warning #2 (live quote)
    old_block_2 = (
        "                if self._config.drop_quotes_missing_side:\n"
        "                    self._log.warning(\n"
        "                        f\"Dropping QuoteTick for {instrument.id}: \"\n"
        "                        f\"bid_price={bid_price}, ask_price={ask_price}\",\n"
        "                    )\n"
        "                    return\n"
    )
    new_block_2 = (
        "                if self._config.drop_quotes_missing_side:\n"
        "                    self._log_drop_quote_warning_throttled(\n"
        "                        instrument.id,\n"
        "                        f\"bid_price={bid_price}, ask_price={ask_price}\",\n"
        "                    )\n"
        "                    return\n"
    )
    if old_block_2 in out:
        out = out.replace(old_block_2, new_block_2, 1)

    changed = out != text
    return out, changed


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


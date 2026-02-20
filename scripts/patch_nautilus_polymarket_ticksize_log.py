#!/usr/bin/env python3
"""
Patch Nautilus Polymarket data client tick-size warning spam:
- throttle repeated "Instrument tick size changed" warnings.
- log compact message instead of dumping full instrument payload.

Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "_log_tick_size_warning_throttled"


def find_target_file(project_root: Path) -> Path:
    candidates = sorted(project_root.glob("venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/data.py"))
    if not candidates:
        raise FileNotFoundError("Cannot find Nautilus Polymarket data.py inside venv.")
    return candidates[-1]


def apply_patch_to_text(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False

    out = text

    # Ensure we can use time.time() for throttling.
    if "import time\n" not in out:
        out = out.replace("import asyncio\n", "import asyncio\nimport time\n", 1)

    # Add tick-size warning caches.
    anchor_cache = "        self._drop_quote_warn_throttle_sec: float = 30.0\n"
    if anchor_cache in out and "_tick_size_warn_last_ts" not in out:
        out = out.replace(
            anchor_cache,
            anchor_cache
            + "        self._tick_size_warn_last_ts: dict[str, float] = {}\n"
            + "        self._tick_size_warn_throttle_sec: float = 60.0\n",
            1,
        )

    # Add helper method near existing log helper.
    anchor_connect = "    async def _connect(self) -> None:\n"
    if anchor_connect in out:
        helper = (
            "    def _log_tick_size_warning_throttled(self, instrument: BinaryOption, change: PolymarketTickSizeChange) -> None:\n"
            "        key = str(instrument.id)\n"
            "        now_ts = time.time()\n"
            "        last_ts = self._tick_size_warn_last_ts.get(key, 0.0)\n"
            "        if now_ts - last_ts < self._tick_size_warn_throttle_sec:\n"
            "            return\n"
            "        self._tick_size_warn_last_ts[key] = now_ts\n"
            "        ws_tick = getattr(change, \"tick_size\", None)\n"
            "        if ws_tick is None:\n"
            "            ws_tick = getattr(change, \"min_tick_size\", None)\n"
            "        self._log.warning(\n"
            "            f\"Instrument tick size changed: id={instrument.id} price_increment={instrument.price_increment} ws_tick={ws_tick}\",\n"
            "        )\n\n"
        )
        out = out.replace(anchor_connect, helper + anchor_connect, 1)

    old_line = '        self._log.warning(f"Instrument tick size changed: {instrument}")\n'
    new_line = "        self._log_tick_size_warning_throttled(instrument=instrument, change=ws_message)\n"
    if old_line in out:
        out = out.replace(old_line, new_line, 1)

    return out, out != text


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


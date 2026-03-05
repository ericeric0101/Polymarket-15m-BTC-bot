#!/usr/bin/env python3
"""
Patch Nautilus Polymarket execution client:
1) Throttle "Cannot cancel on Polymarket: no VenueOrderId" to warn only once per order.
2) Avoid calling `generate_order_canceled` if the order is already canceled to prevent `InvalidStateTrigger: CANCELED -> CANCELED` spew in the engine.
"""

import argparse
from pathlib import Path

MARKER_CANCEL_WARNING = "_log_cancel_no_venue_id_throttled"
MARKER_ALREADY_CANCELED = "_is_order_already_canceled"

def find_target_file(project_root: Path) -> Path:
    candidates = sorted(project_root.glob("venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/execution.py"))
    if not candidates:
        raise FileNotFoundError("Cannot find Nautilus Polymarket execution.py inside venv.")
    return candidates[-1]

def apply_patch_to_text(text: str) -> tuple[str, bool]:
    out = text

    # Add cache map in init if not there
    anchor_cache = "        self._ack_events_trade: dict[VenueOrderId, asyncio.Event] = {}\n"
    if anchor_cache in out and "self._warned_no_venue_id" not in out:
        out = out.replace(
            anchor_cache,
            anchor_cache + "        self._warned_no_venue_id: set[str] = set()\n"
        )

    # 1. Patch `cancel_order` to throttle warnings
    old_warn = '            self._log.warning("Cannot cancel on Polymarket: no VenueOrderId")\n'
    if old_warn in out:
        new_warn = (
            "            # " + MARKER_CANCEL_WARNING + "\n"
            "            client_id_str = str(order.client_order_id.value)\n"
            "            warned = getattr(self, '_warned_no_venue_id', None)\n"
            "            if warned is None:\n"
            "                warned = set()\n"
            "                self._warned_no_venue_id = warned\n"
            "            if client_id_str not in warned:\n"
            "                warned.add(client_id_str)\n"
            "                self._log.warning(f\"Cannot cancel on Polymarket: no VenueOrderId for {client_id_str}\")\n"
        )
        out = out.replace(old_warn, new_warn, 1)

    # 2. Patch `_handle_ws_order_msg` CANCELLATION branch
    old_cancel = (
        "            case PolymarketEventType.CANCELLATION:\n"
        "                self.generate_order_canceled(\n"
        "                    strategy_id=strategy_id,\n"
        "                    instrument_id=instrument_id,\n"
        "                    client_order_id=client_order_id,\n"
        "                    venue_order_id=venue_order_id,\n"
        "                    ts_event=millis_to_nanos(int(msg.timestamp)),\n"
        "                )\n"
    )
    if old_cancel in out:
        new_cancel = (
            "            case PolymarketEventType.CANCELLATION:\n"
            "                # " + MARKER_ALREADY_CANCELED + "\n"
            "                order_obj = self._cache.order(client_order_id) if client_order_id else None\n"
            "                if order_obj is None or not order_obj.is_canceled:\n"
            "                    self.generate_order_canceled(\n"
            "                        strategy_id=strategy_id,\n"
            "                        instrument_id=instrument_id,\n"
            "                        client_order_id=client_order_id,\n"
            "                        venue_order_id=venue_order_id,\n"
            "                        ts_event=millis_to_nanos(int(msg.timestamp)),\n"
            "                    )\n"
            "                else:\n"
            "                    self._log.debug(f\"Order {client_order_id!r} already canceled - skipping duplicate cancellation event\")\n"
        )
        out = out.replace(old_cancel, new_cancel, 1)

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

    already = MARKER_ALREADY_CANCELED in original and MARKER_CANCEL_WARNING in original
    if args.check:
        if not args.quiet:
            print(f"target={target}")
            print("status=patched" if already else "status=not_patched")
        return 0 if already else 1

    if changed:
        patched_ok = MARKER_ALREADY_CANCELED in patched and MARKER_CANCEL_WARNING in patched
        if not patched_ok:
            if not args.quiet:
                print("patch_failed: expected markers missing after patch attempt")
            return 2
        target.write_text(patched, encoding="utf-8")
        if not args.quiet:
            print(f"patched {target}")
    else:
        if already:
            if not args.quiet:
                print(f"already patched {target}")
            return 0
        if not args.quiet:
            print(f"patch_not_applied: expected patterns not found in {target}")
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Patch Nautilus Polymarket user trade parsing:
1) Fall back from maker-order numeric fields to trade-level fields when
   Polymarket sends an empty/invalid maker price, matched_amount, or fee_rate_bps.
"""

import argparse
from pathlib import Path

MARKER_SAFE_DECIMAL = "_polymarket_safe_decimal_fallback"


def find_target_file(project_root: Path) -> Path:
    candidates = sorted(
        list(project_root.glob(".venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/schemas/user.py"))
        + list(project_root.glob("venv/lib/python*/site-packages/nautilus_trader/adapters/polymarket/schemas/user.py"))
    )
    if not candidates:
        raise FileNotFoundError("Cannot find Nautilus Polymarket schemas/user.py inside venv.")
    return candidates[-1]


def apply_patch_to_text(text: str) -> tuple[str, bool]:
    out = text

    if "from decimal import Decimal\n" in out and "InvalidOperation" not in out:
        out = out.replace("from decimal import Decimal\n", "from decimal import Decimal, InvalidOperation\n", 1)

    helper_anchor = "\n\nclass PolymarketUserOrder"
    helper = f'''

def _safe_decimal(value: object, fallback: object | None = None) -> Decimal:
    # {MARKER_SAFE_DECIMAL}
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        if fallback is None:
            raise
    try:
        return Decimal(str(fallback))
    except (InvalidOperation, TypeError, ValueError):
        raise
'''
    if helper_anchor in out and MARKER_SAFE_DECIMAL not in out:
        out = out.replace(helper_anchor, helper + helper_anchor, 1)

    replacements = {
        "            return Decimal(self.price)\n": "            return _safe_decimal(self.price)\n",
        "            return Decimal(order.price)\n": "            return _safe_decimal(order.price, self.price)\n",
        "            return Decimal(self.size)\n": "            return _safe_decimal(self.size)\n",
        "            return Decimal(order.matched_amount)\n": "            return _safe_decimal(order.matched_amount, self.size)\n",
        "            return Decimal(self.fee_rate_bps)\n": "            return _safe_decimal(self.fee_rate_bps, 0)\n",
        "            return Decimal(order.fee_rate_bps)\n": "            return _safe_decimal(order.fee_rate_bps, self.fee_rate_bps or 0)\n",
    }
    for old, new in replacements.items():
        if old in out:
            out = out.replace(old, new, 1)

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

    already = MARKER_SAFE_DECIMAL in original
    if args.check:
        if not args.quiet:
            print(f"target={target}")
            print("status=patched" if already else "status=not_patched")
        return 0 if already else 1

    if changed:
        if MARKER_SAFE_DECIMAL not in patched:
            if not args.quiet:
                print("patch_failed: expected marker missing after patch attempt")
            return 2
        target.write_text(patched, encoding="utf-8")
        if not args.quiet:
            print(f"patched {target}")
        return 0

    if already:
        if not args.quiet:
            print(f"already patched {target}")
        return 0
    if not args.quiet:
        print(f"patch_not_applied: expected patterns not found in {target}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

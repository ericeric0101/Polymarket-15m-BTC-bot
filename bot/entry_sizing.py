"""Canonical share-based entry sizing shared by LIVE and DRY-RUN.

Rule ``share_v1`` (strict boundary, as introduced in 8de45cb):
    normalized price <= 0.70 -> 10.0 shares
    normalized price  > 0.70 ->  5.5 shares

The price is normalized to the venue tick *before* the comparison, so float
noise such as ``0.7000000000000001`` or ``0.6999999999999999`` lands in the
``<= 0.70`` bucket.  The share target is an upper bound: the existing L2/risk
cap and its quality multipliers may only reduce it.  A final quantity below
``MIN_ENTRY_SHARES`` is skipped, never rounded up.

``MIN_ENTRY_SHARES`` is the bot's minimum *requested entry* quantity.  It is
not the venue's 5-share minimum SELL size; the 0.5-share difference is a
deliberate sellability buffer for the 0.1% balance haircut and 2-decimal
round-down applied before every SELL.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SIZING_RULE_VERSION = "share_v1_2026-10-09"
SIZING_MODE = "share_target"

LOW_PRICE_TARGET_SHARES = Decimal("10.0")
HIGH_PRICE_TARGET_SHARES = Decimal("5.5")
HIGH_PRICE_THRESHOLD = Decimal("0.70")
MIN_ENTRY_SHARES = Decimal("5.5")

# Versioned bounds for the final resolved configuration.
MAX_TARGET_SHARES = Decimal("10.0")
MIN_TARGET_SHARES = Decimal("5.5")

DEFAULT_PRICE_TICK = Decimal("0.01")
# Existing 10% collateral buffer, now applied to the exact entry notional.
BALANCE_BUFFER_MULTIPLIER = Decimal("1.1")
VENUE_SIZE_STEP = Decimal("0.01")

SKIP_BELOW_MIN_ENTRY_SHARES = "entry_qty_below_min_entry_shares"
SKIP_INVALID_PRICE = "entry_price_invalid"


def _to_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def normalize_entry_price(price: Any, tick: Any = DEFAULT_PRICE_TICK) -> Decimal | None:
    """Round a price to the nearest venue tick (half-up); ``None`` if unusable."""
    value = _to_decimal(price)
    step = _to_decimal(tick)
    if value is None or value <= 0:
        return None
    if step is None or step <= 0:
        step = DEFAULT_PRICE_TICK
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def round_down_to_venue_size(quantity: Any) -> Decimal:
    value = _to_decimal(quantity)
    if value is None or value <= 0:
        return Decimal("0")
    return value.quantize(VENUE_SIZE_STEP, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class EntrySizingRule:
    low_price_target_shares: Decimal = LOW_PRICE_TARGET_SHARES
    high_price_target_shares: Decimal = HIGH_PRICE_TARGET_SHARES
    high_price_threshold: Decimal = HIGH_PRICE_THRESHOLD

    def as_payload(self) -> dict[str, Any]:
        return {
            "sizing_mode": SIZING_MODE,
            "sizing_version": SIZING_RULE_VERSION,
            "low_price_target_shares": float(self.low_price_target_shares),
            "high_price_target_shares": float(self.high_price_target_shares),
            "high_price_threshold": float(self.high_price_threshold),
            "min_entry_shares": float(MIN_ENTRY_SHARES),
        }


CANONICAL_ENTRY_SIZING_RULE = EntrySizingRule()


def entry_sizing_violations(rule: EntrySizingRule) -> list[str]:
    """Return why a resolved rule is outside the versioned bounds (empty = valid)."""
    problems = []
    for name in ("low_price_target_shares", "high_price_target_shares"):
        value = getattr(rule, name)
        if not (MIN_TARGET_SHARES <= value <= MAX_TARGET_SHARES):
            problems.append(f"{name}={value} outside [{MIN_TARGET_SHARES}, {MAX_TARGET_SHARES}]")
    if rule.high_price_target_shares > rule.low_price_target_shares:
        problems.append("high_price_target_shares exceeds low_price_target_shares")
    # Above the boundary an override may only shrink exposure: a larger
    # high-price target would raise the per-entry notional above the
    # canonical worst case (10 x 0.70 = $7.00).
    if rule.high_price_target_shares > HIGH_PRICE_TARGET_SHARES:
        problems.append(
            f"high_price_target_shares={rule.high_price_target_shares} exceeds canonical {HIGH_PRICE_TARGET_SHARES}"
        )
    if rule.high_price_threshold != HIGH_PRICE_THRESHOLD:
        problems.append(f"high_price_threshold={rule.high_price_threshold} != {HIGH_PRICE_THRESHOLD}")
    return problems


def resolve_entry_sizing_rule(
    *, low_price_target_shares: Any, high_price_target_shares: Any, high_price_threshold: Any,
) -> tuple[EntrySizingRule, list[str]]:
    """Validate the final resolved values; out-of-bounds input falls back to canonical.

    The returned rule is always within bounds, so an invalid override can never
    produce larger sizing.  Callers decide whether violations refuse startup.
    """
    values = [_to_decimal(v) for v in (low_price_target_shares, high_price_target_shares, high_price_threshold)]
    if any(v is None for v in values):
        return CANONICAL_ENTRY_SIZING_RULE, ["entry sizing value is not a finite number"]
    candidate = EntrySizingRule(*values)
    problems = entry_sizing_violations(candidate)
    return (CANONICAL_ENTRY_SIZING_RULE if problems else candidate), problems


def target_entry_shares(
    entry_price: Any, *, tick: Any = DEFAULT_PRICE_TICK, rule: EntrySizingRule = CANONICAL_ENTRY_SIZING_RULE,
) -> Decimal:
    """Base share target for a BUY at ``entry_price``; 0 for an unusable price."""
    price = normalize_entry_price(entry_price, tick)
    if price is None:
        return Decimal("0")
    if price <= rule.high_price_threshold:
        return rule.low_price_target_shares
    return rule.high_price_target_shares


@dataclass(frozen=True)
class EntrySizingDecision:
    normalized_price: Decimal | None
    base_target_shares: Decimal
    cap_quantity: Decimal | None
    size_multiplier: Decimal
    final_quantity: Decimal
    skip_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skip_reason)

    @property
    def clipped(self) -> bool:
        return self.final_quantity < self.base_target_shares

    @property
    def expected_notional_usdc(self) -> Decimal:
        return self.final_quantity * (self.normalized_price or Decimal("0"))

    def as_payload(self) -> dict[str, Any]:
        return {
            "sizing_mode": SIZING_MODE,
            "sizing_version": SIZING_RULE_VERSION,
            "entry_price": float(self.normalized_price) if self.normalized_price is not None else None,
            "requested_shares": float(self.base_target_shares),
            "cap_quantity": float(self.cap_quantity) if self.cap_quantity is not None else None,
            "size_multiplier": float(self.size_multiplier),
            "final_normalized_shares": float(self.final_quantity),
            "expected_notional_usdc": float(self.expected_notional_usdc),
            "clipped": self.clipped,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


def size_entry(
    *,
    entry_price: Any,
    tick: Any = DEFAULT_PRICE_TICK,
    cap_quantity: Any = None,
    size_multiplier: Any = Decimal("1"),
    rule: EntrySizingRule = CANONICAL_ENTRY_SIZING_RULE,
) -> EntrySizingDecision:
    """The single production entry-sizing decision (LIVE and DRY-RUN).

    ``cap_quantity`` is the existing L2/risk/inventory cap, which already
    carries the quality multipliers.  Without a cap, multipliers apply to the
    share target directly.  Either way the result can only shrink, is rounded
    down to the venue size step, and is skipped below ``MIN_ENTRY_SHARES``.
    """
    price = normalize_entry_price(entry_price, tick)
    multiplier = max(Decimal("0"), _to_decimal(size_multiplier) or Decimal("0"))
    cap = _to_decimal(cap_quantity) if cap_quantity is not None else None
    if price is None:
        return EntrySizingDecision(None, Decimal("0"), cap, multiplier, Decimal("0"), SKIP_INVALID_PRICE)
    base = target_entry_shares(price, tick=tick, rule=rule)
    raw = min(base, max(Decimal("0"), cap)) if cap is not None else base * min(Decimal("1"), multiplier)
    final = round_down_to_venue_size(raw)
    reason = SKIP_BELOW_MIN_ENTRY_SHARES if final < MIN_ENTRY_SHARES else ""
    return EntrySizingDecision(price, base, cap, multiplier, final, reason)


def is_canonical_rule(rule: EntrySizingRule) -> bool:
    return rule == CANONICAL_ENTRY_SIZING_RULE


def startup_sizing_summary(rule: EntrySizingRule, violations: list[str]) -> str:
    """One secret-free log line describing the effective sizing rule."""
    return (
        f"Entry sizing: version={SIZING_RULE_VERSION} mode={SIZING_MODE} "
        f"<= {rule.high_price_threshold} -> {rule.low_price_target_shares} sh, "
        f"> {rule.high_price_threshold} -> {rule.high_price_target_shares} sh, "
        f"min_entry={MIN_ENTRY_SHARES} sh, violations={len(violations)}"
    )


def log_entry_sizing_skip_once(
    logged: set, *, market: str, decision: EntrySizingDecision, limiting_factor: str, log_fn: Any,
) -> bool:
    """Log one line per (market, reason); return True when a line was emitted."""
    key = (str(market or ""), decision.skip_reason)
    if key in logged:
        return False
    if len(logged) >= 512:
        logged.clear()
    logged.add(key)
    log_fn(
        f"Skip entry ({decision.skip_reason}): market={market} price={decision.normalized_price} "
        f"base_target={decision.base_target_shares} cap={decision.cap_quantity} "
        f"limit={limiting_factor or 'none'} multiplier={decision.size_multiplier} "
        f"final={decision.final_quantity} min_entry={MIN_ENTRY_SHARES} version={SIZING_RULE_VERSION}"
    )
    return True

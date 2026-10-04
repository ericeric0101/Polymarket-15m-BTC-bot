"""Pure, offline-safe helpers for position lifecycle research.

The functions in this module deliberately have no strategy, database, or
execution imports.  They provide one stable identity and deterministic
feature extraction for research joins only; callers must never use their
output as trading authority.
"""
from __future__ import annotations

from typing import Any, Iterable


SIGMA_CROSSES: tuple[tuple[float, str], ...] = (
    (2.0, "sigma_cross_2_ts"),
    (1.5, "sigma_cross_1_5_ts"),
    (1.0, "sigma_cross_1_ts"),
    (0.75, "sigma_cross_0_75_ts"),
    (0.5, "sigma_cross_0_5_ts"),
    (0.25, "sigma_cross_0_25_ts"),
)
FLIP_P_CROSSES: tuple[tuple[float, str], ...] = (
    (0.10, "flip_p_10_ts"),
    (0.15, "flip_p_15_ts"),
    (0.20, "flip_p_20_ts"),
    (0.30, "flip_p_30_ts"),
    (0.40, "flip_p_40_ts"),
    (0.50, "flip_p_50_ts"),
)


def position_lifecycle_id(*, market_slug: str, instrument_id: str, entry_client_order_id: str) -> str:
    """Return the durable, deterministic identity of one opened position.

    The entry fill's client order id is immutable and unique for a process;
    slug and instrument retain cross-run provenance.  This is intentionally
    not derived from a timestamp, which would be vulnerable to clock and
    rollover ambiguity.
    """
    return "|".join((str(market_slug), str(instrument_id), str(entry_client_order_id)))


def fill_lifecycle_metadata(*, market_slug: str, instrument_id: str, client_order_id: str,
                            side: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Attach identity to canonical inventory transitions without changing accounting.

    Partial fills and scale-ins retain the first opening order's identity.
    Unknown recovered identities remain unknown rather than being invented.
    """
    pre_qty = number(before.get("qty")) or 0.0
    post_qty = number(after.get("qty")) or 0.0
    fresh = side.lower() == "buy" and pre_qty <= 0 < post_qty
    if fresh:
        after["position_lifecycle_id"] = position_lifecycle_id(
            market_slug=market_slug, instrument_id=instrument_id, entry_client_order_id=client_order_id,
        )
        after["entry_client_order_id"] = str(client_order_id)
    identity = after.get("position_lifecycle_id") if side.lower() == "buy" else before.get("position_lifecycle_id")
    entry_id = after.get("entry_client_order_id") if side.lower() == "buy" else before.get("entry_client_order_id")
    if post_qty <= 0:
        after.pop("position_lifecycle_id", None)
        after.pop("entry_client_order_id", None)
    return {"position_lifecycle_id": identity, "entry_client_order_id": entry_id,
            "position_qty_before": pre_qty, "position_qty_after": post_qty,
            "fresh_position_entry": fresh}


def number(value: Any) -> float | None:
    try:
        output = float(value)
        return output if output == output and output not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def held_side_probability(row: dict[str, Any], side: str) -> float | None:
    """Return fresh analytic probability for the held outcome only."""
    if row.get("p_ex_fresh") is not True:
        return None
    side = str(side).upper()
    return number(row.get("p_up_ex_market" if side == "UP" else "p_down_ex_market" if side == "DOWN" else None))


def adverse_btc(row: dict[str, Any], *, side: str, horizon_sec: int) -> bool | None:
    """Interpret an already-captured BTC return against a held side."""
    value = number(row.get(f"btc_return_{int(horizon_sec)}s_bps"))
    if value is None or str(side).upper() not in {"UP", "DOWN"}:
        return None
    return value < 0 if str(side).upper() == "UP" else value > 0


def first_crossings(rows: Iterable[dict[str, Any]], *, side: str) -> dict[str, float | None]:
    """Extract first adverse structural crossings from synchronized snapshots.

    ``required_move_sigma`` is an absolute structural distance, so lower is
    more fragile.  Opposite analytic probability is taken only when its
    provenance is fresh.  Unavailable inputs remain null.
    """
    ordered = sorted(rows, key=lambda row: number(row.get("snapshot_ts")) or -1.0)
    result: dict[str, float | None] = {name: None for _, name in SIGMA_CROSSES + FLIP_P_CROSSES}
    result.update({f"first_adverse_btc{seconds}_ts": None for seconds in (5, 10, 30)})
    result["first_settlement_state_flip_ts"] = None
    side = str(side).upper()
    for row in ordered:
        ts = number(row.get("snapshot_ts"))
        if ts is None:
            continue
        sigma = number(row.get("required_move_sigma"))
        if sigma is not None:
            sigma = abs(sigma)
            for threshold, name in SIGMA_CROSSES:
                if result[name] is None and sigma <= threshold:
                    result[name] = ts
        probability = held_side_probability(row, side)
        if probability is not None:
            flip_probability = 1.0 - probability
            for threshold, name in FLIP_P_CROSSES:
                if result[name] is None and flip_probability >= threshold:
                    result[name] = ts
        for seconds in (5, 10, 30):
            name = f"first_adverse_btc{seconds}_ts"
            if result[name] is None and adverse_btc(row, side=side, horizon_sec=seconds) is True:
                result[name] = ts
        leader = str(row.get("settlement_state_side") or "").upper()
        if result["first_settlement_state_flip_ts"] is None and leader in {"UP", "DOWN"} and leader != side:
            result["first_settlement_state_flip_ts"] = ts
    return result

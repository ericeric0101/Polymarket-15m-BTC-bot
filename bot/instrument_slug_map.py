"""Instrument -> market-slug attribution for fills that outlive a market switch.

A resting SELL of market N can fill after the strategy has rolled over to
market N+1.  The journal must book that fill to N, not to the current market.
Each market switch registers its outcome instruments here; the map is bounded
and in-memory (a fill arriving after a process restart falls back to the
current market and is labelled as such by the caller).  Pure bookkeeping: no
execution authority, never raises.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

MAX_TRACKED_INSTRUMENTS = 64


def register_market_instruments(strategy: Any, slug: str, instruments: Iterable[Any]) -> None:
    try:
        if not slug:
            return
        mapping = getattr(strategy, "instrument_slug_by_key", None)
        if not isinstance(mapping, dict):
            mapping = {}
            strategy.instrument_slug_by_key = mapping
        for inst in instruments or ():
            if inst is None:
                continue
            key = str(inst)
            mapping.pop(key, None)  # re-insert so the newest registration is the youngest
            mapping[key] = str(slug)
        while len(mapping) > MAX_TRACKED_INSTRUMENTS:
            mapping.pop(next(iter(mapping)))
    except Exception:
        return


def slug_for_instrument(strategy: Any, instrument_key: Any) -> Optional[str]:
    try:
        mapping = getattr(strategy, "instrument_slug_by_key", None)
        if not isinstance(mapping, dict) or instrument_key is None:
            return None
        value = mapping.get(str(instrument_key))
        return str(value) if value else None
    except Exception:
        return None

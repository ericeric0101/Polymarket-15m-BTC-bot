"""Future live-handoff boundary; intentionally disabled in this release."""
from __future__ import annotations


def handoff_confirmed_candidate(*_args, **_kwargs) -> bool:
    """Never touch orders until a separately approved live policy exists."""
    return False

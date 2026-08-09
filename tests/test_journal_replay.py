import pytest

from bot.journal_replay import (
    candidate_from_payload,
    dry_run_fill_from_payload,
    replay_candidates,
    select_one_candidate_per_market,
)


def test_journal_replay_selects_one_candidate_and_scores_binary_settlement():
    first = candidate_from_payload(
        "2026-01-01T00:01:00+00:00",
        {"slug": "market-a", "shadow_candidate_side": "BUY_UP", "ask_up": 0.60},
    )
    later = candidate_from_payload(
        "2026-01-01T00:02:00+00:00",
        {"slug": "market-a", "shadow_candidate_side": "BUY_DOWN", "ask_down": 0.40},
    )
    assert first is not None and later is not None

    selected = select_one_candidate_per_market([first, later], selection="first")
    results = replay_candidates(selected, {"market-a": "UP"}, default_qty=6)

    assert len(results) == 1
    assert results[0].won is True
    assert results[0].pnl_per_share == 0.40
    assert results[0].qty == 6
    assert results[0].pnl == pytest.approx(2.40)


def test_journal_replay_rejects_incomplete_candidate_payload():
    assert candidate_from_payload("now", {"slug": "market", "shadow_candidate_side": "BUY_UP"}) is None


def test_dry_run_fill_candidate_uses_filled_limit_price_and_outcome_side():
    candidate = dry_run_fill_from_payload(
        "now",
        {"slug": "market", "side": "DOWN", "entry_price": 0.64},
        side="DOWN",
        price=0.65,
        qty=5.4,
    )

    assert candidate is not None
    assert candidate.slug == "market"
    assert candidate.side == "BUY_DOWN"
    assert candidate.entry_price == 0.64
    assert candidate.qty == 5.4


def test_dry_run_fill_candidate_rejects_unusable_fill_record():
    assert dry_run_fill_from_payload(
        "now",
        {"slug": "market", "side": "UP"},
        side="UP",
        price=None,
        qty=5.4,
    ) is None

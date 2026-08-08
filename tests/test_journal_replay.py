from bot.journal_replay import candidate_from_payload, replay_candidates, select_one_candidate_per_market


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
    results = replay_candidates(selected, {"market-a": "UP"})

    assert len(results) == 1
    assert results[0].won is True
    assert results[0].pnl_per_share == 0.40


def test_journal_replay_rejects_incomplete_candidate_payload():
    assert candidate_from_payload("now", {"slug": "market", "shadow_candidate_side": "BUY_UP"}) is None

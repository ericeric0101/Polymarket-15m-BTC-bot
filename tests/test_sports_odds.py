import pytest

from bot.research.sports_odds import (
    TokenFair,
    fair_from_odds,
    match_event,
    name_match_level,
    normalize_tokens,
    parse_pm_event,
    taker_fee_per_share,
    token_edge,
    token_fairs,
)


def _team(name, alias="", ordering=""):
    return {"name": name, "alias": alias, "ordering": ordering}


def _ml(question, outcomes, tokens, rate=0.03):
    import json
    return {"id": question[:8], "question": question, "sportsMarketType": "moneyline",
            "outcomes": json.dumps(outcomes), "clobTokenIds": json.dumps(tokens),
            "feesEnabled": True, "feeSchedule": {"rate": rate, "exponent": 1}, "acceptingOrders": True}


def _odds_event(home, away, t, prices, book="pinnacle"):
    return {"id": f"{home}-{away}", "home_team": home, "away_team": away, "commence_time": t,
            "bookmakers": [{"key": book, "markets": [{"key": "h2h", "last_update": t,
                            "outcomes": [{"name": k, "price": v} for k, v in prices.items()]}]}]}


SOCCER_EVENT = {
    "id": "1", "slug": "epl-eve-che-2026-10-17", "title": "Everton FC vs. Chelsea FC",
    "startTime": "2026-10-17T11:30:00Z",
    "teams": [_team("Everton FC", "Everton", "home"), _team("Chelsea FC", "Chelsea", "away")],
    "markets": [
        _ml("Will Everton FC win on 2026-10-17?", ["Yes", "No"], ["e_y", "e_n"]),
        _ml("Will Everton FC vs. Chelsea FC end in a draw?", ["Yes", "No"], ["d_y", "d_n"]),
        _ml("Will Chelsea FC win on 2026-10-17?", ["Yes", "No"], ["c_y", "c_n"]),
        {"sportsMarketType": "total_corners", "outcomes": "[]", "clobTokenIds": "[]"},
    ],
}
NHL_EVENT = {
    "id": "2", "slug": "nhl-nj-wsh-2026-10-17", "title": "Devils vs. Capitals",
    "startTime": "2026-10-17T17:00:00Z",
    "teams": [_team("Devils", "Devils", "away"), _team("Capitals", "Capitals", "home")],
    "markets": [_ml("Devils vs. Capitals", ["Devils", "Capitals"], ["nj", "wsh"], rate=0.05)],
}


# --- names ---------------------------------------------------------------------------------

def test_normalize_drops_club_tokens_and_accents():
    assert normalize_tokens("Atlético Madrid CF") == ("atletico", "madrid")
    assert normalize_tokens("Brighton & Hove Albion") == ("brighton", "and", "hove", "albion")


@pytest.mark.parametrize("pm, book, level", [
    (("Chelsea FC", "Chelsea"), "Chelsea", "exact"),
    (("Devils",), "New Jersey Devils", "tokens"),
    (("Brighton and Hove Albion FC",), "Brighton and Hove Albion", "exact"),
    (("Mike Malott",), "Mike Malott", "exact"),
    (("Manchester City FC", "Man City"), "Manchester United", None),
    (("City",), "Manchester City", None),           # generic-only never matches
    (("United",), "Newcastle United", None),
    (("Wolves",), "Wolverhampton Wanderers", "exact"),  # league alias
])
def test_name_match_level(pm, book, level):
    assert name_match_level(pm, book, "epl") == level


def test_league_alias_does_not_leak_to_other_leagues():
    assert name_match_level(("Wolves",), "Wolverhampton Wanderers", "nhl") is None


# --- event parsing + matching ---------------------------------------------------------------

def test_parse_pm_event_keeps_only_moneyline_and_skips_live():
    ev = parse_pm_event(SOCCER_EVENT, "epl")
    assert ev is not None and len(ev.markets) == 3
    assert ev.start.isoformat() == "2026-10-17T11:30:00+00:00"
    assert parse_pm_event({**SOCCER_EVENT, "live": True}, "epl") is None


def test_match_requires_time_window_and_both_names():
    ev = parse_pm_event(NHL_EVENT, "nhl")
    good = _odds_event("Washington Capitals", "New Jersey Devils", "2026-10-17T17:00:00Z",
                       {"Washington Capitals": 1.85, "New Jersey Devils": 2.04})
    other_time = _odds_event("Washington Capitals", "New Jersey Devils", "2026-10-18T17:00:00Z",
                             {"Washington Capitals": 1.85, "New Jersey Devils": 2.04})
    m = match_event(ev, [other_time, good], tol_min=20)
    assert m.status == "matched" and m.odds_event is good
    assert m.team_map == {"Devils": "New Jersey Devils", "Capitals": "Washington Capitals"}
    assert m.level == "tokens" and m.home_away_consistent is True
    assert match_event(ev, [other_time], tol_min=20).status == "no_candidate"
    one_team = _odds_event("Washington Capitals", "Boston Bruins", "2026-10-17T17:00:00Z", {})
    assert match_event(ev, [one_team], tol_min=20).status == "no_name_match"


def test_two_matching_candidates_is_ambiguous_not_a_guess():
    ev = parse_pm_event(NHL_EVENT, "nhl")
    a = _odds_event("Washington Capitals", "New Jersey Devils", "2026-10-17T17:00:00Z", {})
    b = _odds_event("Washington Capitals", "New Jersey Devils", "2026-10-17T17:10:00Z", {})
    assert match_event(ev, [a, b], tol_min=20).status == "ambiguous"


# --- fair value + token mapping -------------------------------------------------------------

def test_three_way_fair_and_yes_no_tokens():
    ev = parse_pm_event(SOCCER_EVENT, "epl")
    book = _odds_event("Everton", "Chelsea", "2026-10-17T11:30:00Z",
                       {"Everton": 2.97, "Chelsea": 2.41, "Draw": 3.63})
    m = match_event(ev, [book], tol_min=20)
    fair = fair_from_odds(book, three_way=True)
    assert sum(fair.probs.values()) == pytest.approx(1.0)
    assert fair.overround == pytest.approx(1 / 2.97 + 1 / 2.41 + 1 / 3.63)
    tf = {t.token_id: t for t in token_fairs(ev, m, fair)}
    assert set(tf) == {"e_y", "e_n", "d_y", "d_n", "c_y", "c_n"}
    assert tf["e_y"].fair == pytest.approx(fair.probs["Everton"])
    assert tf["e_n"].fair == pytest.approx(1 - fair.probs["Everton"])
    assert tf["d_y"].fair == pytest.approx(fair.probs["Draw"])


def test_two_way_outcome_tokens():
    ev = parse_pm_event(NHL_EVENT, "nhl")
    book = _odds_event("Washington Capitals", "New Jersey Devils", "2026-10-17T17:00:00Z",
                       {"Washington Capitals": 1.85, "New Jersey Devils": 2.04})
    m = match_event(ev, [book], tol_min=20)
    tf = {t.token_id: t for t in token_fairs(ev, m, fair_from_odds(book, three_way=False))}
    assert tf["nj"].fair + tf["wsh"].fair == pytest.approx(1.0)
    assert tf["nj"].fee_rate == 0.05


def test_fair_rejects_wrong_shape_and_wide_book():
    two = _odds_event("A", "B", "2026-10-17T17:00:00Z", {"A": 1.9, "B": 1.9})
    assert fair_from_odds(two, three_way=True) is None
    wide = _odds_event("A", "B", "2026-10-17T17:00:00Z", {"A": 1.5, "B": 1.5})
    assert fair_from_odds(wide, three_way=False) is None
    other_book = _odds_event("A", "B", "2026-10-17T17:00:00Z", {"A": 1.9, "B": 1.9}, book="draftkings")
    assert fair_from_odds(other_book, three_way=False) is None


# --- edge -----------------------------------------------------------------------------------

def test_taker_fee_and_edge_depth():
    assert taker_fee_per_share(0.5, 0.03) == pytest.approx(0.0075)
    tf = TokenFair("m", "t", "X", fair=0.55, fee_rate=0.03, fee_exponent=1.0)
    book = {"bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "100"}, {"price": "0.50", "size": "20"},
                     {"price": "0.56", "size": "999"}]}
    e = token_edge(tf, book)
    assert e.best_ask == 0.50 and e.best_bid == 0.48
    assert e.taker_edge == pytest.approx(0.55 - 0.50 - 0.0075)
    assert e.maker_edge == pytest.approx(0.07)
    assert e.plus_ev_shares == pytest.approx(120)        # 0.50 and 0.52 levels, not 0.56
    assert e.plus_ev_usdc == pytest.approx(20 * 0.50 + 100 * 0.52)


def test_empty_book_has_no_edge():
    e = token_edge(TokenFair("m", "t", "X", 0.5, 0.03, 1.0), None)
    assert e.taker_edge is None and e.maker_edge is None and e.plus_ev_usdc == 0


@pytest.mark.parametrize("league, pm, book, level", [
    ("ucl", "FC Bayern München", "Bayern Munich", "exact"),
    ("ucl", "FC Internazionale Milano", "Inter Milan", "exact"),
    ("ucl", "Sporting CP", "Sporting Lisbon", "exact"),      # shared soccer alias in a cup
    ("ucl", "Racing Club de Lens", "RC Lens", "tokens"),
    ("lal", "Athletic Club", "Athletic Bilbao", "exact"),
    ("ufc", "SuYoung You", "Su Young You", "exact"),
    ("ufc", "Katlyn Cerminara", "Katlyn Chookagian", "exact"),
    ("nhl", "Sporting CP", "Sporting Lisbon", None),         # soccer aliases stay in soccer
])
def test_aliases_and_spacing(league, pm, book, level):
    assert name_match_level((pm,), book, league) == level

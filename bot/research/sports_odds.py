"""Sports-odds vs Polymarket scanner logic (research S1, read-only, never trades).

Pure functions only: no network, no disk. ``scripts/sports_odds_scan.py`` does the I/O.

Pipeline for one scan:
1. Polymarket: Gamma events per league (``series_id``), keep pre-game moneyline markets.
2. Bookmaker: The Odds API ``/v4/sports/{key}/odds`` h2h (Pinnacle is the fair source).
3. ``match_event``: league + kickoff time window + BOTH team names, unique candidate.
4. ``fair_from_odds``: proportional de-vig of Pinnacle's h2h (2- or 3-way).
5. ``token_fairs``: map each Polymarket token to the fair probability of its outcome.
6. ``token_edge``: fair vs CLOB book after the market's own taker fee.

Design notes (see reports/sports_odds_research_20261017/):
- Matching is scoped by league and kickoff time, so team names only need to tell
  apart the few games that start in the same window. There is no global alias
  table and no substring fallback: polymm's global ``TEAM_ALIASES`` maps 48 names
  to more than one canonical team (e.g. "rangers" NHL vs Scottish football,
  "miamiredhawks" -> "miamihurricanes"), and its ``contains`` fallback matches any
  name that is a substring of another. A name match here is whole-token only.
- Ambiguity (two candidate games, or one Polymarket team matching both book teams)
  is a non-match, logged for review, never a guess.
- De-vig method is proportional (same as polymm ``vig_removal.py``, re-implemented).
  Raw decimal odds are stored with every scan so other methods can be re-run later.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# League map: Polymarket sport code (gamma /sports) -> The Odds API sport key.
# kickoff_tol_min: max |PM start - book commence| for a candidate game.
# MMA cards list one start time on Polymarket for every bout, so its window is wide
# and the two fighter names carry the match.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class League:
    pm_sport: str
    odds_key: str
    three_way: bool
    kickoff_tol_min: int = 20


LEAGUES: dict[str, League] = {
    lg.pm_sport: lg
    for lg in (
        League("epl", "soccer_epl", True),
        League("lal", "soccer_spain_la_liga", True),
        League("bun", "soccer_germany_bundesliga", True),
        League("sea", "soccer_italy_serie_a", True),
        League("fl1", "soccer_france_ligue_one", True),
        League("ucl", "soccer_uefa_champs_league", True),
        League("uel", "soccer_uefa_europa_league", True),
        League("mls", "soccer_usa_mls", True),
        League("nhl", "icehockey_nhl", False),
        League("nba", "basketball_nba", False),
        League("nfl", "americanfootball_nfl", False),
        League("cfb", "americanfootball_ncaaf", False),
        League("mlb", "baseball_mlb", False),
        League("ufc", "mma_mixed_martial_arts", False, kickoff_tol_min=12 * 60),
        # Less-followed leagues that still have a Pinnacle line (direction B probe).
        League("elc", "soccer_efl_champ", True),
        League("shl", "icehockey_sweden_hockey_league", False),
        League("liiga", "icehockey_liiga", False),
    )
}

# Prereg S1 v1 (reports/sports_odds_research_20261017/prereg.md): main + explore leagues.
MAIN_LEAGUES = ("epl", "lal", "bun", "sea", "fl1", "ucl", "uel", "mls", "nhl", "nba", "nfl", "ufc")
EXPLORE_LEAGUES = ("elc", "shl", "liiga")
DEFAULT_LEAGUES = MAIN_LEAGUES + EXPLORE_LEAGUES

FAIR_BOOK = "pinnacle"
# One Odds API credit covers up to 10 bookmakers per market; the others are stored
# for later comparison (exchanges are sharp too) but do not drive the fair value.
BOOKMAKERS = ("pinnacle", "betfair_ex_eu", "matchbook", "smarkets", "lowvig",
              "betonlineag", "draftkings", "fanduel")

MAX_OVERROUND = 1.10          # wider than this: not a sharp two-sided price, skip
SUSPECT_GAP = 0.20            # |fair - PM mid| above this is more likely a bad match

# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Whole tokens dropped before comparing (club-form words, never distinguishing alone).
_DROP_TOKENS = frozenset({"fc", "afc", "cf", "sc", "ac", "cd", "ssc", "rc", "the", "club", "calcio", "1"})
# Same word in another language; applied per token before comparing.
_TOKEN_SYNONYMS = {"munchen": "munich", "milano": "milan", "internazionale": "inter", "praha": "prague"}
# Letters NFKD does not split into base + accent (they would otherwise be deleted).
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss", "ł": "l", "Ł": "l",
                           "đ": "d", "Đ": "d", "ı": "i", "œ": "oe", "Œ": "oe", "þ": "th", "ð": "d"})
# A name made only of these cannot identify a team by itself (e.g. "City", "United").
_GENERIC_TOKENS = frozenset({
    "city", "united", "town", "real", "athletic", "atletico", "sporting", "inter",
    "st", "saint", "fc", "county", "rovers", "wanderers", "albion", "new", "york",
    "los", "angeles", "la", "san", "de", "of", "and",
})

# Spelling differences found in the unmatched log. Keys and values are normalized
# token strings. Only add genuinely different names, never a guess. "soccer" applies
# to every soccer league (cups mix clubs from all of them); a league's own table wins.
LEAGUE_ALIASES: dict[str, dict[str, str]] = {
    "soccer": {
        "wolves": "wolverhampton wanderers",
        "spurs": "tottenham hotspur",
        "man utd": "manchester united",
        "man city": "manchester city",
        "nottm forest": "nottingham forest",
        "sporting cp": "sporting lisbon",
        "athletic": "athletic bilbao",
        "ferencvarosi tc": "ferencvaros tc",
        "ru saint gilloise": "union saint gilloise",
        "stade rennais 1901": "rennes",
        "stade rennais": "rennes",
        "olympiakos sfp": "olympiakos piraeus",
        "olympique lyonnais": "lyon",
        "pfk levski sofia": "pfc levski sofia",
    },
    "shl": {
        "djurgarden": "djurgardens",                # Swedish genitive in the club name
        "bjoerkloeven": "bjorkloven",               # "oe" transliteration of "ö"
    },
    "ufc": {
        "timmy cuamba": "timothy cuamba",
        "katlyn cerminara": "katlyn chookagian",   # same fighter, married name
    },
}


def normalize_tokens(name: str) -> tuple[str, ...]:
    """Lowercase, strip accents and punctuation, '&' -> 'and', drop club-form tokens."""
    s = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return tuple(_TOKEN_SYNONYMS.get(t, t) for t in s.split() if t not in _DROP_TOKENS)


def _aliased(tokens: tuple[str, ...], league: str) -> tuple[str, ...]:
    key = " ".join(tokens)
    tables = [LEAGUE_ALIASES.get(league, {})]
    lg = LEAGUES.get(league)
    if lg is not None and lg.odds_key.startswith("soccer_"):
        tables.append(LEAGUE_ALIASES["soccer"])
    for table in tables:
        if key in table:
            return tuple(table[key].split())
    return tokens


def _is_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    it = iter(long)
    return all(tok in it for tok in short)


def name_match_level(pm_names: Iterable[str], book_name: str, league: str) -> Optional[str]:
    """'exact' | 'tokens' | None. ``pm_names`` = Polymarket name plus its alias field.

    'tokens': every token of the shorter name appears, in order, in the longer one,
    and the shorter name is not only generic words ("Devils" ~ "New Jersey Devils",
    "Brighton" ~ "Brighton and Hove Albion"; "City" never matches anything).
    """
    b = _aliased(normalize_tokens(book_name), league)
    if not b:
        return None
    best = None
    for raw in pm_names:
        p = _aliased(normalize_tokens(raw), league)
        if not p:
            continue
        if p == b or "".join(p) == "".join(b):    # "SuYoung You" == "Su Young You"
            return "exact"
        short, long = (p, b) if len(p) <= len(b) else (b, p)
        if set(short) <= _GENERIC_TOKENS:
            continue
        if _is_subsequence(short, long):
            best = "tokens"
    return best


# ---------------------------------------------------------------------------
# Polymarket event parsing
# ---------------------------------------------------------------------------


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


@dataclass
class PmTeam:
    name: str
    alias: str = ""
    ordering: str = ""          # "home" | "away" | ""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n for n in (self.name, self.alias) if n)


@dataclass
class PmMarket:
    market_id: str
    question: str
    outcomes: list[str]
    token_ids: list[str]
    fee_rate: float
    fee_exponent: float
    accepting_orders: bool


@dataclass
class PmEvent:
    event_id: str
    slug: str
    title: str
    league: str
    start: Optional[datetime]
    teams: list[PmTeam]
    markets: list[PmMarket] = field(default_factory=list)
    volume: float = 0.0


def parse_pm_event(event: dict, league: str) -> Optional[PmEvent]:
    """Pre-game moneyline view of one Gamma event, or None if it has none."""
    if event.get("closed") or event.get("ended") or event.get("live"):
        return None
    teams = [PmTeam(str(t.get("name") or ""), str(t.get("alias") or ""), str(t.get("ordering") or ""))
             for t in (event.get("teams") or []) if t.get("name")]
    markets = []
    for m in event.get("markets") or []:
        if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
            continue
        outcomes = [str(o) for o in _json_list(m.get("outcomes"))]
        tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        if len(outcomes) != 2 or len(tokens) != 2:
            continue
        fs = m.get("feeSchedule") or {}
        fee_rate = float(fs.get("rate") or 0.0) if m.get("feesEnabled") else 0.0
        markets.append(PmMarket(
            market_id=str(m.get("id")), question=str(m.get("question") or ""),
            outcomes=outcomes, token_ids=tokens, fee_rate=fee_rate,
            fee_exponent=float(fs.get("exponent") or 1.0),
            accepting_orders=bool(m.get("acceptingOrders")),
        ))
    if not markets or len(teams) != 2:
        return None
    # startTime is kickoff; startDate is when the event was listed, never use it here.
    start = parse_time(event.get("startTime"))
    if start is None:
        starts = {parse_time(m.get("gameStartTime")) for m in event.get("markets") or []
                  if m.get("sportsMarketType") == "moneyline"} - {None}
        start = starts.pop() if len(starts) == 1 else None
    return PmEvent(
        event_id=str(event.get("id")), slug=str(event.get("slug") or ""),
        title=str(event.get("title") or ""), league=league, start=start, teams=teams,
        markets=markets, volume=float(event.get("volume") or 0.0),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    status: str                       # "matched" | "no_candidate" | "no_name_match" | "ambiguous"
    odds_event: Optional[dict] = None
    team_map: dict[str, str] = field(default_factory=dict)   # PM team name -> book team name
    level: str = ""                   # worst name level used: "exact" | "tokens"
    home_away_consistent: Optional[bool] = None
    candidates: int = 0


def _pair_assignment(pm: PmEvent, ev: dict) -> Optional[tuple[dict[str, str], str]]:
    """Assign both PM teams to distinct book teams; None if not exactly one way."""
    book = [ev.get("home_team") or "", ev.get("away_team") or ""]
    levels = [[name_match_level(t.names, b, pm.league) for b in book] for t in pm.teams]
    straight = levels[0][0] and levels[1][1]
    crossed = levels[0][1] and levels[1][0]
    if bool(straight) == bool(crossed):   # neither, or both (ambiguous)
        return None
    if straight:
        pair, lv = {pm.teams[0].name: book[0], pm.teams[1].name: book[1]}, (levels[0][0], levels[1][1])
    else:
        pair, lv = {pm.teams[0].name: book[1], pm.teams[1].name: book[0]}, (levels[0][1], levels[1][0])
    return pair, ("exact" if lv == ("exact", "exact") else "tokens")


def match_event(pm: PmEvent, odds_events: list[dict], tol_min: int) -> MatchResult:
    if pm.start is None:
        return MatchResult("no_candidate")
    window = timedelta(minutes=tol_min)
    cands = [ev for ev in odds_events
             if (t := parse_time(ev.get("commence_time"))) is not None and abs(t - pm.start) <= window]
    if not cands:
        return MatchResult("no_candidate")
    hits = [(ev, a) for ev in cands if (a := _pair_assignment(pm, ev)) is not None]
    if not hits:
        return MatchResult("no_name_match", candidates=len(cands))
    if len(hits) > 1:
        return MatchResult("ambiguous", candidates=len(cands))
    ev, (team_map, level) = hits[0]
    consistent = None
    homes = [t.name for t in pm.teams if t.ordering == "home"]
    if len(homes) == 1:
        consistent = team_map[homes[0]] == ev.get("home_team")
    return MatchResult("matched", ev, team_map, level, consistent, len(cands))


# ---------------------------------------------------------------------------
# Fair value
# ---------------------------------------------------------------------------


@dataclass
class Fair:
    probs: dict[str, float]           # book outcome name ("Draw" for draw) -> fair prob
    overround: float
    last_update: Optional[datetime]
    raw_odds: dict[str, float]


def fair_from_odds(odds_event: dict, three_way: bool, book: str = FAIR_BOOK) -> Optional[Fair]:
    """Proportional de-vig of ``book``'s h2h for one Odds API event."""
    for bm in odds_event.get("bookmakers") or []:
        if bm.get("key") != book:
            continue
        for mk in bm.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            odds = {str(o["name"]): float(o["price"]) for o in mk.get("outcomes") or []
                    if o.get("name") and o.get("price")}
            if len(odds) != (3 if three_way else 2) or any(v <= 1.0 for v in odds.values()):
                return None
            implied = {k: 1.0 / v for k, v in odds.items()}
            over = sum(implied.values())
            if not (1.0 <= over <= MAX_OVERROUND):
                return None
            return Fair({k: v / over for k, v in implied.items()}, over,
                        parse_time(mk.get("last_update") or bm.get("last_update")), odds)
    return None


@dataclass
class TokenFair:
    market_id: str
    token_id: str
    label: str                        # e.g. "Chelsea FC YES", "Devils"
    fair: float
    fee_rate: float
    fee_exponent: float
    accepting_orders: bool = True
    book_outcome: str = ""            # outcome name on the book side ("Draw" for draw)
    side: str = "outcome"             # "yes" | "no" (fair = 1 - p(book_outcome)) | "outcome"


_DRAW_RE = re.compile(r"\bdraw\b", re.IGNORECASE)
_WILL_WIN_RE = re.compile(r"^\s*will\s+(.+?)\s+win\b", re.IGNORECASE)


def token_fairs(pm: PmEvent, match: MatchResult, fair: Fair) -> list[TokenFair]:
    """Fair probability for every token of every moneyline market we can map.

    Two shapes on Polymarket:
    - one market whose two outcomes are the two team names (NHL, NBA, UFC ...);
    - Yes/No markets "Will X win on DATE?" / "Will A vs. B end in a draw?" (soccer).
    A market we cannot map unambiguously is skipped, never guessed.
    """
    out: list[TokenFair] = []
    for m in pm.markets:
        lower = [o.lower() for o in m.outcomes]
        if lower == ["yes", "no"]:
            if _DRAW_RE.search(m.question) and "Draw" in fair.probs:
                p_yes, label, book_name = fair.probs["Draw"], "Draw", "Draw"
            else:
                mm = _WILL_WIN_RE.match(m.question)
                if not mm:
                    continue
                named = mm.group(1)
                team = [t for t in pm.teams
                        if name_match_level((named,), t.name, pm.league) == "exact"
                        or (t.alias and name_match_level((named,), t.alias, pm.league) == "exact")]
                if len(team) != 1:
                    continue
                book_name = match.team_map.get(team[0].name)
                if book_name not in fair.probs:
                    continue
                p_yes, label = fair.probs[book_name], team[0].name
            out.append(TokenFair(m.market_id, m.token_ids[0], f"{label} YES", p_yes,
                                 m.fee_rate, m.fee_exponent, m.accepting_orders, book_name, "yes"))
            out.append(TokenFair(m.market_id, m.token_ids[1], f"{label} NO", 1.0 - p_yes,
                                 m.fee_rate, m.fee_exponent, m.accepting_orders, book_name, "no"))
        else:
            for outcome, tok in zip(m.outcomes, m.token_ids):
                team = [t for t in pm.teams if outcome in t.names]
                if len(team) != 1:
                    break
                book_name = match.team_map.get(team[0].name)
                if book_name not in fair.probs:
                    break
                out.append(TokenFair(m.market_id, tok, outcome, fair.probs[book_name],
                                     m.fee_rate, m.fee_exponent, m.accepting_orders, book_name))
            else:
                continue
            # A half-mapped two-outcome market is dropped as a whole.
            out = [t for t in out if t.market_id != m.market_id]
    return out


# ---------------------------------------------------------------------------
# Edge vs the order book
# ---------------------------------------------------------------------------


def taker_fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Taker fee in USDC per share bought at ``price``: rate * (p(1-p))^exponent.

    Conservative reading of the market's ``feeSchedule``. execution/rebate_model.py
    uses ``p * rate * (p(1-p))^exponent`` (smaller by a factor p); which one the
    exchange charges on sports markets is not verified yet, so the scanner uses the
    larger cost.
    """
    p = min(max(price, 0.0), 1.0)
    return rate * (p * (1.0 - p)) ** exponent


@dataclass
class TokenEdge:
    best_bid: Optional[float]
    best_ask: Optional[float]
    ask_size: float
    taker_edge: Optional[float]       # fair - ask - fee(ask), per share
    maker_edge: Optional[float]       # fair - bid (resting at the bid, no fee)
    plus_ev_shares: float             # ask-side shares with fair - price - fee > 0
    plus_ev_usdc: float               # cost of those shares
    mid: Optional[float]


def _levels(book: dict, side: str) -> list[tuple[float, float]]:
    rows = []
    for lv in book.get(side) or []:
        try:
            rows.append((float(lv["price"]), float(lv["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def token_edge(tf: TokenFair, book: Optional[dict]) -> TokenEdge:
    bids = sorted(_levels(book or {}, "bids"), key=lambda r: -r[0])
    asks = sorted(_levels(book or {}, "asks"), key=lambda r: r[0])
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    taker = None if ask is None else tf.fair - ask - taker_fee_per_share(ask, tf.fee_rate, tf.fee_exponent)
    maker = None if bid is None else tf.fair - bid
    shares = usdc = 0.0
    for price, size in asks:
        if tf.fair - price - taker_fee_per_share(price, tf.fee_rate, tf.fee_exponent) <= 0:
            break
        shares += size
        usdc += size * price
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return TokenEdge(bid, ask, asks[0][1] if asks else 0.0, taker, maker, shares, usdc, mid)

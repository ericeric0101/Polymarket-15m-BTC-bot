"""Read-only daily scan: Polymarket sports moneylines vs de-vigged Pinnacle (research S1).

Never places orders. One run = one snapshot:

    .venv/bin/python scripts/sports_odds_scan.py                  # default leagues
    .venv/bin/python scripts/sports_odds_scan.py --leagues epl,nhl
    .venv/bin/python scripts/sports_odds_scan.py --dry-credits    # show cost, call nothing paid
    .venv/bin/python scripts/sports_odds_scan.py --replay data/sports_odds/raw/2026-10-17/102317Z

Cost: 1 The Odds API credit per league (h2h, <=10 bookmakers). Polymarket calls are free.
The scan refuses to spend below --credit-reserve remaining credits (fail-closed).

Output (data/ is gitignored), all under data/sports_odds/:
    raw/YYYY-MM-DD/<HHMMSS>Z/  odds_<league>.json.gz, pm_<league>.json.gz, books.json.gz
    scans/YYYY-MM-DD/<HHMMSS>Z_tokens.csv   one row per matched Polymarket token
    scans/YYYY-MM-DD/<HHMMSS>Z_report.md    ranking + match stats + unmatched list
    scan_log.jsonl                          one line per run (credits, counts)

The API key comes from ODDS_API_KEY in .env and is never printed or written.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.research.sports_odds import (  # noqa: E402
    BOOKMAKERS,
    DEFAULT_LEAGUES,
    FAIR_BOOK,
    LEAGUES,
    SUSPECT_GAP,
    fair_from_odds,
    match_event,
    parse_pm_event,
    token_edge,
    token_fairs,
)

ODDS_URL = "https://api.the-odds-api.com/v4/sports/{key}/odds"
ODDS_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"   # costs 0 credits
GAMMA_SPORTS_URL = "https://gamma-api.polymarket.com/sports"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
OUT_ROOT = REPO_ROOT / "data" / "sports_odds"
TIMEOUT = 20
PREGAME_MIN = 5            # ignore games starting within this many minutes
HORIZON_DAYS = 7           # ignore games further out than this
ODDS_STALE_MIN = 30        # flag Pinnacle prices older than this at scan time


class ScanError(RuntimeError):
    pass


def _load_key() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise ScanError("ODDS_API_KEY missing (put it in .env)")
    return key


def _redact(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def fetch_odds(session: requests.Session, league: str, key: str) -> tuple[list, dict]:
    params = {"apiKey": key, "markets": "h2h", "oddsFormat": "decimal",
              "bookmakers": ",".join(BOOKMAKERS), "dateFormat": "iso"}
    try:
        r = session.get(ODDS_URL.format(key=LEAGUES[league].odds_key), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ScanError(_redact(f"odds {league}: {exc}", key)) from None
    usage = {h: r.headers.get(h) for h in ("x-requests-remaining", "x-requests-used", "x-requests-last")}
    if r.status_code != 200:
        raise ScanError(_redact(f"odds {league}: HTTP {r.status_code} {r.text[:200]}", key))
    return r.json(), usage


def fetch_credit_usage(session: requests.Session, key: str) -> dict:
    """Remaining credits via the free /v4/sports call, so the reserve guard holds from call one."""
    try:
        r = session.get(ODDS_SPORTS_URL, params={"apiKey": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ScanError(_redact(f"odds sports: {exc}", key)) from None
    if r.status_code != 200:
        raise ScanError(_redact(f"odds sports: HTTP {r.status_code} {r.text[:200]}", key))
    return {h: r.headers.get(h) for h in ("x-requests-remaining", "x-requests-used", "x-requests-last")}


def fetch_series_ids(session: requests.Session) -> dict[str, str]:
    r = session.get(GAMMA_SPORTS_URL, timeout=TIMEOUT)
    r.raise_for_status()
    return {str(s["sport"]): str(s["series"]) for s in r.json() if s.get("sport") and s.get("series")}


def fetch_pm_events(session: requests.Session, series_id: str) -> list[dict]:
    out, offset = [], 0
    while True:
        r = session.get(GAMMA_EVENTS_URL, params={"series_id": series_id, "closed": "false",
                                                  "limit": 100, "offset": offset}, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json()
        out.extend(page)
        if len(page) < 100 or offset > 2000:
            return out
        offset += 100


def fetch_books(session: requests.Session, token_ids: list[str]) -> dict[str, dict]:
    books: dict[str, dict] = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        r = session.post(CLOB_BOOKS_URL, json=[{"token_id": t} for t in chunk], timeout=TIMEOUT)
        r.raise_for_status()
        for b in r.json():
            books[str(b.get("asset_id"))] = b
    return books


def _dump_gz(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _fmt(x: Optional[float], nd: int = 3) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def _load_gz(path: pathlib.Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_replay(raw_dir: pathlib.Path) -> tuple[datetime, dict, dict, dict]:
    """A stored snapshot: (scan time, pm by league, odds by league, books). No network."""
    now = datetime.strptime(f"{raw_dir.parent.name} {raw_dir.name}", "%Y-%m-%d %H%M%SZ").replace(tzinfo=timezone.utc)
    pm = {f.name[3:-8]: _load_gz(f) for f in sorted(raw_dir.glob("pm_*.json.gz"))}
    odds = {f.name[5:-8]: _load_gz(f) for f in sorted(raw_dir.glob("odds_*.json.gz"))}
    books = _load_gz(raw_dir / "books.json.gz") if (raw_dir / "books.json.gz").exists() else {}
    return now, {k: v for k, v in pm.items() if k in odds}, odds, books


def acquire(leagues: list[str], credit_reserve: int, raw_dir: pathlib.Path) -> tuple[dict, dict, dict]:
    """Fetch Polymarket events and book odds per league; stores raw responses."""
    key = _load_key()
    session = requests.Session()
    session.headers["User-Agent"] = "s1-sports-odds-research/0.1"
    series = fetch_series_ids(session)
    usage = fetch_credit_usage(session, key)
    pm_by_league: dict[str, list] = {}
    odds_by_league: dict[str, list] = {}
    for lg in leagues:
        if lg not in series:
            print(f"  {lg}: no Polymarket series, skipped")
            continue
        remaining = usage.get("x-requests-remaining")
        if remaining is not None and int(float(remaining)) <= credit_reserve:
            print(f"  STOP: {remaining} credits left <= reserve {credit_reserve}")
            break
        pm_raw = fetch_pm_events(session, series[lg])
        odds_raw, usage = fetch_odds(session, lg, key)
        _dump_gz(raw_dir / f"pm_{lg}.json.gz", pm_raw)
        _dump_gz(raw_dir / f"odds_{lg}.json.gz", odds_raw)
        pm_by_league[lg], odds_by_league[lg] = pm_raw, odds_raw
    return pm_by_league, odds_by_league, usage


def analyse(now: datetime, pm_by_league: dict, odds_by_league: dict,
            stats: dict, unmatched: list) -> list[tuple]:
    """Match, de-vig and map tokens; fills ``stats``/``unmatched``; returns pending events."""
    pending: list[tuple] = []
    for lg, pm_raw in pm_by_league.items():
        league = LEAGUES[lg]
        st = stats.setdefault(lg, {"pm_events": 0, "pregame": 0, "matched": 0, "exact": 0,
                                   "no_candidate": 0, "no_name_match": 0, "ambiguous": 0,
                                   "no_fair": 0, "odds_events": len(odds_by_league[lg])})
        for raw in pm_raw:
            ev = parse_pm_event(raw, lg)
            if ev is None:
                continue
            st["pm_events"] += 1
            if ev.start is None or not (now + timedelta(minutes=PREGAME_MIN) <= ev.start
                                        <= now + timedelta(days=HORIZON_DAYS)):
                continue
            st["pregame"] += 1
            m = match_event(ev, odds_by_league[lg], league.kickoff_tol_min)
            if m.status != "matched":
                st[m.status] += 1
                unmatched.append({"league": lg, "status": m.status, "title": ev.title,
                                  "start": ev.start.isoformat(), "candidates": m.candidates})
                continue
            fair = fair_from_odds(m.odds_event, league.three_way)
            if fair is None:
                st["no_fair"] += 1
                continue
            st["matched"] += 1
            st["exact"] += m.level == "exact"
            pending.append((ev, m, fair, token_fairs(ev, m, fair)))
    return pending


def build_rows(now: datetime, pending: list[tuple], books: dict) -> list[dict]:
    rows: list[dict] = []
    for ev, m, fair, tfs in pending:
        age = (now - fair.last_update).total_seconds() / 60 if fair.last_update else None
        for tf in tfs:
            book = books.get(tf.token_id)
            e = token_edge(tf, book)
            gap = None if e.mid is None else tf.fair - e.mid
            rows.append({
                "scan_utc": now.isoformat(timespec="seconds"), "league": ev.league,
                "event": ev.title, "slug": ev.slug, "start_utc": ev.start.isoformat(),
                "hours_to_start": round((ev.start - now).total_seconds() / 3600, 2),
                "token": tf.label, "token_id": tf.token_id, "market_id": tf.market_id,
                "fair": round(tf.fair, 4), "bid": e.best_bid, "ask": e.best_ask, "mid": e.mid,
                "ask_size": e.ask_size, "fee_rate": tf.fee_rate, "has_book": book is not None,
                "taker_edge": None if e.taker_edge is None else round(e.taker_edge, 4),
                "maker_edge": None if e.maker_edge is None else round(e.maker_edge, 4),
                "plus_ev_shares": round(e.plus_ev_shares, 2), "plus_ev_usdc": round(e.plus_ev_usdc, 2),
                "fair_minus_mid": None if gap is None else round(gap, 4),
                "suspect": gap is not None and abs(gap) > SUSPECT_GAP,
                "match_level": m.level, "home_away_consistent": m.home_away_consistent,
                "overround": round(fair.overround, 4),
                "odds_age_min": None if age is None else round(age, 1),
                "odds_stale": age is not None and age > ODDS_STALE_MIN,
                "pm_volume": round(ev.volume, 0),
                "book_event_id": m.odds_event.get("id"),
                "raw_odds": json.dumps(fair.raw_odds, sort_keys=True),
            })
    return rows


def run(leagues: list[str], credit_reserve: int, dry_credits: bool,
        replay_dir: Optional[pathlib.Path] = None) -> int:
    if replay_dir is not None:
        now, pm_by_league, odds_by_league, books = load_replay(replay_dir)
        usage: dict = {}
        suffix = "_replay"
        print(f"replay {replay_dir}: leagues={','.join(odds_by_league)} (0 credits)")
    else:
        now = datetime.now(timezone.utc)
        suffix = ""
    stamp = now.strftime("%H%M%SZ")
    day = now.strftime("%Y-%m-%d")
    raw_dir = OUT_ROOT / "raw" / day / stamp
    scan_dir = OUT_ROOT / "scans" / day
    if replay_dir is None:
        print(f"scan {day} {stamp}: leagues={','.join(leagues)} cost={len(leagues)} credits")
        if dry_credits:
            return 0
        pm_by_league, odds_by_league, usage = acquire(leagues, credit_reserve, raw_dir)

    stats: dict[str, dict] = {}
    unmatched: list[dict] = []
    pending = analyse(now, pm_by_league, odds_by_league, stats, unmatched)
    if replay_dir is None:
        books = fetch_books(requests.Session(), [tf.token_id for *_, tfs in pending for tf in tfs])
        _dump_gz(raw_dir / "books.json.gz", books)
    # On replay, tokens matched only by newer rules have no stored book (has_book=False).
    rows = build_rows(now, pending, books)

    scan_dir.mkdir(parents=True, exist_ok=True)
    csv_path = scan_dir / f"{stamp}{suffix}_tokens.csv"
    if rows:
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    report_path = scan_dir / f"{stamp}{suffix}_report.md"
    report_path.write_text(render_report(now, rows, stats, unmatched, usage))

    if replay_dir is None:
        log = {"scan_utc": now.isoformat(timespec="seconds"), "leagues": leagues, "stats": stats,
               "tokens": len(rows), "credits": usage, "csv": str(csv_path.relative_to(REPO_ROOT)),
               "report": str(report_path.relative_to(REPO_ROOT))}
        with open(OUT_ROOT / "scan_log.jsonl", "a") as fh:
            fh.write(json.dumps(log) + "\n")

    for lg, st in stats.items():
        print(f"  {lg:4s} pm={st['pm_events']:3d} pregame={st['pregame']:3d} matched={st['matched']:3d} "
              f"(exact {st['exact']}) no_cand={st['no_candidate']} no_name={st['no_name_match']} "
              f"ambig={st['ambiguous']} no_fair={st['no_fair']} book_events={st['odds_events']}")
    print(f"  tokens={len(rows)} (no stored book: {sum(not r['has_book'] for r in rows)}) "
          f"credits_remaining={usage.get('x-requests-remaining')}")
    print(f"  report: {report_path.relative_to(REPO_ROOT)}")
    return 0


def render_report(now: datetime, rows: list[dict], stats: dict, unmatched: list[dict], usage: dict) -> str:
    clean = [r for r in rows if not r["suspect"] and not r["odds_stale"]]
    lines = [f"# S1 sports odds scan {now.isoformat(timespec='seconds')}", "",
             f"Fair = proportional de-vig of {FAIR_BOOK} h2h. Taker edge = fair - ask - fee(ask) per share,"
             " fee = rate x p(1-p) from each market's feeSchedule. Read-only; nothing was traded.", "",
             f"Credits remaining: {usage.get('x-requests-remaining')}", "",
             "## Match stats", "",
             "| league | PM events | pre-game | matched | exact | no candidate | no name | ambiguous | no fair |",
             "|---|---|---|---|---|---|---|---|---|"]
    for lg, s in stats.items():
        lines.append(f"| {lg} | {s['pm_events']} | {s['pregame']} | {s['matched']} | {s['exact']} | "
                     f"{s['no_candidate']} | {s['no_name_match']} | {s['ambiguous']} | {s['no_fair']} |")

    def table(title: str, items: list[dict], key: str) -> None:
        lines.extend(["", f"## {title}", "",
                      "| league | event | token | h to start | fair | bid | ask | edge | +EV $ | ask size |",
                      "|---|---|---|---|---|---|---|---|---|---|"])
        for r in items:
            lines.append(f"| {r['league']} | {r['event']} | {r['token']} | {r['hours_to_start']} | "
                         f"{r['fair']:.3f} | {_fmt(r['bid'])} | {_fmt(r['ask'])} | {_fmt(r[key], 4)} | "
                         f"{r['plus_ev_usdc']:.0f} | {r['ask_size']:.0f} |")

    taker = sorted((r for r in clean if r["taker_edge"] is not None), key=lambda r: -r["taker_edge"])[:25]
    maker = sorted((r for r in clean if r["maker_edge"] is not None), key=lambda r: -r["maker_edge"])[:25]
    table("Top taker edges (fair - ask - fee)", taker, "taker_edge")
    table("Top maker edges at current best bid (fair - bid)", maker, "maker_edge")

    gaps = [abs(r["fair_minus_mid"]) for r in clean if r["fair_minus_mid"] is not None]
    if gaps:
        gaps.sort()
        lines.extend(["", "## |fair - mid| distribution (clean tokens)", "",
                      f"n={len(gaps)} median={gaps[len(gaps) // 2]:.4f} "
                      f"p90={gaps[int(len(gaps) * 0.9)]:.4f} max={gaps[-1]:.4f}"])
    flagged = [r for r in rows if r["suspect"] or r["odds_stale"]]
    if flagged:
        lines.extend(["", f"## Flagged ({len(flagged)}: suspect gap > {SUSPECT_GAP} or stale odds)", ""])
        lines.extend(f"- {r['league']} {r['event']} / {r['token']}: fair {r['fair']:.3f} mid {_fmt(r['mid'])}"
                     f" age {r['odds_age_min']}m level {r['match_level']}" for r in flagged)
    if unmatched:
        lines.extend(["", f"## Unmatched pre-game Polymarket events ({len(unmatched)})", ""])
        lines.extend(f"- {u['league']} [{u['status']}, {u['candidates']} cand] {u['title']} @ {u['start']}"
                     for u in unmatched)
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--leagues", default=",".join(DEFAULT_LEAGUES),
                    help=f"comma list from: {','.join(LEAGUES)}")
    ap.add_argument("--credit-reserve", type=int, default=50,
                    help="stop before spending when remaining credits <= this")
    ap.add_argument("--dry-credits", action="store_true", help="print the cost and exit")
    ap.add_argument("--replay", type=pathlib.Path, default=None,
                    help="re-analyse a stored raw/YYYY-MM-DD/HHMMSSZ snapshot (0 credits, no network)")
    args = ap.parse_args(argv)
    leagues = [s.strip() for s in args.leagues.split(",") if s.strip()]
    bad = [s for s in leagues if s not in LEAGUES]
    if bad:
        ap.error(f"unknown leagues: {bad}")
    try:
        return run(leagues, args.credit_reserve, args.dry_credits, args.replay)
    except (ScanError, requests.RequestException) as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""One-off league probe for S1 (NOT part of the frozen prereg v1). Writes only to data/sports_odds_probe/,
never to data/sports_odds/, so the study analysis never sees it. Costs 1 Odds API credit.
usage: .venv/bin/python scripts/sports_odds_probe.py <pm_code> <odds_key> <three_way 0|1> <alias_league>
alias_league: which league's alias tables to use (e.g. 'uel' to get the shared soccer table)."""
import os, pathlib, sys
from datetime import datetime, timedelta, timezone
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
import requests
from dotenv import load_dotenv
from bot.research.sports_odds import parse_pm_event, match_event, fair_from_odds, token_fairs
import sports_odds_scan as scan

pm_code, odds_key, three_way, alias_lg = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
load_dotenv(REPO / ".env"); key = os.environ["ODDS_API_KEY"]
now = datetime.now(timezone.utc); out = REPO / "data" / "sports_odds_probe" / now.strftime(f"{pm_code}_%Y%m%dT%H%M%SZ")
s = requests.Session(); series = scan.fetch_series_ids(s)
r = s.get(scan.ODDS_URL.format(key=odds_key), params={"apiKey": key, "markets": "h2h", "oddsFormat": "decimal",
          "bookmakers": ",".join(scan.BOOKMAKERS)}, timeout=20)
r.raise_for_status(); odds = r.json(); remaining = r.headers.get("x-requests-remaining")
pm = scan.fetch_pm_events(s, series[pm_code])
scan._dump_gz(out / "odds.json.gz", odds); scan._dump_gz(out / "pm.json.gz", pm)
stats = {"pm_pregame": 0, "matched": 0, "no_candidate": 0, "no_name_match": 0, "ambiguous": 0, "no_fair": 0}
pending, unmatched = [], []
for raw in pm:
    ev = parse_pm_event(raw, alias_lg)
    if ev is None or ev.start is None or not (now + timedelta(minutes=1) <= ev.start <= now + timedelta(days=7)):
        continue
    stats["pm_pregame"] += 1
    m = match_event(ev, odds, 20)
    if m.status != "matched":
        stats[m.status] += 1; unmatched.append((m.status, ev.title, ev.start.isoformat())); continue
    f = fair_from_odds(m.odds_event, three_way)
    if f is None: stats["no_fair"] += 1; continue
    stats["matched"] += 1; pending.append((ev, m, f, token_fairs(ev, m, f)))
books = scan.fetch_books(s, [t.token_id for *_, tfs in pending for t in tfs]); scan._dump_gz(out / "books.json.gz", books)
rows = scan.build_rows(now, pending, books, "probe")
print(f"{pm_code} vs {odds_key}: book events {len(odds)}, credits left {remaining}")
print("PM pre-game events:", stats)
for u in unmatched: print("  unmatched:", u)
v = [r for r in rows if not r["suspect"] and not r["odds_stale"] and r["bid"] is not None and r["ask"] is not None]
gaps = sorted(abs(r["fair_minus_mid"]) for r in v if r["fair_minus_mid"] is not None)
if gaps:
    print(f"tokens {len(v)}  |fair-mid| median {gaps[len(gaps)//2]:.4f} p90 {gaps[int(len(gaps)*.9)]:.4f} max {gaps[-1]:.4f}")
    print("taker edge >= 0.01:", sum(r["taker_edge"] is not None and r["taker_edge"] >= 0.01 for r in v),
          "| hours_to_start", min(r["hours_to_start"] for r in v), "-", max(r["hours_to_start"] for r in v))
    print("suspect (gap > 0.20):", sum(r["suspect"] for r in rows))
for r in sorted(v, key=lambda r: -(r["taker_edge"] or -9))[:10]:
    print(f"  {r['event'][:55]:55s} {r['token'][:26]:26s} h={r['hours_to_start']:5.1f} fair={r['fair']:.3f} "
          f"bid={r['bid']} ask={r['ask']} edge={r['taker_edge']} +EV${r['plus_ev_usdc']:.0f} ask_sz={r['ask_size']:.0f}")

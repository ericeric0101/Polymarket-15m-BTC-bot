"""Forward recorder of Polymarket trades on sports moneyline markets (study S2, read-only, free).

    .venv/bin/python scripts/sports_trades_recorder.py

Why forward: data-api /trades returns newest first and refuses offsets past 10,000, so for
a busy game the pre-game trades (the ones a resting maker bid would meet) fall out of
reach once in-play trading piles up. Every run pages back from the newest trade until it
reaches the last timestamp already stored for that market.

Scope: the S1 leagues (bot.research.sports_odds.DEFAULT_LEAGUES); moneyline markets of
events starting within the next 7 days or started less than 6 hours ago.
Never reads data/sports_odds/ (S1 is blind); writes only data/sports_trades/:
    trades/YYYY-MM-DD.jsonl   one trade per line (UTC day of the trade)
    state.json                market conditionId -> newest stored trade timestamp
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.research.sports_odds import DEFAULT_LEAGUES, parse_time  # noqa: E402

OUT = REPO_ROOT / "data" / "sports_trades"
GAMMA_SPORTS = "https://gamma-api.polymarket.com/sports"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
TRADES = "https://data-api.polymarket.com/trades"
PAGE = 1000
MAX_OFFSET = 10_000
KEEP = ("asset", "conditionId", "side", "price", "size", "timestamp", "transactionHash", "outcome",
        "outcomeIndex", "eventSlug")
TIMEOUT = 30
PAUSE = 0.35                # data-api rate limit; 429 -> backoff and retry


def get_json(session: requests.Session, url: str, params: dict, tries: int = 6):
    """GET with backoff on 429/5xx. Returns (status, json-or-None)."""
    for i in range(tries):
        r = session.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(60, 3 * 2 ** i))
            continue
        if r.status_code == 400:
            return 400, None
        r.raise_for_status()
        time.sleep(PAUSE)
        return 200, r.json()
    raise RuntimeError(f"gave up after {tries} tries: {url}")


def markets_in_scope(session: requests.Session, now: datetime) -> list[dict]:
    series = {s["sport"]: s["series"] for s in session.get(GAMMA_SPORTS, timeout=TIMEOUT).json()
              if s.get("sport") and s.get("series")}
    out = []
    for lg in DEFAULT_LEAGUES:
        if lg not in series:
            continue
        offset = 0
        while True:
            _, page = get_json(session, GAMMA_EVENTS, {"series_id": series[lg], "closed": "false",
                                                       "limit": 100, "offset": offset})
            for e in page:
                start = parse_time(e.get("startTime"))
                if start is None or not (now - timedelta(hours=6) <= start <= now + timedelta(days=7)):
                    continue
                for m in e.get("markets") or []:
                    if m.get("sportsMarketType") == "moneyline" and m.get("conditionId"):
                        out.append({"league": lg, "condition_id": m["conditionId"], "event_id": str(e.get("id")),
                                    "start": start.isoformat()})
            if len(page) < 100 or offset > 2000:
                break
            offset += 100
    return out


def fetch_new(session: requests.Session, cid: str, since_ts: int) -> tuple[list[dict], bool]:
    """Trades with timestamp >= since_ts (newest first pages). Returns (rows, hit_offset_cap)."""
    rows, offset = [], 0
    while offset < MAX_OFFSET:
        status, page = get_json(session, TRADES, {"market": cid, "limit": PAGE, "offset": offset})
        if status == 400:
            return rows, True
        rows += [t for t in page if int(t["timestamp"]) >= since_ts]
        if len(page) < PAGE or min(int(t["timestamp"]) for t in page) < since_ts:
            return rows, False
        offset += PAGE
    return rows, True


def main() -> int:
    now = datetime.now(timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)
    state_path = OUT / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    session = requests.Session()
    markets = markets_in_scope(session, now)
    n_new = n_capped = n_failed = 0
    for mk in markets:
        by_day: dict[str, list[str]] = {}
        cid = mk["condition_id"]
        since = int(state.get(cid, 0))
        try:
            rows, capped = fetch_new(session, cid, since)
        except (RuntimeError, requests.RequestException) as exc:
            n_failed += 1
            print(f"  {cid[:12]}: {exc}")
            continue
        n_capped += capped
        seen = set()
        for t in rows:
            key = (t.get("transactionHash"), t.get("asset"), t.get("side"), t.get("price"), t.get("size"),
                   t.get("timestamp"))
            # Trades exactly at `since` were stored last run; re-stored rows are deduped at analysis.
            if key in seen:
                continue
            seen.add(key)
            rec = {k: t.get(k) for k in KEEP}
            rec.update({"league": mk["league"], "event_id": mk["event_id"], "start": mk["start"],
                        "recorded_utc": now.isoformat(timespec="seconds")})
            day = datetime.fromtimestamp(int(t["timestamp"]), timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(json.dumps(rec))
            n_new += 1
        for day, lines in by_day.items():
            path = OUT / "trades" / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write("\n".join(lines) + "\n")
        if rows:   # state only after the rows are on disk
            state[cid] = max(int(t["timestamp"]) for t in rows)
            state_path.write_text(json.dumps(state))
    print(f"trades {now.isoformat(timespec='seconds')}: markets={len(markets)} new_rows={n_new} "
          f"offset_capped={n_capped} failed={n_failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

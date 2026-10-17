"""Fetch Polymarket settlement values for scanned moneyline markets (research S1, read-only).

    .venv/bin/python scripts/sports_odds_resolve.py

Reads every data/sports_odds/scans/*/*_tokens.csv (not replays), takes markets whose
game started more than SETTLE_HOURS ago and that have no final resolution yet, and
asks Gamma for them. Appends to data/sports_odds/resolutions.jsonl, one line per
market per fetch; the last line for a market wins. Free (Polymarket only).

Prints counts only, never outcomes, so it is safe to run while the study is blind.
"""
from __future__ import annotations

import csv
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "data" / "sports_odds"
RES_PATH = OUT_ROOT / "resolutions.jsonl"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
SETTLE_HOURS = 4
BATCH = 40
TIMEOUT = 20


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except ValueError:
        return []


def load_resolutions(path: pathlib.Path = RES_PATH) -> dict[str, dict]:
    """market_id -> last stored record."""
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["market_id"]] = rec
    return out


def is_final(rec: dict) -> bool:
    return bool(rec.get("closed")) and bool(rec.get("token_values"))


def record_from_market(m: dict, fetched: str) -> dict:
    tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    prices = _json_list(m.get("outcomePrices"))
    values = {}
    if m.get("closed") and len(tokens) == len(prices) == 2:
        try:
            vals = [float(p) for p in prices]
        except (TypeError, ValueError):
            vals = []
        if len(vals) == 2 and abs(sum(vals) - 1.0) < 1e-6:
            values = dict(zip(tokens, vals))     # 0/1, or 0.5/0.5 for a void market
    return {"market_id": str(m.get("id")), "fetched_utc": fetched, "closed": bool(m.get("closed")),
            "uma_status": m.get("umaResolutionStatus"), "token_values": values}


def pending_markets(now: datetime) -> list[str]:
    done = {k for k, v in load_resolutions().items() if is_final(v)}
    due: set[str] = set()
    for path in sorted((OUT_ROOT / "scans").glob("*/*_tokens.csv")):
        if "_replay" in path.name:
            continue
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                start = datetime.fromisoformat(row["start_utc"])
                if start < now - timedelta(hours=SETTLE_HOURS) and row["market_id"] not in done:
                    due.add(row["market_id"])
    return sorted(due)


def main() -> int:
    now = datetime.now(timezone.utc)
    due = pending_markets(now)
    fetched = now.isoformat(timespec="seconds")
    session = requests.Session()
    n_final = 0
    with open(RES_PATH, "a") as fh:
        for i in range(0, len(due), BATCH):
            ids = due[i:i + BATCH]
            r = session.get(GAMMA_MARKETS_URL, params=[("id", x) for x in ids] + [("closed", "true")],
                            timeout=TIMEOUT)
            r.raise_for_status()
            for m in r.json():
                rec = record_from_market(m, fetched)
                fh.write(json.dumps(rec) + "\n")
                n_final += is_final(rec)
    print(f"resolve {fetched}: due={len(due)} newly_final={n_final} still_open={len(due) - n_final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

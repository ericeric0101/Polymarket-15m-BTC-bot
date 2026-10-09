#!/usr/bin/env python3
"""One-time, read-only fetch of Polymarket official resolutions into a versioned cache.

The only network use in the research pipeline. Afterwards every historical
analysis runs offline from the cache file named on its command line.

  python3 scripts/fetch_official_resolutions.py            # all slugs referenced by journal + exports
Output: data/research_export/official_resolution/official_resolutions_<UTC>.json
        (+ .sha256 sidecar; mirrored to RESEARCH_OFFSITE_DIR when set)
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.runtime_env import load_runtime_env  # noqa: E402
from bot.journal_path import resolve_trade_db_path  # noqa: E402

CACHE_VERSION = 1
URL_TEMPLATE = "{base}/events/slug/{slug}"


def referenced_slugs(journal: Path, export_root: Path) -> set[str]:
    slugs: set[str] = set()
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        for (pj,) in conn.execute("SELECT payload_json FROM strategy_events WHERE event_type IN "
                                  "('MARKET_SETTLEMENT','SHADOW_SIM_CYCLE_RESULT','MARKET_CYCLE_PNL')"):
            slug = json.loads(pj or "{}").get("slug")
            if slug:
                slugs.add(slug)
        for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type IN "
                                  "('ORDER_FILLED','FILL_MARKOUT','SHADOW_SIM_ENTRY_FILLED')"):
            d = json.loads(pj or "{}")
            slug = d.get("slug") or d.get("market_slug")
            if slug:
                slugs.add(slug)
    finally:
        conn.close()
    for path in glob.glob(str(export_root / "A_market_summary" / "*.csv")):
        for row in csv.DictReader(open(path)):
            if row["settlement_side"] or row["n_native_v2_snapshots"] not in ("", "0"):
                slugs.add(row["market_slug"])
    return {s for s in slugs if s.startswith("btc-updown-15m-")}


def fetch_one(client: httpx.Client, base: str, slug: str) -> dict:
    url = URL_TEMPLATE.format(base=base, slug=quote(slug, safe=""))
    record = {"slug": slug, "source_url": url}
    for attempt in range(3):
        try:
            response = client.get(url)
            record["http_status"] = response.status_code
            if response.status_code != 200:
                time.sleep(1 + attempt)
                continue
            event = response.json()
            market = (event.get("markets") or [{}])[0]
            outcomes = market.get("outcomes")
            prices = market.get("outcomePrices")
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
            prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
            winners = [str(o).upper() for o, p in zip(outcomes, prices) if float(p) > 0.99]
            record.update({
                "event_slug": event.get("slug"), "condition_id": market.get("conditionId"),
                "closed": market.get("closed"), "uma_resolution_status": market.get("umaResolutionStatus"),
                "end_date": market.get("endDate"), "outcomes": outcomes, "outcome_prices": prices,
                "official_outcome": winners[0] if len(winners) == 1 and winners[0] in ("UP", "DOWN") else "UNRESOLVED",
            })
            return record
        except (httpx.HTTPError, ValueError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            time.sleep(1 + attempt)
    record["official_outcome"] = "FETCH_FAILED"
    return record


def main() -> int:
    load_runtime_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=None, help="trade journal (default: canonical TRADE_DB_PATH)")
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--out-dir", default="data/research_export/official_resolution")
    args = ap.parse_args()
    args.journal = str(resolve_trade_db_path(args.journal))
    base = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
    slugs = sorted(referenced_slugs(Path(args.journal), Path(args.export)))
    started = datetime.now(timezone.utc)
    with httpx.Client(timeout=10.0, headers={"User-Agent": "btc15-research-cache/1"}) as client:
        with ThreadPoolExecutor(4) as pool:
            markets = list(pool.map(lambda s: fetch_one(client, base, s), slugs))
    body = json.dumps(markets, sort_keys=True, separators=(",", ":"))
    content_sha = hashlib.sha256(body.encode()).hexdigest()
    payload = {"cache_version": CACHE_VERSION, "source": "Polymarket Gamma API (read-only)",
               "url_template": URL_TEMPLATE.format(base=base, slug="<slug>"),
               "fetched_at_utc": started.isoformat(), "n_markets": len(markets),
               "content_sha256": content_sha, "markets": markets}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"official_resolutions_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=0, sort_keys=True))
    os.replace(tmp, target)
    file_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".json.sha256").write_text(f"{file_sha}  {target.name}\n")
    offsite = os.getenv("RESEARCH_OFFSITE_DIR")
    if offsite:
        mirror = Path(offsite).expanduser() / "official_resolution"
        mirror.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, mirror / target.name)
        shutil.copyfile(target.with_suffix(".json.sha256"), mirror / (target.name + ".sha256"))
    from collections import Counter
    print(json.dumps({"file": str(target), "file_sha256": file_sha, "content_sha256": content_sha,
                      "n": len(markets), "outcomes": Counter(m["official_outcome"] for m in markets)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Market-level settlement-flip rates with day-level inference.

Prior weekday/weekend reports tested rows (markets) as independent and kept only
"complete" markets, which made the result depend on pseudo-replication and on
an exclusion that was itself associated with the outcome. This tool:

* uses one observation per market (the 15-minute window) and reports N_days;
* compares groups only through a permutation of DAY labels, printing the number
  of distinct permutations and the minimum attainable p-value;
* always reports the same statistic for the excluded (incomplete) markets so
  that outcome-associated exclusion is visible next to the headline number.

flip(k) = sign(official TWAP - strike) at T-k (latest fresh native-v2 snapshot
within 5 s before the cutoff) differs from the canonical settlement side.
Weekend is Saturday/Sunday in UTC unless --tz is given.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.research.store import ResearchStore  # noqa: E402

CUTOFFS = (300, 180, 120, 60, 30)
CUTOFF_TOLERANCE_SEC = 5.0
MAX_GAP_SEC = 10.0
MAX_FIRST_OFFSET_SEC = 30.0


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def day_permutation_test(by_day: dict[str, tuple[int, int]], group_a_days: Iterable[str]) -> dict[str, Any]:
    """One-sided test of rate(B) - rate(A) > 0 by permuting day labels.

    With few days the attainable p-value is bounded below by 1 / #permutations;
    callers must report it rather than a row-level p-value.
    """
    days = sorted(by_day)
    group_a = set(group_a_days) & set(days)
    if not group_a or len(group_a) == len(days):
        return {"observed_diff": None, "p_one_sided": None, "distinct_permutations": 0, "min_attainable_p": None}

    def diff(a_days: set[str]) -> float | None:
        ka = sum(by_day[d][0] for d in days if d in a_days)
        na = sum(by_day[d][1] for d in days if d in a_days)
        kb = sum(by_day[d][0] for d in days if d not in a_days)
        nb = sum(by_day[d][1] for d in days if d not in a_days)
        return None if not na or not nb else kb / nb - ka / na

    observed = diff(group_a)
    stats = [diff(set(combo)) for combo in itertools.combinations(days, len(group_a))]
    stats = [value for value in stats if value is not None]
    p_value = sum(1 for value in stats if value >= observed - 1e-12) / len(stats)
    return {"observed_diff": observed, "p_one_sided": p_value, "distinct_permutations": len(stats),
            "min_attainable_p": 1 / len(stats)}


def market_rows(store: ResearchStore, *, tz: ZoneInfo) -> list[dict[str, Any]]:
    settlements = {}
    for item in store.get_settlements():
        side = item.get("canonical_settlement_side")
        if side in ("UP", "DOWN") and item.get("settlement_reference_is_canonical") is True:
            settlements[item["market_slug"]] = item
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in store.get_prediction_snapshots(provenance="NATIVE_V2"):
        by_market[snapshot["market_slug"]].append(snapshot)
    rows = []
    for slug, snapshots in sorted(by_market.items()):
        if slug not in settlements:
            continue
        try:
            start = float(slug.rsplit("-", 1)[-1])
        except ValueError:
            continue
        end = start + 900
        inside = sorted((s for s in snapshots if start <= float(s["snapshot_ts"]) <= end), key=lambda s: s["snapshot_ts"])
        times = [float(s["snapshot_ts"]) for s in inside]
        if not times:
            continue
        max_gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)
        complete = (times[0] - start <= MAX_FIRST_OFFSET_SEC and end - times[-1] <= MAX_GAP_SEC
                    and max_gap <= MAX_GAP_SEC)
        settle = settlements[slug]["canonical_settlement_side"]
        flips = {}
        for cutoff in CUTOFFS:
            target = end - cutoff
            fresh = [s for s in inside if target - CUTOFF_TOLERANCE_SEC <= float(s["snapshot_ts"]) <= target
                     and s.get("twap_fresh") is True and s.get("official_twap") is not None and s.get("strike")]
            if not fresh:
                continue
            twap, strike = float(fresh[-1]["official_twap"]), float(fresh[-1]["strike"])
            if twap == strike:
                continue
            flips[cutoff] = int(("UP" if twap > strike else "DOWN") != settle)
        local_day = datetime.fromtimestamp(start, tz)
        rows.append({"market_slug": slug, "day": local_day.strftime("%Y-%m-%d"),
                     "weekend": local_day.weekday() >= 5, "complete": complete, "max_gap_sec": max_gap,
                     "twap_cross_count": settlements[slug].get("twap_cross_count"), "flip": flips})
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"unit": "market", "inference": "day_label_permutation", "cutoffs": {}}
    for cutoff in CUTOFFS:
        result = {}
        for label, subset in (("complete", [r for r in rows if r["complete"]]),
                              ("excluded_incomplete", [r for r in rows if not r["complete"]])):
            by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            weekend_days = set()
            for row in subset:
                if cutoff not in row["flip"]:
                    continue
                by_day[row["day"]][0] += row["flip"][cutoff]
                by_day[row["day"]][1] += 1
                if row["weekend"]:
                    weekend_days.add(row["day"])
            k = sum(v[0] for v in by_day.values())
            n = sum(v[1] for v in by_day.values())
            frozen = {day: (v[0], v[1]) for day, v in by_day.items()}
            result[label] = {"flips": k, "n_markets": n, "n_days": len(frozen),
                             "rate": k / n if n else None, "wilson95": wilson(k, n),
                             "per_day": {d: {"flips": a, "n_markets": b} for d, (a, b) in sorted(frozen.items())},
                             "weekday_minus_weekend": day_permutation_test(frozen, weekend_days)}
        output["cutoffs"][f"T-{cutoff}"] = result
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--tz", default="UTC", help="timezone that defines calendar days and weekends")
    args = parser.parse_args()
    store = ResearchStore(args.db)
    rows = market_rows(store, tz=ZoneInfo(args.tz))
    result = summarize(rows)
    result.update({"db": args.db, "timezone": args.tz, "native_v2_exclusions": dict(store.prediction_exclusions),
                   "markets_settled_with_snapshots": len(rows)})
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

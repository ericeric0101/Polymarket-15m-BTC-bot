#!/usr/bin/env python3
"""Market-level flip statistics from the audit manifest (never pooled across tiers).

Reads: ../../data_validity_manifest.csv, evidence/cutoff_leaders.csv (both produced by build_manifest.py).
flip(k) = sign(official 60 s TWAP - strike) at T-k (latest fresh snapshot in [T-k-5 s, T-k]) != canonical settlement side.
Weekend = Sat/Sun in UTC (2026-10-03, 2026-10-04).
"""
import csv, itertools, math, random
from collections import defaultdict
from pathlib import Path

EV = Path(__file__).resolve().parent
MAN = EV.parents[1] / "data_validity_manifest.csv"
man = {r["market_id"]: r for r in csv.DictReader(MAN.open())}
cut = list(csv.DictReader((EV / "cutoff_leaders.csv").open()))
WEEKEND = {"2026-10-03", "2026-10-04"}


def wilson(k, n, z=1.959964):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def rows_for(tier, k):
    out = []
    for r in cut:
        m = man[r["market_id"]]
        if m["tier"] != str(tier) or int(r["cutoff_sec"]) != k or r["leader"] not in ("UP", "DOWN"):
            continue
        out.append((m["date_utc"], int(r["flip"])))
    return out


def day_bootstrap_diff(by_day_a, by_day_b, B=5000, seed=7):
    rng = random.Random(seed)
    da, db = list(by_day_a.items()), list(by_day_b.items())
    vals = []
    for _ in range(B):
        sa = [rng.choice(da)[1] for _ in da]
        sb = [rng.choice(db)[1] for _ in db]
        ka, na = sum(x[0] for x in sa), sum(x[1] for x in sa)
        kb, nb = sum(x[0] for x in sb), sum(x[1] for x in sb)
        if na and nb:
            vals.append(kb / nb - ka / na)
    vals.sort()
    return (round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals))], 3)) if vals else None


def day_permutation(by_day, weekend_days):
    days = sorted(by_day)
    obs = None
    stats = []
    for combo in itertools.combinations(days, len(weekend_days)):
        we = set(combo)
        ka = sum(by_day[d][0] for d in days if d in we); na = sum(by_day[d][1] for d in days if d in we)
        kb = sum(by_day[d][0] for d in days if d not in we); nb = sum(by_day[d][1] for d in days if d not in we)
        diff = (kb / nb if nb else 0) - (ka / na if na else 0)
        stats.append(diff)
        if we == set(weekend_days):
            obs = diff
    p_one = sum(1 for s in stats if s >= obs - 1e-12) / len(stats)
    return round(obs, 3), round(p_one, 3), len(stats), round(1 / len(stats), 3)


def main():
    print("Unit = market; tiers reported separately; no cross-tier pooling.")
    for tier in (1, 2):
        print(f"\n=== TIER {tier} ===")
        for k in (300, 180, 120, 60, 30):
            rs = rows_for(tier, k)
            by_day = defaultdict(lambda: [0, 0])
            for d, f in rs:
                by_day[d][0] += f; by_day[d][1] += 1
            n = len(rs); kf = sum(f for _, f in rs)
            line = f"T-{k:>3}: flips={kf}/{n} rate={kf / n if n else float('nan'):.3f} wilson95={wilson(kf, n)} N_days={len(by_day)} per_day={dict(sorted((d, tuple(v)) for d, v in by_day.items()))}"
            print(line)
            if tier == 2:
                we = {d: v for d, v in by_day.items() if d in WEEKEND}
                wd = {d: v for d, v in by_day.items() if d not in WEEKEND}
                kwe, nwe = sum(v[0] for v in we.values()), sum(v[1] for v in we.values())
                kwd, nwd = sum(v[0] for v in wd.values()), sum(v[1] for v in wd.values())
                if nwe and nwd:
                    obs, p, nperm, pmin = day_permutation(dict(by_day), sorted(we))
                    print(f"        weekend {kwe}/{nwe}={kwe / nwe:.3f} {wilson(kwe, nwe)} N_days={len(we)} | weekday {kwd}/{nwd}={kwd / nwd:.3f} {wilson(kwd, nwd)} N_days={len(wd)}"
                          f" | diff(wd-we)={obs} day-bootstrap95={day_bootstrap_diff(we, wd)} perm_p_one_sided={p} distinct_perms={nperm} min_attainable_p={pmin}")


if __name__ == "__main__":
    main()

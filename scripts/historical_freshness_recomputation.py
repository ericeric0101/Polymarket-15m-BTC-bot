"""Offline freshness sensitivity. Never opens a live SQLite database for writing.

Primary outcomes/checkpoints reuse research_analysis and official TWAP summaries.
No offset fit, carry-forward, outcome-conditioned freshness or live strategy instance.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import subprocess

from scripts import research_analysis as ra
from scripts import four_market_prediction_forensics as ff
from scripts.strike_flip_risk_analysis import crossing_count
from bot.entry_session_policy import TAIPEI

COMPONENTS = ("p_ex", "market", "BTC", "TWAP", "joint")
ORIGINAL_FLAGS = ("p_ex_fresh", "market_mid_fresh", "btc_fresh", "twap_fresh", "joint_fresh")
CHECKPOINTS = ra.PRIMARY_CHECKPOINTS
OLD_CUTOFF = datetime.fromisoformat("2026-10-06T20:44:17.342088+08:00").timestamp()
OLD_COUNTS = {"PRIMARY_WEEKDAY": 53, "WEEKEND_PRIMARY": 51, "WEEKDAY_SENSITIVITY": 58}
# Canonical six/five boundaries are function-local in research_analysis.py:1151.
# Analysis cannot move that production helper; use the documented fixed values.
SIGMA_BINS = ((0,.5,"<0.5"),(.5,1,"0.5-1"),(1,2,"1-2"),(2,3,"2-3"),(3,5,"3-5"),(5,math.inf,">5"))
BPS_BINS = ((0,2,"0-2"),(2,5,"2-5"),(5,10,"5-10"),(10,20,"10-20"),(20,math.inf,">20"))


def readonly(path):
    # Intended input is an already completed offline snapshot, never an active DB.
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class TickEvidence:
    """Exact persisted last aggTrade evidence, not reconstructed bar interiors.

    Each last endpoint binds source timestamp, local receipt and its own close.
    A completed bar can be read later, but only endpoint observations received
    at/before the target are eligible. Source-clock anchors are prior endpoints.
    Millisecond receipt rounding is conservatively handled with an upper bound.
    """

    def __init__(self, points):
        self.points = sorted(set(points), key=lambda p: (p[1], p[0], p[2]))
        self.receipts = [(p[1] + 1) / 1000 for p in self.points]
        self.by_source = defaultdict(list)
        self.source_max = []
        maximum = 0
        for source, received, price in self.points:
            self.by_source[source].append((received, price))
            maximum = max(maximum, source)
            self.source_max.append(maximum)

    def exact(self, source_ts, now, limit=10.0):
        if ra._num(source_ts) is None or source_ts <= 0:
            return None, "MISSING_REQUIRED_FIELD"
        source = round(source_ts * 1000)
        eligible = [(recv, px) for recv, px in self.by_source.get(source, [])
                    if (recv + 1) / 1000 <= now]
        if not eligible or len({px for _, px in eligible}) != 1:
            return None, "NOT_RECOMPUTABLE"
        # Repeated receipts do not rejuvenate an old observation.
        recv, price = min(eligible)
        if now - recv / 1000 > limit:
            return None, "STALE_VALID"
        i = bisect.bisect_right(self.receipts, now) - 1
        if i < 0:
            return None, "NOT_RECOMPUTABLE"
        source_age = (self.source_max[i] - source) / 1000
        if not 0 <= source_age <= limit:
            return None, "STALE_VALID"
        return {"price": price, "receipt_ts": recv / 1000,
                "same_clock_value_age": source_age}, "FRESH_RECOMPUTED"


def load_tick_evidence(root, days):
    import pyarrow.parquet as pq
    points, inventory, failures = [], [], []
    for path in sorted(Path(root).glob("BTCUSDT_1s_*.parquet")):
        if not any(day in path.name for day in days):
            continue
        before = (path.stat().st_mtime_ns, path.stat().st_size, digest(path))
        try:
            columns = ["open", "close", "first_source_ts", "last_source_ts",
                       "first_received_ts", "last_received_ts"]
            for row in pq.read_table(path, columns=columns).to_pylist():
                # Only final max-source endpoint can match the snapshot's latest
                # aggTrade. The bar's first tick cannot prove latest-state value.
                for prefix, price_key in (("last", "close"),):
                    src, recv, px = row[prefix + "_source_ts"], row[prefix + "_received_ts"], row[price_key]
                    if src and recv and ra._num(px) is not None and px > 0:
                        points.append((int(src), int(recv), float(px)))
        except Exception as exc:
            failures.append({"file": path.name, "error_class": type(exc).__name__})
        after = (path.stat().st_mtime_ns, path.stat().st_size, digest(path))
        if before != after:
            raise RuntimeError("Parquet input changed during read: " + path.name)
        inventory.append({"file": path.name, "sha256": before[2], "mtime_ns": before[0], "size": before[1]})
    return TickEvidence(points), inventory, failures


def classify_row(original, ticks):
    """Preserve strict original evidence; only exact BTC tuples can recover NULL.

    Receipt-only market liveness does not restore stale-cleared BBO values.
    Original true values remain explicitly ORIGINAL, not independently repaired.
    """
    r = dict(original)
    r["freshness_provenance_class"] = "HISTORICAL_RECOMPUTED"
    labels, reasons = {}, {}
    required = {"p_ex": ("p_up_ex_market", "p_ex_source_ts"),
                "market": ("market_quote_up_received_ts", "market_quote_down_received_ts"),
                "BTC": ("btc_source_ts",), "TWAP": ("official_twap", "twap_source_ts")}
    for component, flag in zip(COMPONENTS[:-1], ORIGINAL_FLAGS[:-1]):
        value_ok = {"p_ex": ra._num(original.get("p_up_ex_market")) is not None,
                    "market": ff._up_mid(original)[0] is not None,
                    "BTC": ra._num(original.get("btc_spot")) is not None,
                    "TWAP": ra._num(original.get("official_twap")) is not None}
        ts_key={"p_ex":"p_ex_source_ts","BTC":"btc_source_ts","TWAP":"twap_source_ts"}.get(component)
        if ts_key and (ra._num(original.get(ts_key)) is None or original[ts_key]<=0):
            labels[component] = "MISSING_REQUIRED_FIELD"
            reasons[component] = "NO_REQUIRED_TIMESTAMP"
        elif original.get(flag) is True and value_ok[component]:
            labels[component] = "FRESH_ORIGINAL"
            reasons[component] = "ORIGINAL_STRICT_POLICY_RETAINED"
        elif any(key not in original for key in required[component]):
            labels[component] = "MISSING_REQUIRED_FIELD"
            reasons[component] = "NO_REQUIRED_TIMESTAMP_OR_VALUE_FIELD"
        else:
            labels[component] = "NOT_RECOMPUTABLE"
            reasons[component] = "STALE_CLEARED_VALUE_OR_UNPROVEN_CLOCK"
    if original.get("btc_fresh") is not True:
        evidence, status = ticks.exact(original.get("btc_source_ts"), original["snapshot_ts"])
        labels["BTC"] = status
        if evidence is not None:
            r["btc_fresh"], r["btc_spot"] = True, evidence["price"]
            r["btc_recomputed_receipt_ts"] = evidence["receipt_ts"]
            r["btc_recomputed_value_age_sec"] = evidence["same_clock_value_age"]
            reasons["BTC"] = "LOCAL_RECEIPT_AUTHORITY+SAME_CLOCK_DOMAIN"
        else:
            reasons["BTC"] = "EXACT_ENDPOINT_UNAVAILABLE_OR_STALE"
    # Check already-retained market values against the same-authority receipt.
    if labels["market"] == "FRESH_ORIGINAL":
        sides = [s for s in ("up", "down") if original.get("market_mid_" + s + "_fresh") is True]
        ages = [original["snapshot_ts"] - original["market_quote_" + s + "_received_ts"]
                for s in sides if ra._num(original.get("market_quote_" + s + "_received_ts")) is not None]
        if not ages:
            labels["market"] = "MISSING_REQUIRED_FIELD"
        elif not any(0 <= a <= 2 for a in ages):
            labels["market"] = "STALE_VALID"
    good = {"FRESH_ORIGINAL", "FRESH_RECOMPUTED"}
    r["joint_fresh"] = all(labels[c] in good for c in COMPONENTS[:-1])
    if r["joint_fresh"]:
        labels["joint"] = "FRESH_ORIGINAL" if original.get("joint_fresh") is True else "FRESH_RECOMPUTED"
        reasons["joint"] = "ORIGINAL_STRICT_POLICY_RETAINED" if labels["joint"] == "FRESH_ORIGINAL" else "LOCAL_RECEIPT_AUTHORITY+SAME_CLOCK_DOMAIN"
    else:
        labels["joint"] = ("MISSING_REQUIRED_FIELD" if "MISSING_REQUIRED_FIELD" in labels.values()
                           else "NOT_RECOMPUTABLE" if "NOT_RECOMPUTABLE" in labels.values() else "STALE_VALID")
        reasons["joint"] = "UNRESOLVED_OR_STALE_COMPONENT"
    return r, labels, reasons


def load_inputs(db, journal, cutoff, run_id=None):
    groups = defaultdict(list)
    store = ra.ResearchStore(db)
    for r in store.get_prediction_snapshots(end_ts=cutoff, run_id=run_id, provenance="HISTORICAL_RECOMPUTATION_INPUT"):
        if ra.market_context(r["market_slug"])["market_start_taipei"][:10] in {"2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"}:
            groups[r["market_slug"]].append(r)
    summaries = {}
    for s in store.get_settlements():
        if run_id is not None and s["run_id"]!=run_id:
            continue
        if s["summary_epoch_ns"] / 1e9 <= cutoff:
            key = (s["run_id"], s["market_slug"])
            if key not in summaries or s["summary_epoch_ns"] > summaries[key]["summary_epoch_ns"]:
                summaries[key] = s
    strikes, runs = defaultdict(list), {}
    with readonly(journal) as c:
        for run, raw in c.execute("SELECT run_id,notes_json FROM strategy_runs"):
            manifest = json.loads(raw or "{}").get("run_manifest") or {}
            runs[run] = {k: manifest.get(k) for k in ("git_commit", "runtime_git_revision", "cycle_idx", "process_instance_id")}
        for run, ts, raw in c.execute("SELECT run_id,ts,payload_json FROM strategy_events WHERE event_type='MARKET_STRIKE_LOCKED'"):
            when = datetime.fromisoformat(ts).timestamp()
            if when > cutoff:
                continue
            p = json.loads(raw or "{}"); slug = p.get("slug") or p.get("market_slug")
            if p.get("authoritative") is True and p.get("strike_status") == "verified" and p.get("strike_source") == "polymarket_crypto_price_twap_open":
                strikes[(run, slug)].append((when, float(p["strike"])))
    return groups, summaries, strikes, runs


def exact(row, strikes):
    value = ra._num(row.get("strike"))
    return value is not None and value>0 and any(ts <= row["snapshot_ts"] and abs(v - value) < 1e-6
                                    for ts, v in strikes.get((row["run_id"], row["market_slug"]), []))


def gate(slug, rows, summaries, strikes, cutoff):
    """The mid-check gate; freshness can change, all collection criteria remain."""
    span = rows[-1]["snapshot_ts"] - rows[0]["snapshot_ts"]
    gap = max((b["snapshot_ts"]-a["snapshot_ts"] for a,b in zip(rows, rows[1:])), default=0)
    ids = {r["run_id"] for r in rows}
    summary = summaries.get((rows[0]["run_id"], slug), {})
    if int(slug.rsplit("-", 1)[1]) + 900 > cutoff:
        return "ACTIVE", {}, summary
    if summary.get("canonical_settlement_side") not in {"UP", "DOWN"} or summary.get("settlement_reference_is_canonical") is not True:
        return "NO_CANONICAL_SETTLEMENT", {}, summary
    if len(ids) != 1 or gap > 15:
        return "INTERRUPTED", {}, summary
    if span < 600:
        return "INSUFFICIENT_COLLECTION_SPAN", {}, summary
    if any(r.get("cycle_idx") is not None for r in rows) and len({r.get("cycle_idx") for r in rows if r.get("cycle_idx") is not None}) > 1:
        return "CYCLE_CONFLICT", {}, summary
    joint = ff._joint_rows([{**r, "up_mid": ff._up_mid(r)[0]} for r in rows])
    if sum(r.get("joint_fresh") is True for r in rows)/len(rows) < .25:
        return "LOW_JOINT_FRESHNESS", {}, summary
    cp = {h: ra._checkpoint_row(joint, h) for h in CHECKPOINTS}
    if not all(r and r.get("settlement_state_side") in {"UP", "DOWN"} for r in cp.values()):
        return "MISSING_FRESH_CHECKPOINT", {}, summary
    if not all(exact(r, strikes) and r.get("twap_fresh") is True for r in cp.values()):
        return "NO_EXACT_STRIKE", {}, summary
    return "SYNCHRONIZED_USABLE", cp, summary


def wilson(k, n):
    if not n:
        return [None, None]
    z=1.95996398454; p=k/n; d=1+z*z/n
    c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [c-h,c+h]


def binary(values):
    n=len(values); k=sum(values)
    return {"k": k, "N": n, "pct": 100*k/n if n else None, "wilson": wilson(k,n)}


def difference(a, b):
    k,n,l,m=a["k"],a["N"],b["k"],b["N"]
    if not n or not m:
        return {"diff_pp": None, "newcombe": None, "fisher": None}
    p,q=k/n,l/m; al,au=a["wilson"];bl,bu=b["wilson"]
    ci=[p-q-math.hypot(p-al,bu-q),p-q+math.hypot(au-p,q-bl)]
    s=k+l;den=math.comb(n+m,s)
    prob=lambda x:math.comb(n,x)*math.comb(m,s-x)/den
    observed=prob(k)
    f=min(1,sum(prob(x) for x in range(max(0,s-m),min(n,s)+1) if prob(x)<=observed+1e-12))
    return {"diff_pp":100*(p-q), "newcombe":[100*v for v in ci], "fisher":f}


def describe(values):
    v=[x for x in values if ra._num(x) is not None]
    return {"N":len(v), "median":statistics.median(v) if v else None,
            "P25":ra._percentile(v,.25), "P75":ra._percentile(v,.75),
            "mean":statistics.mean(v) if v else None}


def metrics(slugs, records, corrected, cp_key="new_cp"):
    output={}
    for h in CHECKPOINTS:
        vals=[]
        for s in slugs:
            r=records[s][cp_key][h];summary=records[s]["summary"]
            market=ff._up_mid(r)[0];pex=ra._num(r.get("p_up_ex_market"))
            joint=ff._joint_rows([{**x,"up_mid":ff._up_mid(x)[0]} for x in corrected[s]])
            observations=[{"close_ts":x["snapshot_ts"],"close":x["official_twap"]} for x in joint
                          if x["snapshot_ts"]>=r["snapshot_ts"] and ra._num(x.get("official_twap")) is not None
                          and ra._num(x.get("strike")) is not None and abs(x["strike"]-r["strike"])<1e-6
                          and x.get("twap_fresh") is True]
            prior = records[s][cp_key].get(CHECKPOINTS[CHECKPOINTS.index(h)-1]) if h!=300 else None
            previous_mid = ff._up_mid(prior)[0] if prior else None
            vals.append({"flip":r["settlement_state_side"]!=summary["canonical_settlement_side"],
                         "distance_usd":abs(r["official_twap"]-r["strike"]),
                         "distance_bps":abs(r["official_twap"]-r["strike"])/r["strike"]*10000,
                         "market":market,"checkpoint_path_change":market-previous_mid if previous_mid is not None else None,
                         "divergence":pex-market if pex is not None else None,
                         "agreement":(pex>=.5)==(market>=.5) if pex is not None else None,
                         "crossings":crossing_count(observations,r["strike"],r["snapshot_ts"],int(s.rsplit("-",1)[1])+900,include_left_context=False),
                         "sigma":ra._num(r.get("required_move_sigma")),"bps":abs(r["required_move_bps"]) if ra._num(r.get("required_move_bps")) is not None else None})
        entry={"flip":binary([v["flip"] for v in vals]),
               "crossings":binary([v["crossings"]>0 for v in vals]),
               "agreement":binary([v["agreement"] for v in vals if v["agreement"] is not None])}
        for key in ("distance_usd","distance_bps","market","checkpoint_path_change","divergence","sigma","bps"):
            entry[key]=describe([v[key] for v in vals])
        entry["abs_divergence"]=describe([abs(v["divergence"]) for v in vals if v["divergence"] is not None])
        entry["bins"]={}
        for field,bins in (("sigma",SIGMA_BINS),("bps",BPS_BINS)):
            entry["bins"][field]=[{"bin":label,**binary([v["flip"] for v in vals if ra._fixed_bin(v[field],bins)==label and (field!="bps" or v[field]!=0)])}
                                    for _,_,label in bins]
            if field=="bps":
                entry["bins"][field].append({"bin":"zero",**binary([v["flip"] for v in vals if v[field]==0])})
            entry["bins"][field].append({"bin":"missing","N":sum(v[field] is None for v in vals)})
        output[str(h)]=entry
    return output


def compare_metrics(left, right):
    result = {}
    for horizon in left:
        a,b=left[horizon],right[horizon]
        result[horizon]={key:difference(a[key],b[key]) for key in ("flip","crossings","agreement")}
        for field in ("sigma","bps"):
            result[horizon][field+"_bins"]={x["bin"]:difference(x,y) for x,y in zip(a["bins"][field],b["bins"][field]) if "k" in x and "k" in y}
    return result


def detail_tables(cohorts, binary_comparisons):
    """Compact report tables; full-precision numbers remain in local JSON."""
    def number(v):
        return "N/A" if v is None else f"{v:.5f}"
    def distribution(d):
        return f'N={d["N"]}; {number(d["median"])} [{number(d["P25"])}, {number(d["P75"])}]; mean {number(d["mean"])}'
    def rate(v):
        lo,hi=v["wilson"]
        return f'{v["k"]}/{v["N"]} ({number(v["pct"])}%); [{number(lo)}, {number(hi)}]'
    lines=[]
    for title,fields in (("8. Strike/path",("distance_usd","distance_bps","market","checkpoint_path_change")),
                         ("9. p_ex comparison",("divergence","abs_divergence")),
                         ("10. Sigma/bps sensitivity",("sigma","bps"))):
        lines += ["", "## "+title, "", "Continuous format: N; median [P25, P75]; mean. One market per horizon; no new model/bootstrap.", "",
                  "| Cohort | Horizon | Metric | Distribution |", "|---|---|---|---|"]
        for name in ("PRIMARY_WEEKDAY","WEEKEND_PRIMARY"):
            for h,v in cohorts[name]["metrics"].items():
                for field in fields:
                    lines.append(f'|{name}|T-{h}|{field}|{distribution(v[field])}|')
    lines += ["", "Fixed sigma/bps bins (zero/missing separate); intervals are proportions, differences are pp. Full precision in JSON.", "",
              "| Horizon | Field/bin | Weekday k/N (%) Wilson | Weekend k/N (%) Wilson | Difference pp Newcombe | Fisher |",
              "|---|---|---|---|---|---|"]
    for h in map(str,CHECKPOINTS):
        for field in ("sigma","bps"):
            a=cohorts["PRIMARY_WEEKDAY"]["metrics"][h]["bins"][field]
            b=cohorts["WEEKEND_PRIMARY"]["metrics"][h]["bins"][field]
            for x,y in zip(a,b):
                if "k" not in x:
                    lines.append(f'|T-{h}|{field}/missing|N={x["N"]}|N={y["N"]}|N/A|N/A|')
                    continue
                d=binary_comparisons["PRIMARY"][h][field+"_bins"][x["bin"]]
                ci=d["newcombe"]
                lines.append(f'|T-{h}|{field}/{x["bin"]}|{rate(x)}|{rate(y)}|{number(d["diff_pp"])} {ci}|{number(d["fisher"])}|')
    return lines


def run(db, journal, parquet, output, cutoff=OLD_CUTOFF):
    for path in (db,journal):
        if "analysis_snapshots" not in Path(path).parts:
            raise ValueError("Use a completed offline analysis_snapshots DB, not an active database")
    hashes={str(p):digest(p) for p in (db,journal)}
    groups,summaries,strikes,runs=load_inputs(db,journal,cutoff)
    ticks,inventory,failures=load_tick_evidence(parquet,{"2026-10-03","2026-10-04","2026-10-05","2026-10-06"})
    records,corrected,row_output={}, {}, []
    for slug,rows in groups.items():
        corrected[slug]=[]
        for row in rows:
            fixed,labels,reasons=classify_row(row,ticks);corrected[slug].append(fixed)
            ctx=ra.market_context(slug);identity=runs.get(row["run_id"],{})
            row_output.append({"market_slug":slug,"snapshot_ts":row["snapshot_ts"],"run_id":row["run_id"],
                               "date":ctx["market_start_taipei"][:10],"regime":ctx["session_regime"],
                               "commit":identity.get("git_commit") or identity.get("runtime_git_revision") or "LEGACY_UNKNOWN",
                               **{c+"_classification":labels[c] for c in COMPONENTS},
                               **{c+"_reason":reasons[c] for c in COMPONENTS}})
        old,oldcp,summary=gate(slug,rows,summaries,strikes,cutoff)
        new,newcp,_=gate(slug,corrected[slug],summaries,strikes,cutoff)
        records[slug]={"old_status":old,"new_status":new,"old_cp":oldcp,"new_cp":newcp,"summary":summary,
                       "date":ra.market_context(slug)["market_start_taipei"][:10],
                       "run_id":rows[0]["run_id"],"rows":len(rows)}
    cohorts={}
    for name,days in (("PRIMARY_WEEKDAY",{"2026-10-06"}),("WEEKEND_PRIMARY",{"2026-10-03","2026-10-04"}),
                      ("WEEKDAY_SENSITIVITY",{"2026-10-05","2026-10-06"})):
        old={s for s,r in records.items() if r["date"] in days and r["old_status"]=="SYNCHRONIZED_USABLE"}
        new={s for s,r in records.items() if r["date"] in days and r["new_status"]=="SYNCHRONIZED_USABLE"}
        if len(old)!=OLD_COUNTS[name]:
            raise RuntimeError(f"Old cohort reproduction failed: {name} {len(old)} != {OLD_COUNTS[name]}")
        cohorts[name]={"old_N":len(old),"new_N":len(new),"added":sorted(new-old),"removed":sorted(old-new),
                       "unchanged":sorted(old&new),"slugs":sorted(new),"metrics":metrics(sorted(new),records,corrected),
                       "old_metrics":metrics(sorted(old),records,groups,cp_key="old_cp")}
    oldflips={300:(13,2),180:(10,1),120:(8,0),60:(4,0),30:(3,0)}
    for h,(a,b) in oldflips.items():
        actual=(cohorts["PRIMARY_WEEKDAY"]["old_metrics"][str(h)]["flip"]["k"],cohorts["WEEKEND_PRIMARY"]["old_metrics"][str(h)]["flip"]["k"])
        if actual!=(a,b):raise RuntimeError("Old flip result reproduction failed: "+str((h,actual)))
    # Existing latest-runtime baseline is ad7cc22, fixed at the old cutoff.
    previous_latest=[s for s in cohorts["PRIMARY_WEEKDAY"]["slugs"] if str(runs.get(records[s]["run_id"],{}).get("runtime_git_revision") or "").startswith("ad7cc22")]
    cohorts["PREVIOUS_RUNTIME_SENSITIVITY"]={"old_N":8,"new_N":len(previous_latest),"slugs":previous_latest,"metrics":metrics(previous_latest,records,corrected)}
    with readonly(db) as connection:
        latest_run, latest_ns=connection.execute("SELECT run_id,MAX(decision_epoch_ns) AS last FROM lead_lag_decisions GROUP BY run_id ORDER BY last DESC LIMIT 1").fetchone()
    lg,ls,lstrike,_=load_inputs(db,journal,latest_ns/1e9,run_id=latest_run)
    latest_records,latest_corrected,latest_row_counts,latest_row_output={}, {}, Counter(), []
    for slug,rows in lg.items():
        latest_corrected[slug]=[]
        for r in rows:
            fixed,labels,reasons=classify_row(r,ticks);latest_corrected[slug].append(fixed)
            latest_row_counts.update((c,labels[c]) for c in COMPONENTS)
            ctx=ra.market_context(slug)
            latest_row_output.append({"market_slug":slug,"snapshot_ts":r["snapshot_ts"],"run_id":r["run_id"],
                                      "date":ctx["market_start_taipei"][:10],"regime":ctx["session_regime"],
                                      "commit":runs.get(latest_run,{}).get("git_commit") or "LEGACY_UNKNOWN",
                                      **{c+"_classification":labels[c] for c in COMPONENTS},
                                      **{c+"_reason":reasons[c] for c in COMPONENTS},"analysis_scope":"LATEST_RUNTIME_ONLY"})
        old,oldcp,summary=gate(slug,rows,ls,lstrike,latest_ns/1e9)
        new,newcp,_=gate(slug,latest_corrected[slug],ls,lstrike,latest_ns/1e9)
        latest_records[slug]={"old_status":old,"new_status":new,"old_cp":oldcp,"new_cp":newcp,"summary":summary}
    latest=[s for s,r in latest_records.items() if r["new_status"]=="SYNCHRONIZED_USABLE"]
    cohorts["LATEST_RUNTIME_ONLY"]={"run_id":latest_run,"commit":runs.get(latest_run,{}).get("git_commit"),
                                  "old_N":sum(r["old_status"]=="SYNCHRONIZED_USABLE" for r in latest_records.values()),
                                  "new_N":len(latest),"slugs":latest,"metrics":metrics(latest,latest_records,latest_corrected),
                                  "market_status_counts":dict(Counter(r["new_status"] for r in latest_records.values())),
                                  "classification_counts":[{"component":k[0],"classification":k[1],"N":v} for k,v in latest_row_counts.items()]}
    left,right=[],[]
    def hour(s):return datetime.fromisoformat(ra.market_context(s)["market_start_taipei"]).hour
    for h in range(24):
        a=sorted(s for s in cohorts["PRIMARY_WEEKDAY"]["slugs"] if hour(s)==h)
        b=sorted(s for s in cohorts["WEEKEND_PRIMARY"]["slugs"] if hour(s)==h)
        n=min(len(a),len(b));left+=a[:n];right+=b[:n]
    matched={"weekday_N":len(left),"weekend_N":len(right),"status":"TOO_SMALL" if len(left)<50 else "DESCRIPTIVE_ONLY",
             "weekday":metrics(left,records,corrected),"weekend":metrics(right,records,corrected)}
    comparisons={str(h):difference(cohorts["PRIMARY_WEEKDAY"]["metrics"][str(h)]["flip"],cohorts["WEEKEND_PRIMARY"]["metrics"][str(h)]["flip"]) for h in CHECKPOINTS}
    binary_comparisons={"PRIMARY":compare_metrics(cohorts["PRIMARY_WEEKDAY"]["metrics"],cohorts["WEEKEND_PRIMARY"]["metrics"]),
                        "MONDAY_SENSITIVITY":compare_metrics(cohorts["WEEKDAY_SENSITIVITY"]["metrics"],cohorts["WEEKEND_PRIMARY"]["metrics"]),
                        "TIME_MATCHED":compare_metrics(matched["weekday"],matched["weekend"])}
    counts=Counter((r["date"],r["commit"],r["regime"],c,r[c+"_classification"]) for r in row_output for c in COMPONENTS)
    nr=[r for r in row_output if r["joint_classification"] in {"NOT_RECOMPUTABLE","MISSING_REQUIRED_FIELD"}]
    recovered=[r for r in row_output if r["joint_classification"]=="FRESH_RECOMPUTED"]
    for p in (db,journal):
        if digest(p)!=hashes[str(p)]:raise RuntimeError("Offline input changed")
    stamp=datetime.now(TAIPEI).strftime("%Y%m%d_%H%M%S_%z");output=Path(output);output.mkdir(parents=True,exist_ok=True)
    base="historical_freshness_recomputation_";md=output/(base+stamp+".md")
    ra._write_csv(output/(base+"rows_"+stamp+".csv"),[{**r,"analysis_scope":"HISTORICAL_FIXED_CUTOFF"} for r in row_output]+latest_row_output)
    by_market=defaultdict(list)
    for r in row_output:by_market[r["market_slug"]].append(r)
    ra._write_csv(output/(base+"markets_"+stamp+".csv"),[{"market_slug":s,**{k:r[k] for k in ("date","run_id","rows","old_status","new_status")},
                      "joint_recovered":sum(x["joint_classification"]=="FRESH_RECOMPUTED" for x in by_market[s]),
                      "joint_not_recomputable":sum(x["joint_classification"] in {"NOT_RECOMPUTABLE","MISSING_REQUIRED_FIELD"} for x in by_market[s]),
                      **{c+"_classification_counts":dict(Counter(x[c+"_classification"] for x in by_market[s])) for c in COMPONENTS}} for s,r in records.items()])
    metadata={"snapshot_hashes":hashes,"parquet_inventory":inventory,"parquet_read_failures":failures,"binary_comparisons":binary_comparisons,
              "cohorts":cohorts,"comparisons":comparisons,"matched":matched,
              "classification_counts":[{"date":k[0],"commit":k[1],"regime":k[2],"component":k[3],"classification":k[4],"N":v} for k,v in counts.items()]}
    (output/(base+stamp+".json")).write_text(json.dumps(metadata,indent=2))
    headline={"HEAD":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
              "CODE_CHANGE_SCOPE":"ANALYSIS_ONLY","ACTIVE_DATA_MUTATED":"NO","PRODUCTION_FRESHNESS_CHANGED":"NO"}
    for name,prefix in (("PRIMARY_WEEKDAY","PRIMARY_WEEKDAY"),("WEEKEND_PRIMARY","WEEKEND"),("WEEKDAY_SENSITIVITY","WEEKDAY_SENSITIVITY")):
        headline[prefix+"_OLD_N"]=cohorts[name]["old_N"];headline[prefix+"_NEW_N"]=cohorts[name]["new_N"]
    headline.update(FRESH_ROWS_RECOVERED=len(recovered),MARKETS_RECOVERED=sum(len(c["added"]) for c in (cohorts["WEEKDAY_SENSITIVITY"],cohorts["WEEKEND_PRIMARY"])),
                    ROWS_NOT_RECOMPUTABLE=len(nr),MARKETS_NOT_RECOMPUTABLE=len({r["market_slug"] for r in nr}))
    for h in (300,120):
        for name,label in (("PRIMARY_WEEKDAY","WEEKDAY"),("WEEKEND_PRIMARY","WEEKEND")):
            v=cohorts[name]["metrics"][str(h)]["flip"];headline[f"T{h}_{label}_NEW"]=f'{v["k"]}/{v["N"]}'
        headline[f"T{h}_DIFF_PP"]=comparisons[str(h)]["diff_pp"]
    unchanged=all(not c["added"] and not c["removed"] for c in (cohorts["PRIMARY_WEEKDAY"],cohorts["WEEKEND_PRIMARY"]))
    samecp=all(records[s]["old_cp"]==records[s]["new_cp"] for s in cohorts["PRIMARY_WEEKDAY"]["slugs"]+cohorts["WEEKEND_PRIMARY"]["slugs"]) if unchanged else False
    stability=("STABLE" if samecp else "DIRECTIONALLY_STABLE_BUT_MAGNITUDE_CHANGED"
               if all(comparisons[str(h)]["diff_pp"] is not None and comparisons[str(h)]["newcombe"][0]>0 for h in (300,120))
               else "INSUFFICIENT_RECOMPUTABLE_DATA")
    headline.update(FRESHNESS_IMPACT="PARTIALLY_UNRESOLVED" if nr else "MATERIAL" if not unchanged else "NONE",
                    REGIME_RESULT_STABILITY=stability,
                    MONDAY_WHOLESALE_REHABILITATION="NO",SHADOW_PNL_USED="NO",STRATEGY_CHANGE_RECOMMENDED="NO")
    lines=["# Historical freshness recomputation", "", "## 1. Executive verdict", "",
           f"FRESHNESS_IMPACT={headline['FRESHNESS_IMPACT']}；REGIME_RESULT_STABILITY={stability}。T300/T120兩個主要差異的Newcombe下界仍>0，方向保留、幅度變動；不是經多重測試確認的效應。保留原始strict flags的資料仍明確標ORIGINAL，不宣稱完整clock修復。", "",
           "## 2. Sources and immutable snapshots", "",f"固定舊截止：{datetime.fromtimestamp(cutoff,TAIPEI).isoformat()}。來源：{Path(db).name} / {Path(journal).name}，完整相對路徑與hash見下表。", ""]
    for path,h in hashes.items():
        relative=str(Path(path).resolve().relative_to(Path.cwd()))
        lines.append(f"- `{relative}` SHA256 `{h}`")
    lines += ["",f"BTC immutable part files={len(inventory)}；hash/mtime/size讀取前後一致；read failures={len(failures)}，詳見local JSON inventory。未使用activeSQLite；不修改任何Parquet。", "",
              "## 3. Corrected freshness semantics", "",
              "不計算local-now減remote-source作為新判定。Market需原始保留值，receipt只證明transport，不能補回清空BBO。p_ex/TWAP保留original strict evidence，不對已清空值放寬。BTC只接受Parquet last exact source-ms對應原snapshot btc_source_ts，綁定該tick close與receipt；first/open不能證明snapshot latest state，完全不用。同source不同價格拒絕。Receipt rounding以ms+1上界排除future；重複receipt取最早，不rejuvenate。", "",
              "BTC transport age=local snapshot-local tick receipt≤10；value age=在snapshot前已收到之最大Binance source timestamp－目標tick source timestamp≤10（同source domain）。後來bar/市場/settlement不參與freshness；讀取未來產生檔案中的既有endpoint不等於使用未來觀察。僅exact endpoint，不用OHLC內部、不carry-forward、不offset fit、不clamp負age。BTC returns不恢復，不用於本報告regime evidence。", "",
              "## 4. Historical reconstructability", "",f"Joint recovered rows={len(recovered)}；joint NOT_RECOMPUTABLE/MISSING={len(nr)}；這是rows，非markets。MARKETS_NOT_RECOMPUTABLE={len({r['market_slug'] for r in nr})}表示含至少一個不可還原joint row的市場，**不表示這些市場全部不可用**。五component按date/commit/regime分類count見JSON；逐列分類/reason見CSV。", ""]
    lines += ["| Component | Classification | Rows |","|---|---|---:|"]
    for c in COMPONENTS:
        for label,n in Counter(r[c+"_classification"] for r in row_output).items():lines.append(f"|{c}|{label}|{n}|")
    lines += ["", "## 5. Cohort OLD vs NEW", "","| Cohort | Old | New | Added | Removed | Unchanged |","|---|---:|---:|---:|---:|---:|"]
    for name in OLD_COUNTS:
        c=cohorts[name];lines.append(f'|{name}|{c["old_N"]}|{c["new_N"]}|{len(c["added"])}|{len(c["removed"])}|{len(c["unchanged"])}|')
    lines += ["", "## 6. Market additions/removals", ""]
    for name in OLD_COUNTS:lines.append(f'{name}: added={cohorts[name]["added"]}; removed={cohorts[name]["removed"]}')
    lines += ["", "Collection non-freshness exclusions: "+str(Counter(r["new_status"] for r in records.values() if r["new_status"] not in {"SYNCHRONIZED_USABLE","LOW_JOINT_FRESHNESS","MISSING_FRESH_CHECKPOINT"})), "",
              "## 7. Weekday/weekend flips", "", "| Horizon | Weekday k/N (%) Wilson | Weekend k/N (%) Wilson | Difference pp Newcombe | Fisher two-sided |", "|---|---|---|---|---|"]
    for h in CHECKPOINTS:
        a=cohorts["PRIMARY_WEEKDAY"]["metrics"][str(h)]["flip"];b=cohorts["WEEKEND_PRIMARY"]["metrics"][str(h)]["flip"];d=comparisons[str(h)]
        lines.append(f'|T-{h}|{a["k"]}/{a["N"]} ({a["pct"]}) {a["wilson"]}|{b["k"]}/{b["N"]} ({b["pct"]}) {b["wilson"]}|{d["diff_pp"]} {d["newcombe"]}|{d["fisher"]}|')
        lines.append(f'\nOLD T-{h}: weekday {oldflips[h][0]}/53；weekend {oldflips[h][1]}/51。\n')
    lines += detail_tables(cohorts,binary_comparisons)
    lines += ["", "## 11. Taipei-hour matched sensitivity", "",f'固定每小時chronological prefix=min兩側hour-count，outcome無關；N={len(left)}/{len(right)}，{matched["status"]}。JSON含各horizon；matching不移除所有confounder。', "",
              "## 12. Monday sensitivity", "",f'Monday new clean={len(set(cohorts["WEEKDAY_SENSITIVITY"]["slugs"])-set(cohorts["PRIMARY_WEEKDAY"]["slugs"]))}；每市場保留continuity、single-run/cycle、canonical settlement、exact-strike、five anchors、span與gap；不wholesale rehabilitation。舊截止ad7cc22 sensitivity由8→{len(previous_latest)}。另取最新run={latest_run}（{cohorts["LATEST_RUNTIME_ONLY"]["commit"]}）獨立LATEST_RUNTIME_ONLY，new clean={len(latest)}；不混入PRIMARY/WEEKEND/58基準增加N，也不計入historical recovered/NR headline counts。最新run逐component counts與market statuses詳見JSON。', "",
              "## 13. Limitations", "", "Original fresh flags保留但不代表source/local時鐘已校正。Missing fields不能推定fresh；僅BTC exact endpoint可恢復，market/p_ex/TWAP cleared values多仍無法還原。重要：新cohort仍是可觀測strict subset，結論不能代表所有被clock bug排除的市場。多horizon Fisher是名義描述，不宣稱多重測試significance；相鄰市場可能相關，一天Tuesday不能代表穩定weekday效應。SHADOW PnL完全未使用。", "",
              "## 14. Final classification", "", "Freshness是否改变clean cohort與regime是否改变為兩個獨立問題。非完全可重算時不能將保留樣本方向穩定解讀成bug無影響。", ""]
    # Include inferential/descriptive sensitivities in Markdown, not only JSON.
    extra=["### Binary sensitivity tables", "", "所有CI均95%；Wilson/Newcombe採market為單位，Fisher未校正multiple testing。", "",
           "| Comparison | Horizon | Endpoint | Weekday k/N (%) Wilson | Weekend k/N (%) Wilson | Difference pp Newcombe | Fisher |",
           "|---|---|---|---|---|---|---|"]
    for name,a,b in (("PRIMARY",cohorts["PRIMARY_WEEKDAY"]["metrics"],cohorts["WEEKEND_PRIMARY"]["metrics"]),
                     ("TIME_MATCHED",matched["weekday"],matched["weekend"]),
                     ("MONDAY_SENSITIVITY",cohorts["WEEKDAY_SENSITIVITY"]["metrics"],cohorts["WEEKEND_PRIMARY"]["metrics"])):
        for h in CHECKPOINTS:
            for endpoint in (("crossings","agreement") if name=="PRIMARY" else ("flip",)):
                x,y=a[str(h)][endpoint],b[str(h)][endpoint];d=binary_comparisons[name][str(h)][endpoint]
                extra.append(f'|{name}|T-{h}|{endpoint}|{x["k"]}/{x["N"]} ({x["pct"]}) {x["wilson"]}|{y["k"]}/{y["N"]} ({y["pct"]}) {y["wilson"]}|{d["diff_pp"]} {d["newcombe"]}|{d["fisher"]}|')
    lines[-2:-2]=extra
    summary="HISTORICAL_FRESHNESS_RECOMPUTATION\n"+"\n".join(f"{k}={v}" for k,v in headline.items())
    md.write_text("\n".join(lines)+"\n"+summary+"\n")
    print("REPORT="+str(md));print(summary)
    return md,headline


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",type=Path,required=True);p.add_argument("--journal",type=Path,required=True)
    p.add_argument("--parquet",type=Path,default=Path("data/btc_history_1s"))
    p.add_argument("--output",type=Path,default=Path("reports/research_analysis/prediction_freshness"))
    args=p.parse_args();run(args.db,args.journal,args.parquet,args.output)

#!/usr/bin/env python3
"""Read-only early-warning timeline per position (the single canonical tool).

Supersedes the ad-hoc historical-audit / DRY-RUN scratch scripts.  Inputs are
the 1 s PREDICTION_RESEARCH_SNAPSHOT rows (research DB) and the journal's
STOP_TIMING_* / order events.  Nothing here runs in, or feeds, the trading
runtime; no event reconstructed here can trigger a SELL.

Definitions (see project_overview.md, "Early-warning research semantics"):

* TOKEN_DD = entry executable bid - current held-side executable bid, exact
  Decimal arithmetic, deterioration when DD >= X (X in 0.05/0.10/0.15/0.20).
  Never a midpoint.  Held side BOOK_EMPTY = EXIT_UNAVAILABLE (counted as a
  deterioration, severe); STALE/UNKNOWN = unobserved.
* PRIMARY: recovery when DD < X; second deterioration when DD >= X again.
* HYSTERESIS (pre-registered, not tuned): recovery only when
  DD <= X - HYSTERESIS_TICKS ticks and that holds for >= HYSTERESIS_HOLD_SEC of
  continuous observation.  flap_count = primary episodes - hysteresis episodes.
* Observation gaps: consecutive snapshots more than MAX_OBS_GAP_SEC apart, or
  unobserved points in between, are censored.  A transition after a censored
  interval carries a lower bound; durations carry min/max bounds.  Nothing is
  interpolated.
* BINANCE_ADVERSE_MOVE_BPS: relative to the Binance spot at entry.  Never a
  strike cross.
* CHAINLINK_SPOT_ADVERSE_STRIKE_CROSS: Chainlink spot vs the market strike
  (settlement family, not the 60 s TWAP).  Tie settles UP: adverse for UP when
  spot < strike, for DOWN when spot >= strike.  TWAP cross kept separate.
  No Binance-vs-strike cross is produced; the Binance-Chainlink basis is
  reported only as a descriptive statistic.

Usage (outputs belong under reports/, untracked):
  python -I research/early_warning_timeline.py --journal logs/trade_journal.db \
      --research-db data/research/twap_forward_shadow.db --run-id RUN \
      --out-dir reports/<dir> [--immutable] [--include-shadow]
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

TICK = Decimal("0.01")
THRESHOLDS = ("0.05", "0.10", "0.15", "0.20")
BINANCE_BPS_THRESHOLDS = ("1", "2", "3", "5", "10")
HYSTERESIS_TICKS = 2
HYSTERESIS_HOLD_SEC = 5.0
MAX_OBS_GAP_SEC = 3.0  # 3x the 1 s snapshot cadence
FRESH, STALE, BOOK_EMPTY, UNKNOWN = "FRESH_BID", "STALE", "BOOK_EMPTY", "UNKNOWN"


def dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out.is_finite() else None


def _f(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


# ---------------------------------------------------------------- rows
def normalize_row(row: dict[str, Any], held_side: str) -> dict[str, Any]:
    """One snapshot -> observation for the held (or analyzed) side.

    Schema v2 held-side fields win; then v2 per-side state; then the legacy
    per-side freshness flag (v1 rows cannot tell BOOK_EMPTY from FRESH).
    """
    side = str(held_side).lower()
    if str(row.get("held_side") or "").lower() == side and row.get("held_side_bid_state"):
        state, bid = str(row["held_side_bid_state"]), dec(row.get("held_side_bid"))
    elif row.get(f"{side}_bid_state"):
        state, bid = str(row[f"{side}_bid_state"]), dec(row.get(f"best_bid_{side}"))
    else:
        fresh = row.get(f"market_quote_{side}_fresh", row.get("market_quote_fresh"))
        bid = dec(row.get(f"best_bid_{side}"))
        state = FRESH if fresh and bid is not None and bid > 0 else STALE
    if state not in {FRESH, BOOK_EMPTY}:
        bid = None
    return {
        "t": float(row["snapshot_ts"]), "bid": bid, "bid_state": state,
        "schema_version": int(row.get("snapshot_schema_version") or 1),
        "binance": dec(row.get("btc_spot")) if row.get("btc_fresh") else None,
        "chainlink": dec(row.get("chainlink_spot")) if row.get("chainlink_spot_fresh") else None,
        "twap": dec(row.get("official_twap")) if row.get("twap_fresh") else None,
        "strike": dec(row.get("strike")),
        "tte": row.get("time_left_sec"), "required_move_sigma": row.get("required_move_sigma"),
        "required_move_z_diffusion": row.get("required_move_z_diffusion"),
    }


# ------------------------------------------------------------ episodes
def _classify(obs: dict[str, Any], entry_bid: Optional[Decimal]) -> tuple[str, Optional[Decimal]]:
    state = obs.get("bid_state")
    if state == BOOK_EMPTY:
        return "EXIT_UNAVAILABLE", None
    bid = obs.get("bid")
    if state != FRESH or bid is None or entry_bid is None:
        return "UNKNOWN", None
    return "VALUE", entry_bid - bid


def deterioration_episodes(obs: list[dict[str, Any]], entry_bid: Optional[Decimal], threshold: str, *,
                           hysteresis: bool = False) -> dict[str, Any]:
    x = Decimal(threshold)
    recover_at = x - HYSTERESIS_TICKS * TICK
    entry = dec(entry_bid)
    episodes: list[dict[str, Any]] = []
    gaps: list[dict[str, float]] = []
    cur: Optional[dict[str, Any]] = None
    prev_t: Optional[float] = None
    last_obs_t: Optional[float] = None
    unobserved = False
    hold_start: Optional[float] = None
    observed = total = 0

    def close(end_ts: float, end_lower: float, confirmed: Optional[float]) -> None:
        nonlocal cur
        cur.update({"end_ts": end_ts, "end_ts_lower": end_lower, "end_censored": end_lower != end_ts,
                    "censored_at_end": False, "recovery_confirmed_ts": confirmed,
                    "duration_sec": end_ts - cur["start_ts"],
                    "duration_min_sec": end_lower - cur["start_ts"],
                    "duration_max_sec": end_ts - cur["start_ts_lower"]})
        episodes.append(cur)
        cur = None

    for o in obs:
        t = float(o["t"])
        total += 1
        if prev_t is not None and t - prev_t > MAX_OBS_GAP_SEC:
            gaps.append({"from_ts": prev_t, "to_ts": t, "duration_sec": t - prev_t})
            unobserved = True
        prev_t = t
        kind, dd = _classify(o, entry)
        if kind == "UNKNOWN":
            unobserved = True
            continue
        observed += 1
        censored = unobserved and last_obs_t is not None
        lower = last_obs_t if censored else t
        deteriorated = kind == "EXIT_UNAVAILABLE" or dd >= x
        if cur is None:
            if deteriorated:
                cur = {"start_ts": t, "start_ts_lower": lower, "start_censored": censored,
                       "max_dd": dd, "exit_unavailable_seen": kind == "EXIT_UNAVAILABLE",
                       "spans_unobserved": False}
                hold_start = None
        else:
            if unobserved:
                cur["spans_unobserved"] = True
                hold_start = None
            if dd is not None and (cur["max_dd"] is None or dd > cur["max_dd"]):
                cur["max_dd"] = dd
            cur["exit_unavailable_seen"] |= kind == "EXIT_UNAVAILABLE"
            if not hysteresis:
                if not deteriorated:
                    close(t, lower, None)
            elif dd is not None and dd <= recover_at:
                if hold_start is None:
                    hold_start, hold_lower = t, lower
                if t - hold_start >= HYSTERESIS_HOLD_SEC:
                    close(hold_start, hold_lower, t)
                    hold_start = None
            else:
                hold_start = None
        last_obs_t = t
        unobserved = False
    if cur is not None:
        cur.update({"end_ts": None, "end_ts_lower": None, "end_censored": True, "censored_at_end": True,
                    "recovery_confirmed_ts": None, "duration_sec": None,
                    "duration_min_sec": (last_obs_t - cur["start_ts"]) if last_obs_t is not None else None,
                    "duration_max_sec": None})
        episodes.append(cur)
    for ep in episodes:
        ep["max_dd"] = str(ep["max_dd"]) if ep["max_dd"] is not None else None
        ep["max_dd_is_lower_bound"] = bool(ep["spans_unobserved"] or ep["censored_at_end"])
    first = episodes[0] if episodes else None
    second = episodes[1] if len(episodes) > 1 else None
    status = "UNKNOWN" if observed == 0 else ("TRIGGERED" if episodes else "NOT_TRIGGERED")
    return {
        "threshold": threshold, "variant": "HYSTERESIS" if hysteresis else "PRIMARY", "status": status,
        "episode_count": len(episodes), "episodes": episodes,
        "first_trigger_ts": first["start_ts"] if first else None,
        "first_recovery_ts": first["end_ts"] if first else None,
        "first_episode_duration_sec": first["duration_sec"] if first else None,
        "first_episode_duration_bounds": [first["duration_min_sec"], first["duration_max_sec"]] if first else None,
        "second_trigger_ts": second["start_ts"] if second else None,
        "time_recovery_to_second_trigger_sec": (second["start_ts"] - first["end_ts"])
        if second and first and first["end_ts"] is not None else None,
        "coverage": {"observed_points": observed, "total_points": total,
                     "observed_fraction": observed / total if total else None, "gaps": gaps},
    }


def token_episode_summary(obs: list[dict[str, Any]], entry_bid: Optional[Decimal]) -> dict[str, Any]:
    out = {}
    for x in THRESHOLDS:
        primary = deterioration_episodes(obs, entry_bid, x)
        hyst = deterioration_episodes(obs, entry_bid, x, hysteresis=True)
        out[x] = {"primary": primary, "hysteresis": hyst,
                  "flap_count": max(0, primary["episode_count"] - hyst["episode_count"])}
    return out


# ----------------------------------------------------- spot / reference
def _adverse(side: str, price: Decimal, strike: Decimal) -> bool:
    return price < strike if str(side).upper() == "UP" else price >= strike


def spot_cross(points: Iterable[tuple[float, Optional[Decimal]]], held_side: str,
               strike: Optional[Decimal]) -> dict[str, Any]:
    """First adverse strike cross of a settlement-family reference (Chainlink spot or TWAP)."""
    strike = dec(strike)
    seen_known, unobserved, state, prev_t = False, False, None, None
    crosses = 0
    first: Optional[dict[str, Any]] = None
    for t, price in points:
        if prev_t is not None and t - prev_t > MAX_OBS_GAP_SEC:
            unobserved = True
        prev_t = t
        if price is None or strike is None or strike <= 0:
            unobserved = True
            continue
        adverse = _adverse(held_side, price, strike)
        if not seen_known:
            seen_known = True
            if adverse:
                crosses += 1
                first = {"status": "ADVERSE_AT_FIRST_KNOWN_OBSERVATION" if unobserved else "ADVERSE_AT_ENTRY",
                         "ts": t, "censored": unobserved}
        elif adverse and state is False:
            crosses += 1
            if first is None:
                first = {"status": "CROSS", "ts": t, "censored": unobserved}
        state = adverse
        unobserved = False
    if first is None:
        first = {"status": "NOT_TRIGGERED" if seen_known else "UNKNOWN", "ts": None, "censored": False}
    return {**first, "adverse_cross_count": crosses}


def binance_move_events(points: Iterable[tuple[float, Optional[Decimal]]], held_side: str,
                        entry_binance: Optional[Decimal]) -> dict[str, dict[str, Any]]:
    entry = dec(entry_binance)
    sign = Decimal(1) if str(held_side).upper() == "UP" else Decimal(-1)
    out = {x: {"status": "UNKNOWN", "ts": None, "censored": False, "move_bps": None} for x in BINANCE_BPS_THRESHOLDS}
    if entry is None or entry <= 0:
        return out
    known, unobserved, prev_t = False, False, None
    for t, price in points:
        if prev_t is not None and t - prev_t > MAX_OBS_GAP_SEC:
            unobserved = True
        prev_t = t
        if price is None:
            unobserved = True
            continue
        known = True
        move = sign * (entry - price) / entry * Decimal("10000")
        for x in BINANCE_BPS_THRESHOLDS:
            if out[x]["ts"] is None and move >= Decimal(x):
                out[x] = {"status": "TRIGGERED", "ts": t, "censored": unobserved, "move_bps": float(move)}
        unobserved = False
    for x in BINANCE_BPS_THRESHOLDS:
        if out[x]["ts"] is None and known:
            out[x]["status"] = "NOT_TRIGGERED"
    return out


# ------------------------------------------------------------- timeline
JOURNAL_EVENTS = ("T_MINUS2", "T_BREAKER_ELIGIBLE", "T_EXIT_SUBMIT", "T_EXIT_FILL")


def _context(obs: list[dict[str, Any]], t: float) -> dict[str, Any]:
    best = None
    for o in obs:
        if o["t"] <= t + 1e-9:
            best = o
        else:
            break
    if best is None or t - best["t"] > MAX_OBS_GAP_SEC:
        return {"tte": None, "required_move_sigma": None, "required_move_z_diffusion": None}
    return {"tte": best["tte"], "required_move_sigma": best["required_move_sigma"],
            "required_move_z_diffusion": best["required_move_z_diffusion"]}


def position_timeline(rows: list[dict[str, Any]], *, held_side: str, entry_ts: float,
                      entry_bid: Optional[Decimal], entry_binance: Optional[Decimal] = None,
                      strike: Optional[Decimal] = None, events: Optional[dict[str, Optional[float]]] = None,
                      end_ts: Optional[float] = None) -> dict[str, Any]:
    obs = sorted((normalize_row(r, held_side) for r in rows), key=lambda o: o["t"])
    obs = [o for o in obs if o["t"] >= entry_ts - 1e-9 and (end_ts is None or o["t"] <= end_ts)]
    strike_value = dec(strike) or next((o["strike"] for o in obs if o["strike"] is not None), None)
    if entry_binance is None:
        entry_binance = next((o["binance"] for o in obs if o["binance"] is not None), None)

    def stamp(status: str, ts: Optional[float], **extra: Any) -> dict[str, Any]:
        body = {"status": status, "ts": ts, **extra}
        if ts is not None:
            body["context"] = _context(obs, ts)
        return body

    timeline: dict[str, Any] = {"ENTRY": stamp("TRIGGERED", float(entry_ts))}
    for x, ev in binance_move_events([(o["t"], o["binance"]) for o in obs], held_side, entry_binance).items():
        timeline[f"T_BINANCE_{x}BPS"] = stamp(ev["status"], ev["ts"], censored=ev["censored"], move_bps=ev["move_bps"])
    token = token_episode_summary(obs, dec(entry_bid))
    for x, summary in token.items():
        key = f"T_TOKEN_{x.replace('0.', '0').replace('.', '')}"
        primary = summary["primary"]
        status = primary["status"]
        first_ep = primary["episodes"][0] if primary["episodes"] else {}
        timeline[f"{key}_FIRST"] = stamp(status, primary["first_trigger_ts"],
                                         censored=first_ep.get("start_censored", False))
        rec = primary["first_recovery_ts"]
        timeline[f"{key}_RECOVERY"] = stamp(
            "TRIGGERED" if rec is not None else ("NOT_TRIGGERED" if status == "TRIGGERED" else status), rec)
        sec = primary["second_trigger_ts"]
        timeline[f"{key}_SECOND"] = stamp(
            "TRIGGERED" if sec is not None else ("NOT_TRIGGERED" if status == "TRIGGERED" else status), sec)
    for name, field in (("T_CHAINLINK_SPOT_CROSS", "chainlink"), ("T_TWAP_CROSS", "twap")):
        cross = spot_cross([(o["t"], o[field]) for o in obs], held_side, strike_value)
        timeline[name] = stamp(cross["status"], cross["ts"], censored=cross["censored"],
                               adverse_cross_count=cross["adverse_cross_count"])
    events = events or {}
    for name in JOURNAL_EVENTS:
        if name not in events:
            timeline[name] = {"status": "UNKNOWN", "ts": None}
        elif events[name] is None:
            timeline[name] = {"status": "NOT_TRIGGERED", "ts": None}
        else:
            timeline[name] = stamp("TRIGGERED", float(events[name]))
    basis = [float((o["binance"] - o["chainlink"]) / o["chainlink"] * Decimal("10000"))
             for o in obs if o["binance"] is not None and o["chainlink"] is not None and o["chainlink"] > 0]
    versions = sorted({o["schema_version"] for o in obs})
    return {
        "held_side": str(held_side).upper(), "entry_ts": float(entry_ts), "entry_bid": _f(dec(entry_bid)),
        "entry_binance": _f(dec(entry_binance)), "strike": _f(strike_value),
        "timeline": timeline, "token": token,
        "basis": {"binance_minus_chainlink_bps_median": statistics.median(basis) if basis else None,
                  "samples": len(basis), "label": "DESCRIPTIVE_ONLY_NONCANONICAL"},
        "schema_versions_seen": versions, "book_empty_detectable": any(v >= 2 for v in versions),
        "snapshot_count": len(obs),
    }


# ------------------------------------------------------------------ CLI
def _connect(path: str, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{Path(path).resolve()}?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True)


def _loads(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ts(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def load_positions(journal: sqlite3.Connection, run_id: str, include_shadow: bool) -> list[dict[str, Any]]:
    positions = []
    rows = journal.execute("SELECT ts, event_type, payload_json FROM strategy_events WHERE run_id=? AND event_type "
                           "LIKE 'STOP_TIMING_%' ORDER BY id", (run_id,)).fetchall()
    by_epoch: dict[str, dict[str, Any]] = {}
    for ts, event_type, raw in rows:
        p = _loads(raw)
        epoch = p.get("position_epoch")
        if event_type == "STOP_TIMING_POSITION_OPENED" and epoch:
            by_epoch[epoch] = {"kind": "LIVE", "slug": p.get("slug"), "instrument_id": p.get("instrument_id"),
                               "held_side": p.get("held_side"), "entry_ts": p.get("entry_fill_ts") or p.get("obs_wall_ts"),
                               "entry_bid": p.get("entry_executable_bid"), "entry_bid_state": p.get("entry_bid_state"),
                               "entry_binance": p.get("entry_binance_spot"), "strike": p.get("strike"),
                               "events": {}, "settled": False, "telemetry_disabled": False}
        elif epoch in by_epoch:
            pos = by_epoch[epoch]
            if event_type == "STOP_TIMING_COMPONENT_FIRST_TRUE" and p.get("component") == "abs_loss_threshold":
                pos["events"].setdefault("T_MINUS2", p.get("obs_wall_ts"))
            elif event_type == "STOP_TIMING_BREAKER_FIRST_ELIGIBLE":
                pos["events"].setdefault("T_BREAKER_ELIGIBLE", p.get("obs_wall_ts"))
            elif event_type == "STOP_TIMING_POSITION_SETTLEMENT":
                pos["settled"] = True
                pos["telemetry_disabled"] = bool(p.get("telemetry_disabled"))
    for pos in by_epoch.values():
        if pos["settled"] and not pos["telemetry_disabled"]:
            for name in ("T_MINUS2", "T_BREAKER_ELIGIBLE"):
                pos["events"].setdefault(name, None)
        orders = journal.execute("SELECT ts, event_type, side FROM order_events WHERE run_id=? AND instrument_id=? "
                                 "ORDER BY id", (run_id, pos["instrument_id"])).fetchall()
        for ts, event_type, side in orders:
            t = _ts(ts)
            if t is None or t < float(pos["entry_ts"] or 0):
                continue
            if event_type == "ORDER_TAKER_EXIT_SUBMIT":
                pos["events"].setdefault("T_EXIT_SUBMIT", t)
            elif event_type == "ORDER_FILLED" and str(side or "").upper() == "SELL":
                pos["events"].setdefault("T_EXIT_FILL", t)
        if pos["settled"]:
            pos["events"].setdefault("T_EXIT_SUBMIT", None)
            pos["events"].setdefault("T_EXIT_FILL", None)
        positions.append(pos)
    if include_shadow:
        for ts, raw in journal.execute("SELECT ts, payload_json FROM order_events WHERE run_id=? AND "
                                       "event_type='SHADOW_SIM_ENTRY_FILLED' ORDER BY id", (run_id,)):
            p = _loads(raw)
            positions.append({"kind": "SHADOW", "slug": p.get("slug") or p.get("market_slug"),
                              "instrument_id": p.get("instrument_id"), "held_side": str(p.get("side") or "").upper(),
                              "entry_ts": _ts(ts), "entry_bid": p.get("quote_bid"), "entry_bid_state": "SHADOW_QUOTE",
                              "entry_binance": None, "strike": None, "events": {}})
    return positions


def load_snapshots(research: sqlite3.Connection, run_id: str, slug: str) -> list[dict[str, Any]]:
    out = []
    for (raw,) in research.execute("SELECT payload_json FROM lead_lag_decisions WHERE slug=? AND payload_json LIKE "
                                   "'%PREDICTION_RESEARCH_SNAPSHOT%' ORDER BY decision_epoch_ns", (slug,)):
        p = _loads(raw)
        if p.get("event_type") == "PREDICTION_RESEARCH_SNAPSHOT" and p.get("run_id") == run_id and p.get("snapshot_ts"):
            out.append(p)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", required=True)
    ap.add_argument("--research-db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--immutable", action="store_true", help="open DBs with immutable=1 (bot must be stopped)")
    ap.add_argument("--include-shadow", action="store_true", help="also reconstruct DRY-RUN shadow entries")
    args = ap.parse_args(argv)
    journal, research = _connect(args.journal, args.immutable), _connect(args.research_db, args.immutable)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for pos in load_positions(journal, args.run_id, args.include_shadow):
        if not pos.get("slug") or pos.get("entry_ts") is None or pos.get("held_side") not in {"UP", "DOWN"}:
            continue
        try:
            end_ts = float(str(pos["slug"]).rsplit("-", 1)[-1]) + 900.0
        except ValueError:
            end_ts = None
        rows = load_snapshots(research, args.run_id, pos["slug"])
        result = position_timeline(rows, held_side=pos["held_side"], entry_ts=float(pos["entry_ts"]),
                                   entry_bid=dec(pos.get("entry_bid")), entry_binance=dec(pos.get("entry_binance")),
                                   strike=dec(pos.get("strike")), events=pos["events"], end_ts=end_ts)
        results.append({"kind": pos["kind"], "slug": pos["slug"], "instrument_id": pos.get("instrument_id"),
                        "entry_bid_state": pos.get("entry_bid_state"), **result})
    with open(out_dir / "early_warning_timeline.json", "w") as fh:
        json.dump({"run_id": args.run_id, "positions": results}, fh, indent=1, default=str)
    keys = sorted({k for r in results for k in r["timeline"]})
    with open(out_dir / "early_warning_timeline.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["kind", "slug", "held_side", "event", "status", "ts", "tte", "required_move_sigma",
                         "required_move_z_diffusion"])
        for r in results:
            for k in keys:
                ev = r["timeline"].get(k, {"status": "UNKNOWN", "ts": None})
                ctx = ev.get("context") or {}
                writer.writerow([r["kind"], r["slug"], r["held_side"], k, ev["status"], ev["ts"], ctx.get("tte"),
                                 ctx.get("required_move_sigma"), ctx.get("required_move_z_diffusion")])
    with open(out_dir / "token_episodes.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["kind", "slug", "threshold", "variant", "episode", "start_ts", "start_ts_lower",
                         "end_ts", "end_ts_lower", "duration_sec", "duration_min_sec", "duration_max_sec",
                         "max_dd", "max_dd_is_lower_bound", "exit_unavailable_seen", "flap_count", "observed_fraction"])
        for r in results:
            for x, summary in r["token"].items():
                for variant in ("primary", "hysteresis"):
                    block = summary[variant]
                    for i, ep in enumerate(block["episodes"] or [None]):
                        ep = ep or {}
                        writer.writerow([r["kind"], r["slug"], x, block["variant"], i if ep else None,
                                         ep.get("start_ts"), ep.get("start_ts_lower"), ep.get("end_ts"),
                                         ep.get("end_ts_lower"), ep.get("duration_sec"), ep.get("duration_min_sec"),
                                         ep.get("duration_max_sec"), ep.get("max_dd"), ep.get("max_dd_is_lower_bound"),
                                         ep.get("exit_unavailable_seen"), summary["flap_count"],
                                         block["coverage"]["observed_fraction"]])
    print(json.dumps({"positions": len(results), "out_dir": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

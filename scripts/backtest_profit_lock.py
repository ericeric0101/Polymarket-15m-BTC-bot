#!/usr/bin/env python3
"""Offline early-entry and dynamic profit-protection research.

This module is intentionally isolated from live trading. Public trade prints
are sparse execution proxies, not historical BBO or guaranteed fills.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.backtest_simple_trend_hold import (  # noqa: E402
    _sample_market_files, _write_csv, entry_proxy, load_btc_candles,
    market_start, threshold_side, winner_from_gamma, _returns_for_market,
)

ENTRY_CONFIGS = ((120, 0), (120, 2), (120, 5), (180, 0), (180, 2), (180, 5))
LOCK_LADDERS = {
    "LOCK_A_LOOSE": ((.05, -.05), (.10, 0), (.15, .05), (.20, .10), (.30, .15)),
    "LOCK_B_BREAKEVEN": ((.05, 0), (.10, .03), (.15, .05), (.20, .10), (.30, .15)),
    "LOCK_C_TIGHT": ((.03, -.03), (.05, 0), (.10, .05), (.15, .075), (.20, .10)),
}
PARTIAL_B_LADDER = ((.15, .05), (.20, .10), (.30, .15))
STRATEGIES = ("HOLD_TO_SETTLEMENT", "TP5", "TP10", "TP20", "TP10_OR_SETTLEMENT", "TP10_OR_TMINUS2M",
              "LOCK_A_LOOSE", "LOCK_B_BREAKEVEN", "LOCK_C_TIGHT",
              "TRAIL_5", "TRAIL_7.5", "TRAIL_10", "TRAIL_15",
              "PARTIAL_A", "PARTIAL_B", "TIMELOCK_A", "TIMELOCK_B", "TIMELOCK_C",
              "COMBINED_LOCK_B_SL20", "COMBINED_TRAIL10_SL20",
              "COMBINED_LOCK_B_SL20_NOPROGRESS180_PRICE_PROXY",
              "CURRENT_180S_5BPS_HOLD")
COMBINED_STRATEGIES = {
    "COMBINED_LOCK_B_SL20": "LOCK_B_BREAKEVEN",
    "COMBINED_TRAIL10_SL20": "TRAIL_10",
    "COMBINED_LOCK_B_SL20_NOPROGRESS180_PRICE_PROXY": "LOCK_B_BREAKEVEN",
}
MFE_THRESHOLDS = (.03, .05, .075, .10, .15, .20, .30, .40)
ENTRY_PRICE_BUCKETS = ((0, .60, "<0.60"), (.60, .70, "0.60-0.70"), (.70, .75, "0.70-0.75"),
                       (.75, .80, "0.75-0.80"), (.80, .85, "0.80-0.85"), (.85, .90, "0.85-0.90"),
                       (.90, 1.000001, ">0.90"))
COSTS = (("zero_execution_cost", 0.0, 0.0), ("existing_1pct_notional_fee_stress", .01, 0.0),
         ("plus_1c_exit_slippage", 0.0, .01), ("plus_2c_exit_slippage", 0.0, .02))


def _active_ladder_floor(strategy: str, peak_return: float, current_floor: float = -math.inf) -> float | None:
    """Return a monotonic floor (returns are fractions of entry price)."""
    ladder = LOCK_LADDERS.get(strategy)
    if ladder is None:
        return None
    active = [floor for activation, floor in ladder if peak_return + 1e-12 >= activation]
    if not active:
        return None
    candidate = max(active)
    return max(candidate, current_floor) if current_floor != -math.inf else candidate


def _partition_rows(rows: list[dict[str, Any]], partition: str) -> list[dict[str, Any]]:
    if partition == "weekday":
        return [r for r in rows if r.get("weekday_weekend") == "weekday"]
    if partition == "weekend":
        return [r for r in rows if r.get("weekday_weekend") == "weekend"]
    return list(rows)


def _timed_exit_marks(marks: list[dict[str, Any]], market_end: float, slug: str,
                      entry_config: str) -> list[dict[str, Any]]:
    result=[]
    for seconds_left in (300,180,120,60):
        target=market_end-seconds_left
        selected=next((m for m in marks if float(m["ts"])>=target),None)
        fallback="FIRST_SELL_PRINT_AT_OR_AFTER_TARGET"
        if selected is None:
            selected=next((m for m in reversed(marks) if float(m["ts"])<target),None)
            fallback="NO_LATER_PRINT_USE_LAST_PRIOR" if selected else "NO_SELL_PRINT"
        result.append({"slug":slug,"entry_config":entry_config,"requested_exit_time":target,
                       "requested_exit_time_label":f"T-{seconds_left//60}m",
                       "selected_exit_print_time":float(selected["ts"]) if selected else None,
                       "exit_delay_sec":float(selected["ts"])-target if selected else None,
                       "selected_exit_price":float(selected["price"]) if selected else None,
                       "fallback_reason":fallback,"proxy_print_id":f"{slug}:{selected['ts']}:{selected['price']}" if selected else None})
    used=defaultdict(list)
    for row in result:
        if row.get("proxy_print_id"): used[row["proxy_print_id"]].append(row)
    for group in used.values():
        if len(group)>1:
            for row in group: row["fallback_reason"] += "|SAME_PROXY_PRINT_REUSED"
    return result


def _settle_pnl(entry: float, side: str, winner: str | None, notional: float) -> float | None:
    if winner not in {"UP", "DOWN"}:
        return None
    return notional / entry * ((1.0 if side == winner else 0.0) - entry)


def _sim_once(strategy: str, entry: float, entry_ts: float, market_end: float,
              sell_marks: list[dict[str, Any]], side: str, winner: str | None,
              ambiguity_policy: str, max_gap_sec: float) -> dict[str, Any]:
    shares = 5.0 / entry
    marks = sorted((m for m in sell_marks if entry_ts < float(m["ts"]) < market_end), key=lambda m: float(m["ts"]))
    groups: list[list[dict[str, Any]]] = []
    for mark in marks:
        if groups and float(groups[-1][0]["ts"]) == float(mark["ts"]):
            groups[-1].append(mark)
        else:
            groups.append([mark])
    max_gap = max([float(groups[0][0]["ts"]) - entry_ts] +
                  [float(groups[i][0]["ts"]) - float(groups[i-1][0]["ts"]) for i in range(1, len(groups))] +
                  ([market_end - float(groups[-1][0]["ts"])] if groups else [market_end-entry_ts]))
    peak = 0.0
    floor: float | None = None
    sold_fraction = 0.0
    partial_exit_proceeds = 0.0
    partial_exit_fraction = 0.0
    exit_mark = None
    exit_kind = "SETTLEMENT"
    ambiguous = False
    last_return = None

    def floor_for(name: str, peak_value: float, prior: float | None) -> float | None:
        prior_num = prior if prior is not None else -math.inf
        return _active_ladder_floor(name, peak_value, prior_num)

    for group in groups:
        ts = float(group[0]["ts"])
        if len(group) > 1:
            group = sorted(group, key=lambda m: float(m["price"]),
                           reverse=(ambiguity_policy == "pessimistic"))
        remaining = 900.0 - (ts - (market_end - 900.0))
        base_name = COMBINED_STRATEGIES.get(strategy, strategy)
        timelock = strategy in {"TIMELOCK_A", "TIMELOCK_B", "TIMELOCK_C"}
        time_boundary = 120 if strategy in {"TIMELOCK_A", "TIMELOCK_B"} else 180
        for mark in group:
            px = float(mark["price"])
            ret = px / entry - 1.0
            peak = max(peak, ret)
            if strategy in COMBINED_STRATEGIES and ret <= -0.20 + 1e-12:
                exit_mark, exit_kind = mark, "HARD_STOP_20"
                break
            if (strategy == "COMBINED_LOCK_B_SL20_NOPROGRESS180_PRICE_PROXY"
                    and ts - entry_ts >= 180 and peak < .05 and ret < 0):
                exit_mark, exit_kind = mark, "NO_PROGRESS_PRICE_ONLY_PROXY"
                break
            # Fixed TP baselines and partial first leg.
            tp = {"TP5": .05, "TP10": .10, "TP20": .20, "TP10_OR_TMINUS2M": .10,
                  "TP10_OR_SETTLEMENT": .10}.get(strategy)
            if tp is not None and ret >= tp:
                exit_mark, exit_kind = mark, "FIXED_TP"
                break
            if strategy in {"PARTIAL_A", "PARTIAL_B"} and sold_fraction == 0 and ret >= .10:
                partial_exit_fraction = .5
                partial_exit_proceeds = .5 * shares * px
                sold_fraction = .5
                peak = max(peak, ret)
                if strategy == "PARTIAL_A":
                    continue
                base_name = "LOCK_A_LOOSE"
            if base_name.startswith("TRAIL_"):
                distance = float(base_name.split("_")[1]) / 100
                candidate = peak - distance if peak >= .05 else None
                if candidate is not None:
                    floor = candidate if floor is None else max(floor, candidate)
            elif strategy in LOCK_LADDERS or base_name in LOCK_LADDERS:
                if strategy == "PARTIAL_B" and sold_fraction > 0:
                    candidates=[v for activation,v in PARTIAL_B_LADDER if peak+1e-12>=activation]
                    if candidates: floor=max(floor if floor is not None else -math.inf,max(candidates))
                else:
                    floor = floor_for(base_name, peak, floor)
            elif timelock:
                base_name = "LOCK_A_LOOSE"
                peak = max(peak, ret)
                floor = floor_for(base_name, peak, floor)
                if remaining <= time_boundary and ret > 0:
                    if strategy == "TIMELOCK_B":
                        floor = max(floor if floor is not None else -math.inf, .03 if ret >= .05 else 0.0)
                    else:
                        floor = max(floor if floor is not None else -math.inf, 0.0)
            if floor is not None and ret <= floor + 1e-12:
                exit_mark, exit_kind = mark, "TIME_LOCK" if timelock and remaining <= time_boundary else (
                    "TRAILING_STOP" if base_name.startswith("TRAIL_") else "PROFIT_LOCK")
                break
            last_return = ret
        if exit_mark is not None:
            break
        # Ordering inside a timestamp can change whether a newly activated floor
        # is breached. The wrapper below compares both orderings.

    if strategy == "TP10_OR_TMINUS2M" and exit_mark is None:
        forced_ts = market_end - 120
        after = next((m for m in marks if float(m["ts"]) >= forced_ts), None)
        chosen = after or next((m for m in reversed(marks) if float(m["ts"]) < forced_ts), None)
        if chosen:
            exit_mark, exit_kind = chosen, "TIMED_SELL_PRINT_PROXY"
    if exit_mark:
        px = float(exit_mark["price"])
        remaining_fraction = 1.0 - sold_fraction
        pnl = partial_exit_proceeds + remaining_fraction * shares * px - 5.0
        sold_notional = partial_exit_fraction * shares * (float(sell_marks[0]["price"]) if sell_marks else entry) + remaining_fraction * shares * px
        exit_ts = float(exit_mark["ts"])
        exit_price = px
    else:
        settlement = _settle_pnl(entry, side, winner, 5.0)
        if settlement is None:
            return {"net_pnl": None, "gross_pnl": None, "exit_kind": "UNRESOLVED", "exit_ts": None,
                    "exit_price": None, "ambiguous": False, "partial_exit": sold_fraction > 0,
                    "partial_fraction": sold_fraction, "peak_return_pct": peak*100,
                    "trailing_execution_uncertain": max_gap > max_gap_sec, "max_gap_after_entry_sec": max_gap}
        remainder_pnl = (1.0-sold_fraction) * settlement
        pnl = partial_exit_proceeds - sold_fraction * 5.0 + remainder_pnl
        gross_profit = pnl
        exit_ts = market_end
        exit_price = 1.0 if side == winner else 0.0
        exit_kind = "SETTLEMENT"
        sold_notional = partial_exit_fraction * 5.0
    return {"gross_pnl": pnl, "net_pnl": pnl, "exit_kind": exit_kind, "exit_ts": exit_ts,
            "exit_price": exit_price, "ambiguous": ambiguous, "partial_exit": sold_fraction > 0,
            "partial_fraction": sold_fraction, "partial_exit_proceeds": partial_exit_proceeds,
            "peak_return_pct": peak*100, "peak_profit_usdc": max(0.0, peak)*5.0,
            "trailing_execution_uncertain": bool(max_gap > max_gap_sec and strategy not in {"HOLD_TO_SETTLEMENT"}),
            "max_gap_after_entry_sec": max_gap, "last_observed_return_pct": last_return*100 if last_return is not None else None,
            "sold_notional": sold_notional}


def _replay_exit(strategy: str, entry: float, entry_ts: float, market_end: float,
                 sell_marks: list[dict[str, Any]], side: str, winner: str | None,
                 ambiguity_policy: str = "pessimistic", max_gap_sec: float = 30.0) -> dict[str, Any]:
    """Timestamp-ordered exit replay; same-timestamp ordering is sensitivity-tested."""
    result = _sim_once(strategy, entry, entry_ts, market_end, sell_marks, side, winner,
                       ambiguity_policy, max_gap_sec)
    # Compare the two possible within-timestamp orderings once for the whole
    # path. Replaying the path for every duplicated timestamp is quadratic.
    low_first = _sim_once(strategy, entry, entry_ts, market_end, sell_marks, side, winner, "optimistic", max_gap_sec)
    high_first = _sim_once(strategy, entry, entry_ts, market_end, sell_marks, side, winner, "pessimistic", max_gap_sec)
    ambiguous = low_first.get("exit_kind") != high_first.get("exit_kind") or low_first.get("exit_ts") != high_first.get("exit_ts") or (
        low_first.get("net_pnl") is not None and high_first.get("net_pnl") is not None and abs(low_first["net_pnl"]-high_first["net_pnl"]) > 1e-10)
    result["ambiguous"] = ambiguous
    if ambiguous and ambiguity_policy == "exclude_ambiguous":
        result["net_pnl"] = None
    elif ambiguous and ambiguity_policy == "optimistic":
        result = low_first
        result["ambiguous"] = True
    elif ambiguous and ambiguity_policy == "pessimistic":
        result = high_first
        result["ambiguous"] = True
    return result


def _split_development_holdout(dates: list[str]) -> tuple[list[str], list[str]]:
    ordered = sorted(set(dates))
    if len(ordered) < 2:
        return ordered, []
    cut = min(len(ordered)-1, max(1, int(len(ordered)*.65)))
    return ordered[:cut], ordered[cut:]


def _path_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(r["net_pnl"]) for r in rows if r.get("net_pnl") is not None]
    wins = [x for x in values if x > 0]; losses = [x for x in values if x < 0]
    ordered = sorted((r for r in rows if r.get("net_pnl") is not None), key=lambda x: (x["local_date"], x["slug"]))
    equity = high = dd = 0.0; streak = longest = 0
    for row in ordered:
        x = float(row["net_pnl"]); equity += x; high = max(high, equity); dd = max(dd, high-equity)
        streak = streak+1 if x < 0 else 0; longest = max(longest, streak)
    invested = sum(float(r.get("notional", 5)) for r in rows if r.get("net_pnl") is not None)
    vals = sorted(values)
    q = lambda p: vals[min(len(vals)-1, max(0, int((len(vals)-1)*p)))] if vals else None
    mfe_rows = [r for r in rows if r.get("mfe_usdc") is not None and float(r["mfe_usdc"]) > 0 and r.get("net_pnl") is not None]
    return {"n": len(values), "profitability_rate": sum(x > 0 for x in values)/len(values) if values else None,
            "mean_pnl": statistics.mean(values) if values else None, "median_pnl": statistics.median(values) if values else None,
            "ev_per_trade": statistics.mean(values) if values else None, "roi": sum(values)/invested if invested else None,
            "profit_factor": sum(wins)/abs(sum(losses)) if losses else (math.inf if wins else None),
            "max_drawdown": dd, "longest_losing_streak": longest,
            "average_win": statistics.mean(wins) if wins else None, "average_loss": statistics.mean(losses) if losses else None,
            "p10": q(.10), "p5": q(.05), "p1": q(.01), "worst_trade": min(values) if values else None,
            "winner_retention_ratio": sum(max(0, float(r["net_pnl"])) for r in mfe_rows)/sum(float(r["mfe_usdc"]) for r in mfe_rows) if mfe_rows and sum(float(r["mfe_usdc"]) for r in mfe_rows)>0 else None,
            "ever_positive_then_negative_rate": sum(bool(r.get("ever_positive_then_negative")) for r in rows)/len(rows) if rows else None,
            "median_giveback": statistics.median([r["giveback_usdc"] for r in rows if r.get("giveback_usdc") is not None]) if any(r.get("giveback_usdc") is not None for r in rows) else None}


def _entry_excursion(marks: list[dict[str, Any]], entry: float, entry_ts: float) -> dict[str, Any]:
    later = [m for m in marks if float(m["ts"]) > entry_ts]
    if not later:
        return {"mfe_pct": None, "mae_pct": None, "mfe_usdc": None, "mae_usdc": None,
                "time_to_mfe_sec": None, "time_to_mae_sec": None, "max_gap_after_entry_sec": None}
    high = max(later, key=lambda x: float(x["price"])); low = min(later, key=lambda x: float(x["price"]))
    mfe = max(0.0, float(high["price"])/entry-1); mae = min(0.0, float(low["price"])/entry-1)
    times = sorted({float(m["ts"]) for m in later})
    gaps = [times[0]-entry_ts] + [b-a for a,b in zip(times,times[1:])] + [900-(times[-1]-entry_ts)]
    return {"mfe_pct": mfe*100, "mae_pct": mae*100, "mfe_usdc": mfe*5, "mae_usdc": mae*5,
            "time_to_mfe_sec": float(high["ts"])-entry_ts, "time_to_mae_sec": float(low["ts"])-entry_ts,
            "max_gap_after_entry_sec": max(gaps), "median_gap_after_entry_sec": statistics.median(gaps)}


def _apply_cost(result: dict[str, Any], entry: float, notional: float, fee_rate: float, slip: float) -> dict[str, Any]:
    if result.get("net_pnl") is None:
        return {**result, "gross_pnl": None, "fee_usdc": None, "net_pnl": None, "roi": None}
    raw = float(result["net_pnl"])
    # Model a single entry fee and fees only on actual sold proceeds. Settlement
    # payout is not counted as a market sell; partial legs are charged once.
    fee = fee_rate * notional
    if result.get("exit_kind") not in {"SETTLEMENT", "UNRESOLVED"}:
        fraction = max(0.0, 1.0-float(result.get("partial_fraction", 0)))
        px = max(0.0, float(result.get("exit_price") or 0)-slip)
        raw -= fraction * (notional/entry) * slip
        fee += fee_rate * fraction * (notional/entry) * px
    if result.get("partial_exit"):
        fee += fee_rate * float(result.get("partial_exit_proceeds") or 0)
        raw -= float(result.get("partial_fraction", 0)) * (notional/entry) * slip
    return {**result, "gross_pnl": result.get("gross_pnl"), "fee_usdc": fee,
            "net_pnl": raw-fee, "roi": (raw-fee)/notional}


def _entry_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [float(r["entry_price"]) for r in rows]
    pnls = [float(r["settlement_pnl"]) for r in rows if r.get("settlement_pnl") is not None]
    outcomes = [r for r in rows if r.get("winner") in {"UP", "DOWN"}]
    metrics = _path_metrics([{**r, "net_pnl": r.get("settlement_pnl")} for r in rows])
    q = lambda p: sorted(entries)[min(len(entries)-1, int((len(entries)-1)*p))] if entries else None
    return {"n": len(rows), "mean_entry_price": statistics.mean(entries) if entries else None,
            "median_entry_price": statistics.median(entries) if entries else None, "p25_entry_price": q(.25), "p75_entry_price": q(.75),
            "direction_accuracy": sum(r["side"] == r["winner"] for r in outcomes)/len(outcomes) if outcomes else None,
            "mean_settlement_pnl": statistics.mean(pnls) if pnls else None, "roi": metrics["roi"],
            "profit_factor": metrics["profit_factor"], "max_drawdown": metrics["max_drawdown"],
            "average_win": metrics["average_win"], "average_loss": metrics["average_loss"]}


def _selection_score(metrics: dict[str, Any]) -> tuple[int, int, float, float, float]:
    """Transparent Pareto-style ordering: viability first, then risk measures."""
    ev = metrics.get("ev_per_trade")
    pf = metrics.get("profit_factor")
    return (int(ev is not None and ev > 0), int(pf is not None and pf > 1),
            -float(metrics.get("max_drawdown") or math.inf),
            -abs(float(metrics.get("average_loss") or math.inf)),
            float(metrics.get("winner_retention_ratio") or -math.inf))


def run(*, cache_dirs: list[Path], sample_csv: Path, btc_cache: Path, output: Path,
        start_date: date, end_date: date, timezone_name: str = "America/New_York",
        offline: bool = True, max_gap_sec: float = 30.0) -> dict[str, Any]:
    markets, _ = _sample_market_files(cache_dirs, sample_csv)
    starts = [market_start(m.get("slug", "")) for m in markets]
    starts = [s for s in starts if s]
    if not starts:
        raise RuntimeError("No cached public market records found.")
    candles, candle_diag = load_btc_candles(min(starts)-1200, max(starts)+960, btc_cache, offline=offline)
    dates_all = sorted({datetime.fromtimestamp(s, ZoneInfo(timezone_name)).date().isoformat() for s in starts})
    dev_dates, hold_dates = _split_development_holdout(dates_all)
    candles_by_market = {s: _returns_for_market(candles, s) for s in starts}
    entries: list[dict[str, Any]] = []; market_meta = {}; trade_paths = {}; quality = []
    for market in markets:
        slug = market.get("slug"); start = market_start(slug or "")
        if not start:
            continue
        local_dt = datetime.fromtimestamp(start, ZoneInfo(timezone_name))
        if not start_date <= local_dt.date() <= end_date:
            continue
        trades = []
        for raw in market.get("trades", []):
            try:
                ts=float(raw["timestamp"]); px=float(raw["price"]); size=float(raw["size"])
                side=str(raw.get("outcome", "")).upper(); aggressor=str(raw.get("side", "")).upper()
            except (KeyError, TypeError, ValueError):
                continue
            if start <= ts < start+900 and 0 < px < 1 and size > 0 and side in {"UP", "DOWN"}:
                trades.append({"ts":ts,"price":px,"size":size,"outcome":side,"aggressor":aggressor})
        trades.sort(key=lambda r:(r["ts"],r["price"],r["outcome"]))
        trade_paths[slug]=trades
        winner=winner_from_gamma((market.get("gamma") or {}).get("market") or {})
        weekday_weekend="weekend" if local_dt.weekday()>=5 else "weekday"
        meta={"slug":slug,"start":start,"end":start+900,"local_date":local_dt.date().isoformat(),
              "weekday_weekend":weekday_weekend,"et_hour_block":f"{local_dt.hour//6*6:02d}-{local_dt.hour//6*6+6:02d}","winner":winner}
        market_meta[slug]=meta
        by_outcome={side:[t for t in trades if t["outcome"]==side] for side in ("UP","DOWN")}
        quality.append({"slug":slug,"winner":winner,"trade_prints":len(trades),"entry_execution_source":"ENTRY_EXECUTION_PROXY_PUBLIC_TRADE_PRINT_VWAP_5S",
                        "exit_execution_source":"EXIT_PROXY_PUBLIC_SELL_PRINT","historical_bbo_available":False,
                        "trade_fetch_status":market.get("trade_fetch_status")})
        if winner not in {"UP","DOWN"} or start not in candles_by_market:
            continue
        returns=candles_by_market[start]
        for obs, threshold in ENTRY_CONFIGS:
            move=returns.get(obs)
            side=threshold_side(move,threshold) if move is not None else None
            if side is None:
                continue
            signal_ts=start+obs
            raw=[{"timestamp":t["ts"],"price":t["price"],"size":t["size"],"outcome":t["outcome"]} for t in trades]
            entry,status=entry_proxy(raw,side,signal_ts,0,"vwap_5s")
            if entry is None:
                continue
            entry_ts=signal_ts+5
            all_marks=[t for t in by_outcome[side] if t["ts"]>entry_ts]
            sell_marks=[t for t in all_marks if t["aggressor"]=="SELL"]
            ex=_entry_excursion(all_marks,entry,entry_ts)
            settlement=_settle_pnl(entry,side,winner,5.0)
            row={**meta,"observation_sec":obs,"threshold_bps":threshold,"entry_config":f"{obs}/{threshold}",
                 "signal_return_bps":move,"side":side,"entry_ts":entry_ts,"signal_ts":signal_ts,"entry_price":entry,
                 "entry_status":status,"entry_execution_proxy":"ENTRY_EXECUTION_PROXY_PUBLIC_TRADE_PRINT_VWAP_5S",
                 "exit_execution_proxy":"EXIT_PROXY_PUBLIC_SELL_PRINT","notional":5.0,"shares":5.0/entry,
                 "direction_correct":side==winner,"settlement_pnl":settlement,"mfe_source":"ALL_PUBLIC_OUTCOME_PRINTS_SAMPLED_NOT_BBO",
                 "mae_source":"ALL_PUBLIC_OUTCOME_PRINTS_SAMPLED_NOT_BBO",**ex}
            peak=max(0,float(ex["mfe_pct"] or 0)/100)
            mae=float(ex["mae_pct"] or 0)
            row["mfe_mae_proxy_uncertain"]=bool((ex.get("max_gap_after_entry_sec") or 0)>30)
            row["ever_positive_then_negative"] = peak>0 and settlement is not None and settlement<0
            row["peak_profit_usdc"]=peak*5
            row["giveback_usdc"]=(row["peak_profit_usdc"]-max(0,settlement)) if settlement is not None and peak>0 else None
            row["first_sell_marks"]=len(sell_marks)
            entries.append(row)
    output.mkdir(parents=True,exist_ok=True)

    # Entry summaries: one independent opportunity per market/config; groups
    # are never pooled across overlapping configs.
    entry_summary=[]; early_vs_current=[]; mfe_summary=[]; mfe_thresholds=[]; mae_distribution=[]
    for config in [f"{o}/{t}" for o,t in ENTRY_CONFIGS]:
        group=[r for r in entries if r["entry_config"]==config]
        for partition, subset in (("all",group),("weekday",[r for r in group if r["weekday_weekend"]=="weekday"]),
                                  ("weekend",[r for r in group if r["weekday_weekend"]=="weekend"])):
            entry_summary.append({"entry_config":config,"partition":partition,**_entry_stats(subset)})
            mfe_vals=[float(r["mfe_pct"]) for r in subset if r.get("mfe_pct") is not None]
            mae_vals=[float(r["mae_pct"]) for r in subset if r.get("mae_pct") is not None]
            mfe_summary.append({"entry_config":config,"partition":partition,"n":len(subset),
                                "median_mfe_pct":statistics.median(mfe_vals) if mfe_vals else None,
                                "median_mae_pct":statistics.median(mae_vals) if mae_vals else None,
                                "median_time_to_mfe_sec":statistics.median([r["time_to_mfe_sec"] for r in subset if r.get("time_to_mfe_sec") is not None]) if subset else None,
                                "median_time_to_mae_sec":statistics.median([r["time_to_mae_sec"] for r in subset if r.get("time_to_mae_sec") is not None]) if subset else None})
            for threshold in MFE_THRESHOLDS:
                hit=sum(r.get("mfe_pct") is not None and float(r["mfe_pct"])>=threshold*100 for r in subset)
                mfe_thresholds.append({"entry_config":config,"partition":partition,"threshold_pct":threshold*100,"n":len(subset),"hit_n":hit,"hit_rate":hit/len(subset) if subset else None})
            if mae_vals:
                sorted_mae=sorted(mae_vals)
                q=lambda p:sorted_mae[min(len(sorted_mae)-1,int((len(sorted_mae)-1)*p))]
                mae_distribution.append({"entry_config":config,"partition":partition,"n":len(mae_vals),"median_mae_pct":statistics.median(mae_vals),
                                        "p25":q(.25),"p50":q(.50),"p75":q(.75),"p90_adverse":q(.10),"worst":min(mae_vals)})
    for partition in ("all","weekday","weekend"):
        a=[r for r in entries if r["entry_config"]=="120/0" and (partition=="all" or r["weekday_weekend"]==partition)]
        b=[r for r in entries if r["entry_config"]=="180/5" and (partition=="all" or r["weekday_weekend"]==partition)]
        am,bm=_entry_stats(a),_entry_stats(b)
        paired={x["slug"]:x for x in b}
        deltas=[]
        for x in a:
            y=paired.get(x["slug"])
            if y:
                deltas.append((x,y))
        early_vs_current.append({"partition":partition,"early_n":len(a),"current_n":len(b),
            "early_mean_entry":am["mean_entry_price"],"current_mean_entry":bm["mean_entry_price"],
            "entry_price_difference":(am["mean_entry_price"]-bm["mean_entry_price"]) if am["mean_entry_price"] is not None and bm["mean_entry_price"] is not None else None,
            "accuracy_difference":(am["direction_accuracy"]-bm["direction_accuracy"]) if am["direction_accuracy"] is not None and bm["direction_accuracy"] is not None else None,
            "EV_difference":(am["mean_settlement_pnl"]-bm["mean_settlement_pnl"]) if am["mean_settlement_pnl"] is not None and bm["mean_settlement_pnl"] is not None else None,
            "ROI_difference":(am["roi"]-bm["roi"]) if am["roi"] is not None and bm["roi"] is not None else None,
            "paired_markets":len(deltas),"paired_entry_price_difference":statistics.mean([x["entry_price"]-y["entry_price"] for x,y in deltas]) if deltas else None,
            "paired_accuracy_difference":statistics.mean([int(x["direction_correct"])-int(y["direction_correct"]) for x,y in deltas]) if deltas else None,
            "paired_EV_difference":statistics.mean([x["settlement_pnl"]-y["settlement_pnl"] for x,y in deltas if x["settlement_pnl"] is not None and y["settlement_pnl"] is not None]) if deltas else None})
    decomposition=[]
    for partition in ("all","weekday","weekend"):
        early_by={r["slug"]:r for r in entries if r["entry_config"]=="120/0" and (partition=="all" or r["weekday_weekend"]==partition)}
        current_by={r["slug"]:r for r in entries if r["entry_config"]=="180/5" and (partition=="all" or r["weekday_weekend"]==partition)}
        components=[]
        for slug in sorted(set(early_by)&set(current_by)):
            e,c=early_by[slug],current_by[slug]
            payout=1.0 if e["side"]==e["winner"] else 0.0
            e_pnl=5*(payout-e["entry_price"])/e["entry_price"]
            current_side_early_price_pnl=5*((1.0 if e["side"]==c["winner"] else 0.0)-c["entry_price"])/c["entry_price"]
            both_at_current_price_early_side=5*((1.0 if e["side"]==e["winner"] else 0.0)-c["entry_price"])/c["entry_price"]
            current_pnl=5*((1.0 if c["side"]==c["winner"] else 0.0)-c["entry_price"])/c["entry_price"]
            price_component=e_pnl-current_side_early_price_pnl
            direction_component=both_at_current_price_early_side-current_pnl
            components.append({"slug":slug,"entry_price_effect_usdc":price_component,"direction_effect_usdc":direction_component,
                               "early_pnl":e_pnl,"current_pnl":current_pnl,"total_pnl_difference":e_pnl-current_pnl,
                               "winner_payout_effect_usdc":direction_component})
        decomposition.append({"partition":partition,"paired_n":len(components),
            "mean_entry_price_effect_usdc":statistics.mean(x["entry_price_effect_usdc"] for x in components) if components else None,
            "mean_direction_effect_usdc":statistics.mean(x["direction_effect_usdc"] for x in components) if components else None,
            "mean_total_pnl_difference_usdc":statistics.mean(x["total_pnl_difference"] for x in components) if components else None,
            "loser_frequency_early":sum(x["early_pnl"]<0 for x in components)/len(components) if components else None,
            "loser_frequency_current":sum(x["current_pnl"]<0 for x in components)/len(components) if components else None,
            "loser_severity_early":statistics.mean([x["early_pnl"] for x in components if x["early_pnl"]<0]) if any(x["early_pnl"]<0 for x in components) else None,
            "loser_severity_current":statistics.mean([x["current_pnl"] for x in components if x["current_pnl"]<0]) if any(x["current_pnl"]<0 for x in components) else None})

    # Replay exit state machines under the three same-timestamp policies.
    replay=[]; ambiguous_cases=[]; timed_audit=[]
    for row in entries:
        marks=[m for m in trade_paths[row["slug"]] if m["outcome"]==row["side"] and m["aggressor"]=="SELL"]
        for strategy in STRATEGIES:
            if strategy=="CURRENT_180S_5BPS_HOLD" and row["entry_config"]!="180/5":
                continue
            for ambiguity_policy in ("optimistic","pessimistic","exclude_ambiguous"):
                raw_result=_replay_exit(strategy,row["entry_price"],row["entry_ts"],row["end"],marks,row["side"],row["winner"],ambiguity_policy,max_gap_sec)
                if raw_result.get("ambiguous"):
                    ambiguous_cases.append({"slug":row["slug"],"entry_config":row["entry_config"],"strategy":strategy,
                                            "ambiguity_policy":ambiguity_policy,"exit_ts":raw_result.get("exit_ts"),"exit_kind":raw_result.get("exit_kind")})
                for cost_name,fee_rate,slip in COSTS:
                    priced=_apply_cost(raw_result,row["entry_price"],5.0,fee_rate,slip)
                    peak_usdc=float(row.get("peak_profit_usdc") or 0)
                    strategy_giveback=peak_usdc-float(priced["net_pnl"]) if priced.get("net_pnl") is not None and peak_usdc>0 else None
                    replay.append({**row,"ever_positive_then_negative":bool(peak_usdc>0 and priced.get("net_pnl") is not None and float(priced["net_pnl"])<0),
                                   "giveback_usdc":strategy_giveback,"strategy":strategy,"ambiguity_policy":ambiguity_policy,"cost_scenario":cost_name,**priced})
            if strategy=="TP10_OR_TMINUS2M":
                timed_audit.extend(_timed_exit_marks(marks,row["end"],row["slug"],row["entry_config"]))
    # _timed_exit_marks flags reuse among T-5/T-3/T-2/T-1 in the same
    # market/config; same print across entry configs is expected shared evidence.

    def grouped_matrix(source: list[dict[str,Any]], include_partition=True):
        out=[]
        buckets=defaultdict(list)
        for item in source:
            buckets[(item["entry_config"],item["strategy"],item["ambiguity_policy"],item["cost_scenario"])].append(item)
        for config,strategy,policy,cost in sorted(buckets):
            group=buckets[(config,strategy,policy,cost)]
            for partition, subset in (("all",group),("weekday_only",[r for r in group if r["weekday_weekend"]=="weekday"]),
                                      ("weekend_only",[r for r in group if r["weekday_weekend"]=="weekend"]),
                                      ("development",[r for r in group if r["local_date"] in dev_dates]),
                                      ("holdout",[r for r in group if r["local_date"] in hold_dates]),
                                      ("development_weekday",[r for r in group if r["local_date"] in dev_dates and r["weekday_weekend"]=="weekday"]),
                                      ("holdout_weekday",[r for r in group if r["local_date"] in hold_dates and r["weekday_weekend"]=="weekday"]),
                                      ("development_weekend",[r for r in group if r["local_date"] in dev_dates and r["weekday_weekend"]=="weekend"]),
                                      ("holdout_weekend",[r for r in group if r["local_date"] in hold_dates and r["weekday_weekend"]=="weekend"])):
                out.append({"entry_config":config,"strategy":strategy,"ambiguity_policy":policy,"cost_scenario":cost,"partition":partition,**_path_metrics(subset)})
        return out
    matrix=grouped_matrix(replay)
    weekday_matrix=[r for r in matrix if r["partition"]=="weekday_only"]
    weekend_matrix=[r for r in matrix if r["partition"]=="weekend_only"]
    # Entry x exit price buckets and weekday/weekend path summary.
    interaction=[]
    for lo,hi,label in ENTRY_PRICE_BUCKETS:
        for config in sorted({r["entry_config"] for r in entries}):
            subset=[r for r in entries if r["entry_config"]==config and lo<=r["entry_price"]<hi]
            if subset:
                interaction.append({"entry_price_bucket":label,"entry_config":config,"n":len(subset),
                    "mean_settlement_pnl":statistics.mean(r["settlement_pnl"] for r in subset if r["settlement_pnl"] is not None),
                    "median_mfe_pct":statistics.median(r["mfe_pct"] for r in subset if r["mfe_pct"] is not None),
                    "median_mae_pct":statistics.median(r["mae_pct"] for r in subset if r["mae_pct"] is not None)})
    weekday_paths=[]
    for group in ("weekday","weekend"):
        for config in sorted({r["entry_config"] for r in entries}):
            subset=[r for r in entries if r["weekday_weekend"]==group and r["entry_config"]==config]
            weekday_paths.append({"partition":group,"entry_config":config,"n":len(subset),
                "median_mfe":statistics.median([r["mfe_pct"] for r in subset if r["mfe_pct"] is not None]) if subset else None,
                "median_mae":statistics.median([r["mae_pct"] for r in subset if r["mae_pct"] is not None]) if subset else None,
                "median_time_to_mfe":statistics.median([r["time_to_mfe_sec"] for r in subset if r["time_to_mfe_sec"] is not None]) if subset else None,
                "median_time_to_peak":statistics.median([r["time_to_mfe_sec"] for r in subset if r["time_to_mfe_sec"] is not None]) if subset else None,
                "median_giveback":statistics.median([r["giveback_usdc"] for r in subset if r["giveback_usdc"] is not None]) if subset else None,
                "ever_positive_then_negative_rate":sum(r["ever_positive_then_negative"] for r in subset)/len(subset) if subset else None})
    giveback=[]; positive_negative=[]; strategy_giveback=[]; strategy_positive_negative=[]
    for config in sorted({r["entry_config"] for r in entries}):
        for part in ("all","weekday","weekend"):
            subset=[r for r in entries if r["entry_config"]==config and (part=="all" or r["weekday_weekend"]==part)]
            vals=[r["giveback_usdc"] for r in subset if r.get("giveback_usdc") is not None]
            vals.sort()
            q=lambda p: vals[min(len(vals)-1,int((len(vals)-1)*p))] if vals else None
            giveback.append({"entry_config":config,"partition":part,"n":len(vals),"median_giveback":statistics.median(vals) if vals else None,"p75":q(.75),"p90":q(.90)})
            posneg=[r for r in subset if r["ever_positive_then_negative"]]
            positive_negative.append({"entry_config":config,"partition":part,"n":len(subset),"ever_positive_then_negative_n":len(posneg),
                                      "rate":len(posneg)/len(subset) if subset else None,
                                      **{f"mfe_ge_{t*100:g}_then_final_negative_rate":sum((r.get("mfe_pct") or 0)>=t*100 and (r.get("settlement_pnl") or 0)<0 for r in subset)/len(subset) if subset else None for t in (.05,.10,.15,.20)}})
    for config in sorted({r["entry_config"] for r in entries}):
        for strategy in STRATEGIES:
            if strategy=="CURRENT_180S_5BPS_HOLD" and config!="180/5": continue
            base=[r for r in replay if r["entry_config"]==config and r["strategy"]==strategy and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress"]
            for part in ("all","weekday","weekend"):
                subset=[r for r in base if part=="all" or r["weekday_weekend"]==part]
                gv=[r["giveback_usdc"] for r in subset if r.get("giveback_usdc") is not None]
                gv.sort(); qg=lambda p:gv[min(len(gv)-1,int((len(gv)-1)*p))] if gv else None
                strategy_giveback.append({"entry_config":config,"strategy":strategy,"partition":part,"n":len(gv),
                                          "median_giveback":statistics.median(gv) if gv else None,"p75":qg(.75),"p90":qg(.90)})
                strategy_positive_negative.append({"entry_config":config,"strategy":strategy,"partition":part,"n":len(subset),
                    "ever_positive_then_finished_negative_n":sum(bool(r.get("ever_positive_then_negative")) for r in subset),
                    "ever_positive_then_finished_negative_rate":sum(bool(r.get("ever_positive_then_negative")) for r in subset)/len(subset) if subset else None,
                    **{key:value for t in (.05,.10,.15,.20) for key,value in (
                        (f"mfe_ge_{t*100:g}_n",sum((r.get("mfe_pct") or 0)>=t*100 for r in subset)),
                        (f"mfe_ge_{t*100:g}_then_exit_negative_rate_of_all",sum((r.get("mfe_pct") or 0)>=t*100 and (r.get("net_pnl") is not None and r["net_pnl"]<0) for r in subset)/len(subset) if subset else None),
                        (f"mfe_ge_{t*100:g}_then_exit_negative_rate_of_threshold_hits",sum((r.get("mfe_pct") or 0)>=t*100 and (r.get("net_pnl") is not None and r["net_pnl"]<0) for r in subset)/sum((r.get("mfe_pct") or 0)>=t*100 for r in subset) if any((r.get("mfe_pct") or 0)>=t*100 for r in subset) else None)) for t in (.05,.10,.15,.20)}})

    # Select at most three development strategies using stated Pareto-style
    # criteria, then report their chronological holdout results.
    dev_candidates=[r for r in matrix if r["partition"]=="development_weekday" and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress" and r["n"]>=5]
    ranked=sorted(dev_candidates,key=_selection_score,reverse=True)
    top_keys=[]
    for r in ranked:
        key=(r["entry_config"],r["strategy"])
        if key not in top_keys: top_keys.append(key)
        if len(top_keys)==3: break
    dynamic_ranked=[r for r in ranked if r["strategy"].startswith(("LOCK_","TRAIL_","PARTIAL_","TIMELOCK_"))]
    best_dynamic_key=(dynamic_ranked[0]["entry_config"],dynamic_ranked[0]["strategy"]) if dynamic_ranked else None
    development_holdout=[]
    for config,strategy in top_keys:
        for part in ("development_weekday","holdout_weekday","development","holdout"):
            row=next((x for x in matrix if x["entry_config"]==config and x["strategy"]==strategy and x["partition"]==part and x["ambiguity_policy"]=="pessimistic" and x["cost_scenario"]=="existing_1pct_notional_fee_stress"),{})
            development_holdout.append({"entry_config":config,"strategy":strategy,"partition":part,"selected_using":"development Pareto-style criteria only",**row})
    if best_dynamic_key:
        for part in ("development_weekday","holdout_weekday"):
            row=next((x for x in matrix if x["entry_config"]==best_dynamic_key[0] and x["strategy"]==best_dynamic_key[1] and x["partition"]==part and x["ambiguity_policy"]=="pessimistic" and x["cost_scenario"]=="existing_1pct_notional_fee_stress"),{})
            development_holdout.append({"entry_config":best_dynamic_key[0],"strategy":best_dynamic_key[1],"partition":part,
                                        "selection_group":"best_dynamic_only","selected_using":"weekday development risk-first criteria only",**row})
    # Weekly block bootstrap on the top configurations' holdout-independent full
    # sample estimates; intervals are descriptive given sparse public sampling.
    bootstrap=[]
    import random
    rng=random.Random(29)
    for config,strategy in top_keys:
        sample=[r for r in replay if r["entry_config"]==config and r["strategy"]==strategy and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress" and r["weekday_weekend"]=="weekday" and r.get("net_pnl") is not None]
        blocks=defaultdict(list)
        for r in sample: blocks[r["local_date"]].append(float(r["net_pnl"]))
        means=[]; rois=[]; drawdowns=[]
        if len(blocks)>=2:
            names=list(blocks)
            for _ in range(1000):
                chosen=rng.choices(names,k=len(names)); vals=[v for name in chosen for v in blocks[name]]
                if vals:
                    means.append(statistics.mean(vals)); rois.append(sum(vals)/(5*len(vals)))
                    eq=peak=dd=0.0
                    for value in vals: eq+=value; peak=max(peak,eq); dd=max(dd,peak-eq)
                    drawdowns.append(dd)
        means.sort();rois.sort();drawdowns.sort()
        ci_idx=lambda seq,p:seq[min(len(seq)-1,int((len(seq)-1)*p))] if seq else None
        bootstrap.append({"entry_config":config,"strategy":strategy,"n_dates":len(blocks),"mean_ev":statistics.mean([v for b in blocks.values() for v in b]) if blocks else None,
                          "ev_ci_low":ci_idx(means,.025),"ev_ci_high":ci_idx(means,.975),
                          "roi_ci_low":ci_idx(rois,.025),"roi_ci_high":ci_idx(rois,.975),
                          "drawdown_p50":ci_idx(drawdowns,.50),"drawdown_p95":ci_idx(drawdowns,.95),
                          "method":"local-date block bootstrap 95%, 1000 resamples"})
    # Variant-specific auxiliary metrics.
    profit_lock_results=[r for r in matrix if r["strategy"].startswith("LOCK_")]
    trailing_results=[r for r in matrix if r["strategy"].startswith("TRAIL_")]
    partial_results=[r for r in matrix if r["strategy"].startswith("PARTIAL_")]
    time_results=[r for r in matrix if r["strategy"].startswith("TIMELOCK_")]
    combined_results=[r for r in matrix if r["strategy"] in COMBINED_STRATEGIES]
    tail=[]
    for config in sorted({r["entry_config"] for r in entries}):
        base=next((r for r in matrix if r["entry_config"]==config and r["strategy"]=="HOLD_TO_SETTLEMENT" and r["partition"]=="all" and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress"),{})
        for r in [x for x in matrix if x["entry_config"]==config and x["partition"]=="all" and x["ambiguity_policy"]=="pessimistic" and x["cost_scenario"]=="existing_1pct_notional_fee_stress"]:
            tail.append({"entry_config":config,"strategy":r["strategy"],"worst_trade_improvement":(r.get("worst_trade")-base.get("worst_trade")) if r.get("worst_trade") is not None and base.get("worst_trade") is not None else None,
                         "p5_improvement":(r.get("p5")-base.get("p5")) if r.get("p5") is not None and base.get("p5") is not None else None,
                         "max_drawdown_reduction":(base.get("max_drawdown")-r.get("max_drawdown")) if r.get("max_drawdown") is not None and base.get("max_drawdown") is not None else None,
                         "average_loss_reduction":(r.get("average_loss")-base.get("average_loss")) if r.get("average_loss") is not None and base.get("average_loss") is not None else None})
    # Output all requested research artifacts.
    files={"entry_config_summary.csv":entry_summary,"early_vs_current.csv":early_vs_current,
           "mfe_mae_by_entry.csv":[r for r in entries],"mfe_thresholds.csv":mfe_thresholds,"giveback_analysis.csv":giveback,
           "entry_exit_matrix.csv":matrix,"weekday_entry_exit_matrix.csv":weekday_matrix,"weekend_entry_exit_matrix.csv":weekend_matrix,
           "profit_lock_results.csv":profit_lock_results,"trailing_results.csv":trailing_results,"partial_tp_results.csv":partial_results,
           "time_lock_results.csv":time_results,"winner_retention.csv":[r for r in matrix if r["partition"] in {"all","holdout"}],
           "combined_exit_results.csv":combined_results,
           "combined_exit_replay.csv":[r for r in replay if r["strategy"] in COMBINED_STRATEGIES],
           "tail_loss_reduction.csv":tail,"ever_positive_then_negative.csv":strategy_positive_negative,"peak_to_exit_giveback.csv":strategy_giveback,
           "entry_price_interaction.csv":interaction,"entry_economics_decomposition.csv":decomposition,
           "development_holdout.csv":development_holdout,"bootstrap.csv":bootstrap,
           "timed_exit_audit.csv":timed_audit,"data_quality.csv":quality+[{"source":"Binance BTCUSDT 1m candles",**d} for d in candle_diag],
           "ambiguous_intrabar_cases.csv":ambiguous_cases,"weekday_weekend_path.csv":weekday_paths,"mae_distribution.csv":mae_distribution,
           "mfe_summary.csv":mfe_summary}
    for name, rows in files.items(): _write_csv(output/name,rows)
    # A compact status summary answers each question without presenting a noisy
    # grid-selected holdout point estimate as a recommendation.
    fmt=lambda x:"n/a" if x is None else f"{x:.4f}"
    early=next((r for r in early_vs_current if r["partition"]=="all"),{})
    early_weekday=next((r for r in early_vs_current if r["partition"]=="weekday"),{})
    early_weekend=next((r for r in early_vs_current if r["partition"]=="weekend"),{})
    entry120_2=next((r for r in entry_summary if r["entry_config"]=="120/2" and r["partition"]=="all"),{})
    baseline180_5=next((r for r in entry_summary if r["entry_config"]=="180/5" and r["partition"]=="all"),{})
    mfe120=next((r for r in mfe_summary if r["entry_config"]=="120/0" and r["partition"]=="all"),{})
    mfe180=next((r for r in mfe_summary if r["entry_config"]=="180/5" and r["partition"]=="all"),{})
    tp10_rows=[r for r in replay if r["entry_config"]=="180/5" and r["strategy"]=="TP10" and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress"]
    tp10_hits=[r for r in tp10_rows if r.get("exit_kind")=="FIXED_TP"]
    hold_posneg=next((r for r in strategy_positive_negative if r["entry_config"]=="180/5" and r["strategy"]=="HOLD_TO_SETTLEMENT" and r["partition"]=="all"),{})
    top_lines=[]
    for r in development_holdout:
        if r["partition"] in {"development_weekday","holdout_weekday"} and not r.get("selection_group"):
            top_lines.append(f"- {r['entry_config']} + {r['strategy']} ({r['partition']}): N={r.get('n',0)}, EV={fmt(r.get('ev_per_trade'))}, ROI={fmt(r.get('roi'))}, PF={fmt(r.get('profit_factor'))}, DD={fmt(r.get('max_drawdown'))}, avg win/loss={fmt(r.get('average_win'))}/{fmt(r.get('average_loss'))}.")
    hold10_negative=next((r.get("ever_positive_then_finished_negative_rate") for r in strategy_positive_negative if r["entry_config"]=="180/5" and r["strategy"]=="HOLD_TO_SETTLEMENT" and r["partition"]=="all"),None)
    holdout_top=next((r for r in development_holdout if r["partition"]=="holdout_weekday"),{})
    first_dev=next((r for r in development_holdout if r["partition"]=="development_weekday"),{})
    best_dynamic_dev=next((r for r in development_holdout if r.get("selection_group")=="best_dynamic_only" and r["partition"]=="development_weekday"),{})
    best_dynamic_hold=next((r for r in development_holdout if r.get("selection_group")=="best_dynamic_only" and r["partition"]=="holdout_weekday"),{})
    chosen_strategy=best_dynamic_hold.get("strategy","LOCK_B_BREAKEVEN")
    chosen_config=best_dynamic_hold.get("entry_config","120/0")
    def mat(config,strategy,part):
        return next((r for r in matrix if r["entry_config"]==config and r["strategy"]==strategy and r["partition"]==part and r["ambiguity_policy"]=="pessimistic" and r["cost_scenario"]=="existing_1pct_notional_fee_stress"),{})
    combined_180=mat("180/5","COMBINED_LOCK_B_SL20","holdout_weekday")
    combined_120=mat("120/5","COMBINED_LOCK_B_SL20","holdout_weekday")
    combined_proxy_120=mat("120/5","COMBINED_LOCK_B_SL20_NOPROGRESS180_PRICE_PROXY","holdout_weekday")
    weekend_metrics=mat(chosen_config,chosen_strategy,"weekend_only")
    weekday_metrics=mat(chosen_config,chosen_strategy,"weekday_only")
    weekend_ev=weekend_metrics.get("ev_per_trade")
    weekday_ev=weekday_metrics.get("ev_per_trade")
    weekend_posneg=next((r.get("ever_positive_then_finished_negative_rate") for r in strategy_positive_negative if r["entry_config"]==chosen_config and r["strategy"]==chosen_strategy and r["partition"]=="weekend"),None)
    weekday_posneg=next((r.get("ever_positive_then_finished_negative_rate") for r in strategy_positive_negative if r["entry_config"]==chosen_config and r["strategy"]==chosen_strategy and r["partition"]=="weekday"),None)
    weekend_giveback=next((r.get("median_giveback") for r in strategy_giveback if r["entry_config"]==chosen_config and r["strategy"]==chosen_strategy and r["partition"]=="weekend"),None)
    weekday_giveback=next((r.get("median_giveback") for r in strategy_giveback if r["entry_config"]==chosen_config and r["strategy"]==chosen_strategy and r["partition"]=="weekday"),None)
    verdict_a="Suggestive" if early.get("paired_EV_difference") is not None and early["paired_EV_difference"]>0 and early.get("paired_markets",0)>=20 else "Cannot assess"
    best_dynamic_bootstrap=next((r for r in bootstrap if r["entry_config"]==best_dynamic_hold.get("entry_config") and r["strategy"]==best_dynamic_hold.get("strategy")),{})
    verdict_b="Suggestive" if best_dynamic_hold.get("ev_per_trade") is not None and best_dynamic_hold["ev_per_trade"]>0 and best_dynamic_hold.get("profit_factor",0)>1 and best_dynamic_bootstrap.get("ev_ci_low",-math.inf)>0 else "Cannot assess"
    verdict_c="Cannot assess"  # no bootstrap CI for the weekday-minus-weekend risk-adjusted contrast
    overall_verdict="Suggestive" if verdict_a=="Suggestive" and best_dynamic_hold.get("ev_per_trade",-math.inf)>0 else "Cannot assess"
    summary=["# Early Entry + Dynamic Profit-Lock Backtest","",
      f"- Cached public markets: {len(market_meta)}; candidate entries: {len(entries)} across six non-pooled configs.",
      f"- Chronological split: {len(dev_dates)} development dates / {len(hold_dates)} holdout dates, 65/35 by date.",
      "- Entry is a 5-second public trade-print VWAP after Binance 1-minute candle signal (ENTRY_EXECUTION_PROXY); it is not a guaranteed executable ask.",
      "- Exit triggers use timestamped SELL-side public prints (EXIT_PROXY_PUBLIC_SELL_PRINT), not historical bid/BBO; sparse-path uncertainty is reported.",
      "- Fixed $5 notional per market/config. Fee/slippage assumptions are sensitivity cases, not verified venue costs. No live strategy or parameters changed.",
      "- Current 180s/5bps live protective logic is NOT FULLY REPLAYABLE; this comparison is a hold-to-settlement research baseline.","",
      "## Direct answers","",
      f"1. Gross (pre-fee) settlement EV, 120/0 vs 180/5: {fmt(next((r.get('mean_settlement_pnl') for r in entry_summary if r['entry_config']=='120/0' and r['partition']=='all'),None))} vs {fmt(baseline180_5.get('mean_settlement_pnl'))} USDC/trade; difference {fmt(early.get('EV_difference'))}. Paired-market difference is {fmt(early.get('paired_EV_difference'))} across {early.get('paired_markets',0)} markets.",
      f"2. 120/2 EV={fmt(entry120_2.get('mean_settlement_pnl'))} vs 180/5={fmt(baseline180_5.get('mean_settlement_pnl'))} (difference {fmt((entry120_2.get('mean_settlement_pnl')-baseline180_5.get('mean_settlement_pnl')) if entry120_2.get('mean_settlement_pnl') is not None and baseline180_5.get('mean_settlement_pnl') is not None else None)}); entry={fmt(entry120_2.get('mean_entry_price'))}, accuracy={fmt(entry120_2.get('direction_accuracy'))}. See entry_config_summary.csv and early_vs_current.csv.",
      f"3. Mean entry 120/0={fmt(next((r.get('mean_entry_price') for r in entry_summary if r['entry_config']=='120/0' and r['partition']=='all'),None))}, 180/5={fmt(baseline180_5.get('mean_entry_price'))}; 120/0 is {fmt(abs(early.get('entry_price_difference') or 0)*100)} cents cheaper on unpaired config samples ({fmt(early.get('paired_entry_price_difference'))} paired price difference).",
      f"4. Accuracy difference (120/0 minus 180/5)={fmt(early.get('accuracy_difference'))} (weekday {fmt(early_weekday.get('accuracy_difference'))}, weekend {fmt(early_weekend.get('accuracy_difference'))}); median MFE/MAE={fmt(mfe120.get('median_mfe_pct'))}%/{fmt(mfe120.get('median_mae_pct'))}% vs {fmt(mfe180.get('median_mfe_pct'))}%/{fmt(mfe180.get('median_mae_pct'))}% (sampled prints, not BBO).",
      f"5. Best dynamic-only exit selected on weekday development is {best_dynamic_dev.get('strategy','n/a')} on {best_dynamic_dev.get('entry_config','n/a')}; weekday holdout EV/PF={fmt(best_dynamic_hold.get('ev_per_trade'))}/{fmt(best_dynamic_hold.get('profit_factor'))}; its date-block EV CI={fmt(best_dynamic_bootstrap.get('ev_ci_low'))} to {fmt(best_dynamic_bootstrap.get('ev_ci_high'))}. Compare fixed TP variants in the matrices; not a live recommendation.",
      f"6. Weekday vs weekend, {chosen_config} + {chosen_strategy}, 1% fee stress EV={fmt(weekday_ev)} vs {fmt(weekend_ev)}; positive-to-negative rate={fmt(weekday_posneg)} vs {fmt(weekend_posneg)}, median giveback={fmt(weekday_giveback)} vs {fmt(weekend_giveback)} USDC. Sample sizes are in the weekday/weekend matrices.",
      f"7. Development-selected holdout candidates are listed below; none selected using holdout. See development_holdout.csv for all requested metrics.",
      f"8. TP10 on 180/5: hits={len(tp10_hits)}/{len(tp10_rows)}, hit rate={fmt(len(tp10_hits)/len(tp10_rows) if tp10_rows else None)}, median hit time={fmt(statistics.median([r['exit_ts']-r['entry_ts'] for r in tp10_hits]) if tp10_hits else None)}s, EV={fmt(mat('180/5','TP10','all').get('ev_per_trade'))}. On 180/5 hold, MFE-positive then settlement-negative rate={fmt(hold10_negative)}; MFE>=5/10/15/20-specific rates are in ever_positive_then_negative.csv (both total-trade and threshold-hit denominators).",
      f"9-12. Weekend MFE/MAE, giveback, positive-to-negative rates and entry-price bands are in weekday_weekend_path.csv, peak_to_exit_giveback.csv, ever_positive_then_negative.csv, and entry_price_interaction.csv.",
      f"13. Top development candidates evaluated without holdout selection: see chronological holdout lines below and bootstrap confidence intervals in bootstrap.csv.",
      f"14. Combined profit/loss replay (LOCK_B + -20% price stop), pessimistic 1% stress, weekday holdout: 180/5 N={combined_180.get('n',0)} EV={fmt(combined_180.get('ev_per_trade'))}, PF={fmt(combined_180.get('profit_factor'))}; 120/5 N={combined_120.get('n',0)} EV={fmt(combined_120.get('ev_per_trade'))}, PF={fmt(combined_120.get('profit_factor'))}. The 120/5 price-only 180s no-progress sensitivity is EV={fmt(combined_proxy_120.get('ev_per_trade'))}, PF={fmt(combined_proxy_120.get('profit_factor'))}. These price-print proxies do not reproduce thesis weakening.",
      "",
      "## Development-selected top candidates (development and untouched holdout)","",*(top_lines or ["- Fewer than five development trades per candidate; no candidate selected."]),"",
      "## Interpretation limits","",
      "- No historical executable ask/bid or L2 is available in this public cache. Public prints may be stale, sparse, and not fillable at the displayed price.",
      "- MFE/MAE are sampled print excursions. Intratimestamp ambiguity is replayed optimistic/pessimistic or excluded; gap uncertainty is not interpolated.",
      "- The 1% notional fee and 1c/2c slippage are stress scenarios. Results are descriptive, not live PnL forecasts.",
      "- A mechanical -20% price stop reduced average-loss severity in several partitions but made the tested holdout EV negative; historical public prints therefore do not support enabling that stop by itself. Thesis-conditioned exits require prospective shadow evidence.",
      "- Weekend is comparison-only; no weekend policy or live entry/exit/size/stop/TP parameter was changed.",
      f"- Hypothesis A — earlier cheaper entry: {verdict_a}. Lower price / accuracy / EV decomposition is in entry_economics_decomposition.csv; proxy-based retrospective evidence is not execution proof.",
      f"- Hypothesis B — dynamic profit-lock vs fixed TP: {verdict_b}. A positive point estimate in a small holdout is only suggestive, not proof of superiority.",
      f"- Hypothesis C — weekday-only risk-adjusted improvement: {verdict_c}. Weekend policy remains unchanged.",""]
    summary[-1:-1]=[f"- Overall direction — move research toward 120s/0–2bps plus dynamic protection: {overall_verdict} for further shadow/prospective research only; not enough to change live policy."]
    (output/"summary.md").write_text("\n".join(summary),encoding="utf-8")
    return {"entries":entries,"replay":replay,"matrix":matrix,"output":output,"markets":market_meta}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir",action="append",default=[])
    parser.add_argument("--sample-csv",default="reports/unified_strategy_research/public_refresh/public_market_sample.csv")
    parser.add_argument("--btc-cache",default="data/btc_history")
    parser.add_argument("--output",default="reports/profit_lock_backtest")
    parser.add_argument("--start",default="2026-07-27");parser.add_argument("--end",default="2026-09-20")
    parser.add_argument("--timezone",default="America/New_York");parser.add_argument("--max-gap-sec",type=float,default=30.0)
    args=parser.parse_args()
    dirs=[Path(x) for x in args.cache_dir] or [Path("data/polymarket_history_unified_20260727_20260920"),Path("data/polymarket_history_public_study_20260727_20260920")]
    result=run(cache_dirs=dirs,sample_csv=Path(args.sample_csv),btc_cache=Path(args.btc_cache),output=Path(args.output),
               start_date=date.fromisoformat(args.start),end_date=date.fromisoformat(args.end),timezone_name=args.timezone,offline=True,max_gap_sec=args.max_gap_sec)
    print(f"markets={len(result['markets'])} entries={len(result['entries'])} replay_rows={len(result['replay'])} output={result['output']}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline short-term take-profit research using cached public market history.

Historical public trade prints are execution proxies only. This script never
changes or imports live trading authority; missing evidence remains missing.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.backtest_simple_trend_hold import (  # noqa: E402
    _last_closed, _read_csv, _returns_for_market, _sample_market_files,
    _write_csv, entry_proxy, load_btc_candles, market_start, threshold_side,
    winner_from_gamma,
)

TPS = (0.03, 0.05, 0.075, 0.10, 0.15, 0.20)
OBSERVATIONS = (120, 180)
THRESHOLDS = (0, 2, 5)
TIME_STOPS = (300, 180, 120, 60)  # seconds remaining
STOP_LOSSES = (None, 0.05, 0.10, 0.15, 0.20)
TIMING_HORIZONS = (30, 60, 120, 180, 300)
PRICE_BUCKETS = ((0, .60, "<0.60"), (.60, .70, "0.60-0.70"), (.70, .75, "0.70-0.75"),
                 (.75, .80, "0.75-0.80"), (.80, .85, "0.80-0.85"), (.85, .90, "0.85-0.90"),
                 (.90, 1.01, ">0.90"))


def _trades(market: dict[str, Any], start: int) -> list[dict[str, Any]]:
    rows = []
    for raw in market.get("trades", []):
        try:
            ts, px, qty = float(raw["timestamp"]), float(raw["price"]), float(raw["size"])
            side = str(raw.get("outcome", "")).upper()
            aggressor = str(raw.get("side", "")).upper()
        except (KeyError, TypeError, ValueError):
            continue
        if start <= ts < start + 900 and side in {"UP", "DOWN"} and 0 < px < 1 and qty > 0:
            rows.append({"ts": ts, "price": px, "size": qty, "outcome": side, "aggressor": aggressor})
    return sorted(rows, key=lambda r: (r["ts"], r["outcome"], r["price"]))


def _sell_marks(rows: list[dict[str, Any]], side: str, after: float, end: float,
                *, sell_only: bool = True) -> list[dict[str, Any]]:
    # Data API side is the taker side; SELL prints are the conservative public
    # sell-side proxy. Non-SELL prints are retained only for excursion marks.
    return [r for r in rows if r["outcome"] == side and after < r["ts"] <= end
            and (not sell_only or r["aggressor"] == "SELL")]


def _first_hit(rows: list[dict[str, Any]], level: float) -> dict[str, Any] | None:
    return next((r for r in rows if r["price"] >= level), None)


def _last_before(rows: list[dict[str, Any]], ts: float) -> dict[str, Any] | None:
    return next((r for r in reversed(rows) if r["ts"] <= ts), None)


def _timed_exit_proxy(rows: list[dict[str, Any]], forced_ts: float) -> tuple[dict[str, Any] | None, float | None]:
    mark = next((r for r in reversed(rows) if r["ts"] >= forced_ts), None)
    if mark is None:
        mark = _last_before(rows, forced_ts)
    return mark, abs(mark["ts"]-forced_ts) if mark else None


def _first_touch(tp_ts: float | None, sl_ts: float | None) -> tuple[str, bool]:
    if tp_ts is not None and sl_ts is not None and tp_ts == sl_ts:
        return "AMBIGUOUS", True
    if tp_ts is not None and (sl_ts is None or tp_ts < sl_ts):
        return "TP", False
    if sl_ts is not None and (tp_ts is None or sl_ts < tp_ts):
        return "SL", False
    return "SETTLEMENT", False


def _outcome_for_token(market_record: dict[str, Any], token_id: str) -> str | None:
    market=(market_record.get("gamma") or {}).get("market") or {}
    outcomes=market.get("outcomes") or []
    tokens=market.get("clobTokenIds") or []
    if isinstance(outcomes,str):
        try: outcomes=json.loads(outcomes)
        except ValueError: outcomes=[]
    if isinstance(tokens,str):
        try: tokens=json.loads(tokens)
        except ValueError: tokens=[]
    return next((str(outcome).upper() for outcome,token in zip(outcomes,tokens) if str(token)==str(token_id)),None)


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _local_stoploss_conflicts(db_path: Path | None, public_by_slug: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if db_path is None or not db_path.exists():
        return [{"status": "UNAVAILABLE", "reason": "trade journal path missing"}]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        exits = conn.execute("SELECT ts,side,price,qty,instrument_id,payload_json FROM order_events WHERE event_type='ORDER_TAKER_EXIT_SUBMIT' ORDER BY id").fetchall()
        fills = conn.execute("SELECT ts,side,price,qty,instrument_id,payload_json FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id").fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return [{"status": "UNAVAILABLE", "reason": f"read-only journal query failed: {type(exc).__name__}"}]
    fills_by_instrument: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fills:
        instrument = str(f["instrument_id"] or "")
        if instrument:
            fills_by_instrument[instrument].append({**dict(f), "ts_epoch": _epoch(f["ts"])})
    result=[]
    for e in exits:
        try:
            payload=json.loads(e["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload={}
        reason=str(payload.get("reason") or e["side"] or "").lower()
        if "stop_loss" not in reason and "stop-loss" not in reason and "absolute_max_loss_breaker" not in str(payload.get("decision_reason", "")).lower():
            continue
        slug=str(payload.get("slug") or "")
        market=public_by_slug.get(slug)
        exit_ts=_epoch(e["ts"])
        instrument=str(e["instrument_id"] or payload.get("instrument_id") or "")
        records=fills_by_instrument.get(instrument, [])
        if exit_ts is None:
            result.append({"slug":slug,"exit_ts":e["ts"],"status":"BAD_EXIT_TIMESTAMP"});continue
        if not market:
            result.append({"slug":slug,"exit_ts":e["ts"],"status":"PUBLIC_MARKET_NOT_CACHED"});continue
        token=instrument.rsplit("-",1)[-1].removesuffix(".POLYMARKET")
        side=_outcome_for_token(market,token)
        start=market_start(slug)
        start_ts=float(start or 0)
        position_qty=cost=0.0; entry_ts=None
        for f in records:
            ts=f.get("ts_epoch")
            if ts is None or ts<start_ts or ts>=exit_ts:
                continue
            qty=float(f["qty"] or 0); price=float(f["price"] or 0)
            if str(f["side"] or "").upper()=="BUY":
                position_qty+=qty; cost+=qty*price; entry_ts=ts if entry_ts is None else min(entry_ts,ts)
            elif str(f["side"] or "").upper()=="SELL" and position_qty>0:
                avg=cost/position_qty
                sold=min(position_qty,qty); position_qty-=sold; cost=max(0,cost-sold*avg)
        if not side:
            result.append({"slug":slug,"exit_ts":e["ts"],"token_id":token,"status":"OUTCOME_TOKEN_UNMAPPED"});continue
        if position_qty<=0 or entry_ts is None:
            result.append({"slug":slug,"exit_ts":e["ts"],"token_id":token,"status":"NO_CONFIRMED_PRE_EXIT_BUY_INVENTORY"});continue
        entry=cost/position_qty
        sells=_sell_marks(market["trades"],side,entry_ts,exit_ts,sell_only=True)
        hits=[]
        for pct in (.03,.05,.075,.10):
            level,reachable=tp_level(entry,pct)
            hit=_first_hit(sells,level) if reachable=="REACHABLE" and sells else None
            observed=bool(sells) and reachable=="REACHABLE"
            hits.append({f"tp{pct*100:g}_hit_before_stop":bool(hit) if observed else None,
                         f"tp{pct*100:g}_hit_ts":hit["ts"] if hit else None})
        result.append({"slug":slug,"exit_ts":e["ts"],"side":side,"entry_price_vwap":entry,"open_qty_at_stop_submit":position_qty,
                       "stop_submit_price":e["price"],"entry_ts":entry_ts,"matched_print_count":len(sells),"status":"JOINED_PROXY_PATH",
                       **{k:v for item in hits for k,v in item.items()},
                       "interpretation":"prior proxy TP touch before protective-exit submission; not proof a live TP order would fill"})
    if not result:
        result.append({"status":"NO_STOP_LOSS_SUBMITS_FOUND"})
    return result


def _load_all_cached_markets(cache_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    cached={}
    for directory in cache_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("btc-updown-15m-*.json"):
            try:
                record=json.loads(path.read_text(encoding="utf-8"))
                slug=record.get("slug")
                if slug:
                    cached[slug]=record
            except (OSError,json.JSONDecodeError):
                continue
    return cached


def _excursions(rows: list[dict[str, Any]], entry: float, entry_ts: float) -> dict[str, Any]:
    if not rows:
        return {"mfe_price": None, "mae_price": None, "mfe_pct": None, "mae_pct": None,
                "time_to_mfe_sec": None, "time_to_mae_sec": None, "excursion_source": "NO_POST_ENTRY_PRINTS"}
    hi = max(rows, key=lambda r: r["price"])
    lo = min(rows, key=lambda r: r["price"])
    return {"mfe_price": hi["price"], "mae_price": lo["price"],
            "mfe_pct": max(0.0, (hi["price"] / entry - 1) * 100),
            "mae_pct": min(0.0, (lo["price"] / entry - 1) * 100),
            "time_to_mfe_sec": hi["ts"] - entry_ts, "time_to_mae_sec": lo["ts"] - entry_ts,
            "excursion_source": "PUBLIC_TRADE_PRINT_MARKS_NOT_BBO"}


def tp_level(entry: float, pct: float) -> tuple[float, str]:
    level = entry * (1 + pct)
    return (level, "REACHABLE") if level <= 1.0 else (level, "UNREACHABLE_BY_PRICE_CAP")


def _fee(notional: float, scenario: str) -> float:
    return notional * .01 if scenario == "existing_fee_assumption_1pct_notional" else 0.0


def _exit_pnl(entry: float, exit_px: float, notional: float, fee_scenario: str,
              slippage: float = 0.0) -> dict[str, float]:
    shares = notional / entry
    effective = max(0.0, exit_px - slippage)
    gross = shares * (effective - entry)
    fee = _fee(notional, fee_scenario)
    return {"gross_pnl": gross, "fee_usdc": fee, "net_pnl": gross - fee,
            "roi": (gross - fee) / notional, "effective_exit_price": effective}


def _settlement_pnl(side: str, winner: str | None, entry: float, notional: float,
                    fee_scenario: str, slippage: float = 0.0) -> dict[str, float] | None:
    if winner not in {"UP", "DOWN"}:
        return None
    payout = 1.0 if side == winner else 0.0
    return _exit_pnl(entry, payout, notional, fee_scenario, slippage=slippage)


def _metrics(rows: list[dict[str, Any]], pnl_key: str = "net_pnl") -> dict[str, Any]:
    vals = [float(r[pnl_key]) for r in rows if r.get(pnl_key) is not None]
    if not vals:
        return {"n": 0, "win_rate": None, "mean_pnl": None, "median_pnl": None,
                "roi": None, "profit_factor": None, "max_drawdown": None,
                "longest_losing_streak": 0, "average_win": None, "average_loss": None,
                "p5": None, "p1": None, "worst_trade": None}
    ordered = sorted(rows, key=lambda x: (str(x.get("local_date", "")), str(x.get("slug", ""))))
    seq = [float(r[pnl_key]) for r in ordered if r.get(pnl_key) is not None]
    wins, losses = [v for v in vals if v > 0], [v for v in vals if v < 0]
    equity = peak = dd = 0.0
    streak = max_streak = 0
    for v in seq:
        equity += v; peak = max(peak, equity); dd = max(dd, peak-equity)
        streak = streak + 1 if v < 0 else 0; max_streak = max(max_streak, streak)
    invested = sum(float(r.get("notional", 5)) for r in rows if r.get(pnl_key) is not None)
    sv = sorted(vals)
    q = lambda p: sv[min(len(sv)-1, int((len(sv)-1)*p))]
    directional=[r for r in rows if r.get("side") in {"UP","DOWN"} and r.get("winner") in {"UP","DOWN"}]
    profitability=sum(v > 0 for v in vals)/len(vals)
    return {"n": len(vals), "win_rate": profitability, "profitability_rate": profitability,
            "settlement_directional_accuracy":sum(r["side"]==r["winner"] for r in directional)/len(directional) if directional else None,
            "mean_pnl": statistics.mean(vals), "median_pnl": statistics.median(vals),
            "roi": sum(vals)/invested if invested else None,
            "profit_factor": sum(wins)/abs(sum(losses)) if losses else (math.inf if wins else 0),
            "max_drawdown": dd, "longest_losing_streak": max_streak,
            "average_win": statistics.mean(wins) if wins else None,
            "average_loss": statistics.mean(losses) if losses else None,
            "p5": q(.05), "p1": q(.01), "worst_trade": min(vals)}


def _block_ci(rows: list[dict[str, Any]], key: str, reps: int = 1500, seed: int = 17) -> tuple[float | None, float | None]:
    blocks: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            blocks[str(r.get("local_date") or r.get("slug"))].append(float(r[key]))
    if len(blocks) < 2:
        return None, None
    rng = random.Random(seed); names = list(blocks); estimates = []
    for _ in range(reps):
        sample = rng.choices(names, k=len(names)); vals = [v for n in sample for v in blocks[n]]
        if vals: estimates.append(statistics.mean(vals))
    estimates.sort()
    return estimates[int(.025*len(estimates))], estimates[min(len(estimates)-1, int(.975*len(estimates)))]


def _candidate_path(row: dict[str, Any], market_trades: list[dict[str, Any]]) -> None:
    entry_ts = float(row["entry_ts"]); end = float(row["market_start_epoch"])+900
    all_marks = _sell_marks(market_trades, row["side"], entry_ts, end, sell_only=False)
    sell_marks = _sell_marks(market_trades, row["side"], entry_ts, end, sell_only=True)
    row.update(_excursions(all_marks, float(row["entry_price"]), entry_ts))
    row["sell_side_proxy_prints_after_entry"] = len(sell_marks)
    row["final_direction_wrong"] = row.get("winner") in {"UP", "DOWN"} and row["side"] != row["winner"]
    for pct in TPS:
        name = f"tp{pct*100:g}"
        level, reach = tp_level(float(row["entry_price"]), pct)
        hit = _first_hit(sell_marks, level) if reach == "REACHABLE" else None
        row[f"{name}_target_price"] = level
        row[f"{name}_reachability"] = reach
        row[f"{name}_hit"] = hit is not None
        row[f"{name}_hit_ts"] = hit["ts"] if hit else None
        row[f"{name}_time_to_hit_sec"] = hit["ts"]-entry_ts if hit else None
        row[f"{name}_exit_price"] = level if hit else None
        row[f"{name}_return_pct"] = pct*100 if hit else None
        row[f"{name}_hit_proxy"] = "TP_HIT_PROXY_PUBLIC_SELL_TRADE_PRINT_NOT_GUARANTEED_EXECUTABLE" if hit else None
        row[f"{name}_final_wrong_but_hit"] = bool(hit and row["final_direction_wrong"])
        before = [x for x in all_marks if not hit or x["ts"] <= hit["ts"]]
        adverse = min(before, key=lambda x: x["price"]) if before else None
        row[f"{name}_mae_before_tp_pct"] = min(0.0, (adverse["price"]/float(row["entry_price"])-1)*100) if hit and adverse else None


def run(*, cache_dirs: list[Path], sample_csv: Path, btc_cache: Path, output: Path,
        start_date: date, end_date: date, timezone_name: str, offline: bool,
        notional: float = 5.0, db_path: Path | None = None) -> dict[str, Any]:
    markets, sample = _sample_market_files(cache_dirs, sample_csv)
    starts = [market_start(m.get("slug", "")) for m in markets]
    starts = [s for s in starts if s]
    if not starts:
        raise RuntimeError("No cached public market records found for the sample.")
    candles, candle_diag = load_btc_candles(min(starts)-1200, max(starts)+960, btc_cache, offline=offline)
    btc_by_start = {int(c["ts"]): c for c in candles}
    dates = sorted({datetime.fromtimestamp(s, ZoneInfo(timezone_name)).date().isoformat() for s in starts})
    dev_dates = set(dates[:max(1, int(len(dates)*.65))]); hold_dates = set(dates[max(1, int(len(dates)*.65)):])
    candidate_rows: list[dict[str, Any]] = []; market_meta = {}; trade_map = {}
    quality: list[dict[str, Any]] = []
    for market in markets:
        slug = market.get("slug"); start = market_start(slug or "")
        if not start:
            continue
        local_dt = datetime.fromtimestamp(start, ZoneInfo(timezone_name))
        if not start_date <= local_dt.date() <= end_date:
            continue
        winner = winner_from_gamma((market.get("gamma") or {}).get("market") or {})
        trades = _trades(market, start); trade_map[slug] = trades
        is_wknd = local_dt.weekday() >= 5
        market_meta[slug] = {"local_date": local_dt.date().isoformat(), "weekday_weekend": "weekend" if is_wknd else "weekday",
                             "et_hour_block": f"{local_dt.hour//6*6:02d}-{local_dt.hour//6*6+6:02d}", "winner": winner,
                             "start": start, "trades": trades}
        quality.append({"slug": slug, "gamma_status": (market.get("gamma") or {}).get("status"),
                        "winner": winner, "trade_fetch_status": market.get("trade_fetch_status"),
                        "trade_prints": len(trades), "price_history_points": sum(len(v) for v in (market.get("price_history") or {}).values()),
                        "entry_source": "PUBLIC_TRADE_PRINT_PROXY", "exit_source": "SELL_SIDE_PUBLIC_TRADE_PRINT_PROXY",
                        "historical_bbo": False, "historical_bid": False, "btc_source": "Binance BTCUSDT 1m OHLCV"})
        opening = btc_by_start.get(start)
        if winner not in {"UP", "DOWN"} or opening is None:
            continue
        returns = _returns_for_market(candles, start)
        for obs in OBSERVATIONS:
            for thresh in THRESHOLDS:
                move = returns.get(obs)
                side = threshold_side(move, thresh) if move is not None else None
                if side is None:
                    continue
                signal_ts = start+obs
                entry, entry_status = entry_proxy(
                    [{"timestamp": r["ts"], "price": r["price"], "size": r["size"], "outcome": r["outcome"]}
                     for r in trades], side, signal_ts, 0, "vwap_5s")
                if entry is None:
                    continue
                # The entry VWAP aggregates the first five seconds after signal;
                # start the post-entry path after that observation window to
                # avoid counting a trade used to price entry as a later exit.
                entry_ts = signal_ts + 5
                row = {"slug": slug, "market_start_epoch": start, "local_date": local_dt.date().isoformat(),
                       "weekday_weekend": "weekend" if is_wknd else "weekday", "et_hour_block": market_meta[slug]["et_hour_block"],
                       "observation_sec": obs, "threshold_bps": thresh, "signal_return_bps": move,
                       "side": side, "winner": winner, "final_direction_correct": side == winner,
                       "signal_ts": signal_ts, "entry_ts": entry_ts, "entry_price": entry, "entry_status": entry_status,
                       "entry_execution_proxy": "ENTRY_EXECUTION_PROXY_PUBLIC_TRADE_PRINT_VWAP_5S",
                       "notional": notional, "shares": notional/entry,
                       "settlement_direction_wrong": side != winner,
                       "protective_exit_replay_status": "NOT_FULLY_REPLAYABLE_NO_HISTORICAL_BBO_OR_LIVE_SIGNAL_STATE"}
                _candidate_path(row, trades)
                candidate_rows.append(row)
    output.mkdir(parents=True, exist_ok=True)

    # One pre-specified signal configuration drives the TP study. The other
    # requested observation/threshold settings are retained as entry-signal
    # sensitivity evidence, not pooled as if they were independent trades.
    analysis_rows = [r for r in candidate_rows if r["observation_sec"] == 180 and r["threshold_bps"] == 5]
    signal_sensitivity = []
    for obs in OBSERVATIONS:
        for threshold in THRESHOLDS:
            group = [r for r in candidate_rows if r["observation_sec"] == obs and r["threshold_bps"] == threshold]
            wins = [r for r in group if r["final_direction_correct"]]
            settlement = [{**r, "net_pnl": _settlement_pnl(r["side"], r["winner"], r["entry_price"], notional, "existing_fee_assumption_1pct_notional")["net_pnl"]}
                          for r in group]
            signal_sensitivity.append({"observation_sec": obs, "threshold_bps": threshold, "n": len(group),
                                       "settlement_directional_accuracy": len(wins)/len(group) if group else None,
                                       **_metrics(settlement), "entry_rule": "one signal entry per market/config; 5s print VWAP proxy"})
    _write_csv(output / "signal_sensitivity.csv", signal_sensitivity)

    # Per TP target hit distributions, excursion summaries, and exit variants.
    tp_rates=[]; tp_time=[]; excursion=[]; variants=[]; cost_rows=[]; timing=[]; tp_sl=[]; ambiguous=[]
    for fee_scenario in ("zero_execution_cost", "existing_fee_assumption_1pct_notional"):
        rows=[]
        for r in analysis_rows:
            pnl=_settlement_pnl(r["side"],r["winner"],r["entry_price"],notional,
                                "zero" if fee_scenario=="zero_execution_cost" else fee_scenario)
            rows.append({**r,"net_pnl":pnl["net_pnl"],"notional":notional})
        for part, subset in (("all",rows),("development",[r for r in rows if r["local_date"] in dev_dates]),
                             ("holdout",[r for r in rows if r["local_date"] in hold_dates])):
            variants.append({"variant":"HOLD_TO_SETTLEMENT","partition":part,"cost_scenario":fee_scenario,**_metrics(subset)})
    variants.append({"variant":"CURRENT_PROTECTIVE_EXIT","partition":"not_replayable",
                     "replay_status":"NOT FULLY REPLAYABLE: no historical BBO/L2 and complete thesis-state replay"})
    for pct in TPS:
        key=f"tp{pct*100:g}"; hits=[r for r in analysis_rows if r.get(f"{key}_hit")]
        for horizon in TIMING_HORIZONS:
            count=sum(float(r[f"{key}_time_to_hit_sec"]) <= horizon for r in hits)
            tp_time.append({"tp_pct":pct*100,"within_sec":horizon,"hit_n":count,"entry_n":len(analysis_rows),"hit_rate":count/len(analysis_rows) if analysis_rows else None})
        tp_rates.append({"tp_pct":pct*100,"entry_n":len(analysis_rows),"hit_n":len(hits),
                         "hit_rate_anytime":len(hits)/len(analysis_rows) if analysis_rows else None,
                         "within_30s":sum(float(r[f"{key}_time_to_hit_sec"])<=30 for r in hits),
                         "within_60s":sum(float(r[f"{key}_time_to_hit_sec"])<=60 for r in hits),
                         "within_120s":sum(float(r[f"{key}_time_to_hit_sec"])<=120 for r in hits),
                         "within_180s":sum(float(r[f"{key}_time_to_hit_sec"])<=180 for r in hits),
                         "within_300s":sum(float(r[f"{key}_time_to_hit_sec"])<=300 for r in hits),
                         "median_time_to_tp_sec":statistics.median([r[f"{key}_time_to_hit_sec"] for r in hits]) if hits else None,
                         "final_wrong_but_tp_hit_n":sum(bool(r[f"{key}_final_wrong_but_hit"]) for r in hits),
                         "final_wrong_but_tp_hit_rate_of_hits":sum(bool(r[f"{key}_final_wrong_but_hit"]) for r in hits)/len(hits) if hits else None,
                         "unreachable_by_price_cap_n":sum(r[f"{key}_reachability"]!="REACHABLE" for r in analysis_rows),
                         "tp_hit_semantics":"first later public SELL trade print >= target; proxy only, not guaranteed executable"})
        mae=[r[f"{key}_mae_before_tp_pct"] for r in hits if r[f"{key}_mae_before_tp_pct"] is not None]
        mfe_ge = [r for r in analysis_rows if r.get("mfe_pct") is not None and float(r["mfe_pct"]) >= pct*100]
        excursion.append({"tp_pct":pct*100,"mfe_ge_rate":len(mfe_ge)/len(analysis_rows) if analysis_rows else None,
                          "hit_n":len(hits),"mae_before_tp_median_pct":statistics.median(mae) if mae else None,
                          "mae_before_tp_p75_pct":sorted(mae)[int(.75*(len(mae)-1))] if mae else None,
                          "mae_before_tp_p90_pct":sorted(mae)[int(.90*(len(mae)-1))] if mae else None,
                          "mae_before_tp_worst_pct":min(mae) if mae else None})
        base=[]
        for r in analysis_rows:
            hit=r[f"{key}_hit"]; settle=_settlement_pnl(r["side"],r["winner"],r["entry_price"],notional,"zero")
            exit_px=r[f"{key}_target_price"] if hit else (1.0 if r["side"]==r["winner"] else 0.0)
            base.append({**r,"net_pnl":_exit_pnl(r["entry_price"],exit_px,notional,"zero")["net_pnl"],"exit_kind":"TP" if hit else "SETTLEMENT"})
        for fee_scenario in ("zero_execution_cost", "existing_fee_assumption_1pct_notional"):
            priced=[]
            for r in analysis_rows:
                hit=r[f"{key}_hit"]; px=r[f"{key}_target_price"] if hit else (1.0 if r["side"]==r["winner"] else 0.0)
                pnl=_exit_pnl(r["entry_price"],px,notional,"zero" if fee_scenario=="zero_execution_cost" else fee_scenario)
                priced.append({**r,"net_pnl":pnl["net_pnl"],"notional":notional})
            for part, subset in (("all",priced),("development",[r for r in priced if r["local_date"] in dev_dates]),
                                 ("holdout",[r for r in priced if r["local_date"] in hold_dates])):
                variants.append({"variant":f"TP{pct*100:g}_OR_SETTLEMENT","tp_pct":pct*100,"partition":part,
                                 "cost_scenario":fee_scenario,**_metrics(subset)})
        for secs_left in TIME_STOPS:
            exited=[]
            for r in analysis_rows:
                hit=r[f"{key}_hit"]; start=float(r["market_start_epoch"]); forced_ts=start+900-secs_left
                if hit:
                    px=float(r[f"{key}_target_price"]); kind="TP"
                else:
                    marks=_sell_marks(trade_map[r["slug"]],r["side"],float(r["entry_ts"]),start+900,sell_only=True)
                    mark, mark_age = _timed_exit_proxy(marks, forced_ts)
                    if mark is None:
                        continue
                    px=mark["price"];kind="TIMED_SELL_PRINT_PROXY"
                    r={**r,"timed_exit_proxy_age_sec":mark_age}
                pnl=_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")
                exited.append({**r,"net_pnl":pnl["net_pnl"],"notional":notional,"exit_kind":kind,"exit_price":px})
            for part, subset in (("all",exited),("development",[r for r in exited if r["local_date"] in dev_dates]),("holdout",[r for r in exited if r["local_date"] in hold_dates])):
                variants.append({"variant":f"TP{pct*100:g}_OR_TMINUS{secs_left//60}M","tp_pct":pct*100,"forced_exit_secs_left":secs_left,
                                 "cost_scenario":"existing_fee_assumption_1pct_notional","partition":part,**_metrics(subset)})
                cost_rows.append({"variant":f"TP{pct*100:g}_OR_TMINUS{secs_left//60}M","tp_pct":pct*100,"partition":part,"cost_scenario":"existing_fee_assumption_1pct_notional",**_metrics(subset)})
    # Fixed-cost sensitivity applied to exact same observed path/exit choice.
    for r in analysis_rows:
        for pct in TPS:
            name=f"tp{pct*100:g}"; hit=r[f"{name}_hit"]
            base_px=r[f"{name}_target_price"] if hit else (1.0 if r["side"]==r["winner"] else 0)
            for cost_label,fee,slip in (("zero_execution_cost",0,0),("existing_fee_assumption_1pct_notional",notional*.01,0),
                                        ("plus_1c_adverse_exit_slippage",0,.01),("plus_2c_adverse_exit_slippage",0,.02)):
                effective=max(0,base_px-slip)
                val=(notional/r["entry_price"])*(effective-r["entry_price"])-fee
                cost_rows.append({"slug":r["slug"],"tp_pct":pct*100,"partition":"all","cost_scenario":cost_label,"net_pnl":val,"roi":val/notional,"notional":notional})
    for pct in TPS:
        for cost_label in ("zero_execution_cost","existing_fee_assumption_1pct_notional","plus_1c_adverse_exit_slippage","plus_2c_adverse_exit_slippage"):
            grouped=[r for r in cost_rows if r.get("tp_pct")==pct*100 and r.get("cost_scenario")==cost_label and r.get("slug")]
            for part, subset in (("all",grouped),("development",[r for r in grouped if r["slug"] in {x["slug"] for x in analysis_rows if x["local_date"] in dev_dates}]),
                                 ("holdout",[r for r in grouped if r["slug"] in {x["slug"] for x in analysis_rows if x["local_date"] in hold_dates}])):
                cost_rows.append({"variant":"TP_OR_SETTLEMENT","tp_pct":pct*100,"partition":part,"cost_scenario":cost_label,
                                  **_metrics(subset)})
    for side in ("weekday","weekend"):
        subset=[r for r in analysis_rows if r["weekday_weekend"]==side]
        for pct in TPS:
            k=f"tp{pct*100:g}"; hits=[r for r in subset if r[k+"_hit"]]
            pnl=[]
            for r in subset:
                px=r[k+"_target_price"] if r[k+"_hit"] else (1.0 if r["side"]==r["winner"] else 0)
                pnl.append({**r,"net_pnl":_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")["net_pnl"]})
            timing.append({"group":side,"tp_pct":pct*100,"entry_n":len(subset),"hit_rate":len(hits)/len(subset) if subset else None,
                           "median_time_to_tp_sec":statistics.median([r[k+"_time_to_hit_sec"] for r in hits]) if hits else None,
                           "mean_mfe_pct":statistics.mean([r["mfe_pct"] for r in subset if r.get("mfe_pct") is not None]) if subset else None,
                           "mean_mae_pct":statistics.mean([r["mae_pct"] for r in subset if r.get("mae_pct") is not None]) if subset else None,
                           **_metrics(pnl)})
    hour_rows=[]
    for h in ("00-06","06-12","12-18","18-24"):
        subset=[r for r in analysis_rows if r["et_hour_block"]==h]
        for pct in TPS:
            k=f"tp{pct*100:g}";hits=[r for r in subset if r[k+"_hit"]]
            hourly_pnl=[]
            for r in subset:
                px=r[k+"_target_price"] if r[k+"_hit"] else (1.0 if r["side"]==r["winner"] else 0)
                hourly_pnl.append({**r,"net_pnl":_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")["net_pnl"]})
            hour_rows.append({"hour_et":h,"tp_pct":pct*100,"n":len(subset),"tp_hit_rate":len(hits)/len(subset) if subset else None,
                              "median_time_to_tp_sec":statistics.median([r[k+"_time_to_hit_sec"] for r in hits]) if hits else None,
                              "mean_mfe_pct":statistics.mean([r["mfe_pct"] for r in subset if r.get("mfe_pct") is not None]) if subset else None,
                              "mean_mae_pct":statistics.mean([r["mae_pct"] for r in subset if r.get("mae_pct") is not None]) if subset else None,
                              **_metrics(hourly_pnl)})
    entry_buckets=[]
    for lo,hi,label in PRICE_BUCKETS:
        subset=[r for r in analysis_rows if lo<=r["entry_price"]<hi]
        for pct in TPS:
            k=f"tp{pct*100:g}"; hits=[r for r in subset if r[k+"_hit"]]
            entry_buckets.append({"entry_price_bucket":label,"tp_pct":pct*100,"n":len(subset),"hit_n":len(hits),
                                  "tp_hit_rate":len(hits)/len(subset) if subset else None,
                                  "reachable_n":sum(r[k+"_reachability"]=="REACHABLE" for r in subset),
                                  "unreachable_n":sum(r[k+"_reachability"]!="REACHABLE" for r in subset)})
    # TP/SL first-touch grid. Exact same-second hits are intrinsically ambiguous.
    for tp in TPS:
        k=f"tp{tp*100:g}"
        for sl in STOP_LOSSES:
            outcomes=[]
            for r in analysis_rows:
                tp_ts=r[k+"_hit_ts"]
                if not r[k+"_hit"] and r[k+"_reachability"]!="REACHABLE": tp_ts=None
                marks=_sell_marks(trade_map[r["slug"]],r["side"],float(r["entry_ts"]),r["market_start_epoch"]+900,sell_only=True)
                sl_row=next((x for x in marks if sl is not None and x["price"]<=r["entry_price"]*(1-sl)),None)
                sl_ts=sl_row["ts"] if sl_row else None
                result, ambiguous_case=_first_touch(tp_ts, sl_ts)
                if ambiguous_case:
                    ambiguous.append({"slug":r["slug"],"tp_pct":tp*100,"sl_pct":sl*100 if sl else None,"timestamp":tp_ts,"resolution":"AMBIGUOUS_INTRABAR_ORDER_PUBLIC_TRADES_SAME_SECOND"})
                outcome={"tp_pct":tp*100,"sl_pct":sl*100 if sl else None,"result":result,"row":r,"tp_ts":tp_ts,"sl_ts":sl_ts}
                outcomes.append(outcome)
            eventual_tp_hit_n = sum(bool(o["row"][k+"_hit"]) and o["result"] == "SL" for o in outcomes)
            for policy in ("optimistic","pessimistic","exclude_ambiguous"):
                chosen=[]
                for o in outcomes:
                    if o["result"]=="AMBIGUOUS" and policy=="exclude_ambiguous": continue
                    res=("TP" if policy=="optimistic" else "SL") if o["result"]=="AMBIGUOUS" else o["result"]
                    r=o["row"]
                    if res=="TP": px=r[k+"_target_price"]
                    elif res=="SL": px=r["entry_price"]*(1-(sl or 0))
                    else: px=1.0 if r["side"]==r["winner"] else 0.0
                    pnl=_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")
                    chosen.append({**r,"net_pnl":pnl["net_pnl"],"notional":notional,"event_result":res})
                tp_sl.append({"tp_pct":tp*100,"sl_pct":sl*100 if sl else None,"ambiguity_policy":policy,
                              "tp_hit_n":sum(x["event_result"]=="TP" for x in chosen),"sl_hit_first_n":sum(x["event_result"]=="SL" for x in chosen),
                              "eventual_tp_hit_but_sl_before_tp_n":eventual_tp_hit_n,
                              **_metrics(chosen)})

    # Chronological dev/holdout summary; select best descriptive config on dev only.
    split_rows=[]
    for r in variants:
        if r.get("partition") in {"development","holdout"}:
            split_rows.append(r)
    dev=[r for r in variants if str(r.get("variant","")).startswith("TP") and r.get("partition")=="development" and r.get("n",0)>=5
         and r.get("cost_scenario")=="existing_fee_assumption_1pct_notional"]
    selected=max(dev,key=lambda x:x.get("mean_pnl") if x.get("mean_pnl") is not None else -math.inf,default=None)
    selected_hold=next((r for r in variants if selected and r.get("variant")==selected.get("variant") and r.get("partition")=="holdout"),None)
    # Block bootstrap per candidate TP+settlement, with date blocks.
    intervals=[]
    for pct in TPS:
        k=f"tp{pct*100:g}"; rows=[]
        for r in analysis_rows:
            px=r[k+"_target_price"] if r[k+"_hit"] else (1.0 if r["side"]==r["winner"] else 0)
            x=_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")
            rows.append({**r,"net_pnl":x["net_pnl"],"roi":x["roi"]})
        for metric in ("net_pnl","roi"):
            lo,hi=_block_ci(rows,metric)
            intervals.append({"tp_pct":pct*100,"metric":metric,"ci_low":lo,"ci_high":hi,"method":"local-date block bootstrap 95%","block_count":len({r['local_date'] for r in rows})})
        hit_rows=[{**r,"hit_value":1.0 if r[k+"_hit"] else 0.0} for r in analysis_rows]
        lo,hi=_block_ci(hit_rows,"hit_value")
        intervals.append({"tp_pct":pct*100,"metric":"tp_hit_rate","ci_low":lo,"ci_high":hi,"method":"local-date block bootstrap 95%","block_count":len({r['local_date'] for r in hit_rows})})
    # Projections are arithmetic illustrations, not expected guaranteed income.
    proj=[]
    for label, subset in (("all_markets",analysis_rows),("weekday_only",[r for r in analysis_rows if r["weekday_weekend"]=="weekday"]),
                          ("weekend_only",[r for r in analysis_rows if r["weekday_weekend"]=="weekend"])):
        group_label={"all_markets":None,"weekday_only":"weekday","weekend_only":"weekend"}[label]
        eligible_slots=sum(1 for m in market_meta.values() if group_label is None or m["weekday_weekend"]==group_label)
        for pct in TPS:
            k=f"tp{pct*100:g}";pnls=[]
            for r in subset:
                px=r[k+"_target_price"] if r[k+"_hit"] else (1.0 if r["side"]==r["winner"] else 0)
                pnls.append(_exit_pnl(r["entry_price"],px,notional,"existing_fee_assumption_1pct_notional")["net_pnl"])
            ev=statistics.mean(pnls) if pnls else None
            entry_rate=len(pnls)/eligible_slots if eligible_slots else None
            ev_per_slot=ev*entry_rate if ev is not None and entry_rate is not None else None
            expected_entries_hour=4*entry_rate if entry_rate is not None else None
            proj.append({"scope":label,"tp_pct":pct*100,"observed_ev_per_trade":ev,"signal_entry_rate_per_sampled_market":entry_rate,
                         "expected_signal_entries_per_hour_illustration":expected_entries_hour,
                         "ev_per_market_slot":ev_per_slot,"ev_per_hour_linear":ev_per_slot*4 if ev_per_slot is not None else None,
                         "ev_per_day_linear":ev_per_slot*96 if ev_per_slot is not None else None,
                         "observed_entry_count":len(pnls),"eligible_sampled_market_slots":eligible_slots,
                         "warning":"linear illustration assumes stratified sample signal rate generalizes and 4 market slots/hour; not guaranteed income"})

    _write_csv(output/"candidate_trades.csv",candidate_rows)
    _write_csv(output/"mfe_mae.csv",candidate_rows)
    _write_csv(output/"tp_hit_rates.csv",tp_rates)
    _write_csv(output/"tp_time_distribution.csv",tp_time)
    _write_csv(output/"tp_sl_matrix.csv",tp_sl)
    _write_csv(output/"tp_timed_exit_matrix.csv",variants)
    _write_csv(output/"weekday_weekend.csv",timing)
    _write_csv(output/"hour_blocks.csv",hour_rows)
    _write_csv(output/"entry_price_buckets.csv",entry_buckets)
    # Strike and local stop-loss data cannot be reliably replayed from these public caches.
    _write_csv(output/"strike_distance_interaction.csv",[{"status":"NOT_AVAILABLE_RELIABLE_STRIKE_DISTANCE_NOT_JOINED","near_far":"not_assessed"}])
    _write_csv(output/"development_holdout.csv",[{"selected_on_development":selected,"matching_holdout":selected_hold,"selection":"highest development mean PnL among variants with >=5 trades; descriptive only"},
                                                   *[r for r in variants if r.get("partition") in {"development","holdout"}]])
    _write_csv(output/"execution_cost_sensitivity.csv",cost_rows)
    _write_csv(output/"ambiguous_intrabar_cases.csv",ambiguous)
    _write_csv(output/"daily_projection.csv",proj)
    _write_csv(output/"confidence_intervals.csv",intervals)
    public_by_slug=_load_all_cached_markets(cache_dirs)
    local_conflicts=_local_stoploss_conflicts(db_path,public_by_slug)
    _write_csv(output/"local_stoploss_tp_conflict.csv",local_conflicts)
    _write_csv(output/"mfe_distribution.csv",excursion)
    _write_csv(output/"data_quality.csv",quality+[{"source":"Binance BTCUSDT 1m","status":d.get("status"),"date":d.get("date"),"rows":d.get("rows")} for d in candle_diag])

    # Complete exit variants for fixed costs, three-minute/other timed stops, and settlement baseline.
    for row in variants:
        pass
    primary=analysis_rows
    def tp_stats(pct: float) -> dict[str, Any]:
        k=f"tp{pct:g}"; group=[r for r in primary]
        hits=[r for r in group if r.get(k+"_hit")]
        return {"n":len(group),"hits":len(hits),"hit_rate":len(hits)/len(group) if group else None,
                "median_time":statistics.median([r[k+"_time_to_hit_sec"] for r in hits]) if hits else None,
                "wrong_but_hit":sum(bool(r[k+"_final_wrong_but_hit"]) for r in hits)}
    lines=["# Short-Term Take-Profit Backtest", "", f"- Public market sample loaded: {len(market_meta)}; signal sensitivity rows: {len(candidate_rows)} across six configs; TP analysis entries: {len(analysis_rows)} (pre-specified 180s/5bps, at most one entry per market).",
           f"- Chronological development dates: {len(dev_dates)}; holdout dates: {len(hold_dates)}. Split is date-ordered (65/35), not randomized.",
           "- Entry execution: 5-second VWAP of public trade prints after 120s/180s BTC open-to-observation simple trend signal; proxy only, no historical ask/BBO.",
           "- TP event: first later SELL-side public trade print on held outcome at/above target; proxy only, not guaranteed executable.",
           "- Excursions use all outcome trade prints as sampled marks; public trade marks are sparse and are not continuous quotes.",
           "- Existing fee assumption is a 1% notional stress proxy, not verified historical fees. Slippage scenarios are adverse exit cents.",
           "- Current live protective-exit logic is NOT FULLY REPLAYABLE from public cache: historical BBO/L2 and full live thesis state are absent.",
           "- This report is offline research only; it does not modify live trading policy.", "", "## Required TP10 answers", ""]
    t10=tp_stats(10)
    t10_stats=tp_stats(10)
    wd10=next((x for x in timing if x["group"]=="weekday" and x["tp_pct"]==10),{})
    we10=next((x for x in timing if x["group"]=="weekend" and x["tp_pct"]==10),{})
    def fmt(value): return "n/a" if value is None else f"{value:.4f}"
    holdout_variants=[r for r in variants if str(r.get("variant","")).startswith("TP")
                      and r.get("partition")=="holdout" and r.get("n",0)>0
                      and r.get("cost_scenario")=="existing_fee_assumption_1pct_notional"]
    best_holdout=max(holdout_variants,key=lambda x:x.get("mean_pnl") if x.get("mean_pnl") is not None else -math.inf,default=None)
    tp10_mfe=next((r for r in excursion if r["tp_pct"]==10),{})
    stop_conflicts=[r for r in tp_sl if r.get("tp_pct")==10 and r.get("ambiguity_policy")=="exclude_ambiguous"]
    conflict_counts={str(int(r["sl_pct"])):r["eventual_tp_hit_but_sl_before_tp_n"] for r in stop_conflicts if r.get("sl_pct") is not None}
    lines += ["- Entry signal sensitivity for each requested 120/180s × 0/2/5bps setting is in signal_sensitivity.csv; these overlapping configurations are not pooled as independent trades.",
              f"- TP10 primary 180s/5bps: N={len(primary)}, hit rate={fmt(t10_stats['hit_rate'])}, median time-to-hit={t10_stats['median_time']}s, final direction wrong but TP hit={t10_stats['wrong_but_hit']}.",
              f"- TP10 weekday: N={wd10.get('entry_n',0)}, hit rate={fmt(wd10.get('hit_rate'))}, EV/market={fmt(wd10.get('mean_pnl'))}; weekend: N={we10.get('entry_n',0)}, hit rate={fmt(we10.get('hit_rate'))}, EV/market={fmt(we10.get('mean_pnl'))} (1% notional fee assumption).",
              f"- TP10 MFE rate (any observed mark >=10%): {fmt(tp10_mfe.get('mfe_ge_rate'))}; MAE-before-TP10 median/P75/P90/worst: {tp10_mfe.get('mae_before_tp_median_pct')}/{tp10_mfe.get('mae_before_tp_p75_pct')}/{tp10_mfe.get('mae_before_tp_p90_pct')}/{tp10_mfe.get('mae_before_tp_worst_pct')}%.",
              f"- TP10 eventual winners stopped first in proxy first-touch grid (counts at -5/-10/-15/-20%): {conflict_counts}.",
              f"- TP10 + settlement-on-miss, 1% fee: {next((r for r in variants if r.get('variant')=='TP10_OR_SETTLEMENT' and r.get('partition')=='all' and r.get('cost_scenario')=='existing_fee_assumption_1pct_notional'),{})}.",
              f"- TP10 + T-2m, 1% fee: {next((r for r in variants if r.get('variant')=='TP10_OR_TMINUS2M' and r.get('partition')=='all'),{})}.",
              f"- Best descriptive TP exit under development selection: {selected.get('variant') if selected else 'none'}; all-sample metrics: {next((r for r in variants if selected and r.get('variant')==selected.get('variant') and r.get('partition')=='all' and r.get('cost_scenario')=='existing_fee_assumption_1pct_notional'),{})}.",
              f"- Development-selected descriptive variant: {selected.get('variant') if selected else 'none'}; its holdout: {selected_hold if selected_hold else 'unavailable'}. Best holdout TP/timed-exit point estimate (not a selection-valid recommendation): {best_holdout}.",
              "- Weekday/weekend differences are in weekday_weekend.csv; note sparse proxy prints and market-level dependence.",
              "- Stop-loss conflict matrix uses SELL-side proxy first-touch; eventual TP hits occurring after SL are counted separately. Same-second events are exported under optimistic/pessimistic/excluded policies.",
              f"- Local stop-loss rows: {sum(r.get('status')=='JOINED_PROXY_PATH' for r in local_conflicts)} joined of {len(local_conflicts)}; qualifying sell-side prints existed for {sum(r.get('status')=='JOINED_PROXY_PATH' and int(r.get('matched_print_count') or 0)>0 for r in local_conflicts)}. Missing print paths are marked unknown, not no-hit; see local_stoploss_tp_conflict.csv. No reliable strike-distance join was available.",
              "- Hypothesis verdict: CANNOT ASSESS conclusively. Descriptive short-term excursions can be measured, but proxy executability, no reliable BBO, selection bias and small samples prevent comparing live suitability to hold-to-redeem.", ""]
    (output/"summary.md").write_text("\n".join(lines),encoding="utf-8")
    return {"entries":candidate_rows,"markets":market_meta,"variants":variants,"output":output}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir",action="append",default=[])
    parser.add_argument("--sample-csv",default="reports/unified_strategy_research/public_refresh/public_market_sample.csv")
    parser.add_argument("--btc-cache",default="data/btc_history")
    parser.add_argument("--output",default="reports/take_profit_backtest")
    parser.add_argument("--start",default="2026-07-27");parser.add_argument("--end",default="2026-09-20")
    parser.add_argument("--timezone",default="America/New_York");parser.add_argument("--offline",action="store_true")
    parser.add_argument("--db",default="logs/trade_journal.db",help="Optional local trade journal, opened read-only for stop-loss/TP path joins")
    args=parser.parse_args()
    dirs=[Path(x) for x in args.cache_dir] or [Path("data/polymarket_history_unified_20260727_20260920"),Path("data/polymarket_history_public_study_20260727_20260920")]
    result=run(cache_dirs=dirs,sample_csv=Path(args.sample_csv),btc_cache=Path(args.btc_cache),output=Path(args.output),
               start_date=date.fromisoformat(args.start),end_date=date.fromisoformat(args.end),timezone_name=args.timezone,offline=args.offline,
               db_path=Path(args.db) if args.db else None)
    print(f"markets={len(result['markets'])} entries={len(result['entries'])} output={result['output']}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())

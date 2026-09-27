#!/usr/bin/env python3
"""Build prospective early-entry shadow summaries from the research journal."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUTS = (
    "candidate_entries.csv", "config_overlap.csv", "paired_entry_comparison.csv", "bbo_path.csv",
    "mfe_mae.csv", "recovery_after_drawdown.csv", "shadow_exit_results.csv", "hold_results.csv",
    "tp20_results.csv", "trail5_results.csv", "trail10_results.csv", "weekday_summary.csv",
    "weekend_shadow_summary.csv", "signal_reversal_analysis.csv", "strike_cross_analysis.csv",
    "fair_deterioration.csv", "live_vs_shadow.csv", "execution_quality.csv", "data_quality.csv",
    "shadow_events_per_hour.csv",
)
CONFIG_NAMES = ("120_0", "120_2", "120_5")


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error:
        return []
    with conn:
        try:
            rows = conn.execute("SELECT slug, decision_epoch_ns, payload_json FROM lead_lag_decisions ORDER BY decision_epoch_ns")
        except sqlite3.Error:
            return []
        result = []
        for slug, ts, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            payload.setdefault("slug", slug)
            payload.setdefault("event_ts", int(ts) / 1_000_000_000)
            result.append(payload)
        return result


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        fields = ["status"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})


def _summarize(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in positions:
        groups[(str(row.get("entry_config")), str(row.get("entry_variant")), str(row.get("exit_policy")))].append(row)
    out = []
    for (config, variant, policy), rows in sorted(groups.items()):
        pnls = [float(r["pnl_usdc"]) for r in rows if isinstance(r.get("pnl_usdc"), (int, float))]
        depth_pnls = [float(r["depth_weighted_pnl_usdc"]) for r in rows if isinstance(r.get("depth_weighted_pnl_usdc"), (int, float))]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        out.append({"entry_config": config, "entry_variant": variant, "exit_policy": policy,
                    "n": len(rows), "settled_n": len(pnls), "profitability_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
                    "ev_usdc_per_trade": sum(pnls) / len(pnls) if pnls else None,
                    "roi_on_5usd": sum(pnls) / (5 * len(pnls)) if pnls else None,
                    "depth_weighted_settled_n": len(depth_pnls),
                    "depth_weighted_ev_usdc_per_trade": sum(depth_pnls) / len(depth_pnls) if depth_pnls else None,
                    "depth_weighted_roi_on_5usd": sum(depth_pnls) / (5 * len(depth_pnls)) if depth_pnls else None,
                    "mean_entry_price": sum(float(r.get("entry_price")) for r in rows if _is_number(r.get("entry_price"))) / sum(_is_number(r.get("entry_price")) for r in rows) if any(_is_number(r.get("entry_price")) for r in rows) else None,
                    "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
                    "average_win": sum(wins) / len(wins) if wins else None,
                    "average_loss": sum(losses) / len(losses) if losses else None,
                    "worst_trade": min(pnls) if pnls else None,
                    "max_drawdown_usdc": _max_drawdown(rows),
                    "mean_mfe_bid_pct": sum(float(r.get("mfe_bid_pct", 0) or 0) for r in rows) / len(rows),
                    "mean_mae_bid_pct": sum(float(r.get("mae_bid_pct", 0) or 0) for r in rows) / len(rows),
                    "fillable_n": sum(r.get("entry_variant") == "ENTRY_TOP_ASK" or r.get("entry_variant") == "ENTRY_DEPTH_WEIGHTED_5USD" for r in rows)})
    return out


def build_report(db: Path, output: Path, trade_db: Path | None = None) -> dict[str, Any]:
    events = _read(db)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_type[str(event.get("event_type", "unknown"))].append(event)
    candidates = by_type["SHADOW_ENTRY_CANDIDATE"]
    marks = by_type["SHADOW_POSITION_MARK"]
    bbo_path = by_type["SHADOW_BBO_SNAPSHOT"] + by_type["SHADOW_BBO_MATERIAL_CHANGE"]
    exits = by_type["SHADOW_EXIT"]
    settlements = by_type["SHADOW_SETTLEMENT"]
    candidates_by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        candidates_by_slug[str(row.get("slug"))].append(row)
    overlap = []
    for slug, rows in candidates_by_slug.items():
        fired = sorted(str(r.get("entry_config")) for r in rows if r.get("candidate_side"))
        overlap.append({"slug": slug, "fired_configs": "+".join(fired) if fired else "none", "config_count": len(fired)})
    positions = list(settlements)
    summary = _summarize(positions)
    output.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[dict[str, Any]]] = {
        "candidate_entries.csv": candidates,
        "config_overlap.csv": overlap,
        "paired_entry_comparison.csv": _paired(candidates, positions),
        "bbo_path.csv": bbo_path,
        "mfe_mae.csv": [{k: r.get(k) for k in ("candidate_id", "position_id", "entry_config", "entry_variant", "exit_policy", "mfe_bid_pct", "mae_bid_pct", "time_to_mfe_sec", "time_to_mae_sec")} for r in positions if "mfe_bid_pct" in r],
        "recovery_after_drawdown.csv": _recovery_summary(positions),
        "shadow_exit_results.csv": exits,
        "hold_results.csv": [r for r in summary if r["exit_policy"] == "HOLD"],
        "tp20_results.csv": [r for r in summary if r["exit_policy"] == "TP20"],
        "trail5_results.csv": [r for r in summary if r["exit_policy"] == "TRAIL5"],
        "trail10_results.csv": [r for r in summary if r["exit_policy"] == "TRAIL10"],
        "weekday_summary.csv": _group_summary(positions, weekend=False),
        "weekend_shadow_summary.csv": _group_summary(positions, weekend=True),
        "signal_reversal_analysis.csv": by_type["SHADOW_SIGNAL_REVERSAL"],
        "strike_cross_analysis.csv": [r for r in marks if r.get("strike_crossed_against")],
        "fair_deterioration.csv": [r for r in marks if r.get("fair_change_from_entry") is not None],
        "live_vs_shadow.csv": _live_join(candidates, trade_db),
        "execution_quality.csv": [{k: r.get(k) for k in ("candidate_id", "entry_config", "entry_top_ask", "entry_top_ask_fillable_notional", "research_shares", "top_level_fillable", "depth_weighted_entry_price", "depth_weighted_entry_status")} for r in candidates],
        "data_quality.csv": [{"event_type": kind, "events": len(rows)} for kind, rows in sorted(by_type.items())],
        "shadow_events_per_hour.csv": by_type["FORWARD_SHADOW_CAPTURE_HEALTH"],
    }
    for name in OUTPUTS:
        _write(output / name, tables.get(name, []))
    settled_markets = {str(r.get("slug")) for r in settlements if r.get("settlement_status") == "observed"}
    weekday_candidates = [r for r in candidates if r.get("experiment_class") == "WEEKDAY_PRIMARY" and r.get("candidate_side")]
    counts = Counter(str(r.get("entry_config")) for r in weekday_candidates)
    lines = ["# Forward Shadow Experiment", "", f"Research DB: `{db}`", "", "This report is observational only; it grants no live order authority.", "",
             f"- Markets observed: {len({str(r.get('slug')) for r in candidates})}",
             f"- Weekday candidate entries: {len(weekday_candidates)}",
             f"- Weekday settled markets: {len(settled_markets)}",
             f"- Full BBO snapshots: {len(by_type['SHADOW_BBO_SNAPSHOT'])}",
             f"- Material BBO changes: {len(by_type['SHADOW_BBO_MATERIAL_CHANGE'])}",
             f"- Position marks: {len(marks)}", f"- Exit events: {len(exits)}"]
    for config in CONFIG_NAMES:
        lines.append(f"- {config}: {counts[config]}")
    lines += ["", "## Exit / entry variant summaries", "", "| Config | Entry | Exit | N | Top settled | Top EV | Depth settled | Depth EV | Win rate |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in summary:
        lines.append(f"| {row['entry_config']} | {row['entry_variant']} | {row['exit_policy']} | {row['n']} | {row['settled_n']} | {_fmt(row['ev_usdc_per_trade'])} | {row['depth_weighted_settled_n']} | {_fmt(row['depth_weighted_ev_usdc_per_trade'])} | {_fmt(row['profitability_rate'])} |")
    targets_met = counts["120_0"] >= 100 and counts["120_2"] >= 75 and counts["120_5"] >= 50
    readiness = "SAMPLE TARGETS MET — REVIEW REQUIRED, NO AUTOMATIC LIVE POLICY CHANGE" if targets_met else "INSUFFICIENT FOR POLICY DECISION until weekday independent candidate targets are met: 120/0 ≥100, 120/2 ≥75, 120/5 ≥50."
    lines += ["", "## Decision readiness", "", readiness, ""]
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    paired_rows = _paired(candidates, positions)
    return {"markets": len({str(r.get("slug")) for r in candidates}), "weekday_candidates": len(weekday_candidates),
            "configs": dict(counts), "paired_markets": len({str(r.get("slug")) for r in paired_rows}),
            "bbo_marks": len(by_type["SHADOW_BBO_SNAPSHOT"]), "settled": len(settled_markets), "output": str(output)}


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{float(value):.4f}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _recovery_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for config in CONFIG_NAMES:
        for weekend in (False, True):
            scoped = [row for row in rows if row.get("entry_config") == config and bool(row.get("is_weekend")) is weekend]
            for threshold in (5, 10, 15, 20):
                key = f"ever_mae_le_minus_{threshold}"
                hit = [row for row in scoped if row.get(key)]
                result.append({"entry_config": config, "cohort": "weekend_shadow" if weekend else "weekday",
                    "mae_threshold_pct": threshold, "n": len(hit),
                    "p_recover_plus_5": _rate(hit, "recovered_plus_5"),
                    "p_recover_plus_10": _rate(hit, "recovered_plus_10"),
                    "p_recover_plus_20": _rate(hit, "recovered_plus_20"),
                    "p_profitable_settlement": _rate(hit, "profitable_settlement")})
    return result


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    observed = [row[key] for row in rows if isinstance(row.get(key), bool)]
    return sum(observed) / len(observed) if observed else None


def _max_drawdown(rows: list[dict[str, Any]]) -> float | None:
    ordered = sorted(rows, key=lambda row: float(row.get("event_ts", 0) or 0))
    equity = peak = drawdown = 0.0
    found = False
    for row in ordered:
        pnl = row.get("pnl_usdc")
        if not isinstance(pnl, (int, float)):
            continue
        found = True
        equity += float(pnl)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown if found else None


def _paired(candidates: list[dict[str, Any]], positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lookup = {(str(r.get("candidate_id")), str(r.get("entry_variant")), str(r.get("exit_policy"))): r for r in positions}
    by_slug: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in candidates:
        if row.get("candidate_side"):
            by_slug[str(row.get("slug"))][str(row.get("entry_config"))] = row
    rows = []
    for slug, configs in by_slug.items():
        for a, b in (("120_0", "120_2"), ("120_0", "120_5"), ("120_2", "120_5")):
            if a not in configs or b not in configs:
                continue
            for variant in ("ENTRY_TOP_ASK", "ENTRY_DEPTH_WEIGHTED_5USD"):
                for policy in ("HOLD", "TP20", "TRAIL5", "TRAIL10"):
                    pos_a = lookup.get((str(configs[a].get("candidate_id")), variant, policy))
                    pos_b = lookup.get((str(configs[b].get("candidate_id")), variant, policy))
                    rows.append({"slug": slug, "config_a": a, "config_b": b,
                                 "entry_variant": variant, "exit_policy": policy,
                                 "side_a": configs[a].get("candidate_side"), "side_b": configs[b].get("candidate_side"),
                                 "entry_ask_a": configs[a].get("entry_top_ask"), "entry_ask_b": configs[b].get("entry_top_ask"),
                                 "pnl_a": pos_a.get("pnl_usdc") if pos_a else None,
                                 "pnl_b": pos_b.get("pnl_usdc") if pos_b else None,
                                 "paired_pnl_delta_b_minus_a": pos_b.get("pnl_usdc") - pos_a.get("pnl_usdc") if pos_a and pos_b and isinstance(pos_a.get("pnl_usdc"), (int, float)) and isinstance(pos_b.get("pnl_usdc"), (int, float)) else None,
                                 "mfe_bid_pct_a": pos_a.get("mfe_bid_pct") if pos_a else None,
                                 "mfe_bid_pct_b": pos_b.get("mfe_bid_pct") if pos_b else None,
                                 "mae_bid_pct_a": pos_a.get("mae_bid_pct") if pos_a else None,
                                 "mae_bid_pct_b": pos_b.get("mae_bid_pct") if pos_b else None,
                                 "exit_reason_a": pos_a.get("exit_reason") if pos_a else None,
                                 "exit_reason_b": pos_b.get("exit_reason") if pos_b else None,
                                 "exit_price_a": pos_a.get("exit_price") if pos_a else None,
                                 "exit_price_b": pos_b.get("exit_price") if pos_b else None,
                                 "same_direction": configs[a].get("candidate_side") == configs[b].get("candidate_side")})
    return rows


def _live_join(candidates: list[dict[str, Any]], trade_db: Path | None) -> list[dict[str, Any]]:
    live: list[dict[str, Any]] = []
    settlements: dict[str, dict[str, Any]] = {}
    if trade_db is not None and trade_db.exists():
        try:
            with sqlite3.connect(trade_db) as conn:
                rows = conn.execute("SELECT ts, event_type, side, price, qty, status, reason, instrument_id, payload_json FROM order_events ORDER BY ts")
                for values in rows:
                    try:
                        payload = json.loads(values[8] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    live.append({"ts": values[0], "event_type": values[1], "side": str(values[2] or "").upper(),
                                 "price": values[3], "qty": values[4], "status": values[5], "reason": values[6],
                                 "instrument_id": str(values[7] or payload.get("instrument_id") or ""),
                                 "slug": str(payload.get("slug") or payload.get("market_slug") or ""),
                                 "payload": payload})
                try:
                    strategy_rows = conn.execute("SELECT event_type, payload_json FROM strategy_events WHERE event_type IN ('MARKET_SETTLEMENT', 'MARKET_CYCLE_PNL')")
                except sqlite3.Error:
                    strategy_rows = ()
                for event_type, raw_payload in strategy_rows:
                    try:
                        payload = json.loads(raw_payload or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    slug = str(payload.get("slug") or "")
                    if slug:
                        target = settlements.setdefault(slug, {})
                        if event_type == "MARKET_SETTLEMENT":
                            target.update({"live_settlement_event": event_type,
                                "live_settlement_outcome": payload.get("outcome"),
                                "live_settlement_pnl_usdc": payload.get("settlement_pnl_usdc")})
                        else:
                            target["live_cycle_combined_pnl_usdc"] = payload.get("cycle_combined_pnl_usdc")
        except sqlite3.Error:
            live = []
    result = []
    for candidate in candidates:
        instrument = str(candidate.get("instrument_id") or "")
        related = [row for row in live if row["instrument_id"] == instrument and (not row["slug"] or row["slug"] == candidate.get("slug"))]
        buys = [row for row in related if row["side"] == "BUY"]
        sells = [row for row in related if row["side"] == "SELL"]
        fills = [row for row in related if "FILL" in str(row["event_type"]).upper() or str(row["status"]).upper() == "FILLED"]
        buy_fills = [row for row in fills if row["side"] == "BUY"]
        sell_fills = [row for row in fills if row["side"] == "SELL"]
        buy_qty = sum(float(row["qty"] or 0) for row in buy_fills)
        sell_qty = sum(float(row["qty"] or 0) for row in sell_fills)
        buy_cost = sum(float(row["qty"] or 0) * float(row["price"] or 0) for row in buy_fills)
        sell_value = sum(float(row["qty"] or 0) * float(row["price"] or 0) for row in sell_fills)
        settlement = settlements.get(str(candidate.get("slug")), {})
        result.append({"candidate_id": candidate.get("candidate_id"), "slug": candidate.get("slug"),
                       "entry_config": candidate.get("entry_config"), "shadow_side": candidate.get("candidate_side"),
                       "live_comparator_available": bool(related), "live_candidate_existed": bool(buys),
                       "live_side": candidate.get("token_side") if buys else None,
                       "live_order_side": buys[-1]["side"] if buys else None,
                       "live_entry_price": buys[0]["price"] if buys else None,
                       "live_fill_price": buy_cost / buy_qty if buy_qty else None,
                       "live_fill_qty": buy_qty if buy_qty else None,
                       "live_exit_fill_price": sell_value / sell_qty if sell_qty else None,
                       "live_exit_reason": sells[-1]["reason"] if sells else None,
                       "live_exit_price": sells[-1]["price"] if sells else None,
                       **settlement,
                       "order_event_count": len(related),
                       "join_basis": "instrument_id plus slug when journal payload includes slug"})
    return result


def _group_summary(positions: list[dict[str, Any]], *, weekend: bool) -> list[dict[str, Any]]:
    group = [r for r in positions if bool(r.get("is_weekend")) is weekend]
    return _summarize(group)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize forward shadow experiment telemetry")
    parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")
    parser.add_argument("--output", default="reports/forward_shadow")
    parser.add_argument("--status", action="store_true", help="print compact collection status")
    parser.add_argument("--trade-db", default="data/trading/trade_journal.db")
    args = parser.parse_args()
    result = build_report(Path(args.db), Path(args.output), Path(args.trade_db))
    if args.status:
        print(f"markets={result['markets']} weekday_candidates={result['weekday_candidates']} "
              f"120/0={result['configs'].get('120_0', 0)} 120/2={result['configs'].get('120_2', 0)} "
              f"120/5={result['configs'].get('120_5', 0)} paired_markets={result['paired_markets']} "
              f"bbo_marks={result['bbo_marks']} settled={result['settled']}")
    else:
        print(f"Forward shadow report written to {result['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

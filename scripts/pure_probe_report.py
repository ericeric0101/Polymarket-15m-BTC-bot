#!/usr/bin/env python3
"""
Report for pure_signal_probe candidate quality.

Joins:
- logs/pure_probe.db PURE_SIGNAL_CANDIDATE rows
- logs/trade_journal.db MARKET_SETTLEMENT rows

Evaluates a simple hold-to-settlement outcome under three execution choices:
- first candidate per market
- best-edge candidate per market
- last candidate per market
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _cutoff(hours: int) -> Optional[datetime]:
    if hours <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _fmt(value: Optional[float], digits: int = 6) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


@dataclass
class Candidate:
    run_id: str
    ts: datetime
    slug: str
    side: str
    edge: float
    time_left_sec: float
    ask_up: Optional[float]
    ask_down: Optional[float]
    fair_up: float
    fair_down: float
    strike: Optional[float]
    spot: Optional[float]

    @property
    def entry_price(self) -> Optional[float]:
        if self.side == "BUY_UP":
            return self.ask_up
        if self.side == "BUY_DOWN":
            return self.ask_down
        return None


@dataclass
class Settlement:
    slug: str
    ts: datetime
    outcome: str
    spot: Optional[float]
    strike: Optional[float]


@dataclass
class EvalRow:
    slug: str
    ts: datetime
    side: str
    edge: float
    entry_price: float
    outcome: str
    pnl: float
    time_left_sec: float


def _pick_one_per_market(candidates: List[Candidate], mode: str) -> List[Candidate]:
    if mode == "all":
        return candidates
    by_slug: Dict[str, List[Candidate]] = defaultdict(list)
    for c in candidates:
        by_slug[c.slug].append(c)
    picked: List[Candidate] = []
    for rows in by_slug.values():
        rows = sorted(rows, key=lambda x: x.ts)
        if mode == "first":
            picked.append(rows[0])
        elif mode == "best":
            picked.append(max(rows, key=lambda x: x.edge))
        elif mode == "last":
            picked.append(rows[-1])
        else:
            raise ValueError(f"Unknown selection mode: {mode}")
    return picked


def _load_candidates(
    db_path: Path,
    cutoff: Optional[datetime],
    run_id: Optional[str],
) -> List[Candidate]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    where = ["event_type='PURE_SIGNAL_CANDIDATE'"]
    params: List[object] = []
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    rows = conn.execute(
        f"""
        SELECT run_id, ts, payload_json
        FROM strategy_events
        WHERE {' AND '.join(where)}
        ORDER BY ts
        """,
        params,
    ).fetchall()
    conn.close()

    out: List[Candidate] = []
    for row in rows:
        ts = _parse_ts(str(row["ts"]))
        if cutoff and ts < cutoff:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        out.append(
            Candidate(
                run_id=str(row["run_id"] or ""),
                ts=ts,
                slug=str(payload.get("slug") or ""),
                side=str(payload.get("candidate_side") or ""),
                edge=_safe_float(payload.get("candidate_edge")),
                time_left_sec=_safe_float(payload.get("time_left_sec")),
                ask_up=_safe_float(payload.get("ask_up"), None),
                ask_down=_safe_float(payload.get("ask_down"), None),
                fair_up=_safe_float(payload.get("fair_up")),
                fair_down=_safe_float(payload.get("fair_down")),
                strike=_safe_float(payload.get("strike"), None),
                spot=_safe_float(payload.get("spot"), None),
            )
        )
    return [c for c in out if c.slug and c.side in {"BUY_UP", "BUY_DOWN"}]


def _load_candidate_snapshots(
    db_path: Path,
    cutoff: Optional[datetime],
    run_id: Optional[str],
) -> List[Candidate]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    where = ["event_type='PURE_SIGNAL_SNAPSHOT'"]
    params: List[object] = []
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    rows = conn.execute(
        f"""
        SELECT run_id, ts, payload_json
        FROM strategy_events
        WHERE {' AND '.join(where)}
        ORDER BY ts
        """,
        params,
    ).fetchall()
    conn.close()

    out: List[Candidate] = []
    for row in rows:
        ts = _parse_ts(str(row["ts"]))
        if cutoff and ts < cutoff:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        side = str(payload.get("candidate_side") or "")
        if side not in {"BUY_UP", "BUY_DOWN"}:
            continue
        out.append(
            Candidate(
                run_id=str(row["run_id"] or ""),
                ts=ts,
                slug=str(payload.get("slug") or ""),
                side=side,
                edge=_safe_float(payload.get("candidate_edge")),
                time_left_sec=_safe_float(payload.get("time_left_sec")),
                ask_up=_safe_float(payload.get("ask_up"), None),
                ask_down=_safe_float(payload.get("ask_down"), None),
                fair_up=_safe_float(payload.get("fair_up")),
                fair_down=_safe_float(payload.get("fair_down")),
                strike=_safe_float(payload.get("strike"), None),
                spot=_safe_float(payload.get("spot"), None),
            )
        )
    return [c for c in out if c.slug]


def _load_settlements(db_path: Path, cutoff: Optional[datetime]) -> Dict[str, Settlement]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ts, payload_json
        FROM strategy_events
        WHERE event_type='MARKET_SETTLEMENT'
        ORDER BY ts
        """
    ).fetchall()
    conn.close()

    out: Dict[str, Settlement] = {}
    for row in rows:
        ts = _parse_ts(str(row["ts"]))
        if cutoff and ts < cutoff:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or "")
        if not slug:
            continue
        out[slug] = Settlement(
            slug=slug,
            ts=ts,
            outcome=str(payload.get("outcome") or ""),
            spot=_safe_float(payload.get("spot"), None),
            strike=_safe_float(payload.get("strike"), None),
        )
    return out


def _evaluate(candidates: Iterable[Candidate], settlements: Dict[str, Settlement]) -> List[EvalRow]:
    out: List[EvalRow] = []
    for c in candidates:
        settle = settlements.get(c.slug)
        entry = c.entry_price
        if settle is None or entry is None:
            continue
        won = (c.side == "BUY_UP" and settle.outcome == "UP") or (
            c.side == "BUY_DOWN" and settle.outcome == "DOWN"
        )
        pnl = (1.0 - entry) if won else -entry
        out.append(
            EvalRow(
                slug=c.slug,
                ts=c.ts,
                side=c.side,
                edge=c.edge,
                entry_price=entry,
                outcome=settle.outcome,
                pnl=pnl,
                time_left_sec=c.time_left_sec,
            )
        )
    return out


def _persistent_candidates(
    snapshots: List[Candidate],
    persistence_sec: float,
    max_gap_sec: float,
) -> List[Candidate]:
    if persistence_sec <= 0:
        return []

    by_slug: Dict[str, List[Candidate]] = defaultdict(list)
    for c in snapshots:
        by_slug[c.slug].append(c)

    result: List[Candidate] = []
    for rows in by_slug.values():
        rows = sorted(rows, key=lambda x: x.ts)
        streak: List[Candidate] = []
        for row in rows:
            if not streak:
                streak = [row]
                continue
            gap = (row.ts - streak[-1].ts).total_seconds()
            if row.side == streak[-1].side and gap <= max_gap_sec:
                streak.append(row)
                continue
            matured = _mature_streak(streak, persistence_sec)
            if matured is not None:
                result.append(matured)
            streak = [row]
        matured = _mature_streak(streak, persistence_sec)
        if matured is not None:
            result.append(matured)
    return result


def _mature_streak(streak: List[Candidate], persistence_sec: float) -> Optional[Candidate]:
    if not streak:
        return None
    start_ts = streak[0].ts
    for row in streak:
        if (row.ts - start_ts).total_seconds() >= persistence_sec:
            return row
    return None


def _summarize(name: str, rows: List[EvalRow]) -> None:
    if not rows:
        print(f"{name}: no settled rows")
        return
    wins = sum(1 for r in rows if r.pnl > 0)
    losses = sum(1 for r in rows if r.pnl <= 0)
    total = sum(r.pnl for r in rows)
    avg_edge = sum(r.edge for r in rows) / len(rows)
    avg_entry = sum(r.entry_price for r in rows) / len(rows)
    avg_t = sum(r.time_left_sec for r in rows) / len(rows)
    print(
        f"{name}: count={len(rows)} win_rate={wins/len(rows):.2%} "
        f"total_pnl={total:.6f} avg_pnl={total/len(rows):.6f} "
        f"avg_edge={avg_edge:.6f} avg_entry={avg_entry:.4f} avg_t_left={avg_t:.1f}s"
    )
    print(f"  wins={wins} losses={losses}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Report pure probe candidate quality against settlement outcomes")
    ap.add_argument("--probe-db", default="./logs/pure_probe.db", help="SQLite DB path for pure probe")
    ap.add_argument("--trade-db", default="./logs/trade_journal.db", help="SQLite DB path for main bot journal")
    ap.add_argument("--hours", type=int, default=24, help="Lookback hours; <=0 means all rows")
    ap.add_argument("--run-id", default=None, help="Optional pure probe run_id filter")
    ap.add_argument(
        "--selection",
        choices=["all", "first", "best", "last"],
        default="all",
        help="Optional one-trade-per-market selection for the focused summary",
    )
    ap.add_argument(
        "--persistence-sec",
        type=float,
        default=0.0,
        help="Require a candidate side to persist this many seconds in snapshots before counting it",
    )
    ap.add_argument(
        "--segment-gap-sec",
        type=float,
        default=10.0,
        help="Maximum snapshot gap to treat as the same candidate streak",
    )
    ap.add_argument("--show", type=int, default=10, help="How many per-row samples to print")
    args = ap.parse_args()

    probe_db = Path(args.probe_db)
    trade_db = Path(args.trade_db)
    if not probe_db.exists():
        print(f"Probe DB not found: {probe_db}")
        return 1
    if not trade_db.exists():
        print(f"Trade DB not found: {trade_db}")
        return 1

    cutoff = _cutoff(args.hours)
    candidates = _load_candidates(probe_db, cutoff, args.run_id)
    candidate_snapshots = _load_candidate_snapshots(probe_db, cutoff, args.run_id)
    settlements = _load_settlements(trade_db, cutoff)

    by_slug: Dict[str, List[Candidate]] = defaultdict(list)
    for c in candidates:
        by_slug[c.slug].append(c)

    settled_market_count = sum(1 for slug in by_slug if slug in settlements)
    unresolved_market_count = len(by_slug) - settled_market_count

    print("=" * 104)
    print("Pure Probe Report")
    print("=" * 104)
    print(f"probe_db: {probe_db}")
    print(f"trade_db: {trade_db}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else 'ALL'}")
    print(f"run_id: {args.run_id or 'ALL'}")
    print(f"selection: {args.selection}")
    print(f"persistence_sec: {args.persistence_sec:.1f}")
    if cutoff:
        print(f"cutoff_utc: {cutoff.isoformat()}")
    print()
    print(f"candidate_rows: {len(candidates)}")
    print(f"candidate_markets: {len(by_slug)}")
    print(f"settled_candidate_markets: {settled_market_count}")
    print(f"unsettled_candidate_markets: {unresolved_market_count}")

    settled_candidates = _evaluate(candidates, settlements)
    print(f"settled_candidate_rows: {len(settled_candidates)}")
    print()

    first_rows = _evaluate([sorted(rows, key=lambda x: x.ts)[0] for rows in by_slug.values()], settlements)
    best_edge_rows = _evaluate([max(rows, key=lambda x: x.edge) for rows in by_slug.values()], settlements)
    last_rows = _evaluate([sorted(rows, key=lambda x: x.ts)[-1] for rows in by_slug.values()], settlements)

    _summarize("all_candidates", settled_candidates)
    _summarize("first_per_market", first_rows)
    _summarize("best_edge_per_market", best_edge_rows)
    _summarize("last_per_market", last_rows)

    focused_candidates = _pick_one_per_market(candidates, args.selection)
    focused_rows = _evaluate(focused_candidates, settlements)
    print()
    _summarize(f"focused_{args.selection}", focused_rows)

    if args.persistence_sec > 0:
        persistent_candidates = _persistent_candidates(
            snapshots=candidate_snapshots,
            persistence_sec=float(args.persistence_sec),
            max_gap_sec=float(args.segment_gap_sec),
        )
        persistent_rows = _evaluate(persistent_candidates, settlements)
        persistent_focused = _evaluate(_pick_one_per_market(persistent_candidates, args.selection), settlements)
        print()
        print(f"persistent_candidate_rows: {len(persistent_candidates)}")
        print(f"persistent_candidate_markets: {len({c.slug for c in persistent_candidates})}")
        _summarize("persistent_all", persistent_rows)
        _summarize(f"persistent_{args.selection}", persistent_focused)

    if settled_candidates:
        print()
        print("[Recent settled candidate rows]")
        for row in sorted(settled_candidates, key=lambda x: x.ts)[-args.show :]:
            print(
                f"{row.ts.isoformat()} {row.slug} {row.side} "
                f"entry={_fmt(row.entry_price,4)} outcome={row.outcome} "
                f"pnl={_fmt(row.pnl,4)} edge={_fmt(row.edge,4)} "
                f"t_left={_fmt(row.time_left_sec,1)}s"
            )

    if unresolved_market_count:
        print()
        print("[Unsettled candidate markets]")
        for slug in sorted(slug for slug in by_slug if slug not in settlements)[: args.show]:
            rows = by_slug[slug]
            print(
                f"{slug} candidate_rows={len(rows)} "
                f"first_ts={min(r.ts for r in rows).isoformat()} "
                f"last_ts={max(r.ts for r in rows).isoformat()}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

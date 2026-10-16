#!/usr/bin/env python3
"""Settlement-evidence reconciliation and historical backfill (append-only).

  fetch     network, read-only: official outcomes, venue TRADE and REDEEM activity for
            every market the journal traded -> evidence cache JSON (no journal writes)
  report    offline: per-market reconciliation (journal raw cycle PnL, journal-fill
            rebuild, venue cash, official payout, redeem cash, effective PnL) -> CSV/JSON/MD
  apply     write the cached evidence rows the journal does not have yet. Requires
            --confirm, a stopped bot (journal writer + LIVE locks), takes a backup first,
            one transaction, idempotent by evidence key; rows carry run_id=<batch id>
  rollback  delete exactly the evidence rows of one batch id (same guards + backup)

Raw journal rows (fills, MARKET_SETTLEMENT, MARKET_CYCLE_PNL) are never modified;
effective PnL is projected by monitoring.pnl_attribution.load_effective_market_pnl.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.journal_path import MaintenanceLockError, require_bot_stopped, resolve_trade_db_path  # noqa: E402
from bot.settlement_evidence import (  # noqa: E402
    EVIDENCE_EVENTS,
    evidence_event_type,
    fetch_activity,
    fetch_gamma_outcome,
    redeem_evidence_from_activity,
    trade_evidence_from_activity,
)
from bot.settlement_confirmation import resolve_wallet_address  # noqa: E402
from monitoring.pnl_attribution import (  # noqa: E402
    FINAL_BASES,
    PNL_BASIS_ORDER,
    load_effective_market_pnl,
    summarize_effective_pnl,
)

BATCH_PREFIX = "pnl_evidence_backfill_"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""


def journal_slugs(journal: Path) -> List[str]:
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        slugs = {s for (s,) in conn.execute(
            "SELECT DISTINCT json_extract(payload_json,'$.slug') FROM order_events WHERE event_type='ORDER_FILLED'")}
        slugs |= {s for (s,) in conn.execute(
            """SELECT DISTINCT json_extract(payload_json,'$.slug') FROM strategy_events
               WHERE event_type='MARKET_SETTLEMENT' AND CAST(json_extract(payload_json,'$.inventory_shares') AS REAL) > 0""")}
    finally:
        conn.close()
    return sorted(s for s in slugs if s and str(s).startswith("btc-updown-15m-"))


def load_cache(path: Path) -> Dict[str, Any]:
    cache = json.loads(path.read_text())
    cache.setdefault("evidence", [])
    return cache


# --- fetch ------------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> int:
    import httpx
    from bot.runtime_env import load_runtime_env
    journal = resolve_trade_db_path(args.journal)
    env: Dict[str, str] = {}
    import os
    env.update(os.environ)
    load_runtime_env(environ=env)
    user = resolve_wallet_address(env)
    slugs = journal_slugs(journal)
    evidence: List[Dict[str, Any]] = []
    errors: List[str] = []
    with httpx.Client(timeout=20) as client:
        for slug in slugs:
            outcome = fetch_gamma_outcome(client, slug)
            if outcome.get("error"):
                errors.append(f"{slug}: {outcome['error']}")
            elif outcome.get("resolved"):
                evidence.append(outcome)
            time.sleep(args.sleep_sec)
        activity = {}
        if user:
            for kind in ("TRADE", "REDEEM"):
                activity[kind] = fetch_activity(client, user, kind, max_pages=args.max_pages)
        else:
            errors.append("no wallet address configured; venue cash not fetched")
    token_slugs = {t: o["slug"] for o in evidence for t in (o.get("token_outcomes") or {})}
    wanted = set(slugs)
    trades = [t for t in trade_evidence_from_activity(activity.get("TRADE", {}).get("rows", []))
              if token_slugs.get(t["token_id"]) in wanted or t["slug"] in wanted]
    redeems = [r for r in redeem_evidence_from_activity(activity.get("REDEEM", {}).get("rows", []))
               if token_slugs.get(r["token_id"]) in wanted or r["slug"] in wanted]
    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "journal_slug_count": len(slugs),
        "outcome_resolved_count": len(evidence),
        "activity": {k: {f: v.get(f) for f in ("complete", "error", "pages")} for k, v in activity.items()},
        "errors": errors,
        "evidence": evidence + trades + redeems,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cache, indent=1))
    print(f"wrote {out}: slugs={len(slugs)} outcomes={len(evidence)} trades={len(trades)} "
          f"redeems={len(redeems)} errors={len(errors)} activity={cache['activity']}")
    return 0 if not errors else 2


# --- report -----------------------------------------------------------------------------

def _run_versions(journal: Path) -> Dict[str, str]:
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            """SELECT r.run_id, r.test_mode,
                 (SELECT json_extract(e.payload_json,'$.runtime_git_revision') FROM strategy_events e
                  WHERE e.run_id=r.run_id AND e.event_type='COLLECTION_LIFECYCLE'
                    AND json_extract(e.payload_json,'$.runtime_git_revision') IS NOT NULL LIMIT 1)
               FROM strategy_runs r"""
        ).fetchall()
        fills_by_run = defaultdict(set)
        for run_id, slug in conn.execute(
            "SELECT run_id, json_extract(payload_json,'$.slug') FROM order_events WHERE event_type='ORDER_FILLED'"):
            fills_by_run[run_id].add(slug)
    finally:
        conn.close()
    out = {}
    for run_id, test_mode, rev in rows:
        for slug in fills_by_run.get(run_id, ()):
            out.setdefault(slug, f"{'LIVE' if test_mode == 0 else 'DRY'}:{rev or 'unrecorded'}")
    return out


def contains_commit(revision: str, fix_commit: str) -> str:
    """'yes' / 'no' / 'unverifiable' for a loaded runtime revision (git ancestry)."""
    import subprocess
    rev = str(revision or "").replace("-dirty", "")
    if not rev or rev == "unrecorded":
        return "unverifiable"
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", fix_commit, rev],
                            capture_output=True, timeout=30)
    if result.returncode == 0:
        return "yes" if "-dirty" not in str(revision) else "yes_base_dirty"
    return "no" if result.returncode == 1 else "unverifiable"


def build_report(journal: Path, cache: Dict[str, Any], *, as_of_ts: float | None, fix_commit: str) -> Dict[str, Any]:
    evidence = cache["evidence"]
    no_trades = [e for e in evidence if not str(e.get("evidence_key", "")).startswith("trade:")]
    effective = load_effective_market_pnl(journal, evidence=evidence, as_of_ts=as_of_ts)
    journal_only = load_effective_market_pnl(journal, evidence=no_trades, as_of_ts=as_of_ts)
    versions = _run_versions(journal)
    ancestry: Dict[str, str] = {}
    rows = []
    for slug in sorted(effective):
        e, j = effective[slug], journal_only.get(slug, {})
        version = versions.get(slug, "")
        rev = version.split(":", 1)[-1] if version else ""
        if rev not in ancestry:
            ancestry[rev] = contains_commit(rev, fix_commit)
        rows.append({
            "slug": slug,
            "market_end_utc": _iso(e["market_end_ts"]),
            "loaded_version": version,
            "version_contains_ledger_fix": ancestry[rev],
            "pnl_basis": e["pnl_basis"],
            "position_state": e["position_state"],
            "outcome_state": e["outcome_state"],
            "official_outcome": e["outcome"],
            "bot_twap_outcome": e["bot_outcome"],
            "redeem_state": e["redeem_state"],
            "cash_source": e["cash_source"],
            "journal_cycle_pnl_usdc": e["journal_cycle_pnl_usdc"],
            "journal_cycle_pnl_source": e["journal_cycle_pnl_source"],
            "journal_fill_buy_cost_usdc": e["journal_buy_cost_usdc"],
            "journal_fill_sell_net_usdc": e["journal_sell_net_usdc"],
            "journal_fill_rebuild_pnl_usdc": j.get("effective_pnl_usdc"),
            "journal_fill_rebuild_basis": j.get("pnl_basis"),
            "venue_buy_cost_usdc": e["buy_cost_usdc"] if e["cash_source"] == "venue_activity" else None,
            "venue_sell_net_usdc": e["sell_net_usdc"] if e["cash_source"] == "venue_activity" else None,
            "held_shares_at_settlement": e["held_shares"],
            "official_payout_usdc": e["expected_payout_usdc"],
            "confirmed_redeem_cash_usdc": e["redeem_cash_usdc"],
            "effective_pnl_usdc": e["effective_pnl_usdc"],
            "journal_minus_effective_usdc": e["journal_vs_effective_usdc"],
            "journal_fill_rebuild_minus_effective_usdc": (
                None if j.get("effective_pnl_usdc") is None or e["effective_pnl_usdc"] is None
                else j["effective_pnl_usdc"] - e["effective_pnl_usdc"]
            ),
            "issues": ";".join(e["issues"]),
        })
    summary = summarize_effective_pnl(effective)
    comparable = [r for r in rows if r["journal_cycle_pnl_usdc"] is not None and r["effective_pnl_usdc"] is not None]
    fixed_rows = [r for r in rows if r["version_contains_ledger_fix"] in ("yes", "yes_base_dirty")]
    return {
        "rows": rows,
        "summary": summary,
        "comparable": {
            "count": len(comparable),
            "journal_sum_usdc": sum(r["journal_cycle_pnl_usdc"] for r in comparable),
            "effective_sum_usdc": sum(r["effective_pnl_usdc"] for r in comparable),
            "abs_diff_ge_0_05": sum(abs(r["journal_minus_effective_usdc"]) >= 0.05 for r in comparable),
        },
        "issue_counts": dict(Counter(i for r in rows for i in r["issues"].split(";") if i)),
        "post_fix_versions": {
            "fix_commit": fix_commit,
            "markets": len(fixed_rows),
            "version_evidence_counts": dict(Counter(r["version_contains_ledger_fix"] for r in rows)),
            "journal_vs_effective_ge_0_05": [
                {k: r[k] for k in ("slug", "loaded_version", "pnl_basis", "position_state", "journal_cycle_pnl_usdc",
                                   "effective_pnl_usdc", "issues")}
                for r in fixed_rows
                if r["journal_minus_effective_usdc"] is not None and abs(r["journal_minus_effective_usdc"]) >= 0.05
            ],
            "missing_cycle_pnl_with_position": [
                r["slug"] for r in fixed_rows
                if r["journal_cycle_pnl_usdc"] is None and r["position_state"] in ("HELD", "PARTIALLY_SOLD_HELD")
            ],
        },
    }


def cmd_report(args: argparse.Namespace) -> int:
    journal = resolve_trade_db_path(args.journal)
    cache = load_cache(Path(args.cache))
    report = build_report(journal, cache, as_of_ts=args.as_of_ts, fix_commit=args.fix_commit)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    with (out / f"pnl_reconciliation_{stamp}.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["rows"][0].keys()) if report["rows"] else ["slug"])
        writer.writeheader()
        writer.writerows(report["rows"])
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journal": str(journal),
        "journal_sha256_prefix": hashlib.sha256(journal.read_bytes()[:1 << 20]).hexdigest()[:16],
        "evidence_cache": str(args.cache),
        "evidence_fetched_at": cache.get("fetched_at"),
        "as_of_ts": args.as_of_ts,
        **{k: report[k] for k in ("summary", "comparable", "issue_counts", "post_fix_versions")},
    }
    (out / f"pnl_reconciliation_{stamp}.json").write_text(json.dumps(meta, indent=1, default=str))
    s = report["summary"]
    print(f"markets={s['market_count']} final={s['final_pnl_usdc']:+.4f} estimated={s['estimated_pnl_usdc']:+.4f} "
          f"unresolved={s['unresolved_count']} comparable={report['comparable']} -> {out}/pnl_reconciliation_{stamp}.*")
    for basis in PNL_BASIS_ORDER:
        b = s["by_basis"][basis]
        print(f"  {basis:18s} n={b['count']:4d} pnl={b['pnl_usdc']:+.4f}")
    return 0


# --- apply / rollback -------------------------------------------------------------------

def _backup(journal: Path, backup_dir: Path, label: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{journal.stem}.{label}.{_utc_stamp()}.db"
    src = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(str(target))
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"backup failed quick_check: {target}")
    finally:
        check.close()
    return target


def pending_evidence(conn: sqlite3.Connection, evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from bot.settlement_evidence import journaled_evidence_keys
    existing = journaled_evidence_keys(conn)
    seen, out = set(), []
    for item in evidence:
        key = str(item.get("evidence_key") or "")
        if key and key not in existing and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def apply_evidence(journal: Path, evidence: List[Dict[str, Any]], batch_id: str) -> int:
    conn = sqlite3.connect(str(journal), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = pending_evidence(conn, evidence)
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
            [(now, batch_id, evidence_event_type(item), json.dumps({**item, "backfill_batch_id": batch_id},
                                                                    ensure_ascii=False, sort_keys=True))
             for item in rows],
        )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_batch(journal: Path, batch_id: str) -> int:
    if not batch_id.startswith(BATCH_PREFIX):
        raise ValueError(f"refusing to roll back a non-backfill run_id: {batch_id}")
    conn = sqlite3.connect(str(journal), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"DELETE FROM strategy_events WHERE run_id=? AND event_type IN ({','.join('?' * len(EVIDENCE_EVENTS))})",
            (batch_id, *EVIDENCE_EVENTS),
        )
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cmd_apply(args: argparse.Namespace) -> int:
    journal = resolve_trade_db_path(args.journal)
    cache = load_cache(Path(args.cache))
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        pending = pending_evidence(conn, cache["evidence"])
    finally:
        conn.close()
    counts = Counter(evidence_event_type(e) for e in pending)
    print(f"pending evidence rows: {len(pending)} {dict(counts)} (journal={journal})")
    if not args.confirm:
        print("DRY-RUN: nothing written. Re-run with --confirm (bot stopped) to apply.")
        return 0
    if not pending:
        print("Nothing to apply (all evidence keys already journaled); no backup, no batch.")
        return 0
    batch_id = f"{BATCH_PREFIX}{_utc_stamp()}"
    try:
        with require_bot_stopped(journal_path=journal) if not args.unsafe_skip_lock_check else nullcontext():
            backup = _backup(journal, Path(args.backup_dir), "pre_" + batch_id)
            written = apply_evidence(journal, cache["evidence"], batch_id)
    except MaintenanceLockError as exc:
        print(f"REFUSED: {exc}. Stop the bot first.", file=sys.stderr)
        return 4
    print(f"applied batch_id={batch_id} rows={written} backup={backup}")
    print(f"rollback: python3 scripts/pnl_evidence_backfill.py rollback --batch-id {batch_id} --confirm")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    journal = resolve_trade_db_path(args.journal)
    if not args.confirm:
        conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
        try:
            n = conn.execute("SELECT count(*) FROM strategy_events WHERE run_id=?", (args.batch_id,)).fetchone()[0]
        finally:
            conn.close()
        print(f"DRY-RUN: {n} rows carry run_id={args.batch_id}. Re-run with --confirm to delete them.")
        return 0
    if not args.batch_id.startswith(BATCH_PREFIX):
        print(f"REFUSED: not a backfill batch id: {args.batch_id}", file=sys.stderr)
        return 2
    try:
        with require_bot_stopped(journal_path=journal) if not args.unsafe_skip_lock_check else nullcontext():
            backup = _backup(journal, Path(args.backup_dir), "pre_rollback_" + args.batch_id)
            deleted = rollback_batch(journal, args.batch_id)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except MaintenanceLockError as exc:
        print(f"REFUSED: {exc}. Stop the bot first.", file=sys.stderr)
        return 4
    print(f"rolled back batch_id={args.batch_id} rows={deleted} backup={backup}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "report", "apply", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--journal", default=None, help="trade journal (default: canonical TRADE_DB_PATH)")
        if name in ("report", "apply"):
            p.add_argument("--cache", required=True)
        if name in ("apply", "rollback"):
            p.add_argument("--confirm", action="store_true")
            p.add_argument("--backup-dir", default=str(REPO_ROOT / "backups" / "pnl_evidence"))
            p.add_argument("--unsafe-skip-lock-check", action="store_true",
                           help="ONLY for a detached copy of the journal (tests / backup validation)")
    fetch = sub.choices["fetch"]
    fetch.add_argument("--out", required=True)
    fetch.add_argument("--sleep-sec", type=float, default=0.1)
    fetch.add_argument("--max-pages", type=int, default=40)
    report = sub.choices["report"]
    report.add_argument("--out-dir", default=str(REPO_ROOT / "reports" / "pnl_reconciliation"))
    report.add_argument("--as-of-ts", type=float, default=None)
    report.add_argument("--fix-commit", default="b0c383f",
                        help="ledger-fix commit; each run's loaded revision is checked by git ancestry")
    sub.choices["rollback"].add_argument("--batch-id", required=True)
    args = parser.parse_args(argv)
    return {"fetch": cmd_fetch, "report": cmd_report, "apply": cmd_apply, "rollback": cmd_rollback}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

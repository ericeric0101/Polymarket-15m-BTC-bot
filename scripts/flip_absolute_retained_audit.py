#!/usr/bin/env python3
"""Offline LIVE sufficiency audit; no network, runtime execution, DB writes or schema creation.

Prints JSON to stdout. --cold-db accepts closed, independently hash-verified staging
copies only. Missing trigger evidence is UNKNOWN, never a negative trigger label.
Reuses canonical live_trades and load_paths; their shadow replay is not a LIVE
conditional-breaker replay. Candidate selection is intentionally gated by coverage.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from final_research_analysis import live_trades
from research_stop_analysis import load_paths
from reproduce_research_iteration import verify_official_cache


def ro(path, *, closed=False):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro' + ('&immutable=1' if closed else ''), uri=True)
    conn.execute('PRAGMA query_only=ON')
    return conn


def audit(args):
    provenance = Path(args.provenance)
    summary = json.loads(provenance.with_suffix('.summary.json').read_text())
    assert hashlib.sha256(provenance.read_bytes()).hexdigest() == summary['file_sha256']
    verify_official_cache(Path(summary['official_cache']))
    with ro(args.journal) as conn:
        live_runs = {r[0] for r in conn.execute("SELECT run_id FROM strategy_runs WHERE mode='LIVE'")}
        positions = {}
        for ts, run, coid, side, price, qty, inst, raw in conn.execute(
            "SELECT ts,run_id,client_order_id,side,price,qty,instrument_id,payload_json FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id"):
            if run not in live_runs or side != 'BUY' or not price or not qty:
                continue
            d = json.loads(raw)
            slug = d.get('slug') or d.get('market_slug')
            if not slug:
                continue
            p = positions.setdefault(slug, {'market_slug': slug, 'first_fill_ts': ts,
                'day_taipei': datetime.fromisoformat(ts).astimezone(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d'),
                'buy_fill_rows': 0, 'buy_qty': 0, 'buy_notional': 0,
                'runs': set(), 'instruments': set(), 'buy_order_ids': set(), 'side_evidence': set()})
            p['runs'].add(run); p['instruments'].add(inst); p['buy_order_ids'].add(coid)
            p['buy_fill_rows'] += 1; p['buy_qty'] += qty; p['buy_notional'] += price * qty

        labels = {r['market_slug']: r for r in csv.DictReader(open(args.provenance))}
        outcomes = {s: r['outcome_used'] for s, r in labels.items() if r['outcome_source_used'] == 'POLYMARKET_OFFICIAL'}
        canonical = live_trades(conn, outcomes)
        for coid, raw in conn.execute("SELECT client_order_id,payload_json FROM order_events WHERE event_type='FILL_MARKOUT' AND side='BUY'"):
            d = json.loads(raw); slug = d.get('slug') or d.get('market_slug')
            if slug in positions and d.get('fill_id') in positions[slug]['buy_order_ids']:
                if d.get('entry_outcome_side') in ('UP', 'DOWN'):
                    positions[slug]['side_evidence'].add(d['entry_outcome_side'])
        decision_reasons = Counter()
        for ts, run, raw in conn.execute("SELECT ts,run_id,payload_json FROM strategy_events WHERE event_type='EXIT_POLICY_DECISION'"):
            if run not in live_runs:
                continue
            d = json.loads(raw); slug = d.get('slug') or d.get('market_slug')
            decision_reasons[d.get('reason')] += 1
            if slug in positions:
                p = positions[slug]
                p['exit_decision_rows'] = p.get('exit_decision_rows', 0) + 1
                p['positive_cost_mark_rows'] = p.get('positive_cost_mark_rows', 0) + int(float(d.get('avg_entry') or 0) > 0 and d.get('net_if_exit') is not None)
                if d.get('reason') == 'absolute_max_loss_breaker':
                    p.setdefault('recorded_absolute_decision_times', []).append(ts)
        for typ, run, raw in conn.execute("SELECT event_type,run_id,payload_json FROM strategy_events WHERE event_type IN ('MARKET_SETTLEMENT','MARKET_STRIKE_LOCKED','MARKET_STRIKE_RECOVERED')"):
            d = json.loads(raw); slug = d.get('slug') or d.get('market_slug')
            if slug in positions:
                p = positions[slug]
                p[typ + '_rows'] = p.get(typ + '_rows', 0) + 1
        for ts, run, coid, raw in conn.execute("SELECT ts,run_id,client_order_id,payload_json FROM order_events WHERE event_type='ORDER_TAKER_EXIT_SUBMIT' ORDER BY id"):
            d = json.loads(raw); slug = d.get('slug') or d.get('market_slug')
            if run in live_runs and slug in positions and d.get('decision_reason') == 'absolute_max_loss_breaker':
                positions[slug].setdefault('recorded_absolute_submits', []).append({
                    'ts': ts, 'client_order_id': coid, 'net_if_exit_estimate': d.get('est_net_if_exit'),
                    'time_left_sec': d.get('time_left_sec'), 'definition': 'observed submit; not first eligible evaluation'})

    paths = load_paths(Path(args.export))
    for slug, p in positions.items():
        # load_paths omits run_id: even slug-matching rows cannot prove same-run
        # exposure. Count slug overlap as availability only, not a valid join.
        p['canonical_path_rows_slug_only'] = sum(len(rs) for (_, s), rs in paths.items() if s == slug)

    cold_inventory = []
    stop_counts = Counter()
    for path in args.cold_db:
        with ro(path, closed=True) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            inventory = {'path': str(Path(path).resolve()), 'tables': sorted(tables)}
            if 'snapshots' in tables:
                inventory['snapshot_range'] = c.execute('SELECT min(observed_ts_ms),max(observed_ts_ms),count(*) FROM snapshots').fetchone()
                for run, slug, n in c.execute('SELECT run_id,polymarket_slug,count(*) FROM snapshots GROUP BY run_id,polymarket_slug'):
                    if slug in positions and run in positions[slug]['runs']:
                        p = positions[slug]
                        p['legacy_reference_snapshot_rows'] = p.get('legacy_reference_snapshot_rows', 0) + n
            if 'lead_lag_decisions' in tables:
                # Timestamp index limits the pass to retained LIVE period.
                lo = min(datetime.fromisoformat(p['first_fill_ts']).timestamp() for p in positions.values())
                hi = max(int(s.rsplit('-', 1)[-1]) + 900 for s in positions)
                for run, slug, ns, raw in c.execute('SELECT run_id,slug,decision_epoch_ns,payload_json FROM lead_lag_decisions WHERE decision_epoch_ns BETWEEN ? AND ?', (int(lo*1e9), int(hi*1e9))):
                    if run not in live_runs or slug not in positions:
                        continue
                    d = json.loads(raw); typ = str(d.get('event_type') or '')
                    if typ.startswith('STOP_SHADOW'):
                        stop_counts[typ] += 1
                        p = positions[slug]; p[typ + '_rows'] = p.get(typ + '_rows', 0) + 1
                        if typ == 'STOP_SHADOW_ACTUAL_STOP':
                            p.setdefault('recorded_actual_stops', []).append(d)
                    if typ == 'SHADOW_POSITION_MARK':
                        positions[slug]['shadow_marks_not_live'] = positions[slug].get('shadow_marks_not_live', 0) + 1
                    if typ in ('SHADOW_BBO_SNAPSHOT', 'SHADOW_BBO_MATERIAL_CHANGE') and d.get('instrument_id') in positions[slug]['instruments']:
                        p = positions[slug]
                        p['same_instrument_bbo_rows'] = p.get('same_instrument_bbo_rows', 0) + 1
            cold_inventory.append(inventory)

    per_day = defaultdict(lambda: {'markets': 0, 'buy_fill_rows': 0, 'paired_comparison_markets': 0})
    for slug, p in positions.items():
        label = labels.get(slug, {})
        p['final_resolution'] = outcomes.get(slug)
        p['resolution_source'] = label.get('outcome_source_used')
        p['journal_resolution'] = label.get('journal_outcome')
        p['side'] = next(iter(p['side_evidence'])) if len(p['side_evidence']) == 1 else None
        p['final_position_outcome'] = ('WIN' if p['side'] == p['final_resolution'] else 'LOSS') if p['side'] and p['final_resolution'] else None
        p.update({'flip_trigger_time': None, 'absolute_breaker_trigger_time': None,
                  'pnl_at_flip_trigger': None, 'pnl_at_absolute_trigger': None,
                  'comparison_status': 'NOT_IDENTIFIABLE',
                  'missing_evidence': ['continuous synchronized LIVE held-token bid/cost/exposure path',
                     'fresh reference/strike path with verified historical clock semantics',
                     'absolute trend confirmation, stable two-vote components/persistence and fair-at-entry state']})
        if p.get('recorded_absolute_submits'):
            p['absolute_breaker_trigger_time'] = p['recorded_absolute_submits'][0]['ts']
            p['absolute_time_status'] = 'VERIFIED_OBSERVED_SUBMIT_NOT_FIRST_ELIGIBILITY'
            p['pnl_at_absolute_trigger'] = p['recorded_absolute_submits'][0]['net_if_exit_estimate']
            p['absolute_pnl_status'] = 'VERIFIED_RECORDED_ESTIMATE_NOT_REALIZED_FILL'
        for key in ('runs', 'instruments', 'buy_order_ids', 'side_evidence'):
            p[key] = sorted(x for x in p[key] if x is not None)
        day = per_day[p['day_taipei']]; day['markets'] += 1; day['buy_fill_rows'] += p['buy_fill_rows']
    metrics = {key: None for key in ('overlap_pct_all_eligible', 'overlap_pct_union', 'flip_only_pct',
        'absolute_only_pct', 'flip_lead_median_sec', 'flip_lead_mean_sec', 'pnl_at_flip_trigger',
        'pnl_at_absolute_trigger', 'theoretical_saved_loss', 'whipsaw_win_rate', 'stop_minus_hold_net_pnl')}
    observed = [p for p in positions.values() if p.get('recorded_absolute_submits')]
    estimates = [p['pnl_at_absolute_trigger'] for p in observed if p['pnl_at_absolute_trigger'] is not None]
    return {'EXISTING_DATA_SUFFICIENT': 'NO', 'NEW_RUNTIME_SHADOW_REQUIRED': 'NO',
        'NEW_READONLY_ANALYSIS_SCRIPT_REQUIRED': 'YES',
        'runtime_shadow_decision_scope': 'No evidence mandates a new Flip-specific runtime/shadow; historical gaps cannot be repaired prospectively.',
        'metrics': metrics, 'eligible_paired_markets': 0, 'eligible_paired_days': 0,
        'live_buy_markets': len(positions), 'live_buy_fill_rows': sum(p['buy_fill_rows'] for p in positions.values()),
        'n_days_taipei': len(per_day), 'per_day_taipei': dict(sorted(per_day.items())),
        'official_resolution_markets': sum(p['final_resolution'] is not None for p in positions.values()),
        'known_held_side_markets': sum(p['side'] is not None for p in positions.values()),
        'recorded_absolute_submit_markets': sum(bool(p.get('recorded_absolute_submits')) for p in positions.values()),
        'historical_absolute_submit_descriptive_only': {
            'markets': len(observed), 'days_taipei': len({p['day_taipei'] for p in observed}),
            'submit_rows': sum(len(p['recorded_absolute_submits']) for p in observed),
            'first_submit_net_estimate_mean': statistics.mean(estimates) if estimates else None,
            'first_submit_net_estimate_median': statistics.median(estimates) if estimates else None,
            'scope': 'mixed historical versions/configurations; not current conditional-rule validation'},
        'canonical_live_trades_rows': len(canonical),
        'canonical_live_trades_scope': 'Accounting/settlement only, not Flip-stop counterfactual.',
        'live_exit_decision_reasons': dict(decision_reasons), 'live_stop_shadow_events': dict(stop_counts),
        'candidate_scan': 'NOT_RUN: no paired reconstructible LIVE cohort; no parameter validation claim.',
        'metrics_definitions': {'overlap_all': 'both/N_eligible', 'overlap_union': 'both/(Flip or Absolute)',
            'flip_only': 'Flip and not Absolute / N_eligible', 'absolute_only': 'Absolute and not Flip / N_eligible',
            'lead': 't_absolute - t_flip; signed, paired markets only',
            'saved_loss_vs_absolute': 'same exposure net exit at Flip minus net exit at Absolute',
            'stop_minus_hold': 'same exposure net exit at Flip minus resolution payout less entry cost/fees',
            'whipsaw': 'official WIN after Flip / officially resolved Flip-triggered markets'},
        'cold_inventory': cold_inventory, 'markets': sorted(positions.values(), key=lambda p:p['first_fill_ts'])}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--journal', default='logs/trade_journal.db')
    ap.add_argument('--export', default='data/research_export')
    ap.add_argument('--provenance', default='data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv')
    ap.add_argument('--cold-db', action='append', default=[])
    print(json.dumps(audit(ap.parse_args()), indent=2))

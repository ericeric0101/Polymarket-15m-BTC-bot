"""Idempotent, opt-in index plan for a copied/offline research database."""
from __future__ import annotations
import sqlite3

INDEX_NAME = "idx_lead_lag_run_slug_time"
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON lead_lag_decisions(run_id, slug, decision_epoch_ns)"

def index_status(conn: sqlite3.Connection) -> bool:
    return any(row[1] == INDEX_NAME for row in conn.execute("PRAGMA index_list(lead_lag_decisions)"))

def apply_index_plan(conn: sqlite3.Connection) -> bool:
    """Caller owns the copied DB; returns whether this call created the index."""
    existed = index_status(conn)
    conn.execute(INDEX_SQL)
    conn.commit()
    return not existed


def migrate_closed_copy(path, *, closed_copy_confirmed: bool = False) -> dict:
    """Explicit opt-in on a closed offline copy; never called by runtime/Store."""
    from pathlib import Path
    path = Path(path)
    if not closed_copy_confirmed:
        raise ValueError('closed offline copy confirmation required')
    # mode=rw refuses missing paths; no accidental new database.
    with sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('copy integrity failed')
        intended = {'index': INDEX_NAME, 'sql': INDEX_SQL, 'destructive': False}
        import logging
        logging.getLogger(__name__).info('Offline closed-copy index plan: %s', intended)
        if index_status(conn):
            columns = [row[2] for row in conn.execute(f'PRAGMA index_info({INDEX_NAME})')]
            if columns != ['run_id', 'slug', 'decision_epoch_ns']:
                raise ValueError('index name already used by a different definition')
        created = apply_index_plan(conn)
        return {**intended, 'created': created, 'quick_check': conn.execute('PRAGMA quick_check').fetchone()[0]}


def benchmark_index(*, rows: int = 100_000) -> dict:
    """Synthetic representative plans/timings, entirely inside a temporary DB."""
    import json
    import statistics
    import tempfile
    import time
    queries = {
        'market_timeline': ('SELECT * FROM lead_lag_decisions WHERE run_id=? AND slug=? ORDER BY decision_epoch_ns,rowid', ('r1', 'm1')),
        'run_timeline': ('SELECT * FROM lead_lag_decisions WHERE run_id=? ORDER BY decision_epoch_ns,rowid', ('r1',)),
        'event_type_slug': ('SELECT * FROM lead_lag_decisions WHERE run_id=? AND slug=? ORDER BY decision_epoch_ns,rowid', ('r1','m1')),
        'time_range': ('SELECT * FROM lead_lag_decisions WHERE run_id=? AND slug=? AND decision_epoch_ns BETWEEN ? AND ? ORDER BY decision_epoch_ns,rowid', ('r1','m1',0, rows//2)),
        'lifecycle': ('SELECT * FROM lead_lag_decisions WHERE run_id=? AND slug=? ORDER BY decision_epoch_ns,rowid', ('r1','m1')),
    }
    result = []
    with tempfile.TemporaryDirectory(prefix='p2-index-') as directory:
        with sqlite3.connect(directory + '/synthetic.sqlite') as conn:
            conn.execute('CREATE TABLE lead_lag_decisions(run_id TEXT,slug TEXT,decision_epoch_ns INTEGER,payload_json TEXT)')
            conn.executemany('INSERT INTO lead_lag_decisions VALUES(?,?,?,?)',
                ((f'r{i % 4}', f'm{i % 100}', i, json.dumps({'event_type':'POSITION_LIFECYCLE_ENTRY' if i % 5 else 'OTHER',
                    'position_lifecycle_id':f'm{i % 100}|up|buy1'})) for i in range(rows)))
            conn.commit()
            for phase in ('before', 'after'):
                if phase == 'after':
                    apply_index_plan(conn)
                for name, (sql, parameters) in queries.items():
                    plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, parameters)]
                    durations = []
                    for _ in range(3):
                        start = time.perf_counter()
                        captured = conn.execute(sql, parameters).fetchall()
                        if name == 'event_type_slug':
                            captured = [r for r in captured if json.loads(r[3]).get('event_type') == 'POSITION_LIFECYCLE_ENTRY']
                        elif name == 'lifecycle':
                            captured = [r for r in captured if json.loads(r[3]).get('position_lifecycle_id') == 'm1|up|buy1']
                        count = len(captured)
                        durations.append((time.perf_counter() - start)*1000)
                    result.append({'phase':phase,'query':name,'rows':count,'elapsed_ms_median':statistics.median(durations),
                                   'plan':plan,'index_used':any(INDEX_NAME in item for item in plan)})
    return {'source':'SYNTHETIC_TEMP_DB','input_rows':rows,'measurements':result,'candidate_index':INDEX_NAME}

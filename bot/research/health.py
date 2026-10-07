"""Read-only projections of existing component authorities, never a BUY gate."""
from __future__ import annotations
from typing import Any

SEVERITY = {'HEALTHY': 0, 'UNKNOWN': 1, 'DEGRADED': 2, 'CRITICAL': 3}


def component_health(metrics: dict | None, *, domain: str) -> dict:
    metrics = dict(metrics or {})
    reasons, state = [], 'HEALTHY'
    def mark(code, severity):
        nonlocal state
        reasons.append(code)
        if SEVERITY[severity] > SEVERITY[state]:
            state = severity
    if not metrics or metrics.get('observation_unavailable'):
        mark('METRICS_UNAVAILABLE', 'UNKNOWN')
    if metrics.get('ready') is False:
        mark('BUY_SAFETY_BLOCK_ACTIVE' if domain == 'Execution' else 'COMPONENT_NOT_READY', 'CRITICAL')
    availability = metrics.get('availability')
    if availability in {'NOT_CONFIGURED', 'DISABLED_BY_POLICY'} and domain != 'Execution':
        return {'state': 'HEALTHY', 'availability': availability,
                'reason_codes': [availability], 'source_metrics': metrics}
    if availability == 'FAILED' or metrics.get('enabled') is False or metrics.get('failure_reason'):
        mark('WRITER_UNAVAILABLE', 'CRITICAL')
    if metrics.get('triggered') is True:
        mark('EXISTING_STORAGE_GUARD_ACTIVE', 'CRITICAL')
    if metrics.get('write_errors', 0) or metrics.get('errors', 0):
        mark('WRITE_OR_CAPTURE_ERROR', 'CRITICAL')
    if metrics.get('queue_drops', 0) or metrics.get('drops', 0):
        mark('DATA_DROPPED', 'DEGRADED')
    if metrics.get('recent_largest_gap_sec') is not None and metrics.get('interval_sec'):
        if metrics['recent_largest_gap_sec'] > 3 * metrics['interval_sec']:
            mark('RECENT_SNAPSHOT_GAP', 'DEGRADED')
    freshness = metrics.get('persisted_joint_fresh_pct', metrics.get('joint_fresh_pct'))
    if freshness is not None and freshness < 100:
        mark('RECENT_STALE_EVIDENCE', 'DEGRADED')
    if metrics.get('queue_capacity') and metrics.get('queue_depth', 0) >= metrics['queue_capacity']:
        mark('WRITER_QUEUE_FULL', 'DEGRADED')
    if metrics.get('warning_active') is True:
        mark('EXISTING_DATAENGINE_QUEUE_WARNING', 'DEGRADED')
    if metrics.get('watchdog_pending') is True:
        mark('EXISTING_WATCHDOG_RECOVERY_PENDING', 'DEGRADED')
    if metrics.get('quote_fresh') is False:
        mark('EXISTING_QUOTE_FRESHNESS_FAILED', 'DEGRADED')
    if metrics.get('sample_count') == 0:
        mark('RECENT_SAMPLES_UNAVAILABLE', 'UNKNOWN')
    return {'state': state, 'availability': 'FAILED' if availability == 'FAILED' or metrics.get('failure_reason') else availability,
            'reason_codes': sorted(reasons), 'source_metrics': metrics}


def aggregate_health(domains: dict[str, dict[str, dict | None]]) -> dict:
    result = {}
    for domain in ('Data', 'Research', 'Storage', 'Execution'):
        components = {name: component_health(value, domain=domain)
                      for name, value in domains.get(domain, {}).items()}
        if not components:
            components = {'unavailable': component_health(None, domain=domain)}
        state = max((c['state'] for c in components.values()), key=SEVERITY.get)
        result[domain] = {'state': state, 'components': components,
                          'reason_codes': sorted({f'{name}:{reason}' for name, item in components.items() for reason in item['reason_codes']})}
    return {'state': max((r['state'] for r in result.values()), key=SEVERITY.get),
            'domains': result, 'observability_only': True}


def strategy_health(strategy: Any, now_ts: float) -> dict:
    """Bounded in-memory reads only; each failing source is isolated independently."""
    def read(owner_name, method, *args, **kwargs):
        try:
            owner = getattr(strategy, owner_name, None)
            if owner is None:
                availability = getattr(strategy, '_btc_history_observer_status', None) if owner_name == 'btc_1s_history_collector' else None
                return {'availability': availability or 'NOT_CONFIGURED'} if hasattr(strategy, owner_name) and owner_name != 'trade_db' else None
            return getattr(owner, method)(*args, **kwargs)
        except Exception:
            return None
    try:
        quote = getattr(strategy, '_last_market_data_health', None)
        shadow = getattr(strategy, 'twap_forward_shadow', None)
        storage = getattr(shadow, '_last_storage_health', None)
        if quote and now_ts - float(quote.get('observed_ts', 0)) > 15:
            quote = {**quote, 'quote_fresh': False, 'projection_stale': True}
        if storage and now_ts - float(storage.get('observed_ts', 0)) > 2 * float(getattr(shadow, 'storage_check_interval_sec', 300)):
            storage = None
        btc = getattr(strategy, 'btc_1s_history_collector', None)
        btc_storage = getattr(btc, '_last_disk_health', None)
        queue = quote.get('queue_window') if quote else None
        watchdog = {'trigger_counts': dict(getattr(strategy, 'quote_watchdog_trigger_counts', {})),
                    'watchdog_pending': bool(getattr(strategy, 'quote_recovery_pending_instruments', set()))} if hasattr(strategy, 'quote_watchdog_trigger_counts') else None
        return aggregate_health({
            'Data': {'quote_transport': quote, 'data_engine': queue, 'watchdog': watchdog},
            'Research': {'prediction': read('prediction_research_snapshotter', 'recent_health', now_ts, slug=getattr(strategy, 'current_market_slug', None) or None),
                         'twap': read('twap_research_db', 'research_health'),
                         'btc': read('btc_1s_history_collector', 'research_health')},
            'Storage': {'twap_guard': storage, 'btc_guard': btc_storage},
            'Execution': {'journal': read('trade_db', 'runtime_health_snapshot')},
        })
    except Exception:
        return aggregate_health({})


def status_health(strategy: Any, now_ts: float) -> str:
    """Single STATUS projection; details log only on a changed state/reason set."""
    try:
        from loguru import logger
        view = strategy_health(strategy, now_ts)
        strategy._derived_health = view
        signature = tuple((name, item['state'], tuple(item['reason_codes'])) for name, item in view['domains'].items())
        previous = getattr(strategy, '_derived_health_signature', None)
        if signature != previous:
            strategy._derived_health_signature = signature
            if previous is not None or any(item['state'] in {'DEGRADED', 'CRITICAL'} for item in view['domains'].values()):
                logger.info('Derived health transition: {}', signature)
        return ''.join(f" {name}={item['state']}" for name, item in view['domains'].items())
    except Exception:
        return ' Data=UNKNOWN Research=UNKNOWN Storage=UNKNOWN Execution=UNKNOWN'

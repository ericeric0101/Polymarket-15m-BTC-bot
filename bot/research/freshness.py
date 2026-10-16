"""Native prediction cohort provenance; historical recomputation stays separate."""
from __future__ import annotations


def native_v2_exclusion_reason(row: dict) -> str | None:
    if str(row.get('freshness_provenance_class', '')).startswith('HISTORICAL_'):
        return 'HISTORICAL_RECOMPUTATION_NOT_NATIVE'
    version = row.get('freshness_clock_semantics_version')
    if version is None:
        return 'MISSING_FRESHNESS_CLOCK_VERSION'
    if type(version) is int and version == 2:
        return None
    return 'LEGACY_FRESHNESS_CLOCK_V1' if version == 1 else 'UNSUPPORTED_FRESHNESS_CLOCK_VERSION'


def accept_native_v2(row: dict, exclusions: dict) -> bool:
    reason = native_v2_exclusion_reason(row)
    if reason is None:
        return True
    exclusions[reason] = exclusions.get(reason, 0) + 1
    return False

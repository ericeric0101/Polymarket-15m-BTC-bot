"""Pre-registered forward evaluator for Entry V3 execution selection (research only, offline, pure).

Implements exactly prereg_final.md (sha256 e4179a84...) section 6 inference:
* PRIMARY family K = 4: FQO_LOWER, FQO_UPPER, EARLY_CANCEL_C, CHASE_B (D: annotation only).
* Each member: paired sign-flip permutation test of Delta = HOLD_PNL_policy - HOLD_PNL_CURRENT,
  one canonical candidate per market, H1: sum(Delta) > 0 (one-sided), 10,000 Monte Carlo flips.
* Holm step-down over the family, family alpha = 0.10.
* FQO: sign(Delta_LOWER) != sign(Delta_UPPER)  ->  FQO_VERDICT = UNRESOLVED, no exception.
* Stopping gate (section 2): N >= 672 canonical candidates AND >= 7 complete Taipei days with
  weekday and weekend present.  Below the gate only counts are returned -- never an EV, PnL
  or p-value.  There is no performance-based early stop.

Parameters the prereg does not fix (seed, p-value estimator, ...) are NOT filled in here:
``PREREG_LITERAL_PARAMS`` leaves them None and the evaluator refuses inference until a
CK-approved addendum supplies them (reports/forward_profile_cleanup_20261013/
prereg_addendum_v1_DRAFT.md).  Data with different run fingerprints are never merged
silently.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

EVALUATOR_VERSION = "prereg_evaluator_v1"
PREREG_SHA256 = "e4179a844600a80ef151efdbbce94daad7c3570a551981ddc9668e9ef5d8e123"
PRIMARY_FAMILY = ("FQO_LOWER", "FQO_UPPER", "EARLY_CANCEL_C", "CHASE_B")
K = len(PRIMARY_FAMILY)
FAMILY_ALPHA = 0.10
N_PERMUTATIONS = 10_000
N_MIN_CANONICAL_CANDIDATES = 672
MIN_COMPLETE_TAIPEI_DAYS = 7
BLOCK_SIZE = 4
INFORMATIVE_N_WARN = 30          # report-only warning threshold (not a decision rule)
UNRESOLVED, PROMISING = "UNRESOLVED", "PROMISING"
PASS, FAIL, NOT_EVALUATED = "PASS", "FAIL", "NOT_EVALUATED"


class FingerprintMergeRefused(ValueError):
    """Candidates from different run fingerprints were offered without an explicit instruction."""


@dataclass(frozen=True)
class EvaluatorParams:
    """Every implementation choice; None = not fixed by the prereg (needs an approved addendum)."""
    n_permutations: int = N_PERMUTATIONS
    family_alpha: float = FAMILY_ALPHA
    one_sided_h1_sum_positive: bool = True
    block_size: int = BLOCK_SIZE
    expected_exit_policy: Optional[Mapping[str, Any]] = None     # A1
    seed: Optional[int] = None                                    # A2
    pvalue_estimator: Optional[str] = None                        # A3: "plus_one" = (1+b)/(B+1)
    fqo_unknown_bound: Optional[str] = None                       # A5: "exclude_and_count"
    b_primary_latency: Optional[str] = None                       # A6: "max_p_of_both"
    quantity_rule: Optional[str] = None                           # A7
    complete_day_rule: Optional[str] = None                       # A8
    bootstrap_b: Optional[int] = None                             # A9
    secondary_denominator: Optional[str] = None                   # A10
    quartile_split: Optional[str] = None                          # A11: "contiguous_first_blocks_larger"
    weekend_requirement: Optional[str] = None                     # A16
    n_scope: Optional[str] = None                                 # A17
    addendum_sha256: Optional[str] = None
    approved_by_ck: bool = False

    def unresolved(self) -> list[str]:
        names = ("expected_exit_policy", "seed", "pvalue_estimator", "fqo_unknown_bound", "b_primary_latency",
                 "quantity_rule", "complete_day_rule", "bootstrap_b", "secondary_denominator", "quartile_split",
                 "weekend_requirement", "n_scope")
        missing = [n for n in names if getattr(self, n) is None]
        if not self.approved_by_ck or not self.addendum_sha256:
            missing.append("addendum_not_approved_by_ck")
        return missing


PREREG_LITERAL_PARAMS = EvaluatorParams()

# Values proposed in the DRAFT addendum.  NOT in force until CK approves (approved_by_ck=True
# and the addendum file hash are supplied by the caller).
PROPOSED_ADDENDUM_VALUES = dict(
    expected_exit_policy={"conditional_breaker_enabled": False, "breaker_v2_effective_for_conditional_exit": False,
                          "catastrophic_breaker_enabled": False, "stop_loss_enabled": False,
                          "endgame_twap_exit_enabled": False, "invalidation_recovery_enabled": False,
                          "recovery_exit_ladder_enabled": False, "maker_urgent_exit_enabled": False,
                          "hold_to_redeem_enabled": "True", "tail_protect_tp_price": "0.97"},
    seed=20261013, pvalue_estimator="plus_one", fqo_unknown_bound="exclude_and_count",
    b_primary_latency="max_p_of_both", quantity_rule="current_actual_filled__policies_planned",
    complete_day_rule="local_records_cover_0015_2345_no_gap_gt_15min", bootstrap_b=10_000,
    secondary_denominator="sum_planned_shares", quartile_split="contiguous_first_blocks_larger",
    weekend_requirement="at_least_one_complete_weekday_and_one_complete_weekend_day",
    n_scope="all_canonical_candidates_in_matching_fingerprint_runs")


# ----------------------------------------------------------------- statistics

def sign_flip_test(deltas: Sequence[float], *, n_permutations: int, seed: int, exact: bool = False) -> dict:
    """One-sided paired sign-flip test of H1: sum(delta) > 0.

    p = (1 + #{T* >= T}) / (B + 1) for Monte Carlo (ties count against H1); the exact
    version enumerates all 2^n sign vectors (used only for small-sample self-tests).
    """
    d = [float(x) for x in deltas if x is not None]
    t_obs = sum(d)
    nonzero = [x for x in d if x != 0.0]
    eps = 1e-12 * max(1.0, sum(abs(x) for x in d))
    if not nonzero:
        return {"n": len(d), "n_nonzero": 0, "t_obs": t_obs, "p": 1.0, "method": "degenerate_all_zero",
                "n_permutations": 0, "seed": seed}
    if exact:
        ge = total = 0
        for signs in itertools.product((1.0, -1.0), repeat=len(nonzero)):
            total += 1
            ge += sum(s * x for s, x in zip(signs, nonzero)) >= t_obs - eps
        return {"n": len(d), "n_nonzero": len(nonzero), "t_obs": t_obs, "p": ge / total, "method": "exact",
                "n_permutations": total, "seed": None}
    rng = random.Random(seed)
    ge = 0
    for _ in range(int(n_permutations)):
        t = 0.0
        for x in nonzero:
            t += x if rng.random() < 0.5 else -x
        ge += t >= t_obs - eps
    return {"n": len(d), "n_nonzero": len(nonzero), "t_obs": t_obs, "p": (1 + ge) / (n_permutations + 1),
            "method": "monte_carlo", "n_permutations": int(n_permutations), "seed": seed}


def holm(pvalues: Mapping[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values (monotone, capped at 1)."""
    order = sorted(pvalues, key=lambda k: (pvalues[k], k))
    m = len(order)
    adjusted, running = {}, 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (m - i) * float(pvalues[key])))
        adjusted[key] = running
    return adjusted


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def fqo_bounds_verdict(sum_lower: Optional[float], sum_upper: Optional[float]) -> Optional[str]:
    """UNRESOLVED whenever the two bounds disagree in sign (no exception); else None (keep checking)."""
    if sum_lower is None or sum_upper is None or _sign(sum_lower) != _sign(sum_upper):
        return UNRESOLVED
    return None


def informative_n(deltas: Iterable[Optional[float]]) -> int:
    return sum(1 for d in deltas if d is not None and d != 0.0)


def quartile_blocks(values: Sequence[float]) -> list[list[float]]:
    n = len(values)
    base, extra = divmod(n, 4)
    out, i = [], 0
    for b in range(4):
        size = base + (1 if b < extra else 0)
        out.append(list(values[i:i + size]))
        i += size
    return out


def leave_one_out_min(groups: Mapping[str, float], total: float) -> Optional[float]:
    return min((total - v for v in groups.values()), default=None)


def bootstrap_interval(values: Sequence[float], *, b: int, seed: int, block: int = 1) -> tuple:
    vals = [v for v in values if v is not None]
    if not vals:
        return (None, None)
    rng = random.Random(seed)
    n = len(vals)
    blocks = [vals[i:i + block] for i in range(0, n - block + 1)] if block > 1 else [[v] for v in vals]
    sums = []
    for _ in range(b):
        s, k = 0.0, 0
        while k < n:
            blk = blocks[rng.randrange(len(blocks))]
            take = blk[: n - k]
            s += sum(take)
            k += len(take)
        sums.append(s)
    sums.sort()
    return sums[int(0.05 * b)], sums[max(0, int(0.95 * b) - 1)]


def day_cluster_interval(by_day: Mapping[str, list[float]], *, b: int, seed: int) -> tuple:
    days = [sum(v) for v in by_day.values()]
    if not days:
        return (None, None)
    rng = random.Random(seed)
    sums = sorted(sum(days[rng.randrange(len(days))] for _ in days) for _ in range(b))
    return sums[int(0.05 * b)], sums[max(0, int(0.95 * b) - 1)]


# ------------------------------------------------------------------ gates

def check_fingerprints(run_fingerprints: Iterable[str], *, allowed: Optional[Iterable[str]] = None) -> str:
    present = sorted({str(f) for f in run_fingerprints if f})
    if not present:
        raise FingerprintMergeRefused("no run fingerprint present")
    if allowed is not None:
        extra = set(present) - set(allowed)
        if extra:
            raise FingerprintMergeRefused(f"fingerprints outside the explicit allow-list: {sorted(extra)}")
        return "|".join(present)
    if len(present) > 1:
        raise FingerprintMergeRefused(f"{len(present)} distinct run fingerprints {present}; refusing to merge "
                                      "without an explicit allow-list (prereg section 1)")
    return present[0]


def check_exit_policy(run_headers: Iterable[Mapping[str, Any]], expected: Mapping[str, Any]) -> list[str]:
    bad = []
    for h in run_headers:
        ep = h.get("exit_policy") or {}
        for k, v in expected.items():
            if str(ep.get(k)) != str(v):
                bad.append(f"run {h.get('run_id')}: exit_policy.{k}={ep.get(k)!r} != {v!r}")
    return bad


def stopping_gate(n_canonical: int, complete_days: Mapping[str, str]) -> dict:
    """complete_days: Taipei date -> 'weekday' | 'weekend' (only COMPLETE days)."""
    kinds = list(complete_days.values())
    met = (n_canonical >= N_MIN_CANONICAL_CANDIDATES and len(kinds) >= MIN_COMPLETE_TAIPEI_DAYS
           and "weekday" in kinds and "weekend" in kinds)
    return {"met": bool(met), "n_canonical_candidates": int(n_canonical),
            "n_min_canonical_candidates": N_MIN_CANONICAL_CANDIDATES, "complete_taipei_days": len(kinds),
            "min_complete_taipei_days": MIN_COMPLETE_TAIPEI_DAYS, "complete_weekdays": kinds.count("weekday"),
            "complete_weekend_days": kinds.count("weekend")}


# --------------------------------------------------------------- evaluation

DELTA_KEYS = {"FQO_LOWER": "d_fqo_lower", "FQO_UPPER": "d_fqo_upper", "EARLY_CANCEL_C": "d_c",
              "CHASE_B_2p4s": "d_b_2p4s", "CHASE_B_3p6s": "d_b_3p6s"}


def _deltas(rows: Sequence[Mapping[str, Any]], key: str) -> list[Optional[float]]:
    return [r.get(key) for r in rows]


def evaluate(rows: Sequence[Mapping[str, Any]], *, run_headers: Sequence[Mapping[str, Any]],
             complete_days: Mapping[str, str], params: EvaluatorParams = PREREG_LITERAL_PARAMS,
             allowed_fingerprints: Optional[Iterable[str]] = None,
             external_criteria: Optional[Mapping[str, Mapping[str, str]]] = None) -> dict:
    """rows: one per canonical candidate (decision_ts order not assumed) with keys market_slug,
    taipei_day, decision_ts, run_fingerprint, excluded (None or reason), qty_planned and the Delta
    keys in DELTA_KEYS.  Returns counts only unless the gate is met AND params are fully resolved."""
    # One execution population only (DRY_RUN vs LIVE, plumbing revision, policy fingerprint, run label),
    # enforced even when an explicit fingerprint allow-list is supplied.
    from bot.dry_run_plumbing import check_population
    check_population(run_headers)
    fingerprint = check_fingerprints([r.get("run_fingerprint") for r in rows] +
                                     [h.get("run_fingerprint") for h in run_headers], allowed=allowed_fingerprints)
    used = sorted((r for r in rows if not r.get("excluded")), key=lambda r: (r.get("decision_ts") or 0.0))
    excluded = [r for r in rows if r.get("excluded")]
    gate = stopping_gate(len(rows), complete_days)
    counts = {"run_fingerprint": fingerprint, "canonical_candidates": len(rows), "excluded": len(excluded),
              "excluded_outcome_unknown": sum(1 for r in excluded if r.get("excluded") == "OUTCOME_UNKNOWN"),
              "gate": gate, "evaluator_version": EVALUATOR_VERSION, "prereg_sha256": PREREG_SHA256}
    if not gate["met"]:
        return {"status": "GATE_NOT_MET", "counts": counts}
    unresolved = params.unresolved()
    if unresolved:
        return {"status": "REFUSED_PENDING_ADDENDUM", "unresolved_parameters": unresolved, "counts": counts}
    policy_problems = check_exit_policy(run_headers, params.expected_exit_policy or {})
    if policy_problems:
        raise FingerprintMergeRefused("runs do not match the pre-registered exit policy: " + "; ".join(policy_problems))
    tests, deltas_by = {}, {}
    for name, key in DELTA_KEYS.items():
        vals = _deltas(used, key)
        if params.fqo_unknown_bound == "exclude_and_count":
            vals = [v for v in vals if v is not None]
        deltas_by[name] = vals
        seed = int(params.seed) + list(DELTA_KEYS).index(name)
        tests[name] = sign_flip_test(vals, n_permutations=params.n_permutations, seed=seed)
    family_p = {"FQO_LOWER": tests["FQO_LOWER"]["p"], "FQO_UPPER": tests["FQO_UPPER"]["p"],
                "EARLY_CANCEL_C": tests["EARLY_CANCEL_C"]["p"],
                "CHASE_B": max(tests["CHASE_B_2p4s"]["p"], tests["CHASE_B_3p6s"]["p"])}
    assert tuple(family_p) == PRIMARY_FAMILY and len(family_p) == K
    adjusted = holm(family_p)
    sums = {n: sum(v for v in vals if v is not None) for n, vals in deltas_by.items()}
    planned = sum(float(r.get("qty_planned") or 0.0) for r in used)
    by_market = {n: {} for n in DELTA_KEYS}
    by_day = {n: {} for n in DELTA_KEYS}
    for r in used:
        for n, key in DELTA_KEYS.items():
            v = r.get(key)
            if v is None:
                continue
            by_market[n][r.get("market_slug")] = by_market[n].get(r.get("market_slug"), 0.0) + v
            by_day[n].setdefault(r.get("taipei_day"), []).append(v)
    ext = external_criteria or {}

    def robust(n):
        blocks = quartile_blocks([v for v in deltas_by[n] if v is not None])
        return {"subblocks_positive": sum(1 for b in blocks if b and sum(b) > 0),
                "leave_one_market_out_min": leave_one_out_min(by_market[n], sums[n]),
                "leave_one_day_out_min": leave_one_out_min({d: sum(v) for d, v in by_day[n].items()}, sums[n])}

    rob = {n: robust(n) for n in DELTA_KEYS}

    def c(ok):
        return PASS if ok else FAIL

    criteria: dict[str, dict[str, str]] = {}
    fqo_forced = fqo_bounds_verdict(sums["FQO_LOWER"], sums["FQO_UPPER"])
    criteria["FQO"] = {
        "lower_delta_positive": c(sums["FQO_LOWER"] > 0), "upper_delta_positive": c(sums["FQO_UPPER"] > 0),
        "subblocks_3_of_4_positive": c(min(rob["FQO_LOWER"]["subblocks_positive"],
                                           rob["FQO_UPPER"]["subblocks_positive"]) >= 3),
        "leave_one_market_out_positive": c(min(rob["FQO_LOWER"]["leave_one_market_out_min"] or 0,
                                               rob["FQO_UPPER"]["leave_one_market_out_min"] or 0) > 0),
        "markout_5s_not_worse": ext.get("FQO", {}).get("markout_5s_not_worse", NOT_EVALUATED),
        "not_single_day_driven": c(min(rob["FQO_LOWER"]["leave_one_day_out_min"] or 0,
                                       rob["FQO_UPPER"]["leave_one_day_out_min"] or 0) > 0),
        "holm_p_below_alpha_both": c(adjusted["FQO_LOWER"] < params.family_alpha
                                     and adjusted["FQO_UPPER"] < params.family_alpha)}
    day_signs = [_sign(sum(v)) for v in by_day["EARLY_CANCEL_C"].values() if sum(v) != 0]
    criteria["EARLY_CANCEL_C"] = {
        "cancelled_fills_worse_than_kept": ext.get("EARLY_CANCEL_C", {}).get("cancelled_fills_worse_than_kept",
                                                                                NOT_EVALUATED),
        "delta_positive": c(sums["EARLY_CANCEL_C"] > 0),
        "ge4_days_ge3_same_sign": c(len(day_signs) >= 4 and max(day_signs.count(1), day_signs.count(-1)) >= 3),
        "not_single_market_or_day": c((rob["EARLY_CANCEL_C"]["leave_one_market_out_min"] or 0) > 0
                                      and (rob["EARLY_CANCEL_C"]["leave_one_day_out_min"] or 0) > 0),
        "holm_p_below_alpha": c(adjusted["EARLY_CANCEL_C"] < params.family_alpha)}
    criteria["CHASE_B"] = {
        "delta_positive_both_latencies": c(sums["CHASE_B_2p4s"] > 0 and sums["CHASE_B_3p6s"] > 0),
        "recovered_winners_gt_added_losers": ext.get("CHASE_B", {}).get("recovered_winners_gt_added_losers",
                                                                         NOT_EVALUATED),
        "markout_5s_deterioration_le_1c": ext.get("CHASE_B", {}).get("markout_5s_deterioration_le_1c", NOT_EVALUATED),
        "not_single_day_driven": c(min(rob["CHASE_B_2p4s"]["leave_one_day_out_min"] or 0,
                                       rob["CHASE_B_3p6s"]["leave_one_day_out_min"] or 0) > 0),
        "holm_p_below_alpha": c(adjusted["CHASE_B"] < params.family_alpha)}

    def verdict(name):
        if name == "FQO" and fqo_forced:
            return fqo_forced
        return PROMISING if all(v == PASS for v in criteria[name].values()) else UNRESOLVED

    b = int(params.bootstrap_b)
    intervals = {n: {"market_90": bootstrap_interval(deltas_by[n], b=b, seed=int(params.seed)),
                     "block4_90": bootstrap_interval(deltas_by[n], b=b, seed=int(params.seed), block=params.block_size),
                     "day_cluster_90": day_cluster_interval(by_day[n], b=b, seed=int(params.seed))}
                 for n in DELTA_KEYS}
    informative = {n: informative_n(deltas_by[n]) for n in DELTA_KEYS}
    return {
        "status": "EVALUATED", "counts": counts, "family": list(PRIMARY_FAMILY), "family_alpha": params.family_alpha,
        "tests": tests, "family_p_raw": family_p, "family_p_holm": adjusted,
        "delta_sum": sums, "delta_ev_per_planned_share": {n: (s / planned if planned else None) for n, s in sums.items()},
        "robustness": rob, "criteria": criteria,
        "verdicts": {"FQO": verdict("FQO"), "EARLY_CANCEL_C": verdict("EARLY_CANCEL_C"), "CHASE_B": verdict("CHASE_B"),
                     "D": "ANNOTATION_ONLY"},
        "intervals_90": intervals,
        "informative_n_report_only": informative,
        "informative_n_warnings": sorted(n for n, v in informative.items() if v < INFORMATIVE_N_WARN),
        "params": {k: v for k, v in params.__dict__.items()},
    }


def approved_params(addendum_sha256: str, **overrides: Any) -> EvaluatorParams:
    """Only for use AFTER CK approval of the addendum file with this hash."""
    return replace(EvaluatorParams(**PROPOSED_ADDENDUM_VALUES), addendum_sha256=addendum_sha256, approved_by_ck=True,
                   **overrides)

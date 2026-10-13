"""Pre-registered forward evaluator self-tests (synthetic data only; no outcomes, no network)."""
import ast
import json
import random
from pathlib import Path

import pytest

import bot.research.prereg_evaluator as ev
from bot.research.candidate_labels import build_label_rows
from bot.research.forward_monitor import OutcomeDataRefused, monitor
from bot.research_startup_guard import PINNED_PREREG_SHA256, PREREG_EVALUATOR_VERSION

ROOT = Path(__file__).resolve().parents[1]
FP = "fp_research_v1"


def test_constants_match_prereg_and_guard():
    assert ev.K == 4 and ev.PRIMARY_FAMILY == ("FQO_LOWER", "FQO_UPPER", "EARLY_CANCEL_C", "CHASE_B")
    assert ev.FAMILY_ALPHA == 0.10 and ev.N_PERMUTATIONS == 10_000
    assert ev.N_MIN_CANONICAL_CANDIDATES == 672 and ev.MIN_COMPLETE_TAIPEI_DAYS == 7 and ev.BLOCK_SIZE == 4
    assert ev.PREREG_SHA256 == PINNED_PREREG_SHA256 and ev.EVALUATOR_VERSION == PREREG_EVALUATOR_VERSION


def test_seed_reproducible_and_seed_sensitive():
    rng = random.Random(1)
    d = [rng.gauss(0.0, 1) for _ in range(80)]
    a = ev.sign_flip_test(d, n_permutations=2000, seed=11)
    assert a == ev.sign_flip_test(d, n_permutations=2000, seed=11)
    assert a["seed"] == 11 and a["n_permutations"] == 2000 and a["method"] == "monte_carlo"
    assert a["p"] != ev.sign_flip_test(d, n_permutations=2000, seed=12)["p"]


def test_exact_enumeration_small_sample():
    assert ev.sign_flip_test([1.0, 1.0, 1.0], n_permutations=0, seed=0, exact=True)["p"] == 1 / 8
    assert ev.sign_flip_test([1.0, -1.0], n_permutations=0, seed=0, exact=True)["p"] == 3 / 4
    assert ev.sign_flip_test([0.0, 0.0], n_permutations=100, seed=0)["p"] == 1.0  # all-zero: never significant
    # zero deltas do not change the statistic or its null distribution
    assert ev.sign_flip_test([1.0, 1.0, 1.0, 0.0, 0.0], n_permutations=0, seed=0, exact=True)["p"] == 1 / 8


def test_monte_carlo_matches_exact():
    d = [0.9, 0.4, -0.2, 1.1, 0.3, -0.5, 0.8, 0.2, 0.6, -0.1]
    exact = ev.sign_flip_test(d, n_permutations=0, seed=0, exact=True)["p"]
    mc = ev.sign_flip_test(d, n_permutations=20000, seed=3)["p"]
    assert abs(exact - mc) < 0.01


def test_holm_known_example():
    adj = ev.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005})
    assert adj == pytest.approx({"d": 0.02, "a": 0.03, "c": 0.06, "b": 0.06})
    assert ev.holm({"x": 0.5, "y": 0.6}) == pytest.approx({"x": 1.0, "y": 1.0})


def test_type_one_error_near_nominal_under_symmetric_null():
    rng = random.Random(2026)
    rejections, reps = 0, 300
    for r in range(reps):
        d = [rng.choice((-1, 1)) * rng.expovariate(1.0) for _ in range(40)]  # symmetric about 0
        rejections += ev.sign_flip_test(d, n_permutations=300, seed=r)["p"] < 0.10
    assert 0.05 <= rejections / reps <= 0.15


def test_power_with_planted_effect():
    rng = random.Random(7)
    rejections, reps = 0, 100
    for r in range(reps):
        d = [rng.gauss(0.5, 1.0) for _ in range(40)]
        rejections += ev.sign_flip_test(d, n_permutations=300, seed=r)["p"] < 0.10
    assert rejections / reps >= 0.85


def test_fqo_opposite_bound_signs_force_unresolved():
    assert ev.fqo_bounds_verdict(5.0, -0.1) == ev.UNRESOLVED
    assert ev.fqo_bounds_verdict(-5.0, 0.1) == ev.UNRESOLVED
    assert ev.fqo_bounds_verdict(0.0, 3.0) == ev.UNRESOLVED
    assert ev.fqo_bounds_verdict(None, 3.0) == ev.UNRESOLVED
    assert ev.fqo_bounds_verdict(2.0, 3.0) is None


def _days(n_weekday=5, n_weekend=2):
    out = {f"2026-10-{12 + i:02d}": "weekday" for i in range(n_weekday)}
    out.update({f"2026-10-{20 + i:02d}": "weekend" for i in range(n_weekend)})
    return out


def _rows(n=700, *, lower=0.2, upper=0.2, c=0.1, b=0.1, seed=5):
    rng = random.Random(seed)
    days = list(_days())
    rows = []
    for i in range(n):
        rows.append({"candidate_id": f"c{i}", "market_slug": f"m{i}", "taipei_day": days[i % len(days)],
                     "decision_ts": 1000.0 + i, "run_fingerprint": FP, "excluded": None, "qty_planned": 5.5,
                     "d_fqo_lower": rng.gauss(lower, 1.0), "d_fqo_upper": rng.gauss(upper, 1.0),
                     "d_c": rng.gauss(c, 1.0), "d_b_2p4s": rng.gauss(b, 1.0), "d_b_3p6s": rng.gauss(b, 1.0)})
    return rows


HEADERS = [{"run_id": "r1", "run_fingerprint": FP, "exit_policy": dict(ev.PROPOSED_ADDENDUM_VALUES["expected_exit_policy"])}]
APPROVED = ev.approved_params("0" * 64, n_permutations=500, bootstrap_b=200)


def test_gate_not_met_outputs_counts_only():
    for rows, days in ((_rows(671), _days()), (_rows(700), _days(6, 0)), (_rows(700), _days(4, 2))):
        out = ev.evaluate(rows, run_headers=HEADERS, complete_days=days, params=APPROVED)
        assert out["status"] == "GATE_NOT_MET"
        assert set(out) == {"status", "counts"}
        flat = json.dumps(out)
        for forbidden in ("\"p\"", "delta_sum", "family_p", "verdict", "intervals"):
            assert forbidden not in flat


def test_literal_prereg_params_refuse_inference_until_addendum_approved():
    out = ev.evaluate(_rows(), run_headers=HEADERS, complete_days=_days())
    assert out["status"] == "REFUSED_PENDING_ADDENDUM"
    assert "seed" in out["unresolved_parameters"] and "addendum_not_approved_by_ck" in out["unresolved_parameters"]
    assert "family_p_raw" not in out
    unapproved = ev.EvaluatorParams(**ev.PROPOSED_ADDENDUM_VALUES)  # values present but not approved
    assert ev.evaluate(_rows(), run_headers=HEADERS, complete_days=_days(), params=unapproved)["status"] == \
        "REFUSED_PENDING_ADDENDUM"


def test_full_evaluation_k4_holm_and_fqo_rule():
    out = ev.evaluate(_rows(lower=0.3, upper=0.3), run_headers=HEADERS, complete_days=_days(), params=APPROVED)
    assert out["status"] == "EVALUATED" and out["family"] == list(ev.PRIMARY_FAMILY)
    assert set(out["family_p_holm"]) == set(ev.PRIMARY_FAMILY) and len(out["family_p_holm"]) == 4
    assert out["family_p_holm"] == pytest.approx(ev.holm(out["family_p_raw"]))
    assert out["family_p_raw"]["CHASE_B"] == max(out["tests"]["CHASE_B_2p4s"]["p"], out["tests"]["CHASE_B_3p6s"]["p"])
    assert out["verdicts"]["D"] == "ANNOTATION_ONLY"
    # markout criterion not supplied -> cannot be PROMISING
    assert out["verdicts"]["FQO"] == ev.UNRESOLVED
    assert out["criteria"]["FQO"]["markout_5s_not_worse"] == ev.NOT_EVALUATED
    strong = ev.evaluate(_rows(lower=0.3, upper=-0.3), run_headers=HEADERS, complete_days=_days(), params=APPROVED,
                         external_criteria={"FQO": {"markout_5s_not_worse": ev.PASS}})
    assert strong["delta_sum"]["FQO_LOWER"] > 0 > strong["delta_sum"]["FQO_UPPER"]
    assert strong["verdicts"]["FQO"] == ev.UNRESOLVED
    assert "informative_n_report_only" in out and out["informative_n_report_only"]["FQO_LOWER"] == 700


def test_fqo_promising_requires_every_criterion():
    out = ev.evaluate(_rows(lower=0.4, upper=0.4, seed=9), run_headers=HEADERS, complete_days=_days(), params=APPROVED,
                      external_criteria={"FQO": {"markout_5s_not_worse": ev.PASS}})
    assert all(v == ev.PASS for v in out["criteria"]["FQO"].values())
    assert out["verdicts"]["FQO"] == ev.PROMISING


def test_fingerprint_merge_refused_without_explicit_instruction():
    rows = _rows()
    rows[0] = {**rows[0], "run_fingerprint": "fp_other"}
    with pytest.raises(ev.FingerprintMergeRefused):
        ev.evaluate(rows, run_headers=HEADERS, complete_days=_days(), params=APPROVED)
    out = ev.evaluate(rows, run_headers=HEADERS, complete_days=_days(), params=APPROVED,
                      allowed_fingerprints=[FP, "fp_other"])
    assert out["status"] == "EVALUATED"


def test_runs_with_other_exit_policy_are_refused():
    bad = [{"run_id": "r9", "run_fingerprint": FP,
            "exit_policy": {**HEADERS[0]["exit_policy"], "catastrophic_breaker_enabled": True}}]
    with pytest.raises(ev.FingerprintMergeRefused):
        ev.evaluate(_rows(), run_headers=bad, complete_days=_days(), params=APPROVED)


def test_informative_n_and_quartiles():
    assert ev.informative_n([0.0, None, 1.0, -2.0]) == 2
    assert [len(b) for b in ev.quartile_blocks(list(range(10)))] == [3, 3, 2, 2]


# ------------------------------------------------- outcome isolation (prereg section 9)

LINKER_MODULES = {"bot.research.candidate_labels", "bot.research.prereg_evaluator", "bot.execution_selection_eval"}


def _import_closure(path: Path, seen=None) -> set:
    seen = set() if seen is None else seen
    tree = ast.parse(path.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for m in mods:
        if m in seen or not m.startswith("bot"):
            continue
        seen.add(m)
        f = ROOT / (m.replace(".", "/") + ".py")
        if f.exists():
            _import_closure(f, seen)
    return seen


@pytest.mark.parametrize("tool", ["bot/research/forward_monitor.py", "scripts/check_candidate_telemetry.py"])
def test_monitoring_tools_cannot_import_outcome_linker(tool):
    closure = _import_closure(ROOT / tool)
    assert not closure & LINKER_MODULES, closure & LINKER_MODULES
    src = (ROOT / tool).read_text()
    assert "official_outcome\"" not in src.replace("_LABEL_TOKENS = (\"official_outcome\"", "")


def test_monitor_refuses_label_records(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_text(json.dumps({"record_type": "LABEL", "candidate_id": "c1", "official_outcome": "UP"}) + "\n")
    with pytest.raises(OutcomeDataRefused):
        monitor([p])
    q = tmp_path / "cand.jsonl"
    q.write_text(json.dumps({"record_type": "CANDIDATE", "candidate_id": "c1", "decision_ts": 1.0, "market_slug": "m",
                             "eligibility": "ACCEPTED", "client_order_id": "x"}) + "\n")
    assert monitor([q])["candidates"] == 1


def test_label_linker_never_mutates_snapshots_and_snapshot_cannot_see_labels():
    cands = [{"candidate_id": "c1", "market_slug": "m1", "candidate_side": "UP", "executable_ask": 0.6,
              "proposed_maker_price": 0.58}]
    before = json.dumps(cands, sort_keys=True)
    rows = build_label_rows(cands, {"m1": {"outcome": "UP", "final_strike": 1.0}},
                            current_fills={"c1": {"filled": True, "fill_price": 0.58, "filled_qty": 5.5}})
    assert json.dumps(cands, sort_keys=True) == before
    assert rows[0]["hold_y"] == 1 and rows[0]["payoff_ps_first_maker"] == pytest.approx(0.42)
    assert build_label_rows(cands, {})[0]["official_outcome"] == "UNKNOWN"
    assert not _import_closure(ROOT / "bot/candidate_telemetry.py") & LINKER_MODULES

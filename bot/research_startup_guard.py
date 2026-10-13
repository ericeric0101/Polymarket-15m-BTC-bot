"""Fail-closed startup guard for the Entry V3 forward research profile.

Research only; no order authority.  When ``STRATEGY_PROFILE`` is the research
profile, startup must prove -- before any order-capable client or shadow writer
exists -- that the run cannot SELL for strategy reasons:

* every key that can enable a strategy-level conditional SELL (exit path
  inventory, reports/forward_profile_cleanup_20261013/exit_path_inventory.csv)
  resolves to its research value, parsed STRICTLY from the raw environment
  string (an empty or unparseable value is a failure, never a default), AND the
  effective ``AppConfig`` field agrees;
* TP is ON at exactly 0.97;
* candidate telemetry, FQO, held-side markout and the frozen C/B/D shadows are ON;
* the tracked git tree is clean and git is available;
* every runtime data path resolves inside the integration worktree (or an
  explicit ``RESEARCH_DATA_ROOT``) and never inside the frozen LIVE checkout's
  ``logs/``, ``data/`` or ``backups/``.

Profile identity is checked for every profile: research telemetry flags ON under
any other (or an unset) profile is a failure.  There is no bypass flag.

``check_instantiated_strategy`` repeats the exit checks on the objects the
strategy actually uses (attributes and the built ``ExitPolicyEngine``), including
a behavioural probe: a deep, thesis-broken loss must not yield a SELL decision.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

RESEARCH_PROFILE = "btc15_twap_v3_entry_v3_forward"
GUARD_VERSION = "research_startup_guard_v1"
# reports/forward_shadow_readiness_20261013/prereg_final.md (CK must re-confirm).
PINNED_PREREG_SHA256 = "e4179a844600a80ef151efdbbce94daad7c3570a551981ddc9668e9ef5d8e123"
PREREG_EVALUATOR_VERSION = "prereg_evaluator_v1"
TP_PRICE = Decimal("0.97")
TP_PRICE_KEY = "TAIL_PROTECT_TP_PRICE"
RESEARCH_DATA_ROOT_KEY = "RESEARCH_DATA_ROOT"

# key -> (research value, AppConfig attribute path, strategy attribute)
STRATEGY_EXIT_CONTROLS: dict[str, tuple[bool, str, str]] = {
    "ABSOLUTE_MAX_LOSS_ENABLED": (False, "exit.absolute_max_loss_enabled", "absolute_max_loss_enabled"),
    "CATASTROPHIC_STOP_LOSS_ENABLED": (False, "exit.catastrophic_stop_loss_enabled", "catastrophic_stop_loss_enabled"),
    "ENDGAME_TWAP_EXIT_ENABLED": (False, "exit.endgame_twap_exit_enabled", "endgame_twap_exit_enabled"),
    "RECOVERY_EXIT_ENABLED": (False, "exit.taker_exit_enabled", "taker_exit_enabled"),
    "TAKER_EXIT_ONLY_AFTER_INVALIDATION": (False, "exit.taker_exit_only_after_invalidation",
                                           "taker_exit_only_after_invalidation"),
    "RECOVERY_EXIT_LADDER_ENABLED": (False, "exit.recovery_exit_ladder_enabled", "recovery_exit_ladder_enabled"),
    "MAKER_URGENT_EXIT_ENABLED": (False, "exit.maker_urgent_exit_enabled", "maker_urgent_exit_enabled"),
    "STOP_LOSS_ENABLED": (False, "exit.stop_loss_enabled", "stop_loss_enabled"),
    # Blocks every non-TP maker SELL quote while inventory is held.
    "HOLD_TO_REDEEM": (True, "exit.hold_to_redeem_enabled", "hold_to_redeem_enabled"),
}
TP_CONTROLS: dict[str, tuple[bool, str, str]] = {
    "TAIL_PROTECT_TP_ENABLED": (True, "exit.tail_protect_tp_enabled", "tail_protect_tp_enabled"),
}
# Research-only telemetry: ON iff the research profile is active.
RESEARCH_IDENTITY_FLAGS: dict[str, str] = {
    "CANDIDATE_DECISION_TELEMETRY_ENABLED": "operations.candidate_decision_telemetry_enabled",
    "FIRST_QUOTE_ONLY_SHADOW_ENABLED": "operations.first_quote_only_shadow_enabled",
    "HELD_SIDE_MARKOUT_TELEMETRY_ENABLED": "operations.held_side_markout_telemetry_enabled",
}
# Frozen E5B v1 shadows (C, B primary comparators; D annotation only).
CBD_SHADOW_FLAGS: dict[str, str] = {
    "EARLY_CANCEL_SHADOW_ENABLED": "operations.early_cancel_shadow_enabled",
    "MAKER_CHASE_SHADOW_ENABLED": "operations.maker_chase_shadow_enabled",
    "PRICE_ADJUSTED_EDGE_SHADOW_ENABLED": "operations.price_adjusted_edge_shadow_enabled",
}
# Exit/SELL-adjacent boolean keys reviewed in S1 that cannot by themselves enable
# a strategy SELL once the controls above hold.  A new exit-like boolean key that
# is in neither table fails tests/test_research_startup_guard.py (coverage test).
REVIEWED_NON_SELL_ENABLING_KEYS: dict[str, str] = {
    "ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED": "Breaker V2 gate: can only suppress the (OFF) conditional breaker",
    "AUTO_REDEEM_APPLY": "settlement redeem (not a SELL order)",
    "AUTO_REDEEM_ENABLED": "settlement redeem (not a SELL order)",
    "AUTO_REDEEM_ON_ROLLOVER": "settlement redeem (not a SELL order)",
    "DEPTH_RISK_SHADOW_ENABLED": "shadow telemetry, no order authority",
    "DEPTH_RISK_SIZING_ENABLED": "BUY sizing only",
    "EXIT_HOLD_BAND_REQUIRES_LOCKED": "modifier of adaptive stop (STOP_LOSS_ENABLED=0)",
    "EXIT_STOP_LOSS_HOLD_ON_NONE_SIGNAL": "modifier of adaptive stop (STOP_LOSS_ENABLED=0)",
    "EXIT_STOP_LOSS_REQUIRES_THESIS_WEAKENING": "modifier of adaptive stop (STOP_LOSS_ENABLED=0)",
    "MAKER_EARLY_PROFIT_HOLD_ENABLED": "hold modifier; maker SELLs blocked by HOLD_TO_REDEEM=1",
    "MAKER_HIGH_COST_EXIT_COOLDOWN_ENABLED": "only blocks SELLs",
    "MAKER_KILL_SWITCH_RESET_ON_ROLLOVER": "engineering safety (kill switch never originates a SELL)",
    "MAKER_PROFITABLE_SELL_CAP_ENABLED": "maker SELL price cap; maker SELLs blocked by HOLD_TO_REDEEM=1",
    "MAKER_PROFIT_RUN_ENABLED": "hold modifier; maker SELLs blocked by HOLD_TO_REDEEM=1",
    "MAKER_SELL_COST_PROTECT_ENABLED": "only blocks SELLs",
    "RECOVERY_EXIT_REQUIRE_TWAP_CONFIRMATION": "modifier of invalidation recovery (RECOVERY_EXIT_ENABLED=0)",
    "SESSION_HARD_PROFIT_LOCK_ENABLED": "stop-new-entry (BUY lock only)",
    "SESSION_MAX_LOSS_ENABLED": "stop-new-entry (BUY lock only)",
    "TAKER_EXIT_ONLY_ON_PROFIT": "modifier of taker exits (all OFF)",
    "TAKER_EXIT_REQUIRE_INVENTORY": "inventory guard (only blocks)",
    "TRAPPED_INVENTORY_RECOVERY_ENABLED": "BUY top-up below the exchange minimum, never a SELL",
}

_STRICT_TRUE = frozenset({"1", "true", "yes", "on"})
_STRICT_FALSE = frozenset({"0", "false", "no", "off"})


class ResearchStartupAssertionError(RuntimeError):
    """Startup refused (fail closed).  The message never contains secret values."""


def asserted_exit_keys() -> frozenset[str]:
    return frozenset(STRATEGY_EXIT_CONTROLS) | frozenset(TP_CONTROLS) | {TP_PRICE_KEY}


def strict_bool(raw: Optional[str]) -> Optional[bool]:
    """True/False only for an explicit token; None (failure) for unset, empty or anything else."""
    if raw is None:
        return None
    token = raw.lower()
    if token in _STRICT_TRUE:
        return True
    if token in _STRICT_FALSE:
        return False
    return None


def strict_tp_price(raw: Optional[str]) -> Optional[Decimal]:
    """Exact decimal with no surrounding whitespace; None on any parsing doubt."""
    if raw is None or raw != raw.strip() or not raw:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _attr(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _violation(key: str, expected: Any, effective: Any, source: str, detail: str = "") -> dict[str, Any]:
    return {"key": key, "expected": str(expected), "effective": str(effective), "source": source,
            "detail": detail}


def _source_of(key: str, sources: Optional[Mapping[str, str]]) -> str:
    if sources is None:
        return "UNKNOWN"
    return sources.get(key, "DEFAULT")


def effective_profile_name(environ: Mapping[str, str], loaded_profile: Optional[str] = None) -> str:
    from bot.runtime_env import DEFAULT_PROFILE
    return str(loaded_profile or environ.get("STRATEGY_PROFILE") or DEFAULT_PROFILE)


def check_profile_identity(app_config: Any, profile_name: str) -> list[dict[str, Any]]:
    """Research telemetry ON under a non-research (or unset) profile is a failure, and vice versa."""
    if profile_name == RESEARCH_PROFILE:
        return []
    out = []
    for key, path in RESEARCH_IDENTITY_FLAGS.items():
        if bool(_attr(app_config, path)):
            out.append(_violation(key, f"OFF unless STRATEGY_PROFILE={RESEARCH_PROFILE}", "ON", "n/a",
                                  f"research telemetry enabled under STRATEGY_PROFILE={profile_name!r}"))
    return out


def check_research_config(app_config: Any, environ: Mapping[str, str],
                          sources: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    """Raw-string AND effective-config checks for every asserted key."""
    out: list[dict[str, Any]] = []
    for table in (STRATEGY_EXIT_CONTROLS, TP_CONTROLS):
        for key, (expected, cfg_path, _attr_name) in table.items():
            raw = environ.get(key)
            parsed = strict_bool(raw)
            effective = _attr(app_config, cfg_path)
            src = _source_of(key, sources)
            if parsed is None:
                out.append(_violation(key, int(expected), "UNSET" if raw is None else repr(raw), src,
                                      "value must be an explicit 0/1/true/false/yes/no/on/off"))
            elif parsed is not expected:
                out.append(_violation(key, int(expected), int(parsed), src))
            elif bool(effective) is not expected:
                out.append(_violation(key, int(expected), f"config={effective!r}", src,
                                      "parsed value and effective AppConfig disagree"))
    raw_price = environ.get(TP_PRICE_KEY)
    price = strict_tp_price(raw_price)
    effective_price = _attr(app_config, "exit.tail_protect_tp_price")
    if price is None or price != TP_PRICE:
        out.append(_violation(TP_PRICE_KEY, TP_PRICE, "UNSET" if raw_price is None else repr(raw_price),
                              _source_of(TP_PRICE_KEY, sources), "must be exactly 0.97 with no whitespace"))
    elif effective_price is None or Decimal(str(effective_price)) != TP_PRICE:
        out.append(_violation(TP_PRICE_KEY, TP_PRICE, f"config={effective_price!r}", _source_of(TP_PRICE_KEY, sources),
                              "parsed value and effective AppConfig disagree"))
    for key, cfg_path in {**RESEARCH_IDENTITY_FLAGS, **CBD_SHADOW_FLAGS}.items():
        raw = environ.get(key)
        parsed = strict_bool(raw)
        if parsed is not True or not bool(_attr(app_config, cfg_path)):
            out.append(_violation(key, 1, "UNSET" if raw is None else repr(raw) if parsed is None else int(parsed),
                                  _source_of(key, sources), "research telemetry / frozen shadow must be ON"))
    return out


# --------------------------------------------------------------------------- git

def _run_git(root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_state(root: Path, runner: Callable[..., Optional[str]] = _run_git) -> dict[str, Any]:
    revision = runner(root, "rev-parse", "HEAD")
    status = runner(root, "status", "--porcelain", "--untracked-files=no")
    common = runner(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if revision is None or status is None:
        return {"available": False, "revision": None, "dirty": None, "dirty_diff_sha256": None,
                "live_checkout_root": None}
    dirty = bool(status.strip())
    diff_sha = None
    if dirty:
        diff = runner(root, "diff", "HEAD") or ""
        diff_sha = hashlib.sha256((status + "\n" + diff).encode()).hexdigest()
    live_root = str(Path(common.strip()).parent) if common and common.strip() else None
    if live_root is not None and Path(live_root) == Path(live_root).parent:
        live_root = None  # filesystem root: not a credible checkout
    return {"available": True, "revision": revision.strip(), "dirty": dirty, "dirty_diff_sha256": diff_sha,
            "live_checkout_root": live_root}


def check_git(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not state.get("available"):
        return [_violation("GIT", "available + clean tracked tree", "UNAVAILABLE", "n/a")]
    if state.get("dirty"):
        return [_violation("GIT", "clean tracked tree", "DIRTY", "n/a",
                           f"dirty_diff_sha256={state.get('dirty_diff_sha256')}")]
    return []


# --------------------------------------------------------------------- data paths

def _anchor(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    return Path(os.path.normpath(p if p.is_absolute() else base / p))


def resolve_runtime_data_paths(environ: Mapping[str, str], *, project_root: Path, cwd: Path) -> dict[str, Path]:
    """Every runtime write location, with the anchor the code actually uses."""
    from bot.journal_path import DEFAULT_TRADE_DB_PATH
    paths = {
        # bot.journal_path.anchor_repo_path: repo-root anchored.
        "TRADE_DB_PATH": _anchor(str(environ.get("TRADE_DB_PATH") or "").strip() or DEFAULT_TRADE_DB_PATH, project_root),
        # bot.settings.build_twap_research_db / BTC1sHistoryCollector / rebate: cwd-relative.
        "TWAP_RESEARCH_DB_PATH": _anchor(environ.get("TWAP_RESEARCH_DB_PATH") or "data/research/twap_forward_shadow.db",
                                         cwd),
        "BTC_1S_HISTORY_DIR": _anchor(environ.get("BTC_1S_HISTORY_DIR") or "data/btc_history_1s", cwd),
        "REBATE_REPORT_DIR": _anchor(environ.get("REBATE_REPORT_DIR") or "./logs/rebate", cwd),
        "TERMINAL_LOG_DIR": _anchor("logs/bot", cwd),
        # bot.candidate_telemetry.build_candidate_telemetry / launcher / research_maintenance: repo root.
        "CANDIDATE_TELEMETRY_DIR": _anchor(str(environ.get("CANDIDATE_TELEMETRY_DIR") or "").strip()
                                           or "data/research/candidate_telemetry", project_root),
        "RESEARCH_PARTITIONS_DIR": project_root / "data/research_partitions",
        "PROJECT_LOG_DIR": project_root / "logs",
    }
    offsite = str(environ.get("RESEARCH_OFFSITE_DIR") or "").strip()
    if offsite:
        paths["RESEARCH_OFFSITE_DIR"] = _anchor(offsite, cwd)
    return paths


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_data_paths(paths: Mapping[str, Path], *, project_root: Path, live_checkout_root: Optional[str],
                     research_data_root: Optional[str] = None) -> list[dict[str, Any]]:
    allowed = [project_root]
    if research_data_root:
        allowed.append(Path(research_data_root).expanduser())
    forbidden: list[Path] = []
    if live_checkout_root:
        live = Path(live_checkout_root)
        forbidden = [live / "logs", live / "data", live / "backups", live / "reports"]
    else:
        return [_violation("DATA_PATHS", "LIVE checkout root known", "UNKNOWN", "n/a",
                           "cannot prove isolation from the frozen LIVE dataset")]
    out = []
    for root in allowed[1:]:
        if any(_inside(root, bad) for bad in forbidden):
            out.append(_violation(RESEARCH_DATA_ROOT_KEY, "outside the frozen LIVE data directories", str(root), "n/a"))
    for name, path in paths.items():
        if any(_inside(path, bad) for bad in forbidden):
            out.append(_violation(name, "outside LIVE logs/data/backups/reports", str(path), "n/a",
                                  "would write into the frozen LIVE dataset"))
        elif not any(_inside(path, root) for root in allowed):
            out.append(_violation(name, f"inside {project_root} or {RESEARCH_DATA_ROOT_KEY}", str(path), "n/a"))
    return out


# -------------------------------------------------------------------- fingerprint

def _file_sha12(rel: str) -> Optional[str]:
    try:
        return hashlib.sha256((Path(__file__).resolve().parent / rel).read_bytes()).hexdigest()[:12]
    except Exception:
        return None


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


def build_policy_fingerprint(app_config: Any, *, profile_name: str, git: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic (no run id, no clock): equal for every restart of the same policy."""
    from bot.candidate_telemetry import SCHEMA_VERSION as CANDIDATE_SCHEMA
    from bot.dry_run_plumbing import PLUMBING_REVISION
    from bot.entry_shadow import SCHEMA_VERSION as E5B_SCHEMA
    from bot.entry_sizing import SIZING_RULE_VERSION
    side = _jsonable(getattr(app_config, "side", None))
    maker = _jsonable(getattr(app_config, "maker", None))
    exit_cfg = getattr(app_config, "exit", None)
    body = {
        "guard_version": GUARD_VERSION,
        "git_revision": git.get("revision"),
        "git_dirty": git.get("dirty"),
        "strategy_profile": profile_name,
        "entry_policy_sha256": hashlib.sha256(json.dumps({"side": side, "maker": maker}, sort_keys=True,
                                                         default=str).encode()).hexdigest(),
        "sizing": {"rule_version": SIZING_RULE_VERSION,
                   "rule": json.loads(json.dumps(_jsonable(getattr(getattr(app_config, "maker", None),
                                                                   "entry_sizing_rule", None)), default=str))},
        "tp": {"enabled": bool(getattr(exit_cfg, "tail_protect_tp_enabled", False)),
               "price": str(getattr(exit_cfg, "tail_protect_tp_price", "")),
               "fraction": str(getattr(exit_cfg, "tail_protect_tp_fraction", "")),
               "min_entry_price": str(getattr(exit_cfg, "tail_protect_tp_min_entry_price", ""))},
        "strategy_exits": {key: bool(_attr(app_config, path)) for key, (_e, path, _a) in STRATEGY_EXIT_CONTROLS.items()},
        "shadow_rules": {"C": "EC1@15s_v1", "B": "CH_B@10s_v1", "D": "edge_cal_v1", "FQO": "FQO_v1",
                         "e5b_schema": E5B_SCHEMA, "entry_shadow_sha12": _file_sha12("entry_shadow.py"),
                         "execution_selection_eval_sha12": _file_sha12("execution_selection_eval.py")},
        "telemetry_schema": CANDIDATE_SCHEMA,
        # Measurement plumbing revision: data before/after the DRY-RUN plumbing fix never share a fingerprint.
        "plumbing_revision": PLUMBING_REVISION,
        "strike_finality_sha12": _file_sha12("strike_finality.py"),
        "prereg_sha256": PINNED_PREREG_SHA256,
        "evaluator_version": PREREG_EVALUATOR_VERSION,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return {**body, "policy_fingerprint_sha256": digest}


def startup_banner(fingerprint: Mapping[str, Any]) -> str:
    exits = " ".join(f"{k}={int(v)}" for k, v in sorted(fingerprint.get("strategy_exits", {}).items()))
    tp = fingerprint.get("tp", {})
    return (f"RESEARCH STARTUP GUARD PASS profile={fingerprint.get('strategy_profile')} "
            f"policy_fingerprint={str(fingerprint.get('policy_fingerprint_sha256'))[:16]} "
            f"git={str(fingerprint.get('git_revision'))[:12]} dirty={int(bool(fingerprint.get('git_dirty')))} "
            f"tp_enabled={int(bool(tp.get('enabled')))} tp_price={tp.get('price')} {exits} "
            f"breaker_v2_effective_for_conditional_exit=0 prereg={str(fingerprint.get('prereg_sha256'))[:8]}")


def _raise(violations: list[dict[str, Any]], stage: str) -> None:
    lines = [f"  - {v['key']}: expected={v['expected']} effective={v['effective']} source={v['source']}"
             + (f" ({v['detail']})" if v.get("detail") else "") for v in violations]
    raise ResearchStartupAssertionError(
        f"Research startup guard FAILED CLOSED at {stage} (STRATEGY_PROFILE={RESEARCH_PROFILE}); "
        f"startup aborted:\n" + "\n".join(lines))


def enforce_startup_guard(app_config: Any, *, environ: Optional[Mapping[str, str]] = None,
                          sources: Optional[Mapping[str, str]] = None, loaded_profile: Optional[str] = None,
                          project_root: Optional[Path] = None, cwd: Optional[Path] = None,
                          git_runner: Callable[..., Optional[str]] = _run_git) -> Optional[dict[str, Any]]:
    """Launcher-level gate.  Returns the policy fingerprint for the research profile, None otherwise."""
    from bot.runtime_env import LAST_PROCESS_ENV_SOURCES, LAST_PROCESS_PROFILE, PROJECT_ROOT
    env = os.environ if environ is None else environ
    if sources is None and environ is None:
        sources = dict(LAST_PROCESS_ENV_SOURCES)
    profile = effective_profile_name(env, loaded_profile or (LAST_PROCESS_PROFILE.get("name") if environ is None else None))
    identity = check_profile_identity(app_config, profile)
    if identity:
        _raise(identity, "profile_identity")
    if profile != RESEARCH_PROFILE:
        return None
    root = Path(project_root or PROJECT_ROOT).resolve()
    git = git_state(root, git_runner)
    violations = check_research_config(app_config, env, sources)
    violations += check_git(git)
    paths = resolve_runtime_data_paths(env, project_root=root, cwd=Path(cwd or os.getcwd()))
    violations += check_data_paths(paths, project_root=root, live_checkout_root=git.get("live_checkout_root"),
                                   research_data_root=str(env.get(RESEARCH_DATA_ROOT_KEY) or "").strip() or None)
    if violations:
        _raise(violations, "launcher")
    return build_policy_fingerprint(app_config, profile_name=profile, git=git)


# ----------------------------------------------------------- instantiated objects

def probe_exit_engine(engine: Any) -> Optional[str]:
    """Behavioural probe: a deep, thesis-broken, locked-invalidated loss must not SELL."""
    from bot.kill_switch import HARD_PROTECTIVE_EXIT_REASONS
    from bot.models import ExitDecisionType, MarketSnapshot, PositionState, SignalDecision
    from execution.exit_policy import ExitStage
    snapshot = MarketSnapshot(instrument_id="probe", phase="ACTIVE", time_left_sec=300.0, best_bid=Decimal("0.10"),
                              best_ask=Decimal("0.11"), fee_rate=Decimal("0"), spread=Decimal("0.01"),
                              spread_pct=Decimal("0.095"), slippage_buffer_pct=Decimal("0"),
                              exit_stage=ExitStage.TAKER, in_reduce_only_tail=False,
                              stop_loss_disabled_in_tail=False, fair=Decimal("0.05"), fair_edge_ps=None,
                              spot_minus_strike_bps=None)
    position = PositionState(instrument_id="probe", qty=Decimal("10"), sellable_qty=Decimal("10"),
                             avg_entry_price=Decimal("0.80"), entry_fee_remaining=Decimal("0"), hold_sec=600.0,
                             stop_loss_confirm_hits=5, held_side="UP", peak_bid=None, peak_fair=None)
    signal = SignalDecision(active_side="DOWN", score=Decimal("-0.9"), locked=True, reason="probe",
                            matches_position=False)
    decision = engine.evaluate(snapshot, position, signal, locked_side_invalidated=True,
                               confirmed_adverse_exit_active=True, stop_loss_pending_active=True,
                               adverse_persistence_sec=600.0, adverse_thesis_weakening_count=3,
                               adverse_thesis_available_count=3)
    if decision.reason in HARD_PROTECTIVE_EXIT_REASONS or decision.decision_type == ExitDecisionType.TAKER_STOP_LOSS:
        return f"exit engine returned {decision.decision_type.value}/{decision.reason}"
    return None


def check_instantiated_strategy(strategy: Any) -> list[dict[str, Any]]:
    out = []
    engine = getattr(strategy, "exit_policy_engine", None)
    engine_cfg = getattr(engine, "config", None)
    engine_fields = {"ABSOLUTE_MAX_LOSS_ENABLED": "absolute_max_loss_enabled",
                     "CATASTROPHIC_STOP_LOSS_ENABLED": "catastrophic_stop_loss_enabled",
                     "STOP_LOSS_ENABLED": "stop_loss_enabled", "HOLD_TO_REDEEM": "hold_to_redeem_enabled"}
    for table in (STRATEGY_EXIT_CONTROLS, TP_CONTROLS):
        for key, (expected, _path, attr) in table.items():
            value = getattr(strategy, attr, None)
            if not isinstance(value, bool) or value is not expected:
                out.append(_violation(key, int(expected), f"strategy.{attr}={value!r}", "RUNTIME"))
            if key in engine_fields:
                eng_value = getattr(engine_cfg, engine_fields[key], None)
                if eng_value is not expected:
                    out.append(_violation(key, int(expected), f"exit_policy_engine.config={eng_value!r}", "RUNTIME"))
    price = getattr(strategy, "tail_protect_tp_price", None)
    try:
        price_ok = price is not None and Decimal(str(price)) == TP_PRICE
    except (InvalidOperation, ValueError):
        price_ok = False
    if not price_ok:
        out.append(_violation(TP_PRICE_KEY, TP_PRICE, f"strategy.tail_protect_tp_price={price!r}", "RUNTIME"))
    if engine is None:
        out.append(_violation("EXIT_ENGINE", "instantiated", "None", "RUNTIME"))
    else:
        try:
            problem = probe_exit_engine(engine)
        except Exception as exc:
            problem = f"probe raised {type(exc).__name__}"
        if problem:
            out.append(_violation("EXIT_ENGINE_PROBE", "no SELL decision on a thesis-broken deep loss", problem,
                                  "RUNTIME"))
    return out


def enforce_instantiated_strategy(strategy: Any, app_config: Any, *, environ: Optional[Mapping[str, str]] = None,
                                  sources: Optional[Mapping[str, str]] = None,
                                  project_root: Optional[Path] = None, cwd: Optional[Path] = None,
                                  git_runner: Callable[..., Optional[str]] = _run_git) -> Optional[dict[str, Any]]:
    """Strategy-level gate (bot.settings), before any shadow writer is constructed."""
    fingerprint = enforce_startup_guard(app_config, environ=environ, sources=sources, project_root=project_root,
                                        cwd=cwd, git_runner=git_runner)
    if fingerprint is None:
        return None
    violations = check_instantiated_strategy(strategy)
    if violations:
        _raise(violations, "strategy_init")
    return fingerprint

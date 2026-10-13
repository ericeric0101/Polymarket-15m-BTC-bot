"""Entry V3 forward research profile: no strategy SELL, fail-closed startup guard, path isolation.

Hermetic: versioned profiles + a temporary .env only (never the real local .env);
git is a fake runner; no network; nothing under the frozen LIVE checkout is read.
"""
import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import dotenv_values

import bot.research_startup_guard as guard
from bot.app_config import AppConfig
from bot.enums import ActiveSide
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.runtime_env import load_runtime_env
from test_absolute_max_loss_breaker import _BreakerExitHost

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = guard.RESEARCH_PROFILE
LIVE = "btc15_twap_v3"
FAKE_LIVE = "/fake/live/checkout"
SECRET = "0xSECRET_SHOULD_NEVER_APPEAR"


def fake_git(dirty=False, available=True, live_root=FAKE_LIVE):
    def runner(root, *args):
        if not available:
            return None
        if args[:2] == ("rev-parse", "HEAD"):
            return "531eacc0000000000000000000000000000000aa\n"
        if args[0] == "status":
            return " M bot/x.py\n" if dirty else ""
        if args[0] == "rev-parse" and "--git-common-dir" in args:
            return f"{live_root}/.git\n" if live_root else "\n"
        if args[0] == "diff":
            return "diff --git a/bot/x.py b/bot/x.py\n"
        return ""
    return runner


def resolve(monkeypatch, tmp_path, *, profile=RESEARCH, shell=None, local_env=""):
    """Run the real loader (shell > .env > profile > default) and build AppConfig from the result."""
    env_file = tmp_path / "local.env"
    env_file.write_text(local_env, encoding="utf-8")
    environ = dict(shell or {})
    if profile is not None:
        environ.setdefault("STRATEGY_PROFILE", profile)
    sources = {}
    load_runtime_env(repo_root=ROOT, env_path=env_file, environ=environ, sources=sources)
    keys = set(dotenv_values(ROOT / f"config/profiles/{LIVE}.env")) | set(
        dotenv_values(ROOT / f"config/profiles/{RESEARCH}.env")) | set(environ) | guard.asserted_exit_keys() | {
        "STRATEGY_PROFILE", "RESEARCH_OFFSITE_DIR", "TRADE_DB_PATH", "TWAP_RESEARCH_DB_PATH", "RESEARCH_DATA_ROOT"}
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in environ.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    return AppConfig.from_env(enable_terminal_dashboard=False), environ, sources


def run_guard(monkeypatch, tmp_path, *, profile=RESEARCH, shell=None, local_env="", git=None, cwd=None):
    cfg, env, sources = resolve(monkeypatch, tmp_path, profile=profile, shell=shell, local_env=local_env)
    return guard.enforce_startup_guard(cfg, environ=env, sources=sources, loaded_profile=env.get("STRATEGY_PROFILE"),
                                       project_root=ROOT, cwd=cwd or ROOT, git_runner=git or fake_git())


# (case_id, layer, overrides, expected) -- also exported to config_precedence_matrix.csv
EXIT_KEYS_ON = {"ABSOLUTE_MAX_LOSS_ENABLED": "1", "CATASTROPHIC_STOP_LOSS_ENABLED": "1", "ENDGAME_TWAP_EXIT_ENABLED": "1",
                "RECOVERY_EXIT_ENABLED": "1", "TAKER_EXIT_ONLY_AFTER_INVALIDATION": "1",
                "RECOVERY_EXIT_LADDER_ENABLED": "1", "MAKER_URGENT_EXIT_ENABLED": "1", "STOP_LOSS_ENABLED": "1",
                "HOLD_TO_REDEEM": "0"}
FLAG_KEYS = list(guard.RESEARCH_IDENTITY_FLAGS) + list(guard.CBD_SHADOW_FLAGS)
MATRIX = [("baseline_research_profile", "none", {}, "PASS")]
for _layer in ("shell", "dotenv"):
    for _k, _v in EXIT_KEYS_ON.items():
        MATRIX.append((f"{_layer}_{_k}={_v}", _layer, {_k: _v}, "FAIL"))
    MATRIX.append((f"{_layer}_TAIL_PROTECT_TP_ENABLED=0", _layer, {"TAIL_PROTECT_TP_ENABLED": "0"}, "FAIL"))
    for _p in ("0.96", "0.98", "0.97 "):
        MATRIX.append((f"{_layer}_TAIL_PROTECT_TP_PRICE={_p!r}", _layer, {"TAIL_PROTECT_TP_PRICE": _p}, "FAIL"))
    MATRIX.append((f"{_layer}_TAIL_PROTECT_TP_PRICE='0.970'", _layer, {"TAIL_PROTECT_TP_PRICE": "0.970"}, "PASS"))
    for _k in FLAG_KEYS:
        MATRIX.append((f"{_layer}_{_k}=0", _layer, {_k: "0"}, "FAIL"))
for _k, _bad in (("ABSOLUTE_MAX_LOSS_ENABLED", ""), ("ABSOLUTE_MAX_LOSS_ENABLED", "maybe"),
                 ("HOLD_TO_REDEEM", ""), ("CATASTROPHIC_STOP_LOSS_ENABLED", " 0"),
                 ("CANDIDATE_DECISION_TELEMETRY_ENABLED", ""), ("ENDGAME_TWAP_EXIT_ENABLED", "disabled")):
    MATRIX.append((f"shell_{_k}={_bad!r}_unparseable", "shell", {_k: _bad}, "FAIL"))
for _bypass in ("RESEARCH_STARTUP_GUARD_DISABLED", "SKIP_RESEARCH_GUARD", "RESEARCH_GUARD_BYPASS", "FORCE_START"):
    MATRIX.append((f"bypass_attempt_{_bypass}", "shell", {_bypass: "1", "ABSOLUTE_MAX_LOSS_ENABLED": "1"}, "FAIL"))


@pytest.mark.parametrize("case_id,layer,overrides,expected", MATRIX, ids=[m[0] for m in MATRIX])
def test_config_precedence_matrix(monkeypatch, tmp_path, case_id, layer, overrides, expected):
    shell = dict(overrides) if layer == "shell" else {}
    local = "".join(f"{k}={v}\n" if v == v.strip() and v else f'{k}="{v}"\n' for k, v in overrides.items()) \
        if layer == "dotenv" else ""
    if expected == "PASS":
        fp = run_guard(monkeypatch, tmp_path, shell=shell, local_env=local)
        assert fp is not None and fp["strategy_profile"] == RESEARCH
    else:
        with pytest.raises(guard.ResearchStartupAssertionError) as exc:
            run_guard(monkeypatch, tmp_path, shell=shell, local_env=local)
        message = str(exc.value)
        assert "FAILED CLOSED" in message and "expected=" in message and "source=" in message
        key = next(k for k in overrides if k in guard.asserted_exit_keys() | set(FLAG_KEYS))
        assert key in message
        if layer in ("shell", "dotenv"):
            assert ("source=SHELL" if layer == "shell" else "source=DOTENV") in message


def test_dotenv_resolution_reports_source_dotenv_and_shell_beats_dotenv(monkeypatch, tmp_path):
    with pytest.raises(guard.ResearchStartupAssertionError) as exc:
        run_guard(monkeypatch, tmp_path, local_env="ENDGAME_TWAP_EXIT_ENABLED=1\nRECOVERY_EXIT_ENABLED=1\n")
    assert "ENDGAME_TWAP_EXIT_ENABLED: expected=0 effective=1 source=DOTENV" in str(exc.value)
    assert "RECOVERY_EXIT_ENABLED: expected=0 effective=1 source=DOTENV" in str(exc.value)
    # A shell value of 0 overrides a .env value of 1 (documented precedence) -> PASS.
    assert run_guard(monkeypatch, tmp_path, shell={"ENDGAME_TWAP_EXIT_ENABLED": "0", "RECOVERY_EXIT_ENABLED": "0"},
                     local_env="ENDGAME_TWAP_EXIT_ENABLED=1\nRECOVERY_EXIT_ENABLED=1\n") is not None


def test_current_live_dotenv_exit_keys_would_fail_closed(monkeypatch, tmp_path):
    """The LIVE checkout's .env sets these two keys to 1 (verified by key/value of these keys only)."""
    with pytest.raises(guard.ResearchStartupAssertionError):
        run_guard(monkeypatch, tmp_path, local_env="HOLD_TO_REDEEM=1\nSTOP_LOSS_ENABLED=0\nRECOVERY_EXIT_ENABLED=1\n"
                                                    "ENDGAME_TWAP_EXIT_ENABLED=1\n")


def test_profile_identity_research_flags_require_research_profile(monkeypatch, tmp_path):
    for profile, shell in ((LIVE, {"CANDIDATE_DECISION_TELEMETRY_ENABLED": "1"}),
                           (LIVE, {"FIRST_QUOTE_ONLY_SHADOW_ENABLED": "1"}),
                           (None, {"HELD_SIDE_MARKOUT_TELEMETRY_ENABLED": "1"})):
        with pytest.raises(guard.ResearchStartupAssertionError) as exc:
            run_guard(monkeypatch, tmp_path, profile=profile, shell=shell)
        assert "profile_identity" in str(exc.value)
    # LIVE / default profile untouched: no research flags -> guard is a no-op.
    assert run_guard(monkeypatch, tmp_path, profile=LIVE) is None
    assert run_guard(monkeypatch, tmp_path, profile=None) is None


def test_misspelled_profile_cannot_load(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_runtime_env(repo_root=ROOT, env_path=tmp_path / "none.env",
                         environ={"STRATEGY_PROFILE": "btc15_twap_v3_entry_v3_foward"})


@pytest.mark.parametrize("git,needle", [(fake_git(dirty=True), "DIRTY"), (fake_git(available=False), "UNAVAILABLE")])
def test_git_dirty_or_unavailable_fails_closed(monkeypatch, tmp_path, git, needle):
    with pytest.raises(guard.ResearchStartupAssertionError) as exc:
        run_guard(monkeypatch, tmp_path, git=git)
    assert needle in str(exc.value)
    if needle == "DIRTY":
        assert re.search(r"dirty_diff_sha256=[0-9a-f]{64}", str(exc.value))


@pytest.mark.parametrize("shell,cwd_is_live", [
    ({"TRADE_DB_PATH": f"{FAKE_LIVE}/logs/trade_journal.db"}, False),
    ({"TWAP_RESEARCH_DB_PATH": f"{FAKE_LIVE}/data/research/twap_forward_shadow.db"}, False),
    ({}, True),  # relative cwd paths resolved from the LIVE checkout
    ({"RESEARCH_OFFSITE_DIR": "/Users/someone/Library/Mobile Documents/offsite"}, False),
    ({"RESEARCH_DATA_ROOT": f"{FAKE_LIVE}/data/research", "TWAP_RESEARCH_DB_PATH": f"{FAKE_LIVE}/data/research/x.db"},
     False),
    ({"BTC_1S_HISTORY_DIR": "/tmp/elsewhere/btc"}, False),
])
def test_data_paths_into_frozen_live_dataset_fail_closed(monkeypatch, tmp_path, shell, cwd_is_live):
    with pytest.raises(guard.ResearchStartupAssertionError) as exc:
        run_guard(monkeypatch, tmp_path, shell=shell, cwd=Path(FAKE_LIVE) if cwd_is_live else None)
    assert "FAILED CLOSED" in str(exc.value)


def test_explicit_research_data_root_is_allowed(monkeypatch, tmp_path):
    root = tmp_path / "research_data"
    fp = run_guard(monkeypatch, tmp_path, shell={"RESEARCH_DATA_ROOT": str(root),
                                                 "TRADE_DB_PATH": str(root / "trade_journal.db"),
                                                 "TWAP_RESEARCH_DB_PATH": str(root / "twap.db")})
    assert fp is not None


def test_live_checkout_root_unknown_fails_closed(monkeypatch, tmp_path):
    with pytest.raises(guard.ResearchStartupAssertionError):
        run_guard(monkeypatch, tmp_path, git=fake_git(live_root=""))


def test_failure_message_never_contains_secrets(monkeypatch, tmp_path):
    with pytest.raises(guard.ResearchStartupAssertionError) as exc:
        run_guard(monkeypatch, tmp_path, shell={"POLYMARKET_PK": SECRET, "POLYMARKET_API_SECRET": SECRET,
                                                "ABSOLUTE_MAX_LOSS_ENABLED": "1"})
    assert SECRET not in str(exc.value)


def test_guard_has_no_bypass_environment_reads():
    src = (ROOT / "bot/research_startup_guard.py").read_text()
    assert not re.search(r"(?i)(bypass|skip|disable|force)[a-z_]*\s*=\s*.*environ", src)
    env_reads = set(re.findall(r'env\.get\("([A-Z0-9_]+)"', src)) | set(re.findall(r'environ\.get\("([A-Z0-9_]+)"', src))
    # CANDIDATE_TELEMETRY_DIR is a data-path key read so the guard can VALIDATE it (inside the allowed
    # roots, outside the frozen LIVE dataset) -- the same class as TRADE_DB_PATH, not a bypass.
    assert env_reads <= {"TRADE_DB_PATH", "TWAP_RESEARCH_DB_PATH", "BTC_1S_HISTORY_DIR", "REBATE_REPORT_DIR",
                         "RESEARCH_OFFSITE_DIR", "STRATEGY_PROFILE", "CANDIDATE_TELEMETRY_DIR"}


# ------------------------------------------------------------------ coverage

def _exit_like_bool_keys():
    text = (ROOT / "bot/app_config.py").read_text()
    keys = set(re.findall(r'_env_bool(?:_inverted)?\(\s*"([A-Z0-9_]+)"', text))
    pat = re.compile(r"EXIT|STOP|LOSS|SELL|TP|REDEEM|PROFIT|RECYCLE|URGENT|RECOVERY|FLATTEN|TAIL|BREAKER|CATASTROPH|"
                     r"HOLD|TRAPPED|RISK|LIQUID|KILL|AUTHORITY")
    return {k for k in keys if pat.search(k)}


def test_every_exit_like_config_key_is_asserted_or_reviewed():
    keys = _exit_like_bool_keys()
    assert len(keys) >= 25
    unclassified = keys - guard.asserted_exit_keys() - set(guard.REVIEWED_NON_SELL_ENABLING_KEYS)
    assert not unclassified, f"new exit-like key(s) must be asserted or reviewed: {sorted(unclassified)}"
    assert not (guard.asserted_exit_keys() & set(guard.REVIEWED_NON_SELL_ENABLING_KEYS))


def test_s1_inventory_strategy_exit_keys_are_all_asserted():
    inv = json.loads((ROOT / "tests/fixtures/strategy_exit_control_keys.json").read_text())
    assert set(inv["strategy_sell_control_keys"]) <= guard.asserted_exit_keys()
    assert set(inv["strategy_sell_control_keys"]) == guard.asserted_exit_keys()


def test_research_profile_file_pins_every_asserted_key_explicitly():
    research = dotenv_values(ROOT / f"config/profiles/{RESEARCH}.env")
    for key, (expected, _p, _a) in {**guard.STRATEGY_EXIT_CONTROLS, **guard.TP_CONTROLS}.items():
        assert research.get(key) == ("1" if expected else "0"), key
    assert research["TAIL_PROTECT_TP_PRICE"] == "0.97"
    for key in FLAG_KEYS:
        assert research[key] == "1"


def test_strict_parsers():
    assert guard.strict_bool("1") is True and guard.strict_bool("OFF") is False
    for bad in (None, "", " 1", "1 ", "2", "enabled"):
        assert guard.strict_bool(bad) is None
    assert guard.strict_tp_price("0.97") == Decimal("0.97") and guard.strict_tp_price("0.970") == Decimal("0.97")
    for bad in (None, "", "0.97 ", " 0.97", "nan", "abc"):
        assert guard.strict_tp_price(bad) is None


# --------------------------------------------- golden: default profiles unchanged

GOLDEN_EFFECTIVE_CONFIG_SHA256 = {
    # Computed at base 531eacc before this change (scratch golden.py; hermetic: profile only, no .env).
    LIVE: "0a2d7da2b43a1f9f114acac02b2f42a4568d523ca84ea05010c7bb492801e75b",
}


def _effective_sha(monkeypatch, tmp_path, profile):
    import dataclasses
    import hashlib
    for k in list(__import__("os").environ):
        if k not in ("PATH", "HOME"):
            monkeypatch.delenv(k, raising=False)
    env = {"STRATEGY_PROFILE": profile}
    load_runtime_env(repo_root=ROOT, env_path=tmp_path / "__no_env__", environ=env)
    for k, v in env.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    cfg = AppConfig.from_env(enable_terminal_dashboard=False)
    return hashlib.sha256(json.dumps(dataclasses.asdict(cfg), sort_keys=True, default=str).encode()).hexdigest()


def test_live_and_default_profile_effective_config_unchanged(monkeypatch, tmp_path):
    assert _effective_sha(monkeypatch, tmp_path, LIVE) == GOLDEN_EFFECTIVE_CONFIG_SHA256[LIVE]


# ----------------------------------------- instantiated objects + behaviour

def _engine_kwargs(cfg):
    src = (ROOT / "bot/settings.py").read_text()
    block = src[src.index("ExitEngineConfig(", src.index("strategy.exit_policy_engine")):]
    block = block[:block.index("),\n    )")]
    return {f: getattr(cfg.exit, a) for f, a in re.findall(r"(\w+)=strategy\.(\w+)", block)}


class _Strategy:
    pass


def _strategy_from(cfg):
    s = _Strategy()
    for key, (_e, path, attr) in {**guard.STRATEGY_EXIT_CONTROLS, **guard.TP_CONTROLS}.items():
        setattr(s, attr, guard._attr(cfg, path))
    s.tail_protect_tp_price = cfg.exit.tail_protect_tp_price
    s.exit_policy_engine = ExitPolicyEngine(ExitEngineConfig(**_engine_kwargs(cfg)))
    return s


def test_instantiated_strategy_check_passes_for_research_and_fails_for_live(monkeypatch, tmp_path):
    research, _, _ = resolve(monkeypatch, tmp_path)
    assert guard.check_instantiated_strategy(_strategy_from(research)) == []
    live, _, _ = resolve(monkeypatch, tmp_path, profile=LIVE, shell={"HOLD_TO_REDEEM": "1"})
    keys = {v["key"] for v in guard.check_instantiated_strategy(_strategy_from(live))}
    assert {"ABSOLUTE_MAX_LOSS_ENABLED", "CATASTROPHIC_STOP_LOSS_ENABLED", "EXIT_ENGINE_PROBE"} <= keys


def test_instantiated_check_catches_engine_out_of_sync_with_attributes(monkeypatch, tmp_path):
    research, _, _ = resolve(monkeypatch, tmp_path)
    s = _strategy_from(research)
    live, _, _ = resolve(monkeypatch, tmp_path, profile=LIVE)
    s.exit_policy_engine = ExitPolicyEngine(ExitEngineConfig(**_engine_kwargs(live)))  # stale engine object
    keys = {v["key"] for v in guard.check_instantiated_strategy(s)}
    assert "EXIT_ENGINE_PROBE" in keys and "ABSOLUTE_MAX_LOSS_ENABLED" in keys


def test_settings_runs_instantiated_guard_before_any_writer():
    src = (ROOT / "bot/settings.py").read_text()
    guard_at = src.index("enforce_instantiated_strategy(strategy, config")
    for writer in ("BTC1sHistoryCollector(", "TradeJournalDB(", "strategy.twap_research_db = build_twap_research_db()", "build_entry_shadow(",
                   "build_candidate_telemetry("):
        assert guard_at < src.index(writer), writer
    assert src.index("strategy.exit_policy_engine = ExitPolicyEngine(") < guard_at


def test_launcher_runs_guard_before_auth_and_never_retries_it():
    src = (ROOT / "bot/launcher.py").read_text()
    main = src[src.index("def main():"):]
    assert main.index("enforce_startup_guard(app_config)") < main.index("run_preflight_checks(")
    loop = src[src.index("def _run_integrated_bot_cycles("):src.index("def acquire_live_process_lock(")]
    assert loop.index("except ResearchStartupAssertionError:") < loop.index("except Exception as e:\n            _collection_transition")


def _scenario_host(cfg, *, bid, score="-0.90", endgame=False):
    h = _BreakerExitHost()
    h.events = []
    h._db_strategy_event = lambda t, p: h.events.append((t, p)) or True
    h._record_exit_policy_decision_throttled = lambda **k: None
    h._record_exit_audit_throttled = lambda **k: None
    h.exit_policy_engine = ExitPolicyEngine(ExitEngineConfig(**_engine_kwargs(cfg)))
    ex = cfg.exit
    for key, (_e, path, attr) in {**guard.STRATEGY_EXIT_CONTROLS, **guard.TP_CONTROLS}.items():
        setattr(h, attr, guard._attr(cfg, path))
    for a in ("absolute_max_loss_usdc", "catastrophic_stop_loss_usdc", "endgame_twap_exit_max_time_left_sec",
              "endgame_twap_exit_min_distance_usd", "endgame_twap_exit_max_age_sec"):
        setattr(h, a, getattr(ex, a))
    h.absolute_max_loss_twap_authority_gate_enabled = False
    for a in dir(ex):
        if a.startswith("taker_exit_"):
            setattr(h, a, getattr(ex, a))
    h.taker_exit_eval_interval_sec = 0
    h.taker_exit_cooldown_sec = 0
    h.taker_exit_max_time_left_sec = 720
    h.active_side = ActiveSide.DOWN
    h.side_decision_score = Decimal(score)
    h._side_invalidation_confirmed_by_slug = {"breaker-test": True}
    h.live_inventory_cost = {"up": {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.69"),
                                    "opened_ts": 10_000.0 - 400}}
    b = Decimal(bid)
    h._get_quote_for_instrument = lambda _i: (b, b + Decimal("0.01"))
    if endgame:
        h.current_market_end_timestamp = 10_060.0
    return h


def _submissions(h):
    for t in range(6):
        asyncio.run(h._maybe_taker_exit_positions(10_000.0 + t * 5, is_simulation=False))
    return [(s.get("reason"), (s.get("decision_payload") or {}).get("decision_reason")) for s in h.submissions]


@pytest.mark.parametrize("bid,endgame", [("0.20", False), ("0.40", False), ("0.55", False), ("0.30", True),
                                         ("0.15", True)])
def test_research_profile_emits_no_strategy_sell_in_stop_scenarios(monkeypatch, tmp_path, bid, endgame):
    research, _, _ = resolve(monkeypatch, tmp_path)
    assert _submissions(_scenario_host(research, bid=bid, endgame=endgame)) == []


def test_same_scenarios_do_sell_under_live_exits(monkeypatch, tmp_path):
    """Control: the harness is able to observe strategy SELLs (LIVE exits ON)."""
    live, _, _ = resolve(monkeypatch, tmp_path, profile=LIVE)
    assert _submissions(_scenario_host(live, bid="0.20"))
    assert _submissions(_scenario_host(live, bid="0.55"))


def test_maker_urgent_exit_returns_before_any_order_activity(monkeypatch, tmp_path):
    research, _, _ = resolve(monkeypatch, tmp_path)
    h = _scenario_host(research, bid="0.20")
    h.test_mode = False
    h._is_dry_run_mode = lambda: False
    touched = []
    h._cancel_maker_order_side = lambda *a, **k: touched.append(a)
    h.submit_order = lambda *a, **k: touched.append(a)
    asyncio.run(h._maybe_maker_urgent_exit(10_000.0))
    assert touched == []


def test_stop_new_entry_guards_do_not_depend_on_disabled_exits(monkeypatch, tmp_path):
    """Session PnL guard / entry session / pause are BUY gates computed without any exit flag."""
    research, _, _ = resolve(monkeypatch, tmp_path)
    assert research.operations.session_pnl_guard_enabled is True  # code default, not overridden by the profile
    import inspect
    import bot.order_submission as osub
    import bot.quote_runtime as qr
    import bot.session_pnl_guard as spg
    src_guard = inspect.getsource(spg)
    for disabled in ("absolute_max_loss", "catastrophic", "endgame_twap", "taker_exit_enabled", "maker_urgent",
                     "stop_loss_enabled"):
        assert disabled not in src_guard
    submit_src = inspect.getsource(osub.submit_maker_quote)
    assert submit_src.index("session_buy_guard_decision") < submit_src.index("recovery_exit_owns_sell_reservation")
    pause_src = inspect.getsource(qr.QuoteRuntimeMixin._prepare_quote_cycle) if hasattr(qr, "QuoteRuntimeMixin") else \
        (ROOT / "bot/quote_runtime.py").read_text()
    assert "_cancel_resting_buys_for_pause" in pause_src and "_run_protective_exit_cycle" in pause_src


def test_session_guard_blocks_buy_at_submit_boundary_with_all_exits_off(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import bot.order_submission as osub
    research, _, _ = resolve(monkeypatch, tmp_path)
    events = []

    class S:
        pass
    s = S()
    for key, (_e, path, attr) in {**guard.STRATEGY_EXIT_CONTROLS, **guard.TP_CONTROLS}.items():
        setattr(s, attr, guard._attr(research, path))
    s.normal_maker_buy_enabled = True
    s._is_dry_run_mode = lambda: False
    s._new_buy_session_decision_fn = lambda now: SimpleNamespace(allowed=True, reason="ok", local_time=None)
    s.session_buy_guard_decision = lambda now: SimpleNamespace(
        allowed=False, reason="session_max_loss", session_date_taipei="2026-10-13",
        realized_pnl_usdc=Decimal("-5"), realized_high_water_usdc=Decimal("0"))
    s._db_order_event = lambda **k: events.append(k)
    s.submit_order = lambda *a, **k: pytest.fail("BUY must not be submitted")
    s._instrument_key = lambda i: str(i)
    s._normalize_instrument_id = lambda i: i
    params = __import__("inspect").signature(osub.submit_maker_quote).parameters
    kwargs = {"side": "buy", "limit_price": Decimal("0.60"), "instrument_id": "up"}
    for name in params:
        if name not in kwargs and name != "strategy" and params[name].default is __import__("inspect").Parameter.empty:
            kwargs[name] = None
    res = osub.submit_maker_quote(s, **kwargs)
    if __import__("inspect").iscoroutine(res):
        asyncio.run(res)
    assert [e["event_type"] for e in events] == ["ORDER_SKIP_SESSION_PNL_GUARD"]


def test_protected_exit_and_entry_modules_untouched_since_integration_base():
    """Unmasked companion of the (baseline-failing) E5B meta-test: this cleanup edits no decision module."""
    import subprocess
    out = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "531eacc", "--",
                          "bot/exit_engine.py", "bot/taker_exit.py", "bot/breaker_authority.py", "bot/entry_sizing.py",
                          "bot/quote_service.py", "bot/quoting.py", "bot/entry_decision.py", "bot/side_decision.py",
                          "bot/entry_shadow.py", "bot/execution_selection_eval.py", "bot/kill_switch.py",
                          "bot/protective_exit.py", "bot/order_submission.py", "run_bot.py"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        pytest.skip("not a git checkout of the integration branch")
    assert out.stdout.split() == []

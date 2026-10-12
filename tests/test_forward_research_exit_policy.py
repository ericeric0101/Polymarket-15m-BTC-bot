"""Entry V3 forward research profile: conditional absolute breaker OFF, everything else unchanged.

Hermetic: reads only versioned profiles in config/profiles (never a local .env).
"""
import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest
from dotenv import dotenv_values

from bot.app_config import AppConfig
from bot.enums import ActiveSide
from bot.exit_engine import ExitEngineConfig, ExitPolicyEngine
from bot.exit_policy_state import effective_exit_policy, exit_policy_summary
from bot.kill_switch import HARD_PROTECTIVE_EXIT_REASONS
from bot.runtime_env import load_runtime_env
from test_absolute_max_loss_breaker import _BreakerExitHost

ROOT = Path(__file__).resolve().parents[1]
LIVE_PROFILE = ROOT / "config/profiles/btc15_twap_v3.env"
RESEARCH = "btc15_twap_v3_entry_v3_forward"
RESEARCH_PROFILE = ROOT / f"config/profiles/{RESEARCH}.env"
SHADOW_FLAGS = {"CANDIDATE_DECISION_TELEMETRY_ENABLED", "FIRST_QUOTE_ONLY_SHADOW_ENABLED",
                "HELD_SIDE_MARKOUT_TELEMETRY_ENABLED", "EARLY_CANCEL_SHADOW_ENABLED", "MAKER_CHASE_SHADOW_ENABLED",
                "PRICE_ADJUSTED_EDGE_SHADOW_ENABLED"}


def _profile_config(monkeypatch, tmp_path, profile=RESEARCH, extra_env=None, local_env=""):
    env_file = tmp_path / "local.env"
    env_file.write_text(local_env, encoding="utf-8")
    environ = {"STRATEGY_PROFILE": profile, **(extra_env or {})}
    load_runtime_env(repo_root=ROOT, env_path=env_file, environ=environ)
    keys = set(dotenv_values(LIVE_PROFILE)) | set(dotenv_values(RESEARCH_PROFILE)) | {"ABSOLUTE_MAX_LOSS_ENABLED"}
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    for k, v in environ.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    return AppConfig.from_env(enable_terminal_dashboard=False)


def test_research_profile_differs_from_live_profile_only_in_breaker_and_shadow_flags():
    live, research = dotenv_values(LIVE_PROFILE), dotenv_values(RESEARCH_PROFILE)
    changed = {k for k in set(live) | set(research) if live.get(k) != research.get(k)}
    assert changed == {"ABSOLUTE_MAX_LOSS_ENABLED"} | SHADOW_FLAGS
    assert live["ABSOLUTE_MAX_LOSS_ENABLED"] == "1" and research["ABSOLUTE_MAX_LOSS_ENABLED"] == "0"
    assert all(research[k] == "1" for k in SHADOW_FLAGS)
    # TP, sizing, entry thresholds, catastrophic settings: byte-identical values.
    for k in ("TAIL_PROTECT_TP_PRICE", "TAIL_PROTECT_TP_ENABLED", "ABSOLUTE_MAX_LOSS_USDC"):
        assert research[k] == live[k]
    assert research["TAIL_PROTECT_TP_PRICE"] == "0.97"


def test_config_precedence_resolves_breaker_off_and_is_explicit(monkeypatch, tmp_path):
    cfg = _profile_config(monkeypatch, tmp_path)
    assert cfg.exit.absolute_max_loss_enabled is False
    assert cfg.exit.catastrophic_stop_loss_enabled is True
    assert cfg.exit.tail_protect_tp_price == Decimal("0.97")
    assert cfg.operations.candidate_decision_telemetry_enabled is True
    # .env beats the profile; a shell value beats both (documented precedence).
    assert _profile_config(monkeypatch, tmp_path, local_env="ABSOLUTE_MAX_LOSS_ENABLED=1\n").exit.absolute_max_loss_enabled
    assert _profile_config(monkeypatch, tmp_path, extra_env={"ABSOLUTE_MAX_LOSS_ENABLED": "1"}).exit.absolute_max_loss_enabled
    # The LIVE profile is unchanged: breaker still ON there.
    assert _profile_config(monkeypatch, tmp_path, profile="btc15_twap_v3").exit.absolute_max_loss_enabled is True


def test_live_and_research_profiles_resolve_identical_entry_and_sizing(monkeypatch, tmp_path):
    live = _profile_config(monkeypatch, tmp_path, profile="btc15_twap_v3")
    research = _profile_config(monkeypatch, tmp_path)
    assert live.maker == research.maker and live.side == research.side
    assert {k: v for k, v in vars(live.exit).items() if k != "absolute_max_loss_enabled"} == \
        {k: v for k, v in vars(research.exit).items() if k != "absolute_max_loss_enabled"}


def _engine_kwargs(cfg):
    src = (ROOT / "bot/settings.py").read_text()
    block = src[src.index("ExitEngineConfig(", src.index("strategy.exit_policy_engine")):]
    block = block[:block.index("),\n    )")]
    return {f: getattr(cfg.exit, a) for f, a in re.findall(r"(\w+)=strategy\.(\w+)", block)}


def _host(cfg, *, bid, score="-0.30"):
    ex = cfg.exit
    h = _BreakerExitHost()
    h.events = []
    h._db_strategy_event = lambda t, p: h.events.append((t, p)) or True
    h._record_exit_policy_decision_throttled = lambda **k: None
    h.exit_policy_engine = ExitPolicyEngine(ExitEngineConfig(**_engine_kwargs(cfg)))
    for a in ("absolute_max_loss_enabled", "absolute_max_loss_usdc", "catastrophic_stop_loss_enabled",
              "stop_loss_enabled", "hold_to_redeem_enabled"):
        setattr(h, a, getattr(ex, a))
    h.absolute_max_loss_twap_authority_gate_enabled = False
    for a in dir(ex):
        if a.startswith("taker_exit_"):
            setattr(h, a, getattr(ex, a))
    h.taker_exit_eval_interval_sec = 0
    h.taker_exit_cooldown_sec = 0
    h.active_side = ActiveSide.DOWN
    h.side_decision_score = Decimal(score)
    h._side_invalidation_confirmed_by_slug = {"breaker-test": True}
    h.live_inventory_cost = {"up": {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.69"), "opened_ts": 10_000.0 - 200}}
    b = Decimal(bid)
    h._get_quote_for_instrument = lambda _i: (b, b + Decimal("0.01"))
    for t in range(4):
        asyncio.run(h._maybe_taker_exit_positions(10_000.0 + t * 10, is_simulation=False))
        if h.submissions:
            break
    return [(s.get("reason"), (s.get("decision_payload") or {}).get("decision_reason")) for s in h.submissions]


def test_1_conditional_breaker_off_emits_no_conditional_sell(monkeypatch, tmp_path):
    research = _profile_config(monkeypatch, tmp_path)
    live = _profile_config(monkeypatch, tmp_path, profile="btc15_twap_v3")
    assert _host(live, bid="0.20") == [("stop_loss", "absolute_max_loss_breaker")]  # LIVE: breaker fires
    subs = _host(research, bid="0.20")
    assert all(reason != "absolute_max_loss_breaker" for _, reason in subs)


def test_2_catastrophic_breaker_still_permits_emergency_sell(monkeypatch, tmp_path):
    research = _profile_config(monkeypatch, tmp_path)
    assert _host(research, bid="0.55") == [("stop_loss", "catastrophic_stop_loss_confirmed")]


def test_3_4_tp_and_integrity_protections_unchanged():
    assert HARD_PROTECTIVE_EXIT_REASONS == frozenset({"absolute_max_loss_breaker", "catastrophic_stop_loss_confirming",
                                                       "catastrophic_stop_loss_confirmed"})
    assert dotenv_values(RESEARCH_PROFILE)["TAIL_PROTECT_TP_PRICE"] == "0.97"


def test_breaker_v2_is_reported_inert_when_conditional_breaker_off():
    class S:
        absolute_max_loss_enabled = False
        absolute_max_loss_twap_authority_gate_enabled = True
        catastrophic_stop_loss_enabled = True
        tail_protect_tp_enabled = True
        tail_protect_tp_price = Decimal("0.97")
    st = effective_exit_policy(S())
    assert st["breaker_v2_gate_enabled"] is True and st["breaker_v2_effective_for_conditional_exit"] is False
    assert st["conditional_breaker_enabled"] is False and st["catastrophic_breaker_enabled"] is True
    assert "breaker_v2_effective_for_conditional_exit=0" in exit_policy_summary(st)
    S.absolute_max_loss_enabled = True
    assert effective_exit_policy(S())["breaker_v2_effective_for_conditional_exit"] is True


def test_candidate_run_header_carries_effective_exit_policy():
    import bot.candidate_telemetry as ct

    class S:
        run_id = "r"
        absolute_max_loss_enabled = False
        absolute_max_loss_twap_authority_gate_enabled = True
        catastrophic_stop_loss_enabled = True
    h = ct.build_run_header(S(), {})
    assert h["exit_policy"]["conditional_breaker_enabled"] is False
    assert h["exit_policy"]["breaker_v2_effective_for_conditional_exit"] is False

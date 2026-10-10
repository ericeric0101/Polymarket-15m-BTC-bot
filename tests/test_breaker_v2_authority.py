"""Breaker V2: canonical-TWAP SELL authority gate for the conditional absolute breaker.

Letters refer to the Stage-2 test plan (A..T).  The runtime decision never reads
an official outcome or a market id.
"""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

import bot.breaker_eligibility as breaker_eligibility
import bot.exit_engine as exit_engine
import bot.settlement_side as settlement_side
import bot.stop_timing_telemetry as stop_timing_telemetry
from bot.breaker_authority import (
    ALLOW,
    DECISION_EVENT,
    FALLBACK_LEGACY,
    REHYDRATED_EVENT,
    SUPPRESS,
    BreakerV2AuthorityBook,
    breaker_v2_authority_summary,
    breaker_v2_buy_block_reason,
    decide_authority,
    market_day_type,
    rehydrated_decision,
)
from bot.enums import ActiveSide
from bot.exit_engine import ExitDecisionType, ExitPolicyEngine
from bot.settlement_side import (
    TWAP_ADVERSE,
    TWAP_FAVORABLE,
    TWAP_FUTURE,
    TWAP_SLUG_MISMATCH,
    TWAP_STALE,
    TWAP_STRIKE_INVALID,
    TWAP_UNAVAILABLE,
    canonical_twap_side_state,
)
from bot.taker_exit import TakerExitMixin
from execution.exit_policy import ExitStage
from test_absolute_max_loss_breaker import _make_config, _position, _signal, _snapshot

NOW = 10_000.0
SLUG = "btc-updown-15m-1791623700"


def _state(**kw):
    base = dict(held_side="UP", twap_features={"official_current_twap": 101.0, "source_ts": NOW - 1.0,
                                                "market_slug": SLUG},
                strike=100.0, strike_verified=True, expected_slug=SLUG, now_ts=NOW)
    base.update(kw)
    return canonical_twap_side_state(**base)


# ------------------------------------------------------------ A-G pure semantics
def test_a_fresh_adverse_allows():
    state = _state(held_side="DOWN")  # TWAP 101 > strike 100 is adverse for DOWN
    assert state["state"] == TWAP_ADVERSE
    assert decide_authority(state["state"])[0] == ALLOW


def test_b_fresh_favorable_suppresses():
    state = _state()
    assert state["state"] == TWAP_FAVORABLE and state["signed_distance_usd"] == pytest.approx(1.0)
    assert decide_authority(state["state"])[0] == SUPPRESS


def test_c_stale_falls_back():
    state = _state(twap_features={"official_current_twap": 101.0, "source_ts": NOW - 5.01, "market_slug": SLUG})
    assert state["state"] == TWAP_STALE
    assert decide_authority(state["state"])[0] == FALLBACK_LEGACY
    exactly_bound = _state(twap_features={"official_current_twap": 101.0, "source_ts": NOW - 5.0,
                                          "market_slug": SLUG})
    assert exactly_bound["state"] == TWAP_FAVORABLE  # same <= 5 s bound as the breaker vote


@pytest.mark.parametrize("features", [None, {}, {"official_current_twap": None, "source_ts": NOW},
                                      {"official_current_twap": 101.0}, {"official_current_twap": "nan", "source_ts": NOW},
                                      {"official_current_twap": 101.0, "source_ts": "garbage"}])
def test_d_unknown_or_unavailable_falls_back(features):
    state = _state(twap_features=features)
    assert state["state"] not in (TWAP_ADVERSE, TWAP_FAVORABLE)
    assert decide_authority(state["state"])[0] == FALLBACK_LEGACY


@pytest.mark.parametrize("kw,expected", [
    ({"strike_verified": False}, TWAP_STRIKE_INVALID),
    ({"strike": None}, TWAP_STRIKE_INVALID),
    ({"strike": 0}, TWAP_STRIKE_INVALID),
    ({"expected_slug": "btc-updown-15m-1791624600"}, TWAP_SLUG_MISMATCH),
])
def test_e_invalid_strike_or_slug_mismatch_falls_back(kw, expected):
    state = _state(**kw)
    assert state["state"] == expected
    assert decide_authority(state["state"])[0] == FALLBACK_LEGACY


def test_f_clearly_future_timestamp_is_never_fresh():
    future = _state(twap_features={"official_current_twap": 101.0, "source_ts": NOW + 3.0, "market_slug": SLUG})
    assert future["state"] == TWAP_FUTURE and future["age_sec"] == pytest.approx(-3.0)
    assert decide_authority(future["state"])[0] == FALLBACK_LEGACY
    skew = _state(twap_features={"official_current_twap": 101.0, "source_ts": NOW + 0.5, "market_slug": SLUG})
    assert skew["state"] == TWAP_FAVORABLE and skew["age_sec"] == 0.0  # existing 1 s skew tolerance


def test_g_tie_settles_up():
    tie = {"official_current_twap": 100.0, "source_ts": NOW - 1.0, "market_slug": SLUG}
    assert _state(held_side="UP", twap_features=tie)["state"] == TWAP_FAVORABLE
    assert _state(held_side="DOWN", twap_features=tie)["state"] == TWAP_ADVERSE
    assert decide_authority(_state(held_side="UP", twap_features=tie)["state"])[0] == SUPPRESS
    assert decide_authority(_state(held_side="DOWN", twap_features=tie)["state"])[0] == ALLOW


def test_weekday_weekend_is_recorded_but_never_a_decision_input():
    assert market_day_type("btc-updown-15m-1791623700") == "WEEKEND"   # Sat 2026-10-10 Taipei
    assert market_day_type("btc-updown-15m-1791277200") == "WEEKDAY"   # Tue 2026-10-06 Taipei
    import inspect
    assert "day" not in inspect.signature(decide_authority).parameters
    assert "slug" not in inspect.signature(decide_authority).parameters


# ------------------------------------------------------------ K/L engine level
def test_k_suppress_blocks_only_the_conditional_absolute_branch():
    engine = ExitPolicyEngine(_make_config())
    args = (_snapshot("0.20", "0.21", time_left_sec=120.0), _position("0.69", hold_sec=90),
            _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN"))
    legacy = engine.evaluate(*args)
    assert legacy.reason == "absolute_max_loss_breaker"
    suppressed = engine.evaluate(*args, suppress_conditional_absolute=True)
    assert suppressed.reason != "absolute_max_loss_breaker"
    assert suppressed.metadata["conditional_absolute_suppressed"] == "1"
    assert suppressed.metadata["legacy_absolute_eligible"] == "1"


@pytest.mark.parametrize("bid", ["0.20", "0.55", "0.80"])
@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("tte", [60.0, 300.0])
def test_k_suppress_flag_is_inert_when_the_absolute_breaker_is_not_eligible(bid, matches, tte):
    engine = ExitPolicyEngine(_make_config())
    side = "UP" if matches else "DOWN"
    args = (_snapshot(bid, time_left_sec=tte), _position("0.69", hold_sec=90),
            _signal(score=Decimal("0.30") if matches else Decimal("-0.30"), matches=matches, active_side=side))
    legacy = engine.evaluate(*args)
    if legacy.reason == "absolute_max_loss_breaker":
        return
    assert engine.evaluate(*args, suppress_conditional_absolute=True) == legacy


def test_l_suppress_plus_catastrophic_condition_still_returns_catastrophic_sell():
    engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False, hold_to_redeem_enabled=True))
    args = (_snapshot("0.20", "0.21", time_left_sec=120.0), _position("0.69", hold_sec=90, confirm_hits=1),
            _signal(score=Decimal("-0.60"), locked=True, matches=False, active_side="DOWN"))
    assert engine.evaluate(*args).reason == "absolute_max_loss_breaker"  # legacy precedence
    result = engine.evaluate(*args, suppress_conditional_absolute=True, stop_loss_pending_active=True)
    assert result.decision_type == ExitDecisionType.TAKER_STOP_LOSS
    assert result.reason == "catastrophic_stop_loss_confirmed"


# ------------------------------------------------------------ host harness
class _Journal:
    """In-memory stand-in for the core journal (strategy events + SELL fills)."""

    def __init__(self):
        self.events = []
        self.sell_fills = []  # (seq, instrument_id, qty)
        self.fail_lookup = False
        self.fail_writes = False

    def write(self, event_type, payload):
        if self.fail_writes:
            return False
        self.events.append((len(self.events), event_type, json.loads(json.dumps(payload, default=str))))
        return True

    def decisions(self):
        return [p for _, t, p in self.events if t == DECISION_EVENT]

    def load_breaker_v2_authority_decision(self, slug, instrument_id):
        if self.fail_lookup:
            return None
        rows = [(seq, p) for seq, t, p in self.events
                if t == DECISION_EVENT and p["slug"] == slug and p["instrument_id"] == instrument_id]
        if not rows:
            return {"decision_record": None, "sell_qty_after_decision": 0.0, "prior_absolute_submit": False}
        seq, record = rows[-1]
        sold = sum(q for s, inst, q in self.sell_fills if inst == instrument_id and s > seq)
        return {"decision_record": record, "sell_qty_after_decision": sold, "prior_absolute_submit": False}


class _Host(TakerExitMixin):
    def __init__(self, *, journal=None, flag=True, twap=101.0, strike=100.0, held="up", opened_ts=NOW - 90,
                 qty="5.5", active_side=ActiveSide.UP, score="0.30", bid="0.20", ask="0.25", slug=SLUG):
        self.absolute_max_loss_twap_authority_gate_enabled = flag
        self.journal = journal if journal is not None else _Journal()
        self.trade_db = self.journal
        self.run_id = "run_test"
        self.taker_exit_enabled = True
        self.hold_to_redeem_enabled = False
        self.taker_exit_only_after_invalidation = False
        self.taker_exit_cooldown_sec = 0
        self.taker_exit_eval_interval_sec = 0
        self.taker_exit_last_eval_ts_by_inst = {}
        self.taker_exit_reject_cooldown_until_by_inst = {}
        self.taker_exit_tail_attempted_by_inst = {}
        self.pending_taker_exit_by_inst = {}
        self.last_taker_exit_ts_by_inst = {}
        self.taker_exit_stop_loss_hits_by_inst = {}
        self._stop_loss_execution_priority_by_inst = {}
        self._stop_adverse_since_by_slug = {slug: NOW - 15.0}
        self._stop_adverse_votes_by_slug = {slug: {"signal_adverse": True, "twap_adverse": True,
                                                   "fair_deteriorated": False, "weakening_count": 2,
                                                   "available_count": 3}}
        self._taker_exit_skip_log_ts_by_key = {}
        self.taker_exit_skip_log_interval_sec = 0
        self.current_market_end_timestamp = NOW + 100  # <=120 s: fast path
        self.current_market_slug = slug
        self.market_strike_cache_by_slug = {slug: strike}
        self.maker_reduce_only_no_new_sell_last_sec = 0
        self.taker_exit_disable_stop_loss_last_sec = 0
        self.endgame_twap_exit_enabled = False
        self.held = held
        self.live_inventory_cost = {held: {"qty": Decimal(qty), "avg_entry_price": Decimal("0.69"),
                                           "opened_ts": opened_ts}}
        self.active_maker_orders = {f"sell:{held}": {"created_ts": NOW - 1, "pending_cancel": False}}
        self.maker_exchange_min_shares = Decimal("5")
        self.taker_exit_slippage_buffer_pct = Decimal("0.002")
        self.taker_exit_stop_loss_max_spread_pct = Decimal("0.03")
        self.taker_exit_stop_loss_confirmations = 2
        self.taker_exit_stop_loss_usdc = Decimal("0.50")
        self.taker_exit_wait_for_sell_quote_sec = 20
        self.taker_exit_max_hold_near_close_sec = 0
        self.maker_high_cost_exit_cooldown_enabled = False
        self.high_cost_exit_cooldown_until_by_inst = {}
        self.market_phase = "ACTIVE"
        self.active_side = active_side
        self.side_decision_score = Decimal(score)
        self.active_side_locked = True
        self.side_decision_reason = "test"
        self._side_invalidation_confirmed_by_slug = {slug: True}
        self.twap = {"official_current_twap": twap, "projected_settlement_side_trend": "DOWN",
                     "source_ts": NOW - 1.0, "market_slug": slug}
        self.twap_forward_shadow = SimpleNamespace(latest=lambda _slug: dict(self.twap))
        self.maker_profit_run_peak_bid_by_inst = {}
        self.maker_profit_run_peak_fair_by_inst = {}
        self.exit_policy = SimpleNamespace(stage=lambda _time_left: ExitStage.PASSIVE)
        self.exit_policy_engine = ExitPolicyEngine(_make_config())
        self.quote = (Decimal(bid), Decimal(ask))
        self.submissions = []
        self.cancelled_buys = []

    def _db_strategy_event(self, event_type, payload):
        return self.journal.write(event_type, payload)

    def _market_strike_is_entry_eligible(self, slug): return slug == self.current_market_slug
    def _maker_quote_instruments(self): return ["up", "down"]
    def _instrument_key(self, inst): return str(inst)
    def _order_key_for(self, side, inst): return f"{side}:{inst}"
    def _normalize_instrument_id(self, inst): return inst
    def _get_quote_for_instrument(self, inst): return self.quote if inst == self.held else (Decimal("0.75"), Decimal("0.80"))
    def _side_for_instrument_id(self, inst): return ActiveSide.UP if inst == "up" else ActiveSide.DOWN
    def _instrument_for_side(self, side): return "up" if side == ActiveSide.UP else "down"
    def _extract_token_id_from_instrument(self, _inst): return None
    async def _get_dynamic_fee_rate(self, **_kwargs): return Decimal("0")
    def _infer_market_fee_rate_default(self): return Decimal("0")
    def _is_emergency_exit_window(self, _time_left): return False
    def _get_effective_sellable_qty(self, **_kwargs): return self.live_inventory_cost[self.held]["qty"]
    def _cancel_maker_order_side(self, side, reason="", instrument_id=None):
        self.cancelled_buys.append((side, instrument_id, reason))

    def _submit_taker_exit_order(self, **kwargs):
        self.submissions.append(kwargs)
        self.pending_taker_exit_by_inst[str(kwargs["instrument_id"])] = True
        return True

    def cycle(self, now=NOW):
        asyncio.run(self._maybe_taker_exit_positions(now, is_simulation=False))

    def decision(self):
        state = self.live_inventory_cost[self.held]
        return self._breaker_v2_book().get(f"{self.current_market_slug}|{self.held}|{float(state['opened_ts']):.6f}")


def _opposite_signal(host):
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal("-0.30")
    return host


def test_b_host_suppress_blocks_the_breaker_sell_and_persists_first():
    host = _opposite_signal(_Host())
    host.cycle()
    assert host.decision() == SUPPRESS
    assert host.submissions == []
    (record,) = host.journal.decisions()
    assert record["decision"] == SUPPRESS and record["twap_state"] == TWAP_FAVORABLE
    for key in ("run_id", "slug", "instrument_id", "position_epoch", "opened_ts", "first_eligibility_ts",
                "decision_ts", "twap_value", "strike", "signed_distance_usd", "signed_distance_bps",
                "twap_age_sec", "time_left_sec", "held_side_best_bid", "breaker_reason", "breaker_components",
                "market_day_type", "legacy_breaker_would_sell"):
        assert key in record
    assert record["legacy_breaker_would_sell"] is True and record["conditional_breaker_suppressed"] is True
    assert record["market_day_type"] == "WEEKEND"


def test_a_host_allow_keeps_the_legacy_breaker_sell():
    host = _opposite_signal(_Host(twap=99.0))
    host.cycle()
    assert host.decision() == ALLOW
    assert len(host.submissions) == 1
    assert host.submissions[0]["decision_payload"]["decision_reason"] == "absolute_max_loss_breaker"


def test_suppress_that_cannot_be_persisted_degrades_to_legacy():
    journal = _Journal()
    journal.fail_writes = True
    host = _opposite_signal(_Host(journal=journal))
    host.cycle()
    assert host.decision() == FALLBACK_LEGACY
    assert len(host.submissions) == 1  # legacy breaker SELL


def test_h_suppress_stays_locked_after_twap_turns_adverse():
    host = _opposite_signal(_Host())
    host.cycle()
    host.twap.update(official_current_twap=99.0, source_ts=NOW + 9.0)  # later fresh adverse cross
    host.cycle(NOW + 5)
    host.cycle(NOW + 10)
    assert host.decision() == SUPPRESS
    assert host.submissions == []
    assert len(host.journal.decisions()) == 1


def test_i_allow_stays_locked():
    host = _opposite_signal(_Host(twap=99.0))
    host.cycle()
    host.pending_taker_exit_by_inst.clear()  # e.g. FOK rejected; retry later
    host.twap.update(official_current_twap=101.0, source_ts=NOW + 4.0)
    host.cycle(NOW + 5)
    assert host.decision() == ALLOW
    assert len(host.submissions) == 2 and len(host.journal.decisions()) == 1


def test_j_fallback_stays_locked():
    host = _opposite_signal(_Host())
    host.twap["source_ts"] = NOW - 30.0  # stale at first eligibility
    host.cycle()
    assert host.decision() == FALLBACK_LEGACY and len(host.submissions) == 1
    host.pending_taker_exit_by_inst.clear()
    host.twap["source_ts"] = NOW + 4.0  # fresh and favorable later
    host.cycle(NOW + 5)
    assert host.decision() == FALLBACK_LEGACY and len(host.submissions) == 2
    assert len(host.journal.decisions()) == 1


def test_l_host_suppress_then_catastrophic_sells():
    host = _opposite_signal(_Host(bid="0.20", ask="0.205"))
    host.active_maker_orders = {}
    host.stop_loss_enabled = False  # LIVE profile: only hard breakers may SELL
    host.exit_policy_engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False, hold_to_redeem_enabled=True))
    host.cycle()  # first eligibility: SUPPRESS, catastrophic confirming 1/2
    assert host.decision() == SUPPRESS and host.submissions == []
    host.cycle(NOW + 5)  # catastrophic confirmed
    assert host.decision() == SUPPRESS
    assert len(host.submissions) == 1
    assert host.submissions[0]["decision_payload"]["decision_reason"] == "catastrophic_stop_loss_confirmed"


def test_m_suppress_keeps_tp_and_sell_authority_but_blocks_buy_exposure():
    host = _opposite_signal(_Host())
    host.cycle()
    assert host.decision() == SUPPRESS
    assert "sell:up" in host.active_maker_orders  # resting TP never cancelled
    assert {inst for side, inst, _ in host.cancelled_buys} == {"up", "down"}
    assert all(side == "buy" for side, _, _ in host.cancelled_buys)
    sell = SimpleNamespace(side="SELL", instrument_id="up")
    buy_held = SimpleNamespace(side="BUY", instrument_id="up")
    buy_pair = SimpleNamespace(side="BUY", instrument_id="down")
    buy_next_market = SimpleNamespace(side="BUY", instrument_id="next-up")
    assert breaker_v2_buy_block_reason(host, sell) == ""
    assert breaker_v2_buy_block_reason(host, buy_held) == "breaker_v2_suppress_blocks_buy_exposure"
    assert breaker_v2_buy_block_reason(host, buy_pair) == "breaker_v2_suppress_blocks_buy_exposure"
    assert breaker_v2_buy_block_reason(host, buy_next_market) == ""


def test_m_execution_chokepoint_lets_tp_sell_through_and_refuses_buy():
    from bot.execution_safety import ExecutionSafetyMixin

    class _Base:
        def submit_order(self, order, *args, **kwargs):
            self.sent.append(order)
            return True

    class _Strategy(ExecutionSafetyMixin, _Base):
        test_mode = False

        def __init__(self):
            self.sent = []
            self.breaker_v2_authority_book = BreakerV2AuthorityBook()
            self.breaker_v2_authority_book.block_buys({"up", "down"})

        def _is_dry_run_mode(self): return False
        def _instrument_key(self, inst): return str(inst)

    strategy = _Strategy()
    assert strategy.submit_order(SimpleNamespace(side="SELL", instrument_id="up")) is True
    assert strategy.submit_order(SimpleNamespace(side="BUY", instrument_id="up")) is False
    assert strategy.submit_order(SimpleNamespace(side="BUY", instrument_id="other")) is True
    assert [o.side for o in strategy.sent] == ["SELL", "BUY"]


def test_n_no_duplicate_sell_under_allow_or_suppress():
    allow = _opposite_signal(_Host(twap=99.0))
    allow.cycle()
    allow.cycle(NOW + 5)  # pending protective SELL in flight
    assert len(allow.submissions) == 1
    suppress = _opposite_signal(_Host(bid="0.20", ask="0.205"))
    suppress.active_maker_orders = {}
    suppress.stop_loss_enabled = False
    suppress.exit_policy_engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False, hold_to_redeem_enabled=True))
    for i in range(4):
        suppress.cycle(NOW + 5 * i)
    assert len(suppress.submissions) == 1
    assert len(suppress.journal.decisions()) == 1


def test_o_partial_fill_or_partial_tp_keeps_the_same_epoch_decision():
    host = _opposite_signal(_Host(qty="10"))
    host.cycle()
    host.live_inventory_cost["up"]["qty"] = Decimal("12")   # another BUY fill aggregated
    host.cycle(NOW + 5)
    host.live_inventory_cost["up"]["qty"] = Decimal("6.2")  # partial TP / partial SELL
    host.twap.update(official_current_twap=99.0, source_ts=NOW + 9.0)
    host.cycle(NOW + 10)
    assert host.decision() == SUPPRESS and host.submissions == []
    assert len(host.journal.decisions()) == 1


def test_p_qty_zero_then_next_position_decides_afresh():
    host = _opposite_signal(_Host())
    host.cycle()
    assert host.decision() == SUPPRESS
    # Full exit resets opened_ts (bot.inventory); a new position opens later.
    host.live_inventory_cost["up"] = {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.69"),
                                      "opened_ts": NOW - 61}
    host.twap.update(official_current_twap=99.0, source_ts=NOW + 4.0)
    host.cycle(NOW + 5)
    assert host.decision() == ALLOW
    assert [d["decision"] for d in host.journal.decisions()] == [SUPPRESS, ALLOW]


def test_q_restart_rehydrates_the_same_decision_without_re_deciding():
    journal = _Journal()
    first = _opposite_signal(_Host(journal=journal))
    first.cycle()
    assert first.decision() == SUPPRESS
    # Process restart: memory lost, opened_ts rebuilt from the journal fill time.
    restarted = _opposite_signal(_Host(journal=journal, twap=99.0, opened_ts=NOW - 89.4))
    restarted.twap["source_ts"] = NOW + 19.0  # fresh ADVERSE now: a re-decision would ALLOW
    restarted.cycle(NOW + 20)
    assert restarted.decision() == SUPPRESS
    assert restarted.submissions == []
    assert len(journal.decisions()) == 1
    assert [t for _, t, _ in journal.events].count(REHYDRATED_EVENT) == 1


def test_q_restart_after_the_old_position_closed_decides_afresh():
    journal = _Journal()
    first = _opposite_signal(_Host(journal=journal))
    first.cycle()
    journal.sell_fills.append((len(journal.events) + 1, "up", 5.5))  # old position fully sold
    journal.events.append((len(journal.events), "ORDER_FILLED", {}))
    restarted = _opposite_signal(_Host(journal=journal, twap=99.0, opened_ts=NOW - 70))
    restarted.twap["source_ts"] = NOW + 19.0
    restarted.cycle(NOW + 20)
    assert restarted.decision() == ALLOW


def test_q_restart_with_unreadable_journal_fails_safe_to_legacy():
    journal = _Journal()
    journal.fail_lookup = True
    host = _opposite_signal(_Host(journal=journal))
    host.cycle()
    assert host.decision() == FALLBACK_LEGACY
    assert len(host.submissions) == 1  # legacy breaker, never a guessed SUPPRESS


def test_q_trade_journal_lookup_round_trip(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB

    db = TradeJournalDB(str(tmp_path / "j.db"), backup_path=str(tmp_path / "b.db"))
    try:
        assert db.load_breaker_v2_authority_decision(SLUG, "up")["decision_record"] is None
        assert db.log_strategy_event("run_x", DECISION_EVENT, {"slug": SLUG, "instrument_id": "up",
                                                               "decision": SUPPRESS, "qty": 5.5})
        found = db.load_breaker_v2_authority_decision(SLUG, "up")
        assert found["decision_record"]["decision"] == SUPPRESS
        assert rehydrated_decision(found) == (SUPPRESS, "rehydrated_from_journal")
        assert db.load_breaker_v2_authority_decision(SLUG, "down")["decision_record"] is None
        assert db.load_breaker_v2_authority_decision("btc-updown-15m-1", "up")["decision_record"] is None
    finally:
        db.stop()


def test_q_rehydration_rules():
    record = {"decision": SUPPRESS, "qty": 5.5}
    assert rehydrated_decision(None) == (FALLBACK_LEGACY, "rehydration_lookup_failed")
    assert rehydrated_decision({"decision_record": record, "sell_qty_after_decision": 1.0}) == (
        SUPPRESS, "rehydrated_from_journal")
    assert rehydrated_decision({"decision_record": record, "sell_qty_after_decision": 5.5})[0] is None
    assert rehydrated_decision({"decision_record": None, "prior_absolute_submit": True})[0] == FALLBACK_LEGACY
    assert rehydrated_decision({"decision_record": None})[0] is None
    assert rehydrated_decision({"decision_record": {"decision": "BOGUS", "qty": 5}})[0] == FALLBACK_LEGACY


def test_r_paired_token_and_next_market_do_not_inherit():
    journal = _Journal()
    up = _opposite_signal(_Host(journal=journal))
    up.cycle()
    assert up.decision() == SUPPRESS
    # Paired token of the same market (held DOWN, TWAP 101 > strike is adverse for DOWN).
    down = _Host(journal=journal, held="down", active_side=ActiveSide.UP, score="0.30")
    down.cycle()
    assert down.decision() == ALLOW
    # Next market, same token key reused by a test double: different slug, fresh decision.
    nxt_slug = "btc-updown-15m-1791624600"
    nxt = _opposite_signal(_Host(journal=journal, slug=nxt_slug, twap=99.0))
    nxt.cycle()
    assert nxt.decision() == ALLOW
    assert [(d["slug"], d["instrument_id"], d["decision"]) for d in journal.decisions()] == [
        (SLUG, "up", SUPPRESS), (SLUG, "down", ALLOW), (nxt_slug, "up", ALLOW)]


def test_s_flag_off_is_the_legacy_breaker():
    calls = []

    class _SpyEngine(ExitPolicyEngine):
        def evaluate(self, *args, **kwargs):
            calls.append(kwargs)
            return super().evaluate(*args, **kwargs)

    host = _opposite_signal(_Host(flag=False))
    host.exit_policy_engine = _SpyEngine(_make_config())
    host.cycle()
    assert len(host.submissions) == 1
    assert host.submissions[0]["decision_payload"]["decision_reason"] == "absolute_max_loss_breaker"
    assert host.journal.events == [] or all(t != DECISION_EVENT for _, t, _ in host.journal.events)
    assert len(calls) == 1 and "suppress_conditional_absolute" not in calls[0]
    assert getattr(host, "breaker_v2_authority_book", None) is None


def test_s_flag_default_is_off(monkeypatch):
    from bot.app_config import AppConfig

    monkeypatch.delenv("ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED", raising=False)
    assert AppConfig.from_env(enable_terminal_dashboard=False).exit.absolute_max_loss_twap_authority_gate_enabled is False
    monkeypatch.setenv("ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED", "1")
    assert AppConfig.from_env(enable_terminal_dashboard=False).exit.absolute_max_loss_twap_authority_gate_enabled is True
    assert "=0" in breaker_v2_authority_summary(False) and "=1" in breaker_v2_authority_summary(True)


def test_s_flag_is_in_the_run_manifest_config_hash(monkeypatch):
    from bot.app_config import AppConfig
    from bot.research.provenance import build_run_manifest

    monkeypatch.delenv("ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED", raising=False)
    off = build_run_manifest(run_id="r", config=AppConfig.from_env(enable_terminal_dashboard=False),
                             mode="TEST", test_mode=True, maker_mode=True)
    monkeypatch.setenv("ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED", "1")
    on = build_run_manifest(run_id="r", config=AppConfig.from_env(enable_terminal_dashboard=False),
                            mode="TEST", test_mode=True, maker_mode=True)
    assert json.dumps(off).count("absolute_max_loss_twap_authority_gate_enabled") >= 1
    assert off["config_hash"] != on["config_hash"]


# ------------------------------------------------------------ T shared helpers
def test_t_production_and_legacy_comparator_share_one_eligibility_predicate():
    assert exit_engine.conditional_absolute_components is breaker_eligibility.conditional_absolute_components
    assert stop_timing_telemetry.conditional_absolute_components is breaker_eligibility.conditional_absolute_components
    assert exit_engine.strong_opposite_signal is stop_timing_telemetry.strong_opposite_signal
    assert stop_timing_telemetry.signed_strike_distance is settlement_side.signed_strike_distance


@pytest.mark.parametrize("bid", ["0.20", "0.40", "0.65"])
@pytest.mark.parametrize("hold", [30.0, 90.0])
@pytest.mark.parametrize("tte", [100.0, 300.0])
@pytest.mark.parametrize("persist,votes", [(0.0, (0, 0)), (16.0, (2, 2)), (16.0, (1, 3))])
@pytest.mark.parametrize("inv", [False, True])
def test_t_comparator_agrees_with_the_engine_on_a_grid(bid, hold, tte, persist, votes, inv):
    config = _make_config()
    engine = ExitPolicyEngine(config)
    snapshot, position = _snapshot(bid, time_left_sec=tte), _position("0.69", hold_sec=hold)
    signal = _signal(score=Decimal("-0.30"), locked=True, matches=False, active_side="DOWN")
    decision = engine.evaluate(snapshot, position, signal, locked_side_invalidated=inv,
                               confirmed_adverse_exit_active=inv, adverse_persistence_sec=persist,
                               adverse_thesis_weakening_count=votes[0], adverse_thesis_available_count=votes[1])
    comps = stop_timing_telemetry.absolute_breaker_components(
        enabled=True, min_hold_sec=60, loss_usdc=Decimal("2.00"), thesis_min_score_abs=Decimal("0.05"),
        hold_sec=hold, avg_entry=Decimal("0.69"), best_bid=Decimal(bid), gross_if_exit=decision.gross_if_exit,
        net_if_exit=decision.net_if_exit, time_left_sec=tte, locked_side_invalidated=inv,
        signal_matches_position=False, signal_side="DOWN", signal_locked=True, signal_score=Decimal("-0.30"),
        adverse_persistence_sec=persist, weakening_count=votes[0], available_count=votes[1])
    assert comps["abs_eligible_recomputed"] == (decision.reason == "absolute_max_loss_breaker")


# ------------------------------------------------------------ telemetry legacy-vs-V2
def test_stop_timing_records_legacy_would_sell_and_v2_decision_under_suppress():
    events = []
    host = _opposite_signal(_Host())
    host.stop_timing_telemetry = stop_timing_telemetry.StopTimingTelemetry(
        emit=lambda event_type, payload: events.append((event_type, payload)) or True)
    host.cycle()
    host.stop_timing_telemetry.on_settlement_safe(slug=SLUG, outcome="UP", settlement_ts=NOW + 100)
    eligible = [p for t, p in events if t == "STOP_TIMING_BREAKER_FIRST_ELIGIBLE"]
    assert len(eligible) == 1
    assert eligible[0]["legacy_breaker_would_sell"] is True
    assert eligible[0]["conditional_breaker_suppressed"] is True
    assert eligible[0]["breaker_v2_authority_decision"] == SUPPRESS
    assert eligible[0]["decision_reason"] != "absolute_max_loss_breaker"
    (settlement,) = [p for t, p in events if t == "STOP_TIMING_POSITION_SETTLEMENT"]
    assert settlement["legacy_breaker_would_sell"] is True
    assert settlement["breaker_v2_authority_decision"] == SUPPRESS
    assert settlement["conditional_breaker_suppressed"] is True
    assert settlement["legacy_exit_estimate"]["best_bid"] == pytest.approx(0.20)
    assert settlement["market_day_type"] == "WEEKEND"


# ------------------------------------------------------------ recent 3-case replay
# Inputs copied verbatim from the journal's STOP_TIMING_BREAKER_FIRST_ELIGIBLE rows
# (held side, TWAP, Chainlink source age, verified strike).  Market ids label the
# cases only; the decision sees no id and no official outcome.
RECENT_FIRST_ELIGIBILITY = [
    ("1791587700", "UP", 82540.21594238927, 2.0964999198913574, 82551.99021645996, ALLOW),
    ("1791589500", "UP", 82549.34069227375, 1.4332540035247803, 82559.14713426317, ALLOW),
    ("1791623700", "DOWN", 82826.4321433614, 1.105302095413208, 82837.20514112496, SUPPRESS),
]


@pytest.mark.parametrize("label,held,twap,age,strike,expected", RECENT_FIRST_ELIGIBILITY)
def test_recent_three_breaker_cases_replay_allow_allow_suppress(label, held, twap, age, strike, expected):
    slug = "btc-updown-15m-replay"
    state = canonical_twap_side_state(
        held_side=held, twap_features={"official_current_twap": twap, "source_ts": NOW - age, "market_slug": slug},
        strike=strike, strike_verified=True, expected_slug=slug, now_ts=NOW)
    assert decide_authority(state["state"])[0] == expected

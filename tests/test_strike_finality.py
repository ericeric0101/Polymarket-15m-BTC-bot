"""Strike finality gate (hermetic, no network).

Polymarket's frontend ``crypto-price`` endpoint returns the market's opening
Price To Beat (``openPrice``) while the window is still running, flagged
``completed=false, incomplete=true``; the value is only final once the window
has completed (``completed=true, incomplete=false``).  The runtime used to lock
the first openPrice it saw ~2 s after the window opened.  For
btc-updown-15m-1791711900 that provisional value (82956.853422…) differed from
the finalized one (82956.368497…) and the runtime labeled an official UP as
DOWN.  Tie semantics are from the market rules: TWAP >= price-to-beat -> Up.
"""
import json
import threading
import time
from decimal import Decimal

import pytest

import bot.strike_finality as sf
from bot.breaker_authority import DECISION_EVENT, SUPPRESS
from bot.execution_events import audit_reconciliation
from test_breaker_v2_authority import _Host as BreakerHost, _opposite_signal
from test_settlement_authority import Host as SettlementHost, UP_INST

# --- the known market (Decimal strings from the official endpoints) --------------------------
KNOWN_SLUG = "btc-updown-15m-1791711900"
KNOWN_START, KNOWN_END = 1791711900, 1791712800
RUNTIME_PROVISIONAL = Decimal("82956.85342236787")
OFFICIAL_FINAL_STRIKE = Decimal("82956.36849728331")
OFFICIAL_CLOSE = Decimal("82956.41468623196")


def _snap(open_price, *, completed=False, incomplete=True, close=None, ts=0.0):
    return sf.parse_crypto_price_payload(
        {"openPrice": None if open_price is None else float(open_price),
         "closePrice": None if close is None else float(close),
         "completed": completed, "incomplete": incomplete, "cached": True},
        fetched_ts=ts,
    )


def _final(open_price, close=None, ts=0.0):
    return _snap(open_price, completed=True, incomplete=False, close=close, ts=ts)


# 1 ---------------------------------------------------------------------------------------------
def test_1_provisional_open_price_never_becomes_final():
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="polymarket_crypto_price_twap_open",
                                 start_ts=KNOWN_START, end_ts=KNOWN_END, now=KNOWN_START + 2)
    assert tracker.status(KNOWN_SLUG) == sf.STATUS_PROVISIONAL
    for snap in (_snap(RUNTIME_PROVISIONAL), _snap(OFFICIAL_FINAL_STRIKE),
                 _snap(OFFICIAL_FINAL_STRIKE, completed=True, incomplete=True),
                 _snap(OFFICIAL_FINAL_STRIKE, completed=None, incomplete=False),
                 _snap(OFFICIAL_FINAL_STRIKE, completed="true", incomplete="false")):
        tracker.observe(KNOWN_SLUG, snap, now=KNOWN_START + 30)
        assert tracker.status(KNOWN_SLUG) == sf.STATUS_PROVISIONAL
        assert tracker.final_strike(KNOWN_SLUG) is None
    assert sf.is_final_snapshot(_snap(OFFICIAL_FINAL_STRIKE)) is False


# 2 ---------------------------------------------------------------------------------------------
def test_2_completed_snapshot_locks_final_strike_immutably():
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=KNOWN_START + 2)
    events = tracker.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE, OFFICIAL_CLOSE), now=KNOWN_END + 3)
    assert tracker.status(KNOWN_SLUG) == sf.STATUS_FINAL
    assert tracker.final_strike(KNOWN_SLUG) == OFFICIAL_FINAL_STRIKE
    (event_type, payload), = [e for e in events if e[0] == sf.EVENT_FINALIZED]
    assert payload["completed"] is True and payload["incomplete"] is False
    assert payload["finalized_ts"] == KNOWN_END + 3
    assert payload["source"] == "polymarket_crypto_price_completed"
    # Immutable: a later different "final" value is journaled as a conflict, never applied.
    later = tracker.observe(KNOWN_SLUG, _final(Decimal("1.0")), now=KNOWN_END + 9)
    assert tracker.final_strike(KNOWN_SLUG) == OFFICIAL_FINAL_STRIKE
    assert [e for e, _ in later] == [sf.EVENT_FINAL_CONFLICT]
    tracker.register_provisional(KNOWN_SLUG, Decimal("2.0"), source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=KNOWN_END + 10)
    assert tracker.status(KNOWN_SLUG) == sf.STATUS_FINAL


# 3 ---------------------------------------------------------------------------------------------
def test_3_provisional_revisions_and_final_delta_are_journaled():
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=KNOWN_START + 2)
    assert tracker.observe(KNOWN_SLUG, _snap(RUNTIME_PROVISIONAL), now=KNOWN_START + 17) == []
    (rev_type, rev), = tracker.observe(KNOWN_SLUG, _snap(OFFICIAL_FINAL_STRIKE), now=KNOWN_START + 47)
    assert rev_type == sf.EVENT_PROVISIONAL_REVISED
    assert Decimal(rev["old_strike"]) == RUNTIME_PROVISIONAL
    assert Decimal(rev["new_strike"]) == OFFICIAL_FINAL_STRIKE
    assert Decimal(rev["delta"]) == OFFICIAL_FINAL_STRIKE - RUNTIME_PROVISIONAL
    assert rev["observed_ts"] == KNOWN_START + 47 and rev["incomplete"] is True
    (_, fin), = tracker.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE), now=KNOWN_END + 2)
    assert Decimal(fin["locked_provisional_strike"]) == RUNTIME_PROVISIONAL
    assert Decimal(fin["delta_vs_locked_provisional"]) == Decimal("-0.48492508456")
    assert fin["differs_from_locked_provisional"] is True


# 4 ---------------------------------------------------------------------------------------------
def test_4_tie_settles_up_per_market_rules_and_requires_decimal():
    strike = Decimal("82956.36849728331")
    assert sf.settle_side(strike, strike) == "UP"
    assert sf.settle_side(strike + Decimal("1E-11"), strike) == "UP"
    assert sf.settle_side(strike - Decimal("1E-11"), strike) == "DOWN"
    with pytest.raises(TypeError):
        sf.settle_side(82956.4, strike)


# 5 + 8 -----------------------------------------------------------------------------------------
def test_5_8_known_market_final_outcome_uses_the_final_strike():
    assert OFFICIAL_CLOSE - OFFICIAL_FINAL_STRIKE == Decimal("0.04618894865")
    assert sf.settle_side(OFFICIAL_CLOSE, OFFICIAL_FINAL_STRIKE) == "UP"          # official
    assert sf.settle_side(OFFICIAL_CLOSE, RUNTIME_PROVISIONAL) == "DOWN"          # what runtime did
    assert RUNTIME_PROVISIONAL - OFFICIAL_FINAL_STRIKE == Decimal("0.48492508456")


def _tracked_settlement_host(*, final=None, qty=Decimal("4"), close=float(OFFICIAL_CLOSE)):
    """Settlement host for the known market: provisional strike locked, end-stamped TWAP tick."""
    host = SettlementHost(twap=close, spot=0.0, qty=qty)
    host.current_market_slug = KNOWN_SLUG
    host.market_strike_cache_by_slug = {KNOWN_SLUG: RUNTIME_PROVISIONAL}
    host._shadow_state = {}
    host.strike_finality = sf.StrikeFinalityTracker()
    host.strike_finality.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x",
                                              start_ts=KNOWN_START, end_ts=KNOWN_END, now=time.time())
    if final is not None:
        host.strike_finality.observe(KNOWN_SLUG, _final(final), now=time.time())
    return host


def _events(host, event_type):
    return [p for e, p in host.events if e == event_type]


def test_5_settlement_with_final_strike_reproduces_official_up():
    host = _tracked_settlement_host(final=OFFICIAL_FINAL_STRIKE).settle()
    settlement = host.event("MARKET_SETTLEMENT")
    assert settlement["outcome"] == "UP"
    assert settlement["strike"] == float(OFFICIAL_FINAL_STRIKE)
    assert settlement["strike_finality"] == sf.STATUS_FINAL
    assert Decimal(settlement["strike_locked_provisional"]) == RUNTIME_PROVISIONAL
    (cycle,) = _events(host, "MARKET_CYCLE_PNL")
    assert cycle["cycle_settlement_pnl_usdc"] == pytest.approx(4 - 4 * 0.75)


# 6 ---------------------------------------------------------------------------------------------
def test_6_missing_final_strike_fails_closed_then_finalizes():
    host = _tracked_settlement_host().settle()
    pending = host.event("MARKET_SETTLEMENT")
    assert pending["outcome"] == "UNKNOWN" and pending["settlement_pending"] is True
    assert pending["outcome_source"] == "strike_unfinalized"
    assert pending["strike_finality"] == sf.STATUS_PROVISIONAL
    assert pending["provisional_outcome_diagnostic"] == "DOWN"
    assert not _events(host, "MARKET_CYCLE_PNL") and host.session_pnl == [] and host.regime == []

    events = host.strike_finality.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE), now=time.time())
    host._on_strike_finality_events(KNOWN_SLUG, events)
    final = _events(host, "MARKET_SETTLEMENT")[-1]
    assert final["outcome"] == "UP" and final["outcome_source"] == "canonical_twap_final_strike"
    assert final["strike"] == float(OFFICIAL_FINAL_STRIKE)
    (cycle,) = _events(host, "MARKET_CYCLE_PNL")
    assert cycle["slug"] == KNOWN_SLUG
    assert [r["source"] for r in host.regime] == ["settlement_strike_finalized"]
    assert _events(host, sf.EVENT_FINALIZED)
    # exactly once
    host._on_strike_finality_events(KNOWN_SLUG, events)
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1


def test_6_finalization_timeout_leaves_unknown_for_startup_reconciliation():
    host = _tracked_settlement_host().settle()
    events = host.strike_finality.expire(KNOWN_SLUG, now=time.time())
    host._on_strike_finality_events(KNOWN_SLUG, events)
    assert host.strike_finality.status(KNOWN_SLUG) == sf.STATUS_PROVISIONAL
    assert not _events(host, "MARKET_CYCLE_PNL")
    (expired,) = _events(host, "MARKET_SETTLEMENT_RELABEL_EXPIRED")
    assert expired["reason"] == "strike_finalization_timeout"
    assert _events(host, sf.EVENT_FINALIZATION_EXPIRED)


def test_6_no_inventory_market_keeps_fill_only_cycle_pnl_but_defers_the_label():
    host = _tracked_settlement_host(qty=Decimal("0")).settle()
    label = host.event("MARKET_SETTLEMENT")
    assert label["outcome"] == "UNKNOWN" and label["outcome_source"] == "strike_unfinalized"
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1  # fill-only PnL is outcome independent
    events = host.strike_finality.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE), now=time.time())
    host._on_strike_finality_events(KNOWN_SLUG, events)
    assert _events(host, "MARKET_SETTLEMENT")[-1]["outcome"] == "UP"
    assert len(_events(host, "MARKET_CYCLE_PNL")) == 1


def test_untracked_legacy_host_is_unchanged():
    host = SettlementHost(twap=80001.0, spot=0.0).settle()
    settlement = host.event("MARKET_SETTLEMENT")
    assert settlement["outcome"] == "UP" and settlement["outcome_source"] == "canonical_twap"


# 7 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("finality", [None, "provisional", "final_same"])
def test_7_breaker_v2_decision_unchanged_and_records_strike_finality(finality):
    host = _opposite_signal(BreakerHost())
    slug = host.current_market_slug
    if finality is not None:
        host.strike_finality = sf.StrikeFinalityTracker()
        host.strike_finality.register_provisional(slug, Decimal("100.0"), source="x", start_ts=0, end_ts=1,
                                                  now=0)
        if finality == "final_same":
            host.strike_finality.observe(slug, _final(Decimal("100.0")), now=2)
    host.cycle()
    assert host.decision() == SUPPRESS and host.submissions == []
    (record,) = [p for _, t, p in host.journal.events if t == DECISION_EVENT]
    expected = {None: "UNTRACKED", "provisional": sf.STATUS_PROVISIONAL, "final_same": sf.STATUS_FINAL}[finality]
    assert record["strike_finality"] == expected
    assert record["strike"] == 100.0


# 9 ---------------------------------------------------------------------------------------------
class _FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))
        return False  # stop event not set


def test_9_polling_failures_keep_provisional_and_expire_bounded():
    clock = _FakeClock(KNOWN_START + 2)
    calls = []

    def failing_fetch(start_ts, end_ts):
        calls.append(clock())
        raise TimeoutError("upstream timeout")

    emitted = []
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=clock())
    poller = sf.StrikeFinalityPoller(tracker, failing_fetch, lambda slug, ev: emitted.extend(ev),
                                     clock=clock, sleep=clock.sleep)
    poller.run_blocking(KNOWN_SLUG, KNOWN_START, KNOWN_END)
    assert tracker.status(KNOWN_SLUG) == sf.STATUS_PROVISIONAL
    assert [e for e, _ in emitted] == [sf.EVENT_FINALIZATION_EXPIRED]
    assert clock() <= KNOWN_END + sf.FINALIZATION_MAX_WAIT_SEC + sf.POST_END_POLL_SEC[-1]
    assert 5 <= len(calls) <= 120  # bounded, backed off


def test_9_poller_start_never_blocks_the_caller():
    release = threading.Event()

    def hanging_fetch(start_ts, end_ts):
        release.wait(5)
        return None

    start = int(time.time())  # a live window, so the poller does not expire immediately
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=start,
                                 end_ts=start + 900, now=time.time())
    poller = sf.StrikeFinalityPoller(tracker, hanging_fetch, lambda *_: None)
    t0 = time.monotonic()
    poller.start(KNOWN_SLUG, start, start + 900)
    poller.start(KNOWN_SLUG, start, start + 900)  # deduplicated
    assert time.monotonic() - t0 < 0.5
    assert poller.active_slugs() == {KNOWN_SLUG}
    poller.stop()
    release.set()


def test_9_poller_finalizes_after_window_end():
    clock = _FakeClock(KNOWN_START + 2)

    def fetch(start_ts, end_ts):
        if clock() < KNOWN_END + 4:
            return _snap(OFFICIAL_FINAL_STRIKE, ts=clock())
        return _final(OFFICIAL_FINAL_STRIKE, OFFICIAL_CLOSE, ts=clock())

    emitted = []
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=clock())
    sf.StrikeFinalityPoller(tracker, fetch, lambda s, ev: emitted.extend(ev), clock=clock,
                            sleep=clock.sleep).run_blocking(KNOWN_SLUG, KNOWN_START, KNOWN_END)
    assert [e for e, _ in emitted] == [sf.EVENT_PROVISIONAL_REVISED, sf.EVENT_FINALIZED]
    assert tracker.final_strike(KNOWN_SLUG) == OFFICIAL_FINAL_STRIKE
    assert KNOWN_END + 4 <= clock() <= KNOWN_END + 8


# 10 --------------------------------------------------------------------------------------------
def test_10_restart_rebuilds_final_and_provisional_state_from_journal():
    rows = [
        ("MARKET_STRIKE_LOCKED", {"slug": KNOWN_SLUG, "strike": float(RUNTIME_PROVISIONAL),
                                  "strike_finality": sf.STATUS_PROVISIONAL}),
        (sf.EVENT_FINALIZED, {"slug": KNOWN_SLUG, "finalized_strike": str(OFFICIAL_FINAL_STRIKE),
                              "finalized_ts": KNOWN_END + 3,
                              "locked_provisional_strike": str(RUNTIME_PROVISIONAL)}),
    ]
    tracker = sf.StrikeFinalityTracker()
    assert sf.restore_finality_from_events(tracker, KNOWN_SLUG, rows) == sf.STATUS_FINAL
    assert tracker.final_strike(KNOWN_SLUG) == OFFICIAL_FINAL_STRIKE

    tracker2 = sf.StrikeFinalityTracker()
    assert sf.restore_finality_from_events(tracker2, KNOWN_SLUG, rows[:1]) == sf.STATUS_PROVISIONAL
    assert tracker2.final_strike(KNOWN_SLUG) is None
    assert sf.restore_finality_from_events(sf.StrikeFinalityTracker(), KNOWN_SLUG, []) is None


def test_10_trade_journal_round_trip(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB
    db = TradeJournalDB(db_path=str(tmp_path / "trading" / "trade_journal.db"))
    run_id = "run_strike_test"
    db.log_strategy_event(run_id, sf.EVENT_FINALIZED, {
        "slug": KNOWN_SLUG, "finalized_strike": str(OFFICIAL_FINAL_STRIKE), "finalized_ts": KNOWN_END + 3})
    rows = db.load_strike_finality_events(KNOWN_SLUG)
    tracker = sf.StrikeFinalityTracker()
    assert sf.restore_finality_from_events(tracker, KNOWN_SLUG, rows) == sf.STATUS_FINAL
    assert tracker.final_strike(KNOWN_SLUG) == OFFICIAL_FINAL_STRIKE


# 11 --------------------------------------------------------------------------------------------
def test_11_journal_write_failure_is_not_fatal():
    host = _tracked_settlement_host().settle()

    def broken(event_type, payload):
        if event_type.startswith("STRIKE_"):
            raise OSError("disk full")
        host.events.append((event_type, payload))

    host._db_strategy_event = broken
    events = host.strike_finality.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE), now=time.time())
    host._on_strike_finality_events(KNOWN_SLUG, events)  # must not raise
    assert _events(host, "MARKET_SETTLEMENT")[-1]["outcome"] == "UP"

    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=0)

    def crash(*_):
        raise RuntimeError("journal down")

    clock = _FakeClock(KNOWN_END + 1)
    sf.StrikeFinalityPoller(tracker, lambda *_: _final(OFFICIAL_FINAL_STRIKE), crash, clock=clock,
                            sleep=clock.sleep).run_blocking(KNOWN_SLUG, KNOWN_START, KNOWN_END)
    assert tracker.status(KNOWN_SLUG) == sf.STATUS_FINAL


# 12 --------------------------------------------------------------------------------------------
def test_12_existing_journal_readers_tolerate_new_event_types():
    tracker = sf.StrikeFinalityTracker()
    tracker.register_provisional(KNOWN_SLUG, RUNTIME_PROVISIONAL, source="x", start_ts=KNOWN_START,
                                 end_ts=KNOWN_END, now=0)
    new_events = (tracker.observe(KNOWN_SLUG, _snap(OFFICIAL_FINAL_STRIKE), now=1)
                  + tracker.observe(KNOWN_SLUG, _final(OFFICIAL_FINAL_STRIKE), now=2))
    rows = [{"event_type": t, "ts": "2026-10-11T10:00:03+00:00", "payload": json.loads(json.dumps(p))}
            for t, p in new_events]
    audit_reconciliation(rows)  # must not raise
    for _, payload in new_events:
        json.dumps(payload)  # journal-serializable

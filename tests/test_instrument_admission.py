"""Real selection/reconciliation and lifecycle, with synthetic provider/network only."""
import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import instrument_admission as admission
from bot import market_runtime as runtime
from bot.enums import ActiveSide, MarketPhase
from bot.lifecycle_runtime import StrategyLifecycleMixin
from bot.ops import handle_waiting_phase_search

BASE = 1800000000
slug = lambda n: f"btc-updown-15m-{BASE + n * 900}"


def pair(n):
    return [SimpleNamespace(id=f"{n}-{side}", info={"market_slug": slug(n),
                "active": True, "closed": False}, side=side) for side in ("up", "down")]


class Cache:
    def __init__(self, instruments=()):
        self.items = {i.id: i for i in instruments}
        self.fail_down = False

    def instruments(self):
        return list(self.items.values())

    def add_instrument(self, instrument):
        if self.fail_down and instrument.side == "down":
            raise RuntimeError("synthetic_cache_failure")
        self.items[instrument.id] = instrument


class Host(StrategyLifecycleMixin):
    def __init__(self, cache):
        self.cache = cache
        self.calls, self.events = [], []
        self.trade_db = SimpleNamespace(enqueue_strategy_event=lambda *a: self.events.append(a) or True)
        self.run_id, self.collection_identity = "synthetic", {"cycle_idx": 1}
        self._stopping = False
        self.market_next_poll_sec = 10
        self.auto_redeem_enabled = False
        self.current_market_slug, self.selected_slug = slug(0), slug(0)
        self.instrument_id = "0-up"
        self.current_market_instruments = ["0-up", "0-down"]
        self.current_phase = MarketPhase.ACTIVE
        self.startup_verbose, self.bi_side_enabled = False, True
        self.active_side, self.active_side_locked = ActiveSide.UP, True
        self.side_decision_reason, self.side_decision_score = "unchanged", 1
        self.side_decision_ts, self.side_decision_inputs = BASE, {}
        self.side_flip_count, self.side_pending_flip_count = 0, 0
        self.side_pending_flip_side = ActiveSide.NONE
        self.market_start_ts_by_slug = {slug(0): BASE}
        self.bi_side_decision_grace_sec = 90
        self.quote_stale_sec = 10
        self.latest_quote_by_inst, self.latest_quote_depth_by_inst = {}, {}
        self.last_quote_update_ts_by_inst, self.last_quote_source_ts_by_inst = {}, {}
        self.last_quote_received_ts_by_inst = {}
        self._managed_market_quote_subscription_ids = {"0-up", "0-down"}
        self._managed_market_l2_subscription_ids = {"0-up", "0-down"}
        self._managed_market_subscription_instruments = {i: i for i in self.current_market_instruments}
        self.next_market_slug = None

    _extract_outcome_from_instrument = staticmethod(lambda i: i.side)
    _normalize_instrument_id = staticmethod(lambda i: i)
    _sync_active_instrument = _log_strike_status = lambda *a, **kw: None
    _reset_maker_state_for_new_market = lambda *a, **kw: None
    _reset_side_decision_state = lambda *a: None
    _instrument_for_side = lambda self, side: self.instrument_id
    _find_btc_instrument = runtime.find_btc_instrument
    subscribe_quote_ticks = lambda self, i: self.calls.append(("quote", i))
    subscribe_order_book_deltas = lambda self, i: self.calls.append(("l2", i))
    unsubscribe_quote_ticks = lambda self, i: self.calls.append(("unquote", i))
    unsubscribe_order_book_deltas = lambda self, i: self.calls.append(("unl2", i))


class Provider:
    def __init__(self, instruments):
        self.items = {i.id: i for i in instruments}
        self.loads = []
        self.gate = None
        self.error = None

    async def load_ids_async(self, ids):
        self.loads.append(ids)
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error

    def find(self, i):
        return self.items.get(i)


@pytest.fixture
def setup(monkeypatch):
    clock = SimpleNamespace(now=BASE + 1)
    monkeypatch.setattr(admission.time, "time", lambda: clock.now)

    async def resolve(s):
        n = (int(s.rsplit("-", 1)[-1]) - BASE) // 900
        return [i.id for i in pair(n)]
    monkeypatch.setattr(admission, "resolve_pair_ids", resolve)

    def collect(instruments, **kwargs):
        rows = []
        for i in instruments:
            start = int(i.info["market_slug"].rsplit("-", 1)[-1])
            rows.append(dict(instrument=i, slug=i.info["market_slug"], market_timestamp=start,
                             end_timestamp=start+900, closed=False,
                             time_diff_minutes=(start-clock.now)/60))
        return rows, int(clock.now)
    monkeypatch.setattr(runtime, "collect_btc_market_candidates", collect)
    return clock


async def drain(controller):
    await asyncio.sleep(0)
    if controller.task is not None:
        await controller.task


def test_admit_deduplicate_and_prewarm_through_existing_selection(setup):
    async def check():
        host = Host(Cache(pair(0)))
        provider = Provider(pair(1))
        ctrl = host._instrument_admission = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        # Actual selection schedules the first unavailable next pair.
        assert host._find_btc_instrument()
        before = list(host.calls)
        for _ in range(100):
            ctrl.request(slug(1))
        await drain(ctrl)
        assert len(provider.loads) == 1
        assert host.calls == before and host.current_market_slug == slug(0)
        assert host.active_side == ActiveSide.UP and host.side_decision_reason == "unchanged"
        assert ctrl.cached_pair(slug(1))
        assert host._find_btc_instrument()
        assert host._managed_market_quote_subscription_ids == {"0-up", "0-down", "1-up", "1-down"}
        assert host._managed_market_l2_subscription_ids == {"0-up", "0-down"}
        assert ("l2", "1-up") not in host.calls
        ctrl.request(slug(1))
        await drain(ctrl)
        assert len(provider.loads) == 1 and len(ctrl.records) == 1
        assert [e[1] for e in host.events if e[1].startswith("DYNAMIC_")] == [
            "DYNAMIC_INSTRUMENT_ADMISSION_REQUESTED", "DYNAMIC_INSTRUMENT_ADMISSION_STARTED",
            "DYNAMIC_INSTRUMENT_ADMISSION_COMPLETED"]
        assert host.events[-2][2].get("pair_complete", True)
    asyncio.run(check())


def test_existing_pair_noop(setup):
    async def check():
        host = Host(Cache(pair(0) + pair(1)))
        provider = Provider([])
        ctrl = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        assert ctrl.request(slug(1))
        await drain(ctrl)
        assert provider.loads == [] and ctrl.records == {}
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["incomplete", "load_error", "cache_error", "wrong_outcome", "wrong_slug"])
def test_failed_pair_quarantined_and_existing_fallback_preserved(setup, failure):
    async def check():
        host = Host(Cache(pair(0)))
        objects = pair(1)
        if failure == "incomplete": objects.pop()
        if failure == "wrong_outcome": objects[1].side = "up"
        if failure == "wrong_slug": objects[1].info["market_slug"] = slug(2)
        provider = Provider(objects)
        if failure == "load_error": provider.error = RuntimeError("halfway_failure")
        host.cache.fail_down = failure == "cache_error"
        ctrl = host._instrument_admission = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        ctrl.request(slug(1))
        await drain(ctrl)
        assert not ctrl.selectable(slug(1)) and ctrl.records[slug(1)]["status"] == "failed"
        setup.now = BASE + 901
        host.current_phase = MarketPhase.WAITING
        host._resolve_btc_15m_market_slugs = lambda: [slug(1)]
        stops = []
        misses = 0
        for _ in range(3):
            misses = handle_waiting_phase_search(host._search_next_market, lambda: None, None,
                slug(1), 10, misses, 3, lambda _: None, lambda _: None, lambda _: None,
                lambda: stops.append(True))
        assert stops == [True] and host.current_market_slug == slug(0)
        ctrl.stop()
        await asyncio.sleep(0)
        await drain(ctrl)
    asyncio.run(check())


@pytest.mark.parametrize("race", ["shutdown", "fallback", "advanced", "expired", "settlement", "reload"])
def test_pending_races(setup, race):
    async def check():
        host = Host(Cache(pair(0)))
        provider = Provider(pair(1))
        provider.gate = asyncio.Event()
        ctrl = host._instrument_admission = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        ctrl.request(slug(1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if race == "shutdown": ctrl.stop()
        if race == "fallback": host._stopping = True
        if race == "advanced": host.current_market_slug = slug(2)
        if race == "expired": setup.now = BASE + 1801
        if race == "settlement": host.current_phase = MarketPhase.SETTLING
        if race == "reload": assert host._find_btc_instrument()
        # Pending pair cannot appear in selection, even if the adapter's
        # periodic instrument publication exposes a single outcome early.
        host.cache.add_instrument(pair(1)[0])
        assert not ctrl.selectable(slug(1))
        provider.gate.set()
        await drain(ctrl)
        success = race in {"settlement", "reload"}
        assert ctrl.selectable(slug(1)) == success
        assert host.current_market_slug == (slug(2) if race == "advanced" else slug(0))
        assert host.calls == []
        assert ctrl.pending is ctrl.task is None
    asyncio.run(check())


def test_dynamic_bound_retry_bound_and_no_task_accumulation(setup):
    async def check():
        host = Host(Cache())
        provider = Provider(sum((pair(i) for i in range(1, 17)), []))
        ctrl = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        for n in range(1, admission.MAX_DYNAMIC_MARKETS + 1):
            setup.now = BASE + (n-1)*900 + 1
            assert ctrl.request(slug(n))
            assert not ctrl.request(slug(n+1))
            await drain(ctrl)
        setup.now += 900
        assert not ctrl.request(slug(admission.MAX_DYNAMIC_MARKETS + 1))
        assert len(ctrl.records) == 14 and len(host.cache.items) == 28
        assert len(provider.loads) == 14
        other = admission.InstrumentAdmission(Host(Cache()), Provider([]), asyncio.get_running_loop())
        for _ in range(3):
            assert other.request(slug(14))
            await drain(other)
            setup.now += 10
        assert not other.request(slug(14))
        assert len(other.records) == 1
    asyncio.run(check())


def test_finite_startup_universe_continues_without_stale_rebuild(setup):
    async def check():
        host = Host(Cache(sum((pair(i) for i in range(5)), [])))
        provider = Provider(pair(5))
        ctrl = host._instrument_admission = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        # Consume every initially available market through actual selection.
        for n in range(5):
            setup.now = BASE + n*900 + 1
            host.current_phase = MarketPhase.ACTIVE
            assert host._find_btc_instrument()
            assert host.current_market_slug == slug(n)
        await drain(ctrl)
        assert ctrl.cached_pair(slug(5))
        setup.now = BASE + 5*900 + 16
        host.current_phase = MarketPhase.WAITING
        host._resolve_btc_15m_market_slugs = lambda: [slug(5)]
        stops = []
        misses = handle_waiting_phase_search(host._search_next_market, lambda: None, None,
            slug(5), 10, 2, 3, lambda _: None, lambda _: None, lambda _: None,
            lambda: stops.append(True))
        assert misses == 0 and stops == []
        assert host.current_market_slug == slug(5)
        assert host.current_market_instruments == ["5-up", "5-down"]
        assert host._managed_market_l2_subscription_ids == {"5-up", "5-down"}
        ctrl.stop()
        await asyncio.sleep(0)
        await drain(ctrl)
    asyncio.run(check())


def test_timeout_does_not_block_owner_loop(setup, monkeypatch):
    async def check():
        host = Host(Cache(pair(0)))
        provider = Provider(pair(1))
        provider.gate = asyncio.Event()
        ctrl = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        monkeypatch.setattr(admission, "LOAD_TIMEOUT_SEC", 0.01)
        ctrl.request(slug(1))
        await asyncio.sleep(0)
        ticks = 0
        while ctrl.pending is not None:
            ticks += 1
            await asyncio.sleep(0.001)
        assert ticks > 1
        assert ctrl.records[slug(1)]["status"] == "failed"
        assert ctrl.task is None and len(host.cache.items) == 2
        failed = next(e[2] for e in host.events if e[1].endswith("FAILED"))
        assert failed["error_class"] == "TimeoutError"
    asyncio.run(check())


def test_stop_before_queued_task_starts(setup):
    async def check():
        host = Host(Cache())
        provider = Provider(pair(1))
        ctrl = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        ctrl.request(slug(1))
        ctrl.stop()
        await asyncio.sleep(0)
        assert provider.loads == [] and ctrl.pending is ctrl.task is None
        assert not ctrl.request(slug(1))
    asyncio.run(check())


def test_cache_failure_can_retry_without_new_ids_or_duplicate_subscriptions(setup):
    async def check():
        host = Host(Cache(pair(0)))
        host.cache.fail_down = True
        provider = Provider(pair(1))
        ctrl = host._instrument_admission = admission.InstrumentAdmission(host, provider, asyncio.get_running_loop())
        ctrl.request(slug(1))
        await drain(ctrl)
        assert not ctrl.selectable(slug(1)) and "1-up" in host.cache.items
        assert not ctrl.request(slug(1))  # retry cadence
        host.cache.fail_down = False
        setup.now += 10
        assert ctrl.request(slug(1))
        await drain(ctrl)
        assert ctrl.selectable(slug(1)) and ctrl.cached_pair(slug(1))
        assert len(ctrl.records) == 1 and host.calls == []
    asyncio.run(check())


def test_existing_async_gamma_parser_is_used(monkeypatch):
    async def fetch(s):
        assert s == slug(1)
        return {"version": "v1", "conditionId": "0x" + "a"*64, "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["11111111111111111111", "22222222222222222222"]'}
    monkeypatch.setattr(admission, "fetch_gamma_market_by_slug", fetch)
    ids = asyncio.run(admission.resolve_pair_ids(slug(1)))
    assert len(ids) == 2
    assert all(str(i).endswith(".POLYMARKET") for i in ids)


def test_bind_reuses_existing_polymarket_provider_and_loop(setup):
    class Client:
        __module__ = "nautilus_trader.adapters.polymarket.data"
    async def check():
        provider = Provider(pair(1))
        client = Client()
        client._instrument_provider = provider
        loop = asyncio.get_running_loop()
        node = SimpleNamespace(kernel=SimpleNamespace(loop=loop,
            data_engine=SimpleNamespace(_clients={"POLYMARKET": client})))
        host = Host(Cache())
        admission.bind_instrument_admission(node, host)
        assert host._instrument_admission.provider is provider
        assert host._instrument_admission.loop is loop
    asyncio.run(check())


def test_admission_bound_math_matches_unchanged_rollover_authority():
    from bot import launcher
    tree = ast.parse(Path(launcher.__file__).read_text())
    intervals = [n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "auto_rollover_sec" for t in n.targets)]
    assert intervals == [10800]
    assert admission.MAX_DYNAMIC_MARKETS == intervals[0] // 900 + 1 + 1


def test_market_switch_registers_instrument_slug_for_late_fills(setup):
    from bot.instrument_slug_map import slug_for_instrument

    host = Host(Cache(pair(0)))
    host._instrument_admission = admission.InstrumentAdmission(host, Provider([]), asyncio.new_event_loop())
    assert host._find_btc_instrument()
    assert host.current_market_slug == slug(0)
    assert host.current_market_instruments
    for inst in host.current_market_instruments:
        assert slug_for_instrument(host, str(inst)) == slug(0)

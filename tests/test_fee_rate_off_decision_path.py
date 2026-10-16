"""Fee lookups must not await the network on the decision path once warmed."""
import asyncio
import time
from decimal import Decimal

from bot.market_runtime import _prefetch_market_fee_rates
from bot.pricing_runtime import PricingRuntimeMixin


class _SlowClient:
    def __init__(self, rate="0.001", delay=0.2):
        self.rate = Decimal(rate)
        self.delay = delay
        self.calls = 0

    async def get_fee_rate_decimal(self, token):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.rate

    def get_health_snapshot(self):
        return {}


class _Host(PricingRuntimeMixin):
    def __init__(self, client):
        self.fee_rate_client = client
        self.fee_rate_fetch_interval_sec = 60
        self._fee_rate_local_cache_by_token = {}
        self._fee_rate_refresh_tasks_by_token = {}
        self._last_fee_log_state_by_token = {}
        self.fee_log_interval_sec = 60
        self.current_token_id = "tok"
        self.maker_fee_rate_default_decimal = Decimal("0.001")
        self.maker_fee_rate_legacy_bps_default = 0
        self.instrument_id = None
        self.cache = None

    @staticmethod
    def _extract_token_id_from_instrument(instrument_id):
        return instrument_id.split("-")[-1]


def _run(coro):
    return asyncio.run(coro)


def test_fresh_cache_hit_does_not_touch_network():
    async def go():
        client = _SlowClient()
        host = _Host(client)
        host._fee_rate_local_cache_by_token["tok"] = {"fee_rate": Decimal("0.002"), "ts": time.time()}
        assert await host._get_dynamic_fee_rate("tok") == Decimal("0.002")
        assert client.calls == 0
    _run(go())


def test_expired_cache_returns_immediately_and_refreshes_once_in_background():
    async def go():
        client = _SlowClient(rate="0.003", delay=0.2)
        host = _Host(client)
        host._fee_rate_local_cache_by_token["tok"] = {"fee_rate": Decimal("0.002"), "ts": time.time() - 90}
        started = time.perf_counter()
        first = await host._get_dynamic_fee_rate("tok")
        second = await host._get_dynamic_fee_rate("tok")
        elapsed = time.perf_counter() - started
        assert first == second == Decimal("0.002")
        assert elapsed < 0.05
        await asyncio.sleep(0.3)
        assert client.calls == 1  # single-flight
        assert await host._get_dynamic_fee_rate("tok") == Decimal("0.003")
        assert host._fee_rate_refresh_tasks_by_token == {}
    _run(go())


def test_very_stale_cache_blocks_again():
    async def go():
        client = _SlowClient(rate="0.004", delay=0.01)
        host = _Host(client)
        host._fee_rate_local_cache_by_token["tok"] = {"fee_rate": Decimal("0.002"), "ts": time.time() - 60 * 10}
        assert await host._get_dynamic_fee_rate("tok") == Decimal("0.004")
        assert client.calls == 1
    _run(go())


def test_cold_lookup_joins_inflight_prefetch():
    async def go():
        client = _SlowClient(rate="0.001", delay=0.1)
        host = _Host(client)
        assert host._prefetch_fee_rates(["tok", "tok", ""]) == 1
        assert await host._get_dynamic_fee_rate("tok") == Decimal("0.001")
        assert client.calls == 1
    _run(go())


def test_failed_refresh_keeps_stale_value_and_clears_task():
    class _Boom(_SlowClient):
        async def get_fee_rate_decimal(self, token):
            self.calls += 1
            raise RuntimeError("network")

    async def go():
        client = _Boom()
        host = _Host(client)
        host._fee_rate_local_cache_by_token["tok"] = {"fee_rate": Decimal("0.002"), "ts": time.time() - 90}
        assert await host._get_dynamic_fee_rate("tok") == Decimal("0.002")
        await asyncio.sleep(0.01)
        assert host._fee_rate_refresh_tasks_by_token == {}
        assert host._fee_rate_local_cache_by_token["tok"]["fee_rate"] == Decimal("0.002")
    _run(go())


def test_market_subscription_prefetches_current_and_prewarm_tokens():
    async def go():
        client = _SlowClient(delay=0.01)
        host = _Host(client)
        _prefetch_market_fee_rates(host, ["PM-UP-111", "PM-DOWN-222", "PM-UP-333"])
        await asyncio.sleep(0.05)
        assert set(host._fee_rate_local_cache_by_token) == {"111", "222", "333"}
        assert client.calls == 3
    _run(go())


def test_prefetch_without_running_loop_is_a_noop():
    host = _Host(_SlowClient())
    assert host._prefetch_fee_rates(["tok"]) == 0

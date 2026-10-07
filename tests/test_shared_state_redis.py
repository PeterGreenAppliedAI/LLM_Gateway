"""Shared state in Redis (D-035), against a real Redis server.

Skipped unless one is reachable: set GATEWAY_TEST_REDIS_URL (default
redis://localhost:6390/15). Each "process" here is a separate Redis client
and backend instance, which is exactly what separate gateway processes
have in common: nothing but the Redis server.
"""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest

if os.environ.get("GATEWAY_TEST_REQUIRE_SERVICES") == "1":
    import redis.asyncio as redis_asyncio
else:
    redis_asyncio = pytest.importorskip("redis.asyncio")

from gateway.config import (  # noqa: E402
    AdmissionConfig,
    GatewayConfig,
    ProviderConfig,
    RoutingConfig,
)
from gateway.dispatch.dispatcher import Dispatcher  # noqa: E402
from gateway.dispatch.registry import ProviderRegistry  # noqa: E402
from gateway.models.common import FinishReason, ProviderType, TaskType  # noqa: E402
from gateway.models.internal import (  # noqa: E402
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
)
from gateway.policy.rate_limiter import (  # noqa: E402
    RateLimitConfig,
    RateLimiter,
    RateLimitExceeded,
)
from gateway.state import create_shared_state  # noqa: E402
from gateway.state.concurrency import (  # noqa: E402
    FallbackConcurrency,
    InMemoryConcurrency,
    RedisConcurrency,
    StoreHealth,
)
from gateway.state.ratelimit import RedisRateStore  # noqa: E402

REDIS_URL = os.environ.get("GATEWAY_TEST_REDIS_URL", "redis://localhost:6390/15")


async def _reachable() -> bool:
    client = redis_asyncio.from_url(REDIS_URL, socket_connect_timeout=0.3)
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


_REDIS = asyncio.run(_reachable())
if os.environ.get("GATEWAY_TEST_REQUIRE_SERVICES") == "1" and not _REDIS:
    raise RuntimeError(f"GATEWAY_TEST_REQUIRE_SERVICES=1 but no Redis at {REDIS_URL}")
pytestmark = pytest.mark.skipif(not _REDIS, reason=f"no Redis at {REDIS_URL}")


@pytest.fixture
def prefix():
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def clients(prefix):
    """Two independent connections = two gateway processes."""
    made = [redis_asyncio.from_url(REDIS_URL) for _ in range(2)]
    yield made
    async for key in made[0].scan_iter(match=f"{prefix}:*"):
        await made[0].delete(key)
    for client in made:
        await client.aclose()


def _slots(client, prefix, **kwargs) -> RedisConcurrency:
    backend = RedisConcurrency(client, prefix, "slots", **kwargs)
    backend.set_capacity("gpu", 2)
    return backend


# =============================================================================
# Concurrency slots
# =============================================================================


class TestRedisSlots:
    @pytest.mark.asyncio
    async def test_cap_shared_across_processes(self, clients, prefix):
        a, b = _slots(clients[0], prefix), _slots(clients[1], prefix)
        first = await a.try_acquire("gpu")
        second = await b.try_acquire("gpu")
        assert first and second
        assert await a.try_acquire("gpu") is None
        assert await b.try_acquire("gpu") is None
        assert await a.in_flight_counts(["gpu"]) == {"gpu": 2}
        await first.release()
        assert await b.try_acquire("gpu") is not None
        await a.close()
        await b.close()

    @pytest.mark.asyncio
    async def test_release_in_one_process_wakes_waiter_in_another(self, clients, prefix):
        # Poll every 5 s: only the release notification can wake it sooner
        a = _slots(clients[0], prefix)
        b = _slots(clients[1], prefix, poll_interval=5.0)
        held = [await a.try_acquire("gpu"), await a.try_acquire("gpu")]
        waiter = asyncio.create_task(b.acquire_any(["gpu"], timeout=4))
        await asyncio.sleep(0.3)  # let b subscribe
        assert not waiter.done()
        loop = asyncio.get_running_loop()
        released_at = loop.time()
        await held[0].release()
        lease = await asyncio.wait_for(waiter, 2)
        assert lease is not None
        assert loop.time() - released_at < 1.0
        await lease.release()
        await held[1].release()
        await a.close()
        await b.close()

    @pytest.mark.asyncio
    async def test_batch_share_applies_across_processes(self, clients, prefix):
        a = _slots(clients[0], prefix, batch_max_share=0.5)
        b = _slots(clients[1], prefix, batch_max_share=0.5)
        assert await a.try_acquire("gpu", "batch") is not None  # 1 of 2: batch's share
        assert await b.try_acquire("gpu", "batch") is None
        assert await b.try_acquire("gpu", "interactive") is not None
        await a.close()
        await b.close()

    @pytest.mark.asyncio
    async def test_crashed_process_slots_expire(self, clients, prefix):
        crashed = _slots(clients[0], prefix, lease_ttl_seconds=1.0)
        survivor = _slots(clients[1], prefix, lease_ttl_seconds=1.0)
        await crashed.try_acquire("gpu")
        await crashed.try_acquire("gpu")
        # Crash: background tasks stop, nothing is released
        for task in crashed._tasks:
            task.cancel()
        crashed._live.clear()
        assert await survivor.try_acquire("gpu") is None
        await asyncio.sleep(1.2)
        assert await survivor.try_acquire("gpu") is not None
        await survivor.close()

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_long_requests_alive(self, clients, prefix):
        holder = _slots(clients[0], prefix, lease_ttl_seconds=0.6)
        other = _slots(clients[1], prefix, lease_ttl_seconds=0.6)
        held = [await holder.try_acquire("gpu"), await holder.try_acquire("gpu")]
        await asyncio.sleep(1.5)  # 2.5 TTLs: renewed, not expired
        assert await other.try_acquire("gpu") is None
        for lease in held:
            await lease.release()
        assert await other.try_acquire("gpu") is not None
        await holder.close()
        await other.close()

    @pytest.mark.asyncio
    async def test_release_survives_cancellation(self, clients, prefix):
        a = _slots(clients[0], prefix)
        lease = await a.try_acquire("gpu")

        async def release_then_cancelled():
            await lease.release()

        task = asyncio.create_task(release_then_cancelled())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert await a.in_flight_counts(["gpu"]) == {"gpu": 0}
        await a.close()

    @pytest.mark.asyncio
    async def test_wait_times_out(self, clients, prefix):
        a = _slots(clients[0], prefix)
        held = [await a.try_acquire("gpu"), await a.try_acquire("gpu")]
        assert await a.acquire_any(["gpu"], timeout=0.3) is None
        assert a.waiting() == 0
        for lease in held:
            await lease.release()
        await a.close()


# =============================================================================
# Rate-limit windows
# =============================================================================


class TestRedisRateWindows:
    @pytest.mark.asyncio
    async def test_windows_shared_across_processes(self, clients, prefix):
        config = RateLimitConfig(requests_per_minute=60, requests_per_hour=1000, burst_limit=3)
        a = RateLimiter(config, RedisRateStore(clients[0], prefix))
        b = RateLimiter(config, RedisRateStore(clients[1], prefix))
        await a.acquire("tenant")
        await a.acquire("tenant")
        state = await b.acquire("tenant")
        assert state.burst_remaining == 0
        with pytest.raises(RateLimitExceeded) as exc:
            await a.acquire("tenant")
        assert exc.value.limit == 3
        assert 0 < exc.value.retry_after <= 10
        # Another key is unaffected; reset clears it everywhere
        await b.acquire("other")
        await b.reset("tenant")
        assert (await a.check("tenant")).burst_remaining == 3

    @pytest.mark.asyncio
    async def test_scaled_limits_apply(self, clients, prefix):
        config = RateLimitConfig(requests_per_minute=60, requests_per_hour=1000, burst_limit=2)
        limiter = RateLimiter(config, RedisRateStore(clients[0], prefix))
        for _ in range(20):  # 600 RPM → burst 20
            await limiter.acquire("fast", rpm_override=600)
        with pytest.raises(RateLimitExceeded):
            await limiter.acquire("fast", rpm_override=600)


# =============================================================================
# Fallback when Redis is unreachable
# =============================================================================


class TestFallback:
    @pytest.mark.asyncio
    async def test_unreachable_redis_falls_back_to_process_limits(self):
        state = create_shared_state("redis://127.0.0.1:1/0", prefix="down")
        state.endpoint_slots.set_capacity("gpu", 1)
        lease = await state.endpoint_slots.try_acquire("gpu")
        assert lease is not None  # served from this process's counts
        assert await state.endpoint_slots.try_acquire("gpu") is None  # still enforced
        assert state.status == {"backend": "redis", "status": "degraded"}
        await lease.release()

        config = RateLimitConfig(requests_per_minute=60, requests_per_hour=1000, burst_limit=1)
        limiter = RateLimiter(config, state.rate_windows)
        await limiter.acquire("k")
        with pytest.raises(RateLimitExceeded):
            await limiter.acquire("k")
        await state.close()

    @pytest.mark.asyncio
    async def test_recovers_when_redis_returns(self, clients, prefix):
        primary = _slots(clients[0], prefix)
        health = StoreHealth("redis", cooldown_seconds=0.1)
        backend = FallbackConcurrency(primary, InMemoryConcurrency(), health)
        backend.set_capacity("gpu", 2)

        original = primary.try_acquire
        calls = {"n": 0}

        async def fail_once(*args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")
            return await original(*args)

        primary.try_acquire = fail_once
        assert await backend.try_acquire("gpu") is not None  # fell back
        assert health.degraded
        await asyncio.sleep(0.15)
        lease = await backend.try_acquire("gpu")  # Redis again
        assert not health.degraded
        assert await primary.in_flight_counts(["gpu"]) == {"gpu": 1}
        await lease.release()
        await primary.close()


# =============================================================================
# End to end: two gateway processes, one GPU
# =============================================================================


@pytest.mark.asyncio
async def test_two_gateways_share_one_endpoint_cap(prefix):
    """Two registries/dispatchers (two gateway processes) behind one Redis:
    12 concurrent requests, endpoint max_concurrent 2 → the engine never
    sees more than 2 at once."""
    config = GatewayConfig(
        providers=[
            ProviderConfig(
                name="gpu", type=ProviderType.VLLM, base_url="http://gpu:8000", max_concurrent=2
            )
        ],
        routing=RoutingConfig(default_provider="gpu"),
        admission=AdmissionConfig(max_queue_wait_seconds=10),
    )
    live = {"now": 0, "peak": 0}

    async def chat(request):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.1)
        live["now"] -= 1
        return InternalResponse(
            request_id="r",
            task=TaskType.CHAT,
            provider="gpu",
            model="m",
            content="ok",
            finish_reason=FinishReason.STOP,
        )

    gateways = []
    for _ in range(2):
        state = create_shared_state(REDIS_URL, prefix=prefix)
        registry = ProviderRegistry(config, endpoint_slots=state.endpoint_slots)
        await registry.initialize()
        adapter = AsyncMock()
        adapter.chat = AsyncMock(side_effect=chat)
        registry._adapters["gpu"] = adapter
        gateways.append((state, registry, Dispatcher(registry)))

    request = InternalRequest(
        task=TaskType.CHAT, model="m", messages=[Message(role=MessageRole.USER, content="x")]
    )
    results = await asyncio.gather(*(gateways[i % 2][2].dispatch(request) for i in range(12)))
    assert len(results) == 12
    assert live["peak"] == 2
    for state, registry, _ in gateways:
        assert state.status == {"backend": "redis", "status": "ok"}
        await registry.close()
        await state.close()

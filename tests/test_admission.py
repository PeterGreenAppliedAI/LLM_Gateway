"""Admission control: per-endpoint concurrency, FIFO wait, overflow (D-032)."""

import asyncio
import gc
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import (
    AdmissionConfig,
    GatewayConfig,
    ProviderConfig,
    ResolutionConfig,
    RoutingConfig,
)
from gateway.dispatch.admission import InMemoryConcurrency
from gateway.dispatch.dispatcher import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import CapacityExceededError, ErrorCode
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
)

# =============================================================================
# Backend
# =============================================================================


class TestInMemoryConcurrency:
    def test_unlimited_admits_and_counts(self):
        c = InMemoryConcurrency({"a": None})
        leases = [c.try_acquire("a") for _ in range(50)]
        assert all(leases)
        assert c.in_flight("a") == 50
        for lease in leases:
            lease.release()
        assert c.in_flight("a") == 0

    def test_capacity_limits(self):
        c = InMemoryConcurrency({"a": 2})
        first, second = c.try_acquire("a"), c.try_acquire("a")
        assert first and second
        assert c.try_acquire("a") is None
        first.release()
        assert c.try_acquire("a") is not None

    def test_release_is_idempotent(self):
        c = InMemoryConcurrency({"a": 1})
        lease = c.try_acquire("a")
        lease.release()
        lease.release()
        assert c.in_flight("a") == 0

    def test_dropped_lease_is_released(self):
        """A stream dropped without closing can't strand its slot."""
        c = InMemoryConcurrency({"a": 1})
        lease = c.try_acquire("a")
        del lease
        gc.collect()
        assert c.in_flight("a") == 0

    @pytest.mark.asyncio
    async def test_waiters_served_in_arrival_order(self):
        c = InMemoryConcurrency({"a": 1})
        held = c.try_acquire("a")
        order: list[int] = []

        async def wait(i: int):
            lease = await c.acquire_any(["a"], timeout=5)
            order.append(i)
            await asyncio.sleep(0)
            lease.release()

        tasks = [asyncio.create_task(wait(i)) for i in range(3)]
        await asyncio.sleep(0.01)
        assert c.waiting() == 3
        held.release()
        await asyncio.gather(*tasks)
        assert order == [0, 1, 2]
        assert c.in_flight("a") == 0

    @pytest.mark.asyncio
    async def test_newcomer_cannot_jump_the_queue(self):
        c = InMemoryConcurrency({"a": 1})
        held = c.try_acquire("a")
        waiter = asyncio.create_task(c.acquire_any(["a"], timeout=5))
        await asyncio.sleep(0.01)
        held.release()
        # The freed slot went to the waiter, not to whoever asks next
        assert c.try_acquire("a") is None
        lease = await waiter
        assert lease.endpoint == "a"
        lease.release()

    @pytest.mark.asyncio
    async def test_waits_for_whichever_frees_first(self):
        c = InMemoryConcurrency({"a": 1, "b": 1})
        held_a, held_b = c.try_acquire("a"), c.try_acquire("b")
        waiter = asyncio.create_task(c.acquire_any(["a", "b"], timeout=5))
        await asyncio.sleep(0.01)
        held_b.release()
        lease = await waiter
        assert lease.endpoint == "b"
        held_a.release()
        lease.release()

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_leaves_queue(self):
        c = InMemoryConcurrency({"a": 1})
        held = c.try_acquire("a")
        assert await c.acquire_any(["a"], timeout=0.02) is None
        assert c.waiting() == 0
        held.release()
        assert c.in_flight("a") == 0

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_leak(self):
        c = InMemoryConcurrency({"a": 1})
        held = c.try_acquire("a")
        waiter = asyncio.create_task(c.acquire_any(["a"], timeout=5))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        held.release()
        assert c.in_flight("a") == 0
        assert c.waiting() == 0


# =============================================================================
# Dispatcher
# =============================================================================


def _config(cap: int = 1, **kwargs) -> GatewayConfig:
    return GatewayConfig(
        providers=[
            ProviderConfig(
                name="primary",
                type=ProviderType.VLLM,
                base_url="http://primary:8000",
                max_concurrent=cap,
            ),
            ProviderConfig(
                name="backup",
                type=ProviderType.OLLAMA,
                base_url="http://backup:11434",
                max_concurrent=cap,
            ),
        ],
        routing=RoutingConfig(default_provider="primary"),
        admission=AdmissionConfig(max_queue_wait_seconds=kwargs.pop("wait", 2.0)),
        **kwargs,
    )


def _request(**kwargs) -> InternalRequest:
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[Message(role=MessageRole.USER, content="Hello")],
        **kwargs,
    )


def _ok(provider: str) -> InternalResponse:
    return InternalResponse(
        request_id="r",
        task=TaskType.CHAT,
        provider=provider,
        model="llama3.2",
        content="ok",
        finish_reason=FinishReason.STOP,
    )


async def _setup(config: GatewayConfig):
    registry = ProviderRegistry(config)
    await registry.initialize()
    for name in ("primary", "backup"):
        adapter = AsyncMock()
        adapter.chat = AsyncMock(return_value=_ok(name))
        registry._adapters[name] = adapter
    return registry, Dispatcher(registry, config.resolution)


class TestDispatcherAdmission:
    @pytest.mark.asyncio
    async def test_overflows_to_next_endpoint_when_full(self):
        registry, dispatcher = await _setup(_config())
        held = registry.admission.try_acquire("primary")
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "backup"
        assert result.was_fallback
        held.release()
        assert registry.admission.in_flight("backup") == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_primary_used_when_it_has_room(self):
        registry, dispatcher = await _setup(_config())
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "primary"
        assert registry.admission.in_flight("primary") == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_waits_when_all_full_then_proceeds(self):
        registry, dispatcher = await _setup(_config())
        held = [registry.admission.try_acquire(n) for n in ("primary", "backup")]
        task = asyncio.create_task(dispatcher.dispatch(_request()))
        await asyncio.sleep(0.02)
        assert not task.done()
        held[1].release()
        result = await task
        assert result.provider_used == "backup"
        held[0].release()
        await registry.close()

    @pytest.mark.asyncio
    async def test_no_overflow_when_fallback_disabled(self):
        registry, dispatcher = await _setup(_config(wait=0.05))
        held = registry.admission.try_acquire("primary")
        with pytest.raises(CapacityExceededError) as exc:
            await dispatcher.dispatch(_request(fallback_allowed=False))
        assert exc.value.details["endpoints"] == ["primary"]
        assert exc.value.retry_after >= 1
        registry.get("backup").chat.assert_not_called()
        held.release()
        await registry.close()

    @pytest.mark.asyncio
    async def test_slot_held_for_request_duration(self):
        registry, dispatcher = await _setup(_config())
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow(request):
            started.set()
            await finish.wait()
            return _ok("primary")

        registry.get("primary").chat.side_effect = slow
        task = asyncio.create_task(dispatcher.dispatch(_request()))
        await started.wait()
        assert registry.admission.in_flight("primary") == 1
        finish.set()
        await task
        assert registry.admission.in_flight("primary") == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_slot_released_on_failure_and_failover(self):
        registry, dispatcher = await _setup(_config())
        registry.get("primary").chat.side_effect = ConnectionError("down")
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "backup"
        assert registry.admission.in_flight("primary") == 0
        assert registry.admission.in_flight("backup") == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_least_loaded_prefers_idle_endpoint(self):
        config = _config(cap=4, resolution=ResolutionConfig(strategy="least_loaded"))
        registry, dispatcher = await _setup(config)
        held = registry.admission.try_acquire("primary")
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "backup"  # 0/4 beats 1/4
        held.release()
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "primary"  # tie: priority order
        await registry.close()

    @pytest.mark.asyncio
    async def test_stream_holds_slot_until_closed(self):
        registry, dispatcher = await _setup(_config())

        async def stream(request):
            for delta in ("a", "b"):
                yield StreamChunk(request_id="r", index=0, delta=delta)

        registry.get("primary").chat_stream = stream
        provider, chunks = await dispatcher.dispatch_stream(_request(stream=True))
        assert provider == "primary"
        assert registry.admission.in_flight("primary") == 1
        assert [c.delta async for c in chunks] == ["a", "b"]
        assert registry.admission.in_flight("primary") == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_stream_closed_early_releases_slot(self):
        registry, dispatcher = await _setup(_config())

        async def stream(request):
            for delta in ("a", "b", "c"):
                yield StreamChunk(request_id="r", index=0, delta=delta)

        registry.get("primary").chat_stream = stream
        _, chunks = await dispatcher.dispatch_stream(_request(stream=True))
        await chunks.__anext__()
        await chunks.aclose()
        assert registry.admission.in_flight("primary") == 0
        await registry.close()


class TestUnderLoad:
    @pytest.mark.asyncio
    async def test_cap_holds_under_contention(self):
        """20 concurrent requests, two endpoints capped at 2: never more than
        2 in flight per endpoint, both used, every request served."""
        config = _config(cap=2)
        registry, dispatcher = await _setup(config)
        live = {"primary": 0, "backup": 0}
        peak = {"primary": 0, "backup": 0}

        def engine(name):
            async def chat(request):
                live[name] += 1
                peak[name] = max(peak[name], live[name])
                await asyncio.sleep(0.01)
                live[name] -= 1
                return _ok(name)

            return chat

        for name in live:
            registry.get(name).chat.side_effect = engine(name)
        results = await asyncio.gather(*(dispatcher.dispatch(_request()) for _ in range(20)))
        assert len(results) == 20
        assert peak == {"primary": 2, "backup": 2}
        assert registry.admission.in_flight("primary") == 0
        assert registry.admission.waiting() == 0
        await registry.close()


# =============================================================================
# HTTP surface
# =============================================================================


def test_capacity_error_is_503_with_retry_after():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/full")
    async def full():
        raise CapacityExceededError(endpoints=["a"], waited_seconds=5.0, retry_after=5)

    resp = TestClient(app).get("/full")
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    assert resp.json()["error"]["code"] == ErrorCode.CAPACITY_EXCEEDED.value

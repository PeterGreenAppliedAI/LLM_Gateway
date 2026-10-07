"""Connection pool sizing and per-key burst scaling (D-033)."""

from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import GatewayConfig, ProviderConfig, RoutingConfig
from gateway.dispatch.circuit import CircuitState
from gateway.dispatch.dispatcher import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.models.common import FinishReason, ProviderType, TaskType
from gateway.models.internal import InternalRequest, InternalResponse, Message, MessageRole
from gateway.policy.rate_limiter import RateLimitConfig, RateLimiter, RateLimitExceeded
from gateway.providers import create_adapter
from gateway.providers.base import POOL_HEADROOM, POOL_TIMEOUT_SECONDS
from gateway.providers.streaming import classify_exception


def _adapter(max_concurrent: int | None, type_: ProviderType = ProviderType.OLLAMA):
    return create_adapter(
        ProviderConfig(
            name="box", type=type_, base_url="http://box:11434", max_concurrent=max_concurrent
        )
    )


def _pool_size(client: httpx.AsyncClient) -> int | None:
    pool = getattr(client._transport, "_pool", None)
    return getattr(pool, "_max_connections", None)


class TestPoolSizing:
    def test_sized_from_max_concurrent(self):
        limits = _adapter(6).http_limits()
        assert limits.max_connections == 6 + POOL_HEADROOM
        assert limits.max_keepalive_connections == 6 + POOL_HEADROOM

    def test_unlimited_keeps_httpx_defaults(self):
        limits = _adapter(None).http_limits()
        assert limits.max_connections == 100
        assert limits.max_keepalive_connections == 20

    def test_pool_wait_is_short(self):
        timeout = _adapter(None).http_timeout()
        assert timeout.pool == POOL_TIMEOUT_SECONDS
        assert timeout.connect == 3.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("type_", [ProviderType.OLLAMA, ProviderType.VLLM, ProviderType.OPENAI])
    async def test_clients_use_the_limits(self, type_):
        adapter = _adapter(3, type_)
        client = await adapter._get_client()
        assert client.timeout.pool == POOL_TIMEOUT_SECONDS
        size = _pool_size(client)
        if size is not None:  # httpx internals; skip the check if they move
            assert size == 3 + POOL_HEADROOM
        await adapter.close()

    def test_pool_timeout_classified_separately(self):
        code, message = classify_exception(httpx.PoolTimeout("no connection"))
        assert code == "pool_timeout"
        assert "pool exhausted" in message
        assert classify_exception(httpx.ReadTimeout("slow"))[0] == "timeout"


class TestPoolTimeoutRouting:
    @pytest.mark.asyncio
    async def test_fails_over_without_tripping_breaker(self):
        config = GatewayConfig(
            providers=[
                ProviderConfig(name="a", type=ProviderType.VLLM, base_url="http://a:8000"),
                ProviderConfig(name="b", type=ProviderType.VLLM, base_url="http://b:8000"),
            ],
            routing=RoutingConfig(default_provider="a"),
        )
        registry = ProviderRegistry(config)
        await registry.initialize()

        def response(provider, code=None):
            return InternalResponse(
                request_id="r",
                task=TaskType.CHAT,
                provider=provider,
                model="m",
                content="" if code else "ok",
                error="pool" if code else None,
                error_code=code,
                finish_reason=FinishReason.ERROR if code else FinishReason.STOP,
            )

        a, b = AsyncMock(), AsyncMock()
        a.chat = AsyncMock(return_value=response("a", "pool_timeout"))
        b.chat = AsyncMock(return_value=response("b"))
        registry._adapters.update(a=a, b=b)
        dispatcher = Dispatcher(registry)
        request = InternalRequest(
            task=TaskType.CHAT, model="m", messages=[Message(role=MessageRole.USER, content="x")]
        )
        for _ in range(10):
            assert (await dispatcher.dispatch(request)).provider_used == "b"
        assert registry.circuit_state("a") == CircuitState.CLOSED
        await registry.close()


class TestBurstScaling:
    def _limiter(self) -> RateLimiter:
        return RateLimiter(
            RateLimitConfig(requests_per_minute=60, requests_per_hour=1000, burst_limit=10)
        )

    def test_default_key_keeps_configured_limits(self):
        assert self._limiter().limits_for(None) == (10, 60, 1000)
        assert self._limiter().limits_for(60) == (10, 60, 1000)

    def test_override_scales_burst_and_hour(self):
        assert self._limiter().limits_for(600) == (100, 600, 10000)

    def test_low_override_never_below_one(self):
        burst, rpm, hour = self._limiter().limits_for(3)
        assert (burst, rpm) == (1, 3)
        assert hour >= rpm

    def test_high_rpm_key_not_capped_by_global_burst(self):
        """Regression: a 600 RPM key was refused after 10 requests in 10s."""
        limiter = self._limiter()
        for _ in range(100):
            limiter.acquire("fast", rpm_override=600)
        with pytest.raises(RateLimitExceeded) as exc:
            limiter.acquire("fast", rpm_override=600)
        assert exc.value.limit == 100

    def test_default_key_still_capped(self):
        limiter = self._limiter()
        for _ in range(10):
            limiter.acquire("normal")
        with pytest.raises(RateLimitExceeded) as exc:
            limiter.acquire("normal")
        assert exc.value.limit == 10

    def test_check_reports_scaled_limits(self):
        limiter = self._limiter()
        limiter.acquire("fast", rpm_override=600)
        state = limiter.check("fast", rpm_override=600)
        assert state.burst_remaining == 99
        assert state.requests_remaining_hour == 9999

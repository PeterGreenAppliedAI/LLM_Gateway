"""Circuit breaker: state machine, dispatcher wiring, health-loop wiring."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import CircuitBreakerConfig, GatewayConfig, ProviderConfig, RoutingConfig
from gateway.dispatch.circuit import CircuitBreaker, CircuitState
from gateway.dispatch.dispatcher import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import AllProvidersUnavailableError, ProviderError
from gateway.models.common import FinishReason, HealthStatus, ProviderType, TaskType
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr("gateway.dispatch.circuit.time.monotonic", c)
    return c


def _breaker(threshold: int = 3, cooldown: float = 10.0) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=threshold, cooldown_seconds=cooldown)
    )


# =============================================================================
# State machine
# =============================================================================


class TestCircuitBreaker:
    def test_closed_allows(self):
        assert _breaker().allow_request()

    def test_trips_at_threshold(self, clock):
        b = _breaker(threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.state == CircuitState.CLOSED
        b.record_failure()
        assert b.state == CircuitState.OPEN
        assert not b.allow_request()

    def test_success_resets_count(self, clock):
        b = _breaker(threshold=3)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        assert b.state == CircuitState.CLOSED

    def test_half_open_single_probe(self, clock):
        b = _breaker(cooldown=10)
        b.trip()
        clock.now += 9
        assert not b.allow_request()
        clock.now += 2
        assert b.allow_request()  # the probe
        assert b.state == CircuitState.HALF_OPEN
        assert not b.allow_request()  # only one probe at a time

    def test_probe_success_closes(self, clock):
        b = _breaker(cooldown=10)
        b.trip()
        clock.now += 11
        assert b.allow_request()
        b.record_success()
        assert b.state == CircuitState.CLOSED
        assert b.allow_request() and b.allow_request()

    def test_probe_failure_reopens_for_full_cooldown(self, clock):
        b = _breaker(threshold=5, cooldown=10)
        b.trip()
        clock.now += 11
        assert b.allow_request()
        b.record_failure()  # one failure in half-open is enough
        assert b.state == CircuitState.OPEN
        clock.now += 9
        assert not b.allow_request()

    def test_released_probe_lets_next_request_probe(self, clock):
        b = _breaker(cooldown=10)
        b.trip()
        clock.now += 11
        assert b.allow_request()
        b.release_probe()
        assert b.state == CircuitState.HALF_OPEN
        assert b.allow_request()

    def test_lost_probe_expires(self, clock):
        """A probe that never reports back can't strand the endpoint half-open."""
        b = _breaker(cooldown=10)
        b.trip()
        clock.now += 11
        assert b.allow_request()
        clock.now += 5
        assert not b.allow_request()
        clock.now += 6
        assert b.allow_request()


# =============================================================================
# Dispatcher wiring
# =============================================================================


@pytest.fixture
def config() -> GatewayConfig:
    return GatewayConfig(
        providers=[
            ProviderConfig(name="primary", type=ProviderType.VLLM, base_url="http://primary:8000"),
            ProviderConfig(name="backup", type=ProviderType.OLLAMA, base_url="http://backup:11434"),
        ],
        routing=RoutingConfig(default_provider="primary"),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=2, cooldown_seconds=30),
    )


def _request(**kwargs) -> InternalRequest:
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[Message(role=MessageRole.USER, content="Hello")],
        **kwargs,
    )


def _response(provider: str, error_code: str | None = None) -> InternalResponse:
    return InternalResponse(
        request_id="r",
        task=TaskType.CHAT,
        provider=provider,
        model="llama3.2",
        content="" if error_code else "ok",
        finish_reason=FinishReason.ERROR if error_code else FinishReason.STOP,
        error="boom" if error_code else None,
        error_code=error_code,
    )


@pytest.fixture
async def setup(config):
    registry = ProviderRegistry(config)
    await registry.initialize()
    primary, backup = AsyncMock(), AsyncMock()
    primary.chat = AsyncMock(return_value=_response("primary", "connection_error"))
    backup.chat = AsyncMock(return_value=_response("backup"))
    registry._adapters["primary"] = primary
    registry._adapters["backup"] = backup
    yield Dispatcher(registry), registry, primary, backup
    await registry.close()


class TestDispatcherWiring:
    @pytest.mark.asyncio
    async def test_failures_open_circuit_then_skip_instantly(self, setup):
        dispatcher, registry, primary, backup = setup
        for _ in range(2):
            result = await dispatcher.dispatch(_request())
            assert result.provider_used == "backup"
        assert registry.circuit_state("primary") == CircuitState.OPEN
        assert primary.chat.await_count == 2

        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "backup"
        assert primary.chat.await_count == 2  # skipped, not called

    @pytest.mark.asyncio
    async def test_upstream_4xx_does_not_count(self, setup):
        dispatcher, registry, primary, _ = setup
        primary.chat.return_value = _response("primary", "http_400")
        for _ in range(3):
            with pytest.raises(ProviderError):
                await dispatcher.dispatch(_request())
        assert registry.circuit_state("primary") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_closes_after_cooldown_probe(self, setup, clock):
        dispatcher, registry, primary, _ = setup
        registry.trip("primary")
        clock.now += 31
        primary.chat.return_value = _response("primary")
        result = await dispatcher.dispatch(_request())
        assert result.provider_used == "primary"
        assert registry.circuit_state("primary") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_all_open_fails_fast(self, setup):
        dispatcher, registry, primary, backup = setup
        registry.trip("primary")
        registry.trip("backup")
        with pytest.raises(AllProvidersUnavailableError) as exc:
            await dispatcher.dispatch(_request())
        assert "circuit open" in str(exc.value.details)
        primary.chat.assert_not_called()
        backup.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_probe_is_released(self, setup, clock):
        dispatcher, registry, primary, _ = setup
        registry.trip("primary")
        clock.now += 31

        started = asyncio.Event()

        async def hang(_request):
            started.set()
            await asyncio.sleep(3600)

        primary.chat.side_effect = hang
        task = asyncio.create_task(dispatcher.dispatch(_request()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The cancelled probe gave no verdict; the next request may probe.
        assert registry.allow_request("primary")

    @pytest.mark.asyncio
    async def test_stream_failures_open_circuit(self, setup):
        dispatcher, registry, primary, backup = setup
        calls: list[str] = []

        def make(name: str, fail: bool):
            async def stream(request):
                calls.append(name)
                if fail:
                    raise ConnectionError("down")
                yield StreamChunk(
                    request_id="r", index=0, delta="ok", finish_reason=FinishReason.STOP
                )

            return stream

        primary.chat_stream = make("primary", True)
        backup.chat_stream = make("backup", False)
        for _ in range(3):
            provider, stream = await dispatcher.dispatch_stream(_request(stream=True))
            assert provider == "backup"
            assert [c.delta async for c in stream] == ["ok"]
        assert calls == ["primary", "backup", "primary", "backup", "backup"]
        assert registry.circuit_state("primary") == CircuitState.OPEN


# =============================================================================
# Health loop wiring
# =============================================================================


class TestHealthLoopWiring:
    @pytest.mark.asyncio
    async def test_unhealthy_check_opens_healthy_check_closes(self, config):
        registry = ProviderRegistry(config)
        await registry.initialize()
        adapter = AsyncMock()
        adapter.health = AsyncMock(return_value=HealthStatus.UNHEALTHY)

        await registry._check_provider_health("primary", adapter)
        assert registry.circuit_state("primary") == CircuitState.OPEN
        assert not registry.allow_request("primary")

        adapter.health.return_value = HealthStatus.HEALTHY
        await registry._check_provider_health("primary", adapter)
        assert registry.circuit_state("primary") == CircuitState.CLOSED
        assert registry.allow_request("primary")
        await registry.close()

    @pytest.mark.asyncio
    async def test_health_exception_opens(self, config):
        registry = ProviderRegistry(config)
        await registry.initialize()
        adapter = AsyncMock()
        adapter.health = AsyncMock(side_effect=RuntimeError("no route"))
        await registry._check_provider_health("primary", adapter)
        assert registry.circuit_state("primary") == CircuitState.OPEN
        await registry.close()


class TestVisibility:
    @pytest.mark.asyncio
    async def test_health_and_metrics_report_circuit(self, config):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from gateway.routes.health import PROMETHEUS_AVAILABLE, router

        registry = ProviderRegistry(config)
        await registry.initialize()
        registry.trip("primary")
        app = FastAPI()
        app.include_router(router)
        app.state.registry = registry
        app.state.config = config
        client = TestClient(app)

        circuits = {p["name"]: p["circuit"] for p in client.get("/health").json()["providers"]}
        assert circuits == {"primary": "open", "backup": "closed"}
        if PROMETHEUS_AVAILABLE:
            body = client.get("/metrics").text
            assert 'circuit_state{endpoint="primary"} 2.0' in body
        await registry.close()

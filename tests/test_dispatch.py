"""Tests for provider dispatch - registry, health monitoring, and dispatcher."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, ProviderConfig, RoutingConfig
from gateway.dispatch.dispatcher import Dispatcher, DispatchResult
from gateway.dispatch.registry import ProviderHealth, ProviderRegistry
from gateway.errors import DispatchError
from gateway.models.common import FinishReason, HealthStatus, ProviderType, TaskType
from gateway.models.internal import InternalRequest, InternalResponse, Message, MessageRole

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def minimal_config() -> GatewayConfig:
    """Minimal config with one provider."""
    return GatewayConfig(
        providers=[
            ProviderConfig(
                name="ollama",
                type=ProviderType.OLLAMA,
                base_url="http://localhost:11434",
            )
        ]
    )


@pytest.fixture
def multi_provider_config() -> GatewayConfig:
    """Config with multiple providers for fallback testing."""
    return GatewayConfig(
        providers=[
            ProviderConfig(
                name="primary",
                type=ProviderType.VLLM,
                base_url="http://primary:8000",
            ),
            ProviderConfig(
                name="fallback1",
                type=ProviderType.OLLAMA,
                base_url="http://fallback1:11434",
            ),
            ProviderConfig(
                name="fallback2",
                type=ProviderType.OLLAMA,
                base_url="http://fallback2:11434",
            ),
        ],
        routing=RoutingConfig(default_provider="primary"),
    )


@pytest.fixture
def sample_request() -> InternalRequest:
    """Sample chat request."""
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[Message(role=MessageRole.USER, content="Hello")],
    )


@pytest.fixture
def sample_response() -> InternalResponse:
    """Sample successful response."""
    return InternalResponse(
        request_id="test-123",
        task=TaskType.CHAT,
        provider="test",
        model="llama3.2",
        content="Hello! How can I help you?",
        finish_reason=FinishReason.STOP,
    )


# =============================================================================
# ProviderHealth Tests
# =============================================================================


class TestProviderHealth:
    """Tests for ProviderHealth state tracking."""

    def test_initial_state(self):
        """Health starts as unknown with no failures."""
        health = ProviderHealth("test")
        assert health.status == HealthStatus.UNKNOWN
        assert health.consecutive_failures == 0
        assert health.last_check is None
        assert not health.is_available()

    def test_record_healthy(self):
        """Recording healthy updates state correctly."""
        health = ProviderHealth("test")
        health.consecutive_failures = 5  # Simulate previous failures

        health.record_healthy()

        assert health.status == HealthStatus.HEALTHY
        assert health.consecutive_failures == 0
        assert health.last_check is not None
        assert health.last_healthy is not None
        assert health.is_available()

    def test_record_unhealthy(self):
        """Recording unhealthy increments failure count."""
        health = ProviderHealth("test")
        health.record_healthy()  # Start healthy

        health.record_unhealthy(HealthStatus.UNHEALTHY, "Connection refused")

        assert health.status == HealthStatus.UNHEALTHY
        assert health.consecutive_failures == 1
        assert health.error_message == "Connection refused"
        assert not health.is_available()

    def test_consecutive_failures(self):
        """Multiple failures increment counter."""
        health = ProviderHealth("test")

        health.record_unhealthy(HealthStatus.UNHEALTHY)
        health.record_unhealthy(HealthStatus.UNHEALTHY)
        health.record_unhealthy(HealthStatus.UNHEALTHY)

        assert health.consecutive_failures == 3

    def test_healthy_resets_failures(self):
        """Becoming healthy resets failure counter."""
        health = ProviderHealth("test")
        health.record_unhealthy(HealthStatus.UNHEALTHY)
        health.record_unhealthy(HealthStatus.UNHEALTHY)

        health.record_healthy()

        assert health.consecutive_failures == 0


# =============================================================================
# ProviderRegistry Tests
# =============================================================================


class TestProviderRegistry:
    """Tests for ProviderRegistry."""

    @pytest.mark.asyncio
    async def test_initialize_creates_adapters(self, minimal_config):
        """Initialize creates adapter for each enabled provider."""
        registry = ProviderRegistry(minimal_config)
        await registry.initialize()

        assert "ollama" in registry.list_providers()
        assert registry.get("ollama") is not None

        await registry.close()

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, minimal_config):
        """Getting non-existent provider returns None."""
        registry = ProviderRegistry(minimal_config)
        await registry.initialize()

        assert registry.get("nonexistent") is None

        await registry.close()

    @pytest.mark.asyncio
    async def test_default_provider_from_routing(self, multi_provider_config):
        """Default provider comes from routing config."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        assert registry.get_default_provider() == "primary"

        await registry.close()

    @pytest.mark.asyncio
    async def test_default_provider_fallback_to_first(self, minimal_config):
        """Without routing config, default is first enabled provider."""
        registry = ProviderRegistry(minimal_config)
        await registry.initialize()

        assert registry.get_default_provider() == "ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_fallback_chain_excludes_provider(self, multi_provider_config):
        """Fallback chain excludes the specified provider."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        chain = registry.get_fallback_chain(exclude="primary")

        assert "primary" not in chain
        assert "fallback1" in chain
        assert "fallback2" in chain

        await registry.close()

    @pytest.mark.asyncio
    async def test_list_healthy_providers(self, multi_provider_config):
        """list_healthy_providers returns only healthy ones."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # Mark one as healthy
        registry._health["primary"].record_healthy()
        registry._health["fallback1"].record_unhealthy(HealthStatus.UNHEALTHY)
        registry._health["fallback2"].record_healthy()

        healthy = registry.list_healthy_providers()

        assert "primary" in healthy
        assert "fallback1" not in healthy
        assert "fallback2" in healthy

        await registry.close()

    @pytest.mark.asyncio
    async def test_close_clears_registry(self, minimal_config):
        """Close clears all adapters and health state."""
        registry = ProviderRegistry(minimal_config)
        await registry.initialize()

        await registry.close()

        assert len(registry.list_providers()) == 0


# =============================================================================
# Dispatcher Tests
# =============================================================================


class TestDispatcher:
    """Tests for Dispatcher request routing."""

    def test_parse_provider_from_model_with_prefix(self):
        """Parse provider from 'provider/model' format."""
        registry = MagicMock()
        dispatcher = Dispatcher(registry)

        provider, model = dispatcher.parse_provider_from_model("ollama/llama3.2")

        assert provider == "ollama"
        assert model == "llama3.2"

    def test_parse_provider_from_model_without_prefix(self):
        """Model without provider prefix returns None provider."""
        registry = MagicMock()
        dispatcher = Dispatcher(registry)

        provider, model = dispatcher.parse_provider_from_model("llama3.2")

        assert provider is None
        assert model == "llama3.2"

    def test_parse_provider_from_model_none(self):
        """None model returns None for both."""
        registry = MagicMock()
        dispatcher = Dispatcher(registry)

        provider, model = dispatcher.parse_provider_from_model(None)

        assert provider is None
        assert model is None

    def test_parse_provider_complex_model_name(self):
        """Parse works with complex model names."""
        registry = MagicMock()
        dispatcher = Dispatcher(registry)

        provider, model = dispatcher.parse_provider_from_model("vllm/meta-llama/Llama-3.1-8B")

        assert provider == "vllm"
        assert model == "meta-llama/Llama-3.1-8B"

    def test_parse_provider_rejects_invalid_provider_name(self):
        """Security: Invalid provider names are rejected to prevent injection.

        Provider names from user input must match SafeIdentifier pattern.
        Invalid names are treated as unprefixed models.
        """
        registry = MagicMock()
        dispatcher = Dispatcher(registry)

        # These should be rejected (contain injection attempts)
        test_cases = [
            "../../etc/passwd/model",  # Path traversal attempt
            "provider\nX-Header: injection/model",  # Header injection
            "123invalid/model",  # Starts with number
            "provider with spaces/model",  # Contains spaces
        ]

        for malicious_model in test_cases:
            provider, model = dispatcher.parse_provider_from_model(malicious_model)
            # Should treat as unprefixed model (provider=None)
            assert provider is None, f"Should reject: {malicious_model}"
            assert model == malicious_model

    @pytest.mark.asyncio
    async def test_resolve_provider_from_model(self, multi_provider_config, sample_request):
        """Resolve uses provider from model string."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()
        dispatcher = Dispatcher(registry)

        request = sample_request.model_copy(update={"model": "fallback1/llama3.2"})
        provider, model = dispatcher.resolve_provider(request)

        assert provider == "fallback1"
        assert model == "llama3.2"

        await registry.close()

    @pytest.mark.asyncio
    async def test_resolve_provider_from_preferred(self, multi_provider_config, sample_request):
        """Resolve uses preferred_provider when no model prefix."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()
        dispatcher = Dispatcher(registry)

        request = sample_request.model_copy(update={"preferred_provider": "fallback2"})
        provider, model = dispatcher.resolve_provider(request)

        assert provider == "fallback2"

        await registry.close()

    @pytest.mark.asyncio
    async def test_resolve_provider_uses_default(self, multi_provider_config, sample_request):
        """Resolve falls back to default when no hints."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()
        dispatcher = Dispatcher(registry)

        provider, model = dispatcher.resolve_provider(sample_request)

        assert provider == "primary"

        await registry.close()

    @pytest.mark.asyncio
    async def test_dispatch_success(self, multi_provider_config, sample_request, sample_response):
        """Successful dispatch returns response with metadata."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # Mark provider healthy and mock the adapter
        registry._health["primary"].record_healthy()
        mock_adapter = AsyncMock()
        mock_adapter.chat = AsyncMock(return_value=sample_response)
        registry._adapters["primary"] = mock_adapter

        dispatcher = Dispatcher(registry)
        result = await dispatcher.dispatch(
            sample_request.model_copy(update={"model": "primary/llama3.2"})
        )

        assert isinstance(result, DispatchResult)
        assert result.provider_used == "primary"
        assert result.was_fallback is False
        assert result.response == sample_response

        await registry.close()

    @pytest.mark.asyncio
    async def test_dispatch_fallback_on_unhealthy(
        self, multi_provider_config, sample_request, sample_response
    ):
        """Dispatch falls back when primary is unhealthy."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # Primary unhealthy, fallback1 healthy
        registry._health["primary"].record_unhealthy(HealthStatus.UNHEALTHY)
        registry._health["fallback1"].record_healthy()

        # Mock fallback adapter
        mock_adapter = AsyncMock()
        mock_adapter.chat = AsyncMock(return_value=sample_response)
        registry._adapters["fallback1"] = mock_adapter

        dispatcher = Dispatcher(registry)
        result = await dispatcher.dispatch(
            sample_request.model_copy(update={"model": "primary/llama3.2"})
        )

        assert result.provider_used == "fallback1"
        assert result.was_fallback is True
        assert "primary" in result.attempted_providers
        assert "fallback1" in result.attempted_providers

        await registry.close()

    @pytest.mark.asyncio
    async def test_dispatch_no_fallback_when_disabled(self, multi_provider_config, sample_request):
        """Dispatch fails without fallback when disabled."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # Primary unhealthy
        registry._health["primary"].record_unhealthy(HealthStatus.UNHEALTHY)

        dispatcher = Dispatcher(registry)
        request = sample_request.model_copy(
            update={"model": "primary/llama3.2", "fallback_allowed": False}
        )

        with pytest.raises(DispatchError) as exc_info:
            await dispatcher.dispatch(request)

        assert "unavailable" in str(exc_info.value)
        assert exc_info.value.code == "provider_unavailable"

        await registry.close()

    @pytest.mark.asyncio
    async def test_dispatch_all_providers_unavailable(self, multi_provider_config, sample_request):
        """Dispatch fails when all providers unavailable."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # All unhealthy
        for name in registry.list_providers():
            registry._health[name].record_unhealthy(HealthStatus.UNHEALTHY)

        dispatcher = Dispatcher(registry)

        with pytest.raises(DispatchError) as exc_info:
            await dispatcher.dispatch(sample_request)

        assert exc_info.value.code == "all_providers_unavailable"

        await registry.close()

    @pytest.mark.asyncio
    async def test_dispatch_strips_provider_prefix_from_model(
        self, multi_provider_config, sample_request, sample_response
    ):
        """Dispatch strips provider prefix before sending to adapter."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()
        registry._health["primary"].record_healthy()

        mock_adapter = AsyncMock()
        mock_adapter.chat = AsyncMock(return_value=sample_response)
        registry._adapters["primary"] = mock_adapter

        dispatcher = Dispatcher(registry)
        await dispatcher.dispatch(sample_request.model_copy(update={"model": "primary/llama3.2"}))

        # Check the model passed to adapter has prefix stripped
        call_args = mock_adapter.chat.call_args[0][0]
        assert call_args.model == "llama3.2"

        await registry.close()


# =============================================================================
# Integration Tests
# =============================================================================


class TestDispatchIntegration:
    """Integration tests for full dispatch flow."""

    @pytest.mark.asyncio
    async def test_full_dispatch_flow(self, multi_provider_config, sample_response):
        """Test complete dispatch flow from config to response."""
        registry = ProviderRegistry(multi_provider_config)
        await registry.initialize()

        # Simulate healthy primary
        registry._health["primary"].record_healthy()
        mock_adapter = AsyncMock()
        mock_adapter.chat = AsyncMock(return_value=sample_response)
        registry._adapters["primary"] = mock_adapter

        dispatcher = Dispatcher(registry)

        # Create request without provider hint - should use default
        request = InternalRequest(
            task=TaskType.CHAT,
            messages=[Message(role=MessageRole.USER, content="Test")],
        )

        result = await dispatcher.dispatch(request)

        assert result.response.content == "Hello! How can I help you?"
        assert result.provider_used == "primary"

        await registry.close()

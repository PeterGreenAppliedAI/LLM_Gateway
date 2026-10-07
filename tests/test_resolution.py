"""Tests for endpoint resolution logic and model catalog.

Tests the 5-step resolution policy:
1. Explicit override: endpoint/model syntax
2. Environment filter: only consider env-approved endpoints
3. Per-model default: config-specified model→endpoint mapping
4. Endpoint priority: first in priority list that has the model
5. Ambiguous → error: when no resolution strategy applies
"""

import pytest

from gateway.catalog.models import DiscoveredModel, ModelCatalog
from gateway.config import (
    EndpointConfig,
    EnvironmentConfig,
    GatewayConfig,
    ModelDefault,
    ResolutionConfig,
)
from gateway.dispatch.dispatcher import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    AmbiguousModelError,
    NoProviderError,
)
from gateway.models.common import ProviderType, TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def endpoints_config() -> GatewayConfig:
    """Config with multiple endpoints and resolution settings."""
    return GatewayConfig(
        endpoints=[
            EndpointConfig(
                name="gpunode-ollama",
                type=ProviderType.OLLAMA,
                url="http://gpunode:11434",
                labels={"prod_eligible": "true", "datacenter": "main"},
            ),
            EndpointConfig(
                name="dgxspark-ollama",
                type=ProviderType.OLLAMA,
                url="http://dgxspark:11434",
                labels={"prod_eligible": "false", "datacenter": "spark"},
            ),
            EndpointConfig(
                name="local-vllm",
                type=ProviderType.VLLM,
                url="http://localhost:8001",
                enabled=False,
            ),
        ],
        environments=[
            EnvironmentConfig(
                name="dev",
                allow_all_discovered=True,
            ),
            EnvironmentConfig(
                name="prod",
                endpoint_filter={"prod_eligible": "true"},
                approved_models=["phi4:14b", "devstral:24b"],
            ),
            EnvironmentConfig(
                name="staging",
                allowed_endpoints=["gpunode-ollama"],
            ),
        ],
        resolution=ResolutionConfig(
            model_defaults=[
                ModelDefault(model="phi4:14b", endpoint="gpunode-ollama"),
                ModelDefault(model="nemotron*", endpoint="dgxspark-ollama"),
            ],
            endpoint_priority=["gpunode-ollama", "dgxspark-ollama"],
            ambiguous_behavior="error",
        ),
    )


@pytest.fixture
def sample_request() -> InternalRequest:
    """Sample chat request."""
    return InternalRequest(
        task=TaskType.CHAT,
        model="phi4:14b",
        messages=[Message(role=MessageRole.USER, content="Hello")],
    )


# =============================================================================
# ModelCatalog Tests
# =============================================================================


class TestModelCatalog:
    """Tests for ModelCatalog."""

    def test_add_model(self):
        """Adding a model stores it correctly."""
        catalog = ModelCatalog()
        model = DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama")

        catalog.add_model(model)

        assert len(catalog.discovered) == 1
        assert catalog.discovered[0].name == "phi4:14b"

    def test_add_model_updates_existing(self):
        """Adding same model/endpoint combination updates it."""
        catalog = ModelCatalog()
        model1 = DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama", size_bytes=100)
        model2 = DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama", size_bytes=200)

        catalog.add_model(model1)
        catalog.add_model(model2)

        assert len(catalog.discovered) == 1
        assert catalog.discovered[0].size_bytes == 200

    def test_get_endpoints_for_model(self):
        """Get all endpoints that have a model."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="dgxspark-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="dgxspark-ollama"))

        endpoints = catalog.get_endpoints_for_model("phi4:14b")

        assert set(endpoints) == {"gpunode-ollama", "dgxspark-ollama"}

    def test_get_models_for_endpoint(self):
        """Get all models on an endpoint."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="devstral:24b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="dgxspark-ollama"))

        models = catalog.get_models_for_endpoint("gpunode-ollama")

        assert set(models) == {"phi4:14b", "devstral:24b"}

    def test_has_model(self):
        """Check if model exists."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))

        assert catalog.has_model("phi4:14b")
        assert catalog.has_model("phi4:14b", endpoint="gpunode-ollama")
        assert not catalog.has_model("phi4:14b", endpoint="other")
        assert not catalog.has_model("nonexistent")

    def test_remove_endpoint_models(self):
        """Remove all models from an endpoint."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="dgxspark-ollama"))

        catalog.remove_endpoint_models("gpunode-ollama")

        assert len(catalog.discovered) == 1
        assert catalog.discovered[0].endpoint == "dgxspark-ollama"

    def test_find_model_with_pattern(self):
        """Find models using glob patterns."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="nemotron:70b", endpoint="dgxspark-ollama"))
        catalog.add_model(DiscoveredModel(name="nemotron:8b", endpoint="dgxspark-ollama"))
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))

        matches = catalog.find_model("nemotron*")

        assert len(matches) == 2
        assert all("nemotron" in m.name for m in matches)


class TestModelCatalogEnvironmentFiltering:
    """Tests for ModelCatalog.effective_models with environment filtering."""

    def test_effective_models_no_environment(self):
        """Without environment, all models are effective."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="dgxspark-ollama"))

        effective = catalog.effective_models(environment=None)

        assert len(effective) == 2

    def test_effective_models_allow_all_discovered(self):
        """allow_all_discovered=True allows all models."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="dgxspark-ollama"))

        env = EnvironmentConfig(name="dev", allow_all_discovered=True)
        effective = catalog.effective_models(environment=env)

        assert len(effective) == 2

    def test_effective_models_endpoint_filter(self):
        """endpoint_filter filters by labels."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="other:7b", endpoint="dgxspark-ollama"))

        env = EnvironmentConfig(
            name="prod",
            endpoint_filter={"prod_eligible": "true"},
        )
        endpoint_labels = {
            "gpunode-ollama": {"prod_eligible": "true"},
            "dgxspark-ollama": {"prod_eligible": "false"},
        }

        effective = catalog.effective_models(environment=env, endpoint_labels=endpoint_labels)

        assert len(effective) == 1
        assert effective[0].endpoint == "gpunode-ollama"

    def test_effective_models_approved_models(self):
        """approved_models filters by model name."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="devstral:24b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="unapproved:7b", endpoint="gpunode-ollama"))

        env = EnvironmentConfig(
            name="prod",
            approved_models=["phi4:14b", "devstral:*"],  # Pattern matching
        )

        effective = catalog.effective_models(environment=env)

        assert len(effective) == 2
        names = {m.name for m in effective}
        assert "phi4:14b" in names
        assert "devstral:24b" in names
        assert "unapproved:7b" not in names

    def test_effective_models_allowed_endpoints(self):
        """allowed_endpoints restricts to specific endpoints."""
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="gpunode-ollama"))
        catalog.add_model(DiscoveredModel(name="phi4:14b", endpoint="dgxspark-ollama"))

        env = EnvironmentConfig(
            name="staging",
            allowed_endpoints=["gpunode-ollama"],
        )

        effective = catalog.effective_models(environment=env)

        assert len(effective) == 1
        assert effective[0].endpoint == "gpunode-ollama"


# =============================================================================
# Dispatcher Resolution Tests
# =============================================================================


class TestDispatcherResolution:
    """Tests for Dispatcher.resolve_endpoint - the 5-step resolution policy."""

    @pytest.mark.asyncio
    async def test_step1_explicit_endpoint_model_syntax(self, endpoints_config, sample_request):
        """Step 1: Explicit endpoint/model syntax takes priority."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # Use explicit endpoint/model syntax
        request = sample_request.model_copy(update={"model": "dgxspark-ollama/phi4:14b"})
        endpoint, model = dispatcher.resolve_endpoint(request)

        assert endpoint == "dgxspark-ollama"
        assert model == "phi4:14b"

        await registry.close()

    @pytest.mark.asyncio
    async def test_step1_unknown_prefix_is_part_of_model_name(
        self, endpoints_config, sample_request
    ):
        """A prefix that isn't a configured endpoint is part of the model name.

        It used to raise EndpointNotFoundError, which broke every Hugging Face
        style model ID ("meta-llama/Llama-3.1-8B-Instruct")."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        request = sample_request.model_copy(update={"model": "meta-llama/Llama-3.1-8B-Instruct"})
        endpoint, model = dispatcher.resolve_endpoint(request)

        assert model == "meta-llama/Llama-3.1-8B-Instruct"
        assert endpoint == "gpunode-ollama"  # first by priority

        await registry.close()

    @pytest.mark.asyncio
    async def test_step2_environment_filter(self, endpoints_config, sample_request):
        """Step 2: Environment filters available endpoints."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # Prod environment only allows prod_eligible endpoints
        prod_env = endpoints_config.get_environment("prod")
        available = ["gpunode-ollama", "dgxspark-ollama"]

        endpoint, model = dispatcher.resolve_endpoint(
            sample_request,
            environment=prod_env,
            available_endpoints=available,
        )

        # Should resolve to gpunode-ollama (only prod_eligible endpoint)
        assert endpoint == "gpunode-ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_step3_model_default(self, endpoints_config, sample_request):
        """Step 3: Per-model defaults are used when configured."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # phi4:14b has a default configured → gpunode-ollama
        available = ["gpunode-ollama", "dgxspark-ollama"]

        endpoint, model = dispatcher.resolve_endpoint(
            sample_request,
            available_endpoints=available,
        )

        assert endpoint == "gpunode-ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_step3_model_default_with_glob(self, endpoints_config):
        """Step 3: Glob patterns in model defaults work."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # nemotron* should match nemotron:70b → dgxspark-ollama
        request = InternalRequest(
            task=TaskType.CHAT,
            model="nemotron:70b",
            messages=[Message(role=MessageRole.USER, content="Hello")],
        )
        available = ["gpunode-ollama", "dgxspark-ollama"]

        endpoint, model = dispatcher.resolve_endpoint(
            request,
            available_endpoints=available,
        )

        assert endpoint == "dgxspark-ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_step4_endpoint_priority(self, endpoints_config):
        """Step 4: Endpoint priority is used when no default configured."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # Use a model without a default
        request = InternalRequest(
            task=TaskType.CHAT,
            model="unknown:7b",
            messages=[Message(role=MessageRole.USER, content="Hello")],
        )
        available = ["dgxspark-ollama", "gpunode-ollama"]  # Note: different order

        endpoint, model = dispatcher.resolve_endpoint(
            request,
            available_endpoints=available,
        )

        # Should use gpunode-ollama per priority (even though dgxspark is first in list)
        assert endpoint == "gpunode-ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_step5_ambiguous_error(self, endpoints_config):
        """Step 5: Ambiguous model raises error when configured."""
        # Create config with no defaults and no priority
        config = GatewayConfig(
            endpoints=[
                EndpointConfig(
                    name="endpoint-a",
                    type=ProviderType.OLLAMA,
                    url="http://a:11434",
                ),
                EndpointConfig(
                    name="endpoint-b",
                    type=ProviderType.OLLAMA,
                    url="http://b:11434",
                ),
            ],
            resolution=ResolutionConfig(
                ambiguous_behavior="error",
            ),
        )

        registry = ProviderRegistry(config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, config.resolution)

        request = InternalRequest(
            task=TaskType.CHAT,
            model="phi4:14b",
            messages=[Message(role=MessageRole.USER, content="Hello")],
        )
        available = ["endpoint-a", "endpoint-b"]

        with pytest.raises(AmbiguousModelError) as exc_info:
            dispatcher.resolve_endpoint(request, available_endpoints=available)

        assert "phi4:14b" in str(exc_info.value)
        assert "endpoint-a" in str(exc_info.value)
        assert "endpoint-b" in str(exc_info.value)

        await registry.close()

    @pytest.mark.asyncio
    async def test_step5_first_priority_behavior(self, endpoints_config):
        """Step 5: first_priority behavior uses first available."""
        config = GatewayConfig(
            endpoints=[
                EndpointConfig(
                    name="endpoint-a",
                    type=ProviderType.OLLAMA,
                    url="http://a:11434",
                ),
                EndpointConfig(
                    name="endpoint-b",
                    type=ProviderType.OLLAMA,
                    url="http://b:11434",
                ),
            ],
            resolution=ResolutionConfig(
                ambiguous_behavior="first_priority",
            ),
        )

        registry = ProviderRegistry(config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, config.resolution)

        request = InternalRequest(
            task=TaskType.CHAT,
            model="phi4:14b",
            messages=[Message(role=MessageRole.USER, content="Hello")],
        )
        available = ["endpoint-b", "endpoint-a"]  # Note order

        endpoint, model = dispatcher.resolve_endpoint(request, available_endpoints=available)

        # Should use first available (endpoint-b)
        assert endpoint == "endpoint-b"

        await registry.close()

    @pytest.mark.asyncio
    async def test_single_endpoint_no_ambiguity(self, endpoints_config):
        """Single endpoint is used without ambiguity check."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        request = InternalRequest(
            task=TaskType.CHAT,
            model="unique:7b",
            messages=[Message(role=MessageRole.USER, content="Hello")],
        )
        available = ["gpunode-ollama"]

        endpoint, model = dispatcher.resolve_endpoint(request, available_endpoints=available)

        assert endpoint == "gpunode-ollama"

        await registry.close()

    @pytest.mark.asyncio
    async def test_no_endpoints_raises_error(self, endpoints_config, sample_request):
        """No available endpoints raises NoProviderError."""
        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()

        dispatcher = Dispatcher(registry, endpoints_config.resolution)

        # Environment that filters out all endpoints
        env = EnvironmentConfig(
            name="empty",
            allowed_endpoints=["nonexistent"],
        )

        with pytest.raises(NoProviderError):
            dispatcher.resolve_endpoint(sample_request, environment=env, available_endpoints=[])

        await registry.close()


# =============================================================================
# DiscoveredModel Tests
# =============================================================================


class TestDiscoveredModel:
    """Tests for DiscoveredModel."""

    def test_matches_exact_pattern(self):
        """Exact pattern match."""
        model = DiscoveredModel(name="phi4:14b", endpoint="test")
        assert model.matches_pattern("phi4:14b")
        assert not model.matches_pattern("phi4:7b")

    def test_matches_glob_pattern(self):
        """Glob pattern matching."""
        model = DiscoveredModel(name="phi4:14b", endpoint="test")
        assert model.matches_pattern("phi4:*")
        assert model.matches_pattern("*:14b")
        assert model.matches_pattern("phi*")
        assert not model.matches_pattern("llama*")

    def test_matches_wildcard(self):
        """Wildcard matches all."""
        model = DiscoveredModel(name="phi4:14b", endpoint="test")
        assert model.matches_pattern("*")


# =============================================================================
# Config Validation Tests
# =============================================================================


class TestEndpointConfigValidation:
    """Tests for endpoint config validation."""

    def test_endpoint_config_auto_migration_to_providers(self):
        """Endpoints auto-migrate to providers."""
        config = GatewayConfig(
            endpoints=[
                EndpointConfig(
                    name="test",
                    type=ProviderType.OLLAMA,
                    url="http://localhost:11434",
                )
            ]
        )

        # Providers should be auto-populated
        assert len(config.providers) == 1
        assert config.providers[0].name == "test"

    def test_providers_auto_migrate_to_endpoints(self):
        """Providers auto-migrate to endpoints."""
        from gateway.config import ProviderConfig

        config = GatewayConfig(
            providers=[
                ProviderConfig(
                    name="test",
                    type=ProviderType.OLLAMA,
                    base_url="http://localhost:11434",
                )
            ]
        )

        # Endpoints should be auto-populated
        assert len(config.endpoints) == 1
        assert config.endpoints[0].name == "test"

    def test_resolution_config_validation(self):
        """Resolution config validates endpoint references."""
        with pytest.raises(ValueError) as exc_info:
            GatewayConfig(
                endpoints=[
                    EndpointConfig(
                        name="test",
                        type=ProviderType.OLLAMA,
                        url="http://localhost:11434",
                    )
                ],
                resolution=ResolutionConfig(
                    model_defaults=[
                        ModelDefault(model="phi4:14b", endpoint="nonexistent"),
                    ]
                ),
            )

        assert "nonexistent" in str(exc_info.value)

    def test_environment_config_validation(self):
        """Environment config validates endpoint references."""
        with pytest.raises(ValueError) as exc_info:
            GatewayConfig(
                endpoints=[
                    EndpointConfig(
                        name="test",
                        type=ProviderType.OLLAMA,
                        url="http://localhost:11434",
                    )
                ],
                environments=[
                    EnvironmentConfig(
                        name="prod",
                        allowed_endpoints=["nonexistent"],
                    )
                ],
            )

        assert "nonexistent" in str(exc_info.value)


# =============================================================================
# Endpoint allowlist enforcement (key + environment scope)
# =============================================================================


class TestAllowedEndpointsEnforced:
    """allowed_endpoints applies to the endpoint actually used.

    Regression: the allowlist was only compared against preferred_provider,
    so catalog routing, priority order, fallback and endpoint/model pins
    could all reach an endpoint the key excluded.
    """

    @pytest.fixture
    async def setup(self, endpoints_config):
        from unittest.mock import AsyncMock

        from gateway.models.common import FinishReason
        from gateway.models.internal import InternalResponse, StreamChunk

        registry = ProviderRegistry(endpoints_config)
        await registry.initialize()
        calls: list[str] = []

        for name in ("gpunode-ollama", "dgxspark-ollama"):
            registry.catalog.add_model(DiscoveredModel(name="shared:7b", endpoint=name))
            registry.get_health(name).record_healthy()
            adapter = registry.get(name)

            async def chat(request, _name=name):
                calls.append(_name)
                return InternalResponse(
                    request_id=request.request_id,
                    task=request.task,
                    provider=_name,
                    model=request.model,
                    content="ok",
                    finish_reason=FinishReason.STOP,
                )

            async def chat_stream(request, _name=name):
                calls.append(_name)
                yield StreamChunk(
                    request_id=request.request_id,
                    index=0,
                    delta="ok",
                    finish_reason=FinishReason.STOP,
                )

            adapter.chat = AsyncMock(side_effect=chat)
            adapter.chat_stream = chat_stream

        dispatcher = Dispatcher(registry, endpoints_config.resolution)
        yield dispatcher, registry, calls
        await registry.close()

    @staticmethod
    def _request(model: str = "shared:7b", **kwargs) -> InternalRequest:
        return InternalRequest(
            task=TaskType.CHAT,
            model=model,
            messages=[Message(role=MessageRole.USER, content="Hello")],
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_catalog_routing_skips_disallowed_priority_endpoint(self, setup):
        dispatcher, _, calls = setup
        result = await dispatcher.dispatch(self._request(allowed_endpoints=["dgxspark-ollama"]))
        # gpunode-ollama is first by priority but not allowed
        assert result.provider_used == "dgxspark-ollama"
        assert calls == ["dgxspark-ollama"]

    @pytest.mark.asyncio
    async def test_fallback_never_reaches_disallowed_endpoint(self, setup):
        dispatcher, registry, calls = setup
        from gateway.errors import AllProvidersUnavailableError
        from gateway.models.common import HealthStatus

        registry.get_health("gpunode-ollama").record_unhealthy(HealthStatus.UNHEALTHY)
        registry.get("gpunode-ollama").health = _unhealthy
        with pytest.raises(AllProvidersUnavailableError):
            await dispatcher.dispatch(self._request(allowed_endpoints=["gpunode-ollama"]))
        assert "dgxspark-ollama" not in calls

    @pytest.mark.asyncio
    async def test_pin_to_disallowed_endpoint_refused(self, setup):
        dispatcher, _, calls = setup
        from gateway.errors import PolicyError

        request = self._request(
            model="gpunode-ollama/shared:7b", allowed_endpoints=["dgxspark-ollama"]
        )
        with pytest.raises(PolicyError) as exc:
            await dispatcher.dispatch(request)
        assert exc.value.code.value == "endpoint_not_allowed"
        assert calls == []

    @pytest.mark.asyncio
    async def test_stream_respects_allowlist(self, setup):
        dispatcher, _, calls = setup
        provider, stream = await dispatcher.dispatch_stream(
            self._request(stream=True, allowed_endpoints=["dgxspark-ollama"])
        )
        assert provider == "dgxspark-ollama"
        assert [chunk.delta async for chunk in stream] == ["ok"]
        assert calls == ["dgxspark-ollama"]

    @pytest.mark.asyncio
    async def test_unrestricted_when_none(self, setup):
        dispatcher, _, _ = setup
        result = await dispatcher.dispatch(self._request())
        assert result.provider_used == "gpunode-ollama"


async def _unhealthy():
    from gateway.models.common import HealthStatus

    return HealthStatus.UNHEALTHY

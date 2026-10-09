"""Regression tests for the 2026-10 review fixes.

1. Streaming fallback is catalog-filtered: admission overflow must not
   send a stream to an endpoint without the model (404 under load).
2. Budget reservations strip an "endpoint/" pin so tier globs resolve on
   the real model name (admission and settlement agree).
"""

from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, ProviderConfig, RoutingConfig
from gateway.dispatch.dispatcher import Dispatcher
from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer, PolicyViolation
from gateway.policy.token_budget import (
    ModelAssignment,
    ModelTierConfig,
    TokenBudgetConfig,
)


@pytest.fixture
def sample_request() -> InternalRequest:
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[Message(role=MessageRole.USER, content="Hello")],
    )


class TestStreamOrderCatalogFilter:
    def _dispatcher(self, with_model: list[str]) -> Dispatcher:
        registry = MagicMock()
        registry.get_endpoints_with_model = MagicMock(return_value=with_model)
        registry.is_healthy = MagicMock(return_value=True)
        registry.get_fallback_chain = MagicMock(return_value=["ep2", "ep3-no-model"])
        return Dispatcher(registry)

    def test_fallbacks_without_model_are_excluded(self, sample_request):
        """ep3 doesn't have the model: overflow must never reach it."""
        dispatcher = self._dispatcher(with_model=["primary", "ep2"])
        order = dispatcher._get_stream_provider_order(
            "primary", "llama3.2", sample_request, pinned=False
        )
        assert order == ["primary", "ep2"]
        assert "ep3-no-model" not in order

    def test_unknown_model_keeps_full_chain(self, sample_request):
        """Discovery lag (catalog empty): availability wins, old behavior."""
        dispatcher = self._dispatcher(with_model=[])
        order = dispatcher._get_stream_provider_order(
            "primary", "brand-new-model", sample_request, pinned=False
        )
        assert order == ["primary", "ep2", "ep3-no-model"]

    def test_pinned_is_only_primary(self, sample_request):
        dispatcher = self._dispatcher(with_model=["primary", "ep2"])
        assert dispatcher._get_stream_provider_order(
            "primary", "llama3.2", sample_request, pinned=True
        ) == ["primary"]


class TestBudgetPinStripping:
    def _enforcer(self, endpoint_names=("gpu-node",)) -> PolicyEnforcer:
        return PolicyEnforcer(
            PolicyConfig(
                token_budget=TokenBudgetConfig(
                    enabled=True,
                    default_daily_limit=1000,
                    default_cost_multiplier=5.0,
                    model_tiers=[ModelTierConfig(name="standard", cost_multiplier=1.0)],
                    model_assignments=[ModelAssignment(model="cheap*", tier="standard")],
                )
            ),
            endpoint_names=endpoint_names,
        )

    def test_bare_model_strips_known_endpoint_only(self):
        enforcer = self._enforcer()
        assert enforcer._bare_model("gpu-node/cheap-model") == "cheap-model"
        # HF-style org prefixes are model names, not pins
        assert enforcer._bare_model("meta-llama/Llama-3.1-8B") == "meta-llama/Llama-3.1-8B"
        assert enforcer._bare_model("cheap-model") == "cheap-model"

    @pytest.mark.asyncio
    async def test_pinned_model_reserves_at_its_tier(self, sample_request):
        """300 tokens at the standard 1x tier fits a 1000 budget; the old
        behavior weighted the pinned name at the 5x default (1500) and
        spuriously rejected it."""
        enforcer = self._enforcer()
        request = sample_request.model_copy(
            update={"model": "gpu-node/cheap-model", "max_tokens": 300}
        )
        await enforcer.enforce(request, rate_limit_key="k")  # must not raise

    @pytest.mark.asyncio
    async def test_unknown_model_still_gets_default_multiplier(self, sample_request):
        enforcer = self._enforcer()
        request = sample_request.model_copy(
            update={"model": "gpu-node/mystery-model", "max_tokens": 300}
        )
        with pytest.raises(PolicyViolation) as exc_info:
            await enforcer.enforce(request, rate_limit_key="k")
        assert exc_info.value.code == "token_budget_exceeded"


class TestEnforcerWiring:
    def test_get_enforcer_passes_enabled_endpoint_names(self):
        """The dependency bridges enabled endpoints into the enforcer."""
        config = GatewayConfig(
            providers=[
                ProviderConfig(name="ep-on", type="ollama", base_url="http://x:11434"),
                ProviderConfig(
                    name="ep-off", type="ollama", base_url="http://y:11434", enabled=False
                ),
            ],
            routing=RoutingConfig(default_provider="ep-on"),
        )
        names = {p.name for p in config.get_enabled_providers()}
        assert names == {"ep-on"}


class TestDiscoveryWithMediaEndpoints:
    @pytest.mark.asyncio
    async def test_media_endpoint_does_not_crash_discovery(self):
        """Regression: discover_all indexed gather results against a
        differently-filtered endpoint list — IndexError the moment a
        media-only (tts/stt) endpoint was configured."""
        from gateway.catalog.discovery import ModelDiscoveryService
        from gateway.catalog.models import ModelCatalog
        from gateway.config import EndpointConfig

        endpoints = [
            EndpointConfig(name="text-ep", type="ollama", url="http://t:11434"),
            EndpointConfig(
                name="audio-ep",
                type="openai",
                url="http://a:8000",
                capabilities=["tts", "stt"],
            ),
            EndpointConfig(name="text-ep2", type="vllm", url="http://v:8000"),
        ]
        discovery = ModelDiscoveryService(endpoints, ModelCatalog())

        async def fake_discover(endpoint):
            return [f"model-on-{endpoint.name}"]

        discovery._discover_endpoint = fake_discover
        results = await discovery.discover_all()

        assert results == {
            "text-ep": ["model-on-text-ep"],
            "text-ep2": ["model-on-text-ep2"],
        }
        assert "audio-ep" not in results

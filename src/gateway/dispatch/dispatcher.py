"""Dispatcher - routes requests to providers with health-aware fallback.

Implements the resolution policy for model→endpoint mapping:
1. Explicit override: endpoint/model syntax
2. Environment filter: only consider env-approved endpoints
3. Per-model default: config-specified model→endpoint mapping
4. Endpoint priority: first in priority list that has the model
5. Ambiguous → error: when no resolution strategy applies

Per rule.md:
- Single Responsibility: Dispatcher only handles request dispatch
- Explicit Boundaries: Clear input (request) and output (response or error)
- No Implicit Trust: Validate provider names from user input

Per API Error Handling Architecture:
- Uses domain errors from gateway.errors
- Errors propagate to exception handler middleware
"""

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Optional, List, AsyncIterator

from gateway.config import (
    SAFE_IDENTIFIER_PATTERN,
    EnvironmentConfig,
    ResolutionConfig,
)
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    DispatchError,
    NoProviderError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    AllProvidersUnavailableError,
    AmbiguousModelError,
    ModelNotFoundError,
    EndpointNotFoundError,
)
from gateway.models.common import HealthStatus
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk
from gateway.providers import ProviderAdapter


# Maximum number of providers to attempt before failing
# Security: Prevents unbounded list growth in attempted_providers
MAX_FALLBACK_ATTEMPTS = 10


@dataclass
class DispatchResult:
    """Result of a dispatch operation."""
    response: InternalResponse
    provider_used: str
    was_fallback: bool = False
    # Security: Bounded list to prevent memory exhaustion
    attempted_providers: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.attempted_providers:
            self.attempted_providers = [self.provider_used]


class Dispatcher:
    """Routes requests to providers with fallback support.

    Resolution logic (5 steps):
    1. Explicit override: endpoint/model syntax
    2. Environment filter: only consider env-approved endpoints
    3. Per-model default: config-specified model→endpoint mapping
    4. Endpoint priority: first in priority list that has the model
    5. Ambiguous → error: when no resolution strategy applies

    Legacy dispatch logic (backward compatible):
    1. Parse provider from model string (e.g., "ollama/llama3.2" → "ollama")
    2. Or use preferred_provider from request
    3. Or use default provider from config
    4. Check health, attempt request
    5. On failure, try fallback providers if allowed
    """

    # Pattern to parse "provider/model" or "endpoint/model" format
    MODEL_PROVIDER_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)/(.+)$")

    def __init__(
        self,
        registry: ProviderRegistry,
        resolution_config: ResolutionConfig | None = None,
    ):
        """Initialize dispatcher with provider registry.

        Args:
            registry: Initialized provider registry with health tracking
            resolution_config: Optional resolution configuration for endpoint selection
        """
        self._registry = registry
        self._resolution_config = resolution_config or ResolutionConfig()

    def parse_provider_from_model(self, model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Parse provider and model from model string.

        Supports formats:
        - "provider/model" → ("provider", "model")
        - "model" → (None, "model")
        - None → (None, None)

        Args:
            model: Model string from request

        Returns:
            Tuple of (provider_name, model_name)

        Security:
            Validates provider name against SafeIdentifier pattern to prevent
            log injection and metric label injection attacks.
        """
        if not model:
            return None, None

        match = self.MODEL_PROVIDER_PATTERN.match(model)
        if match:
            provider_hint = match.group(1)
            model_name = match.group(2)

            # Security: Validate provider name from user input
            if not SAFE_IDENTIFIER_PATTERN.match(provider_hint):
                # Invalid provider name - treat as unprefixed model
                return None, model

            return provider_hint, model_name

        return None, model

    def resolve_provider(self, request: InternalRequest) -> tuple[str, str]:
        """Resolve which provider and model to use for a request.

        Resolution order:
        1. Parse from model string ("provider/model")
        2. Use preferred_provider from request
        3. Use default provider from config

        Args:
            request: The internal request

        Returns:
            Tuple of (provider_name, model_name)

        Raises:
            DispatchError: If no provider can be resolved
        """
        # Try to parse from model string
        provider_hint, model_name = self.parse_provider_from_model(request.model)

        # Use explicit hint if found
        if provider_hint:
            return provider_hint, model_name or request.model

        # Try preferred_provider from request
        if request.preferred_provider:
            return request.preferred_provider, request.model

        # Fall back to default
        default = self._registry.get_default_provider()
        if default:
            return default, request.model

        raise NoProviderError()

    def resolve_endpoint(
        self,
        request: InternalRequest,
        environment: EnvironmentConfig | None = None,
        available_endpoints: list[str] | None = None,
    ) -> tuple[str, str]:
        """Resolve which endpoint and model to use for a request.

        Implements the 5-step resolution policy:
        1. Explicit override: endpoint/model syntax
        2. Environment filter: only consider env-approved endpoints
        3. Per-model default: config-specified model→endpoint mapping
        4. Endpoint priority: first in priority list that has the model
        5. Ambiguous → error: when no resolution strategy applies

        Args:
            request: The internal request
            environment: Optional environment config for filtering
            available_endpoints: Optional list of endpoints that have the model
                               (from catalog discovery). If None, uses registry.

        Returns:
            Tuple of (endpoint_name, model_name)

        Raises:
            EndpointNotFoundError: If explicitly requested endpoint doesn't exist
            ModelNotFoundError: If model not found on any available endpoint
            AmbiguousModelError: If model on multiple endpoints with no default
            NoProviderError: If no endpoint can be resolved
        """
        # Step 1: Check for explicit endpoint/model syntax
        endpoint_hint, model_name = self.parse_provider_from_model(request.model)

        if endpoint_hint:
            # Validate the endpoint exists
            if not self._registry.get(endpoint_hint):
                raise EndpointNotFoundError(endpoint=endpoint_hint)
            return endpoint_hint, model_name or request.model

        # Use raw model name from here
        model_name = request.model or ""

        # Step 2: Filter endpoints by environment
        candidate_endpoints = self._filter_endpoints_by_environment(
            available_endpoints or self._registry.list_providers(),
            environment,
        )

        if not candidate_endpoints:
            raise NoProviderError(
                message="No endpoints available for this environment"
            )

        # If only one endpoint, use it
        if len(candidate_endpoints) == 1:
            return candidate_endpoints[0], model_name

        # Step 3: Check per-model defaults
        default_endpoint = self._find_model_default(model_name)
        if default_endpoint and default_endpoint in candidate_endpoints:
            return default_endpoint, model_name

        # Step 4: Use endpoint priority
        for priority_endpoint in self._resolution_config.endpoint_priority:
            if priority_endpoint in candidate_endpoints:
                return priority_endpoint, model_name

        # Step 5: Handle ambiguity
        if self._resolution_config.ambiguous_behavior == "first_priority":
            # Use first available endpoint
            return candidate_endpoints[0], model_name

        # Default: error on ambiguity
        if len(candidate_endpoints) > 1:
            raise AmbiguousModelError(model=model_name, endpoints=candidate_endpoints)

        # Single endpoint remaining
        if candidate_endpoints:
            return candidate_endpoints[0], model_name

        raise NoProviderError()

    def _filter_endpoints_by_environment(
        self,
        endpoints: list[str],
        environment: EnvironmentConfig | None,
    ) -> list[str]:
        """Filter endpoints based on environment configuration.

        Args:
            endpoints: List of endpoint names to filter
            environment: Environment configuration (None = allow all)

        Returns:
            Filtered list of endpoint names
        """
        if environment is None:
            return endpoints

        filtered = []
        for ep_name in endpoints:
            # Check allowed_endpoints
            if environment.allowed_endpoints:
                if ep_name not in environment.allowed_endpoints:
                    continue

            # Check endpoint_filter labels
            if environment.endpoint_filter:
                endpoint_config = self._registry.get_endpoint_config(ep_name)
                if endpoint_config:
                    labels = getattr(endpoint_config, 'labels', {})
                    if not self._labels_match(labels, environment.endpoint_filter):
                        continue

            filtered.append(ep_name)

        return filtered

    def _labels_match(
        self,
        labels: dict[str, str],
        required: dict[str, str],
    ) -> bool:
        """Check if labels match required filter."""
        for key, value in required.items():
            if labels.get(key) != value:
                return False
        return True

    def _find_model_default(self, model: str) -> str | None:
        """Find default endpoint for a model from config.

        Supports glob patterns (e.g., "phi4:*" matches "phi4:14b").

        Args:
            model: Model name to look up

        Returns:
            Endpoint name if a default is configured, None otherwise
        """
        for model_default in self._resolution_config.model_defaults:
            # Check exact match first
            if model_default.model == model:
                return model_default.endpoint
            # Check glob pattern
            if fnmatch.fnmatch(model, model_default.model):
                return model_default.endpoint
        return None

    async def dispatch(self, request: InternalRequest) -> DispatchResult:
        """Dispatch a request to the appropriate provider.

        Args:
            request: Normalized internal request

        Returns:
            DispatchResult with response and metadata

        Raises:
            DispatchError: If dispatch fails and no fallback available
        """
        provider_name, model_name = self.resolve_provider(request)
        attempted: List[str] = []

        # Update request with resolved model (strip provider prefix)
        if model_name and model_name != request.model:
            request = request.model_copy(update={"model": model_name})

        # Try primary provider
        result = await self._try_provider(provider_name, request)
        attempted.append(provider_name)

        if result is not None:
            return DispatchResult(
                response=result,
                provider_used=provider_name,
                was_fallback=False,
                attempted_providers=attempted
            )

        # Primary failed - try fallbacks if allowed
        if not request.fallback_allowed:
            raise ProviderUnavailableError(provider=provider_name, fallback_disabled=True)

        # Get fallback chain - limited to prevent unbounded attempts
        fallback_chain = self._registry.get_fallback_chain(exclude=provider_name)
        # Security: Cap fallback attempts to prevent resource exhaustion
        max_fallbacks = MAX_FALLBACK_ATTEMPTS - 1  # -1 for primary already tried

        for fallback_name in fallback_chain[:max_fallbacks]:
            result = await self._try_provider(fallback_name, request)
            attempted.append(fallback_name)

            if result is not None:
                return DispatchResult(
                    response=result,
                    provider_used=fallback_name,
                    was_fallback=True,
                    attempted_providers=attempted
                )

        # All providers failed
        raise AllProvidersUnavailableError(attempted=attempted)

    async def _try_provider(
        self, provider_name: str, request: InternalRequest
    ) -> Optional[InternalResponse]:
        """Attempt to dispatch request to a specific provider.

        Args:
            provider_name: Name of provider to try
            request: The request to dispatch

        Returns:
            InternalResponse if successful, None if provider unavailable/unhealthy
        """
        adapter = self._registry.get(provider_name)
        if adapter is None:
            return None

        # Check health (use cached status, don't block on health check)
        if not self._registry.is_healthy(provider_name):
            # Try one on-demand health check in case it recovered
            status = await self._registry.check_health(provider_name)
            if status != HealthStatus.HEALTHY:
                return None

        # Dispatch based on task type
        try:
            response = await self._execute_request(adapter, request)

            # Check if response is an error
            if response.is_error:
                return None

            return response

        except Exception:
            # Mark provider as potentially unhealthy and return None for fallback
            return None

    async def _execute_request(
        self, adapter: ProviderAdapter, request: InternalRequest
    ) -> InternalResponse:
        """Execute request on adapter based on task type.

        Args:
            adapter: Provider adapter to use
            request: The request to execute

        Returns:
            InternalResponse from the provider
        """
        from gateway.models.common import TaskType

        if request.task == TaskType.EMBEDDINGS:
            return await adapter.embeddings(request)
        elif request.task in (TaskType.COMPLETION, TaskType.GENERATE):
            return await adapter.generate(request)
        else:
            # Default to chat for most tasks
            return await adapter.chat(request)

    # =========================================================================
    # Streaming Support
    # =========================================================================

    def _get_stream_provider_order(
        self, primary: str, model_name: str | None, request: InternalRequest,
    ) -> list[str]:
        """Build ordered list of providers to try for streaming.

        Uses the model catalog to prefer providers that have the model,
        then falls back to health-based ordering.
        """
        providers: list[str] = []
        seen: set[str] = set()

        # First: check catalog for providers that have this model
        if model_name:
            catalog_endpoints = self._registry.get_endpoints_with_model(model_name)
            # Prefer healthy endpoints from catalog
            for ep in catalog_endpoints:
                if ep not in seen and self._registry.is_healthy(ep):
                    providers.append(ep)
                    seen.add(ep)
            # Then unhealthy catalog endpoints (model might still work)
            for ep in catalog_endpoints:
                if ep not in seen:
                    providers.append(ep)
                    seen.add(ep)

        # Second: the resolved primary provider
        if primary not in seen:
            providers.append(primary)
            seen.add(primary)

        # Third: fallback chain (if allowed)
        if request.fallback_allowed:
            for fb in self._registry.get_fallback_chain(exclude=primary):
                if fb not in seen:
                    providers.append(fb)
                    seen.add(fb)

        return providers

    async def dispatch_stream(
        self, request: InternalRequest
    ) -> tuple[str, AsyncIterator[StreamChunk]]:
        """Dispatch a streaming request to the appropriate provider.

        Tries providers in order (catalog-aware), peeking at the first chunk
        to detect errors before committing to a provider.

        Args:
            request: Normalized internal request with stream=True

        Returns:
            Tuple of (provider_name, stream_iterator)

        Raises:
            DispatchError: If all providers fail
        """
        from gateway.models.common import FinishReason

        provider_name, model_name = self.resolve_provider(request)

        # Update request with resolved model
        if model_name and model_name != request.model:
            request = request.model_copy(update={"model": model_name})

        providers_to_try = self._get_stream_provider_order(
            provider_name, model_name, request,
        )

        if not providers_to_try:
            raise NoProviderError()

        attempted: list[str] = []

        for try_name in providers_to_try[:MAX_FALLBACK_ATTEMPTS]:
            adapter = self._registry.get(try_name)
            if adapter is None:
                continue

            attempted.append(try_name)

            # Start the stream and peek at the first chunk to detect errors
            stream_iter = adapter.chat_stream(request)
            try:
                first_chunk = await stream_iter.__anext__()
            except StopAsyncIteration:
                # Empty stream - provider returned nothing, try next
                continue
            except Exception:
                # Provider threw during stream startup, try next
                continue

            # If first chunk is an error, try next provider
            # (thinking-only chunks are valid for reasoning models)
            if first_chunk.finish_reason == FinishReason.ERROR and not first_chunk.delta and not first_chunk.thinking:
                continue

            # Success - return a chained stream (first_chunk + rest)
            async def _chain(first: StreamChunk, rest: AsyncIterator[StreamChunk]) -> AsyncIterator[StreamChunk]:
                yield first
                async for chunk in rest:
                    yield chunk

            return try_name, _chain(first_chunk, stream_iter)

        raise AllProvidersUnavailableError(attempted=attempted)

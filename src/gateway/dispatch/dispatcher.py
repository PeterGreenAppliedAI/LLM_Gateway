"""Dispatcher - routes requests to providers with health-aware fallback.

NOT smart routing. Just:
1. Parse provider hint from request
2. Look up provider
3. Check health
4. Forward or fallback

Per rule.md:
- Single Responsibility: Dispatcher only handles request dispatch
- Explicit Boundaries: Clear input (request) and output (response or error)
- No Implicit Trust: Validate provider names from user input

Per API Error Handling Architecture:
- Uses domain errors from gateway.errors
- Errors propagate to exception handler middleware
"""

import re
from dataclasses import dataclass, field
from typing import Optional, List, AsyncIterator

from gateway.config import SAFE_IDENTIFIER_PATTERN
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    DispatchError,
    NoProviderError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    AllProvidersUnavailableError,
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

    Dispatch logic:
    1. Parse provider from model string (e.g., "ollama/llama3.2" → "ollama")
    2. Or use preferred_provider from request
    3. Or use default provider from config
    4. Check health, attempt request
    5. On failure, try fallback providers if allowed
    """

    # Pattern to parse "provider/model" format
    MODEL_PROVIDER_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)/(.+)$")

    def __init__(self, registry: ProviderRegistry):
        """Initialize dispatcher with provider registry.

        Args:
            registry: Initialized provider registry with health tracking
        """
        self._registry = registry

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

    async def dispatch_stream(
        self, request: InternalRequest
    ) -> tuple[str, AsyncIterator[StreamChunk]]:
        """Dispatch a streaming request to the appropriate provider.

        Args:
            request: Normalized internal request with stream=True

        Returns:
            Tuple of (provider_name, stream_iterator)

        Raises:
            DispatchError: If dispatch fails
        """
        provider_name, model_name = self.resolve_provider(request)

        # Update request with resolved model
        if model_name and model_name != request.model:
            request = request.model_copy(update={"model": model_name})

        # For streaming, we don't support fallback mid-stream
        # Just try primary provider
        adapter = self._registry.get(provider_name)
        if adapter is None:
            raise ProviderNotFoundError(provider=provider_name)

        if not self._registry.is_healthy(provider_name):
            # Try fallback for initial connection
            if request.fallback_allowed:
                for fallback_name in self._registry.get_fallback_chain(exclude=provider_name):
                    if self._registry.is_healthy(fallback_name):
                        adapter = self._registry.get(fallback_name)
                        provider_name = fallback_name
                        break
                else:
                    raise ProviderUnavailableError(provider=provider_name, fallback_disabled=False)
            else:
                raise ProviderUnavailableError(provider=provider_name, fallback_disabled=True)

        stream = adapter.chat_stream(request)
        return provider_name, stream

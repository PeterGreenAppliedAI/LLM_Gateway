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
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field

from gateway.config import (
    SAFE_IDENTIFIER_PATTERN,
    EnvironmentConfig,
    ResolutionConfig,
)
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    AllProvidersUnavailableError,
    AmbiguousModelError,
    EndpointNotFoundError,
    ErrorCode,
    GatewayError,
    NoProviderError,
    PolicyError,
    ProviderError,
    ProviderUnavailableError,
)
from gateway.models.common import HealthStatus, TaskType
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk
from gateway.observability import get_logger
from gateway.providers import ProviderAdapter

logger = get_logger(__name__)


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
    attempted_providers: list[str] = field(default_factory=list)

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

    @classmethod
    def parse_provider_from_model(
        cls, model: str | None, endpoints: Collection[str]
    ) -> tuple[str | None, str | None]:
        """Split an "endpoint/model" pin, if the prefix names a configured endpoint.

        - "gpu-node/llama3.1:8b" with endpoint gpu-node → ("gpu-node", "llama3.1:8b")
        - "meta-llama/Llama-3.1-8B-Instruct" → (None, the whole string)
        - "model" → (None, "model"); None → (None, None)

        Only a configured endpoint name makes a pin: Hugging Face model IDs
        ("Qwen/Qwen2.5-7B", "Systran/faster-whisper-small") and Ollama
        namespaced models ("user/model") contain a slash too, and used to be
        misread as pins to an endpoint that doesn't exist.
        """
        if not model:
            return None, None

        match = cls.MODEL_PROVIDER_PATTERN.match(model)
        if match:
            provider_hint, model_name = match.group(1), match.group(2)
            # SAFE_IDENTIFIER check keeps user input out of logs/metric labels
            if SAFE_IDENTIFIER_PATTERN.match(provider_hint) and provider_hint in endpoints:
                return provider_hint, model_name
        return None, model

    def split_pin(self, model: str | None) -> tuple[str | None, str | None]:
        return self.parse_provider_from_model(model, self._registry.list_providers())

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
        provider_hint, model_name = self.split_pin(request.model)

        # Use explicit hint if found
        if provider_hint:
            return provider_hint, model_name or request.model

        # Try preferred_provider from request
        if request.preferred_provider:
            return request.preferred_provider, request.model

        # Catalog-aware: route to an endpoint that actually has the model,
        # honoring endpoint_priority when several do. Without this, every
        # request lands on the default endpoint and 404s for models that
        # only exist elsewhere.
        if request.model:
            candidates = self._permitted(
                request, self._registry.get_endpoints_with_model(request.model)
            )
            if candidates:
                return self.resolve_endpoint(request, available_endpoints=candidates)

        # Fall back to default (catalog empty or model not yet discovered),
        # or the first permitted endpoint when the default is off-limits
        default = self._registry.get_default_provider()
        if default and not self._permitted(request, [default]):
            permitted = self._permitted(request, self._registry.list_providers())
            if permitted:
                default = permitted[0]
        if default:
            return default, request.model

        raise NoProviderError()

    @staticmethod
    def _permitted(request: InternalRequest, endpoints: list[str]) -> list[str]:
        """Endpoints the request may be served by, order preserved."""
        if request.allowed_endpoints is None:
            return list(endpoints)
        return [name for name in endpoints if name in request.allowed_endpoints]

    def _require_permitted(self, request: InternalRequest, endpoint: str) -> None:
        """Refuse an endpoint outside the request's allowed set."""
        if not self._permitted(request, [endpoint]):
            raise PolicyError(
                message=f"Endpoint '{endpoint}' is not allowed for this API key or environment",
                code=ErrorCode.ENDPOINT_NOT_ALLOWED,
            )

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
        endpoint_hint, model_name = self.split_pin(request.model)

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
            raise NoProviderError(message="No endpoints available for this environment")

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
                    labels = getattr(endpoint_config, "labels", {})
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
        # An explicit endpoint/model prefix is a pin: the request must go
        # there and ONLY there. Falling back elsewhere violates the pin and
        # masks the pinned endpoint's real error behind an unrelated one
        # (e.g. a 500 "model failed to load" hidden by a fallback's 404).
        pinned = self.split_pin(request.model)[0] is not None

        provider_name, model_name = self.resolve_provider(request)
        self._require_permitted(request, provider_name)
        attempted: list[str] = []

        # Update request with resolved model (strip provider prefix)
        if model_name and model_name != request.model:
            request = request.model_copy(update={"model": model_name})

        # Collect per-provider failure reasons so an eventual 503 can say
        # WHY (the generic message hides upstream 500 bodies and timeouts)
        errors_seen: list[str] = []

        # Try primary provider. When pinned, error responses raise with the
        # provider's actual error instead of silently returning None.
        result = await self._try_provider(
            provider_name, request, raise_on_error=pinned, error_sink=errors_seen
        )
        attempted.append(provider_name)

        if result is not None:
            return DispatchResult(
                response=result,
                provider_used=provider_name,
                was_fallback=False,
                attempted_providers=attempted,
            )

        # Primary failed - try fallbacks if allowed
        if pinned or not request.fallback_allowed:
            raise ProviderUnavailableError(provider=provider_name, fallback_disabled=True)

        # Get fallback chain - limited to prevent unbounded attempts
        fallback_chain = self._permitted(
            request, self._registry.get_fallback_chain(exclude=provider_name)
        )
        # Only fall back to endpoints that actually have the model —
        # anything else converts the real failure into a confusing 404
        if request.model:
            with_model = set(self._registry.get_endpoints_with_model(request.model))
            if with_model:
                fallback_chain = [name for name in fallback_chain if name in with_model]
        # Security: Cap fallback attempts to prevent unbounded attempts
        max_fallbacks = MAX_FALLBACK_ATTEMPTS - 1  # -1 for primary already tried

        for fallback_name in fallback_chain[:max_fallbacks]:
            result = await self._try_provider(fallback_name, request, error_sink=errors_seen)
            attempted.append(fallback_name)

            if result is not None:
                return DispatchResult(
                    response=result,
                    provider_used=fallback_name,
                    was_fallback=True,
                    attempted_providers=attempted,
                )

        # All providers failed
        raise AllProvidersUnavailableError(
            attempted=attempted,
            last_error=errors_seen[-1] if errors_seen else None,
        )

    async def _try_provider(
        self,
        provider_name: str,
        request: InternalRequest,
        raise_on_error: bool = False,
        error_sink: list[str] | None = None,
    ) -> InternalResponse | None:
        """Attempt to dispatch request to a specific provider.

        Args:
            provider_name: Name of provider to try
            request: The request to dispatch
            raise_on_error: Raise the provider's actual error for ANY error
                response instead of returning None on retryable ones. Used
                for pinned requests, where fallback is not an option and
                the caller must see the real failure.

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
        except GatewayError:
            raise
        except Exception as e:
            logger.warning(
                "Provider dispatch failed",
                provider=provider_name,
                model=request.model,
                error=str(e),
                error_type=type(e).__name__,
            )
            if error_sink is not None:
                error_sink.append(f"{provider_name}: {e}")
            return None

        if response.is_error:
            # Retryable failures (timeouts, connection errors, upstream 5xx)
            # fall through to the next provider. Upstream 4xx means the
            # request itself is wrong (bad model, invalid params) — retrying
            # elsewhere just masks the real error, so propagate it.
            if self._is_retryable_error(response.error_code) and not raise_on_error:
                logger.warning(
                    "Provider returned retryable error",
                    provider=provider_name,
                    model=request.model,
                    error=response.error,
                    error_code=response.error_code,
                )
                if error_sink is not None:
                    error_sink.append(f"{provider_name}: {response.error or 'unknown error'}")
                return None
            raise ProviderError(
                message=response.error or "Provider error",
                provider=provider_name,
                details={"error_code": response.error_code, "model": request.model},
                http_status=self._upstream_client_status(response.error_code),
            )

        return response

    @staticmethod
    def _upstream_client_status(error_code: str | None) -> int | None:
        """Upstream 4xx status to pass through to the client.

        A permanent client-class error (bad model, unsupported tools)
        must not surface as 502 — that reads as "transient, retry me"
        and sends retrying clients hammering a request that can never
        succeed. Server-class errors stay 502.
        """
        if error_code and error_code.startswith("http_4"):
            try:
                return int(error_code.removeprefix("http_"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _is_retryable_error(error_code: str | None) -> bool:
        """Whether an adapter error response justifies trying another provider."""
        if error_code is None:
            return True
        if error_code.startswith("http_"):
            # http_<status>: only server-side errors are retryable
            status = error_code.removeprefix("http_")
            return status.startswith("5")
        # upstream_error: the runtime reported a failure in-band (Ollama
        # error line, OpenAI error frame) — server-side, another box may work
        return error_code in {
            "timeout",
            "connection_error",
            "unknown_error",
            "empty_response",
            "upstream_error",
        }

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
        self,
        primary: str,
        model_name: str | None,
        request: InternalRequest,
        pinned: bool = False,
    ) -> list[str]:
        """Build ordered list of providers to try for streaming.

        Same order as non-streaming dispatch: the resolved primary first
        (it already reflects pins, target_endpoint, per-model defaults and
        priority), then other endpoints that have the model (healthy before
        unhealthy), then the general fallback chain. A pinned request, or
        one with fallback disabled, tries only the primary.
        """
        if pinned or not request.fallback_allowed:
            return [primary]

        providers: list[str] = [primary]
        seen: set[str] = {primary}

        if model_name:
            catalog_endpoints = self._registry.get_endpoints_with_model(model_name)
            for ep in catalog_endpoints:
                if ep not in seen and self._registry.is_healthy(ep):
                    providers.append(ep)
                    seen.add(ep)
            # Unhealthy catalog endpoints last among those with the model
            for ep in catalog_endpoints:
                if ep not in seen:
                    providers.append(ep)
                    seen.add(ep)

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

        pinned = self.split_pin(request.model)[0] is not None

        provider_name, model_name = self.resolve_provider(request)
        self._require_permitted(request, provider_name)

        # Update request with resolved model
        if model_name and model_name != request.model:
            request = request.model_copy(update={"model": model_name})

        providers_to_try = self._permitted(
            request,
            self._get_stream_provider_order(
                provider_name,
                model_name,
                request,
                pinned=pinned,
            ),
        )

        if not providers_to_try:
            raise NoProviderError()

        attempted: list[str] = []
        errors_seen: list[str] = []

        for try_name in providers_to_try[:MAX_FALLBACK_ATTEMPTS]:
            adapter = self._registry.get(try_name)
            if adapter is None:
                continue

            attempted.append(try_name)

            # Start the stream and peek at the first chunk to detect errors
            # before the caller commits to a 200 response
            # Completion/generate tasks stream from the raw-prompt endpoint
            # (no chat template), matching their non-streaming dispatch
            if request.task in (TaskType.COMPLETION, TaskType.GENERATE):
                stream_iter = adapter.generate_stream(request)
            else:
                stream_iter = adapter.chat_stream(request)
            try:
                first_chunk = await stream_iter.__anext__()
            except StopAsyncIteration:
                errors_seen.append(f"{try_name}: empty stream")
                continue
            except Exception as e:
                await _close_quietly(stream_iter)
                errors_seen.append(f"{try_name}: {type(e).__name__}: {e}")
                continue

            # An error before any content means this endpoint can't serve
            # the request (thinking-only and tool-call chunks are content)
            if (
                first_chunk.finish_reason == FinishReason.ERROR
                and not first_chunk.delta
                and not first_chunk.thinking
                and not first_chunk.tool_calls
            ):
                await _close_quietly(stream_iter)
                message = first_chunk.error or "stream failed"
                # Same rule as non-streaming: upstream 4xx means the request
                # itself is wrong, so trying elsewhere only hides the cause.
                # A pinned request never tries elsewhere.
                if pinned or not self._is_retryable_error(first_chunk.error_code):
                    raise ProviderError(
                        message=message,
                        provider=try_name,
                        details={"error_code": first_chunk.error_code, "model": request.model},
                        http_status=self._upstream_client_status(first_chunk.error_code),
                    )
                logger.warning(
                    "Provider stream failed before first chunk",
                    provider=try_name,
                    model=request.model,
                    error=message,
                    error_code=first_chunk.error_code,
                )
                errors_seen.append(f"{try_name}: {message}")
                continue

            return try_name, _chain(first_chunk, stream_iter)

        raise AllProvidersUnavailableError(
            attempted=attempted,
            last_error=errors_seen[-1] if errors_seen else None,
        )


async def _close_quietly(stream: AsyncIterator[StreamChunk]) -> None:
    """Close an upstream stream we're abandoning, so its connection is released now."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("Error closing abandoned stream", exc_info=True)


async def _chain(
    first: StreamChunk, rest: AsyncIterator[StreamChunk]
) -> AsyncIterator[StreamChunk]:
    """The peeked first chunk followed by the rest of the stream.

    Closing this generator closes the upstream stream too; `async for`
    alone would leave it open until garbage collection.
    """
    try:
        yield first
        async for chunk in rest:
            yield chunk
    finally:
        await _close_quietly(rest)

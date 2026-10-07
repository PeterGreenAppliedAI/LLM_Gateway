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

import asyncio
import fnmatch
import math
import re
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field

from gateway.config import (
    SAFE_IDENTIFIER_PATTERN,
    EnvironmentConfig,
    ResolutionConfig,
)
from gateway.dispatch.admission import Lease
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    AllProvidersUnavailableError,
    AmbiguousModelError,
    CapacityExceededError,
    EndpointNotFoundError,
    ErrorCode,
    GatewayError,
    NoProviderError,
    PolicyError,
    ProviderError,
    ProviderUnavailableError,
)
from gateway.models.common import FinishReason, TaskType
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk
from gateway.observability import get_logger
from gateway.observability.metrics import get_metrics
from gateway.providers import ProviderAdapter

logger = get_logger(__name__)


def order_candidates(registry: ProviderRegistry, strategy: str, candidates: list[str]) -> list[str]:
    """priority: as resolved. least_loaded: by in-flight share of max_concurrent.

    The sort is stable, so equally loaded endpoints keep priority order.
    An endpoint without max_concurrent counts its raw in-flight number.
    """
    if strategy != "least_loaded":
        return candidates
    admission = registry.admission

    def load(name: str) -> float:
        capacity = admission.capacity(name)
        in_flight = admission.in_flight(name)
        return in_flight / capacity if capacity else float(in_flight)

    return sorted(candidates, key=load)


async def admit(registry: ProviderRegistry, candidates: list[str], deadline: float) -> Lease:
    """A slot on the first candidate with room; when all are full, the first to free up.

    Shared by text and media dispatch.

    Raises:
        CapacityExceededError: Every candidate stayed full until the deadline.
    """
    admission = registry.admission
    loop = asyncio.get_running_loop()
    started = loop.time()
    lease = await admission.acquire_any(candidates, max(0.0, deadline - started))
    waited = loop.time() - started
    metrics = get_metrics()
    if lease is None:
        metrics.record_admission_rejected()
        # It stayed full for the whole wait; suggest waiting about as long
        retry_after = min(30, max(1, math.ceil(registry.max_queue_wait_seconds)))
        raise CapacityExceededError(
            endpoints=candidates, waited_seconds=waited, retry_after=retry_after
        )
    if waited > 0.001:
        metrics.observe_admission_wait(waited)
    return lease


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

        # Candidates in order: the resolved endpoint, then (unless pinned or
        # fallback is off) the fallback chain. A pin must go there and ONLY
        # there; falling back elsewhere would mask its real error.
        candidates = [provider_name]
        if not pinned and request.fallback_allowed:
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
            candidates += fallback_chain[: MAX_FALLBACK_ATTEMPTS - 1]
        candidates = self._order_candidates(candidates, pinned)
        remaining = list(candidates)
        deadline = self._admission_deadline()

        while remaining:
            # Admission (D-032): the first candidate with a free slot; when
            # all are full, wait for whichever frees first
            lease = await self._admit(remaining, deadline)
            name = lease.endpoint
            remaining.remove(name)
            try:
                # When pinned, error responses raise with the provider's
                # actual error instead of silently returning None
                result = await self._try_provider(
                    name, request, raise_on_error=pinned, error_sink=errors_seen
                )
            finally:
                lease.release()
            attempted.append(name)

            if result is not None:
                return DispatchResult(
                    response=result,
                    provider_used=name,
                    was_fallback=name != provider_name,
                    attempted_providers=attempted,
                )

        if pinned or not request.fallback_allowed:
            raise ProviderUnavailableError(provider=provider_name, fallback_disabled=True)

        # All providers failed
        raise AllProvidersUnavailableError(
            attempted=attempted,
            last_error=errors_seen[-1] if errors_seen else None,
        )

    # ------------------------------------------------------------------
    # Admission control (dispatch/admission.py, D-032)
    # ------------------------------------------------------------------

    def _admission_deadline(self) -> float:
        return asyncio.get_running_loop().time() + self._registry.max_queue_wait_seconds

    def _order_candidates(self, candidates: list[str], pinned: bool) -> list[str]:
        if pinned:
            return candidates
        return order_candidates(self._registry, self._resolution_config.strategy, candidates)

    async def _admit(self, candidates: list[str], deadline: float) -> Lease:
        """A slot on the first candidate with room, waiting until the deadline if none has.

        Raises:
            CapacityExceededError: Every candidate stayed full until the deadline.
        """
        return await admit(self._registry, candidates, deadline)

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

        # Circuit breaker: an endpoint failing repeatedly is skipped instantly
        # instead of probed inside the request (see dispatch/circuit.py)
        if not self._registry.allow_request(provider_name):
            if error_sink is not None:
                error_sink.append(f"{provider_name}: circuit open (recent failures)")
            return None

        # Dispatch based on task type
        try:
            response = await self._execute_request(adapter, request)
        except GatewayError:
            self._registry.release_probe(provider_name)
            raise
        except Exception as e:
            self._registry.record_failure(provider_name)
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
        except BaseException:
            self._registry.release_probe(provider_name)  # cancelled: no verdict
            raise

        if response.is_error:
            # Retryable failures (timeouts, connection errors, upstream 5xx)
            # fall through to the next provider. Upstream 4xx means the
            # request itself is wrong (bad model, invalid params) — retrying
            # elsewhere just masks the real error, so propagate it.
            self._record_outcome(provider_name, response.error_code)
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

        self._registry.record_success(provider_name)
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
            "pool_timeout",
        }

    def _record_outcome(self, name: str, error_code: str | None) -> None:
        """Feed an error response to the endpoint's circuit breaker.

        Retryable errors count as failures; upstream 4xx means the engine is
        alive (the request was wrong). A full gateway connection pool says
        nothing about the engine, so it gives no verdict.
        """
        if error_code == "pool_timeout":
            self._registry.release_probe(name)
        elif self._is_retryable_error(error_code):
            self._registry.record_failure(name)
        else:
            self._registry.record_success(name)

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

        remaining = [
            name
            for name in self._order_candidates(providers_to_try[:MAX_FALLBACK_ATTEMPTS], pinned)
            if self._registry.get(name) is not None
        ]
        deadline = self._admission_deadline()

        while remaining:
            # Admission (D-032); the slot is held until the stream closes
            lease = await self._admit(remaining, deadline)
            try_name = lease.endpoint
            remaining.remove(try_name)
            try:
                opened = await self._open_stream(try_name, request, pinned, attempted, errors_seen)
            except BaseException:
                lease.release()
                raise
            if opened is None:
                lease.release()
                continue
            first_chunk, stream_iter = opened
            return try_name, _chain(first_chunk, stream_iter, lease)

        raise AllProvidersUnavailableError(
            attempted=attempted,
            last_error=errors_seen[-1] if errors_seen else None,
        )

    async def _open_stream(
        self,
        try_name: str,
        request: InternalRequest,
        pinned: bool,
        attempted: list[str],
        errors_seen: list[str],
    ) -> tuple[StreamChunk, AsyncIterator[StreamChunk]] | None:
        """Start a stream on one endpoint and peek at its first chunk.

        Returns None when the endpoint can't serve it and the next one
        should be tried; raises when trying elsewhere would hide the cause.
        """

        adapter = self._registry.get(try_name)
        if adapter is None:
            return None
        if not self._registry.allow_request(try_name):
            errors_seen.append(f"{try_name}: circuit open (recent failures)")
            return None
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
            self._registry.record_failure(try_name)
            errors_seen.append(f"{try_name}: empty stream")
            return None
        except Exception as e:
            self._registry.record_failure(try_name)
            await _close_quietly(stream_iter)
            errors_seen.append(f"{try_name}: {type(e).__name__}: {e}")
            return None
        except BaseException:
            self._registry.release_probe(try_name)  # cancelled: no verdict
            await _close_quietly(stream_iter)
            raise

        # An error before any content means this endpoint can't serve
        # the request (thinking-only and tool-call chunks are content)
        if (
            first_chunk.finish_reason == FinishReason.ERROR
            and not first_chunk.delta
            and not first_chunk.thinking
            and not first_chunk.tool_calls
        ):
            await _close_quietly(stream_iter)
            self._record_outcome(try_name, first_chunk.error_code)
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
            return None

        self._registry.record_success(try_name)
        return first_chunk, stream_iter


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
    first: StreamChunk, rest: AsyncIterator[StreamChunk], lease: Lease | None = None
) -> AsyncIterator[StreamChunk]:
    """The peeked first chunk followed by the rest of the stream.

    Closing this generator closes the upstream stream too; `async for`
    alone would leave it open until garbage collection. The endpoint's
    admission slot is released when the stream ends either way.
    """
    try:
        yield first
        async for chunk in rest:
            yield chunk
    finally:
        try:
            await _close_quietly(rest)
        finally:
            if lease is not None:
                lease.release()

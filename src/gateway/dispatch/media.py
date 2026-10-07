"""Route OpenAI-shaped media requests (/v1/audio/*) to capable endpoints.

Media bodies are binary (audio out, multipart audio in) and don't fit the
InternalRequest/InternalResponse text model, so media gets its own small
dispatcher with the same rules as text (D-020):

- only endpoints that declare the capability (e.g. "tts") are candidates
- the request's access scope (key + environment, D-003) filters them
- pins (endpoint/model, a key's target_endpoint) are honored
- the upstream status is known before anything is sent to the client:
  upstream 4xx returns to the client, retryable failures try the next
  endpoint (D-012/D-013)
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from gateway.config import MediaCapability, ResolutionConfig
from gateway.dispatch.dispatcher import MAX_FALLBACK_ATTEMPTS, Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import (
    AllProvidersUnavailableError,
    ErrorCode,
    NoProviderError,
    PolicyError,
    ProviderError,
    ValidationError,
)
from gateway.models.internal import InternalRequest
from gateway.observability import get_logger
from gateway.providers.streaming import upstream_http_error

logger = get_logger(__name__)

# Builds the upstream request on the endpoint's client, for the model name
# without any endpoint/ prefix. Called once per attempt (bodies such as an
# uploaded file must be rewound by the builder).
RequestBuilder = Callable[[httpx.AsyncClient, str], httpx.Request]


@dataclass
class UpstreamMedia:
    """An upstream response whose status is known to be < 400, body unread."""

    endpoint: str
    model: str
    response: httpx.Response


class MediaDispatcher:
    def __init__(self, registry: ProviderRegistry, resolution: ResolutionConfig | None = None):
        self._registry = registry
        self._resolution = resolution or ResolutionConfig()

    def candidates(self, capability: MediaCapability, request: InternalRequest) -> list[str]:
        """Endpoints to try, in order.

        Raises:
            ValidationError: The pinned endpoint doesn't serve this capability.
            PolicyError: Capable endpoints exist but the key/environment allows none.
            NoProviderError: No endpoint declares the capability.
        """
        pinned, _ = Dispatcher.parse_provider_from_model(request.model)
        capable = [
            name
            for name in self._registry.list_providers()
            if capability in getattr(self._registry.get_endpoint_config(name), "capabilities", [])
        ]

        if pinned:
            if pinned not in capable:
                raise ValidationError(message=f"Endpoint '{pinned}' does not serve {capability}")
            capable = [pinned]
        if not capable:
            raise NoProviderError(message=f"No endpoint is configured for {capability}")

        allowed = request.allowed_endpoints
        permitted = capable if allowed is None else [n for n in capable if n in allowed]
        if not permitted:
            raise PolicyError(
                message=f"No {capability} endpoint is allowed for this API key or environment",
                code=ErrorCode.ENDPOINT_NOT_ALLOWED,
            )
        if pinned:
            return permitted

        model = Dispatcher.parse_provider_from_model(request.model)[1] or ""
        with_model = set(self._registry.get_endpoints_with_model(model)) if model else set()
        priority = {name: i for i, name in enumerate(self._resolution.endpoint_priority)}

        def rank(name: str) -> tuple:
            return (
                name != request.preferred_provider,  # key's target_endpoint first
                not self._registry.is_healthy(name),  # healthy before unhealthy
                name not in with_model,  # endpoints known to have the model
                priority.get(name, len(priority)),  # configured priority
            )

        ordered = sorted(permitted, key=rank)
        return ordered[:1] if not request.fallback_allowed else ordered

    async def send(
        self,
        capability: MediaCapability,
        request: InternalRequest,
        build: RequestBuilder,
    ) -> UpstreamMedia:
        """Open the upstream response on the first endpoint that accepts the request.

        Raises:
            ProviderError: Upstream 4xx (passed through, not retried elsewhere).
            AllProvidersUnavailableError: Every candidate failed retryably.
        """
        model = Dispatcher.parse_provider_from_model(request.model)[1] or request.model or ""
        attempted: list[str] = []
        errors: list[str] = []

        for name in self.candidates(capability, request)[:MAX_FALLBACK_ATTEMPTS]:
            adapter = self._registry.get(name)
            if adapter is None:
                continue
            attempted.append(name)
            try:
                client = await adapter.media_client()
                response = await client.send(build(client, model), stream=True)
            except (httpx.HTTPError, OSError) as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
                logger.warning("Media endpoint failed", endpoint=name, error=str(e))
                continue

            if response.status_code < 400:
                return UpstreamMedia(endpoint=name, model=model, response=response)

            code, message = await upstream_http_error(response)
            await response.aclose()
            status = response.status_code
            # 4xx means the request is wrong: trying elsewhere would hide it.
            # 429 (busy) is the exception: another endpoint may have room.
            if 400 <= status < 500 and status != 429:
                raise ProviderError(
                    message=message,
                    provider=name,
                    details={"error_code": code, "model": model},
                    http_status=status,
                )
            errors.append(f"{name}: {message}")
            logger.warning("Media endpoint returned retryable error", endpoint=name, status=status)

        raise AllProvidersUnavailableError(
            attempted=attempted, last_error=errors[-1] if errors else None
        )

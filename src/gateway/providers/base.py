"""Abstract base class for provider adapters.

Per PRD Section 12: Each adapter must implement:
- health()
- list_models()
- chat(request)
- generate(request) (optional)
- embeddings(request) (optional)

Adapters must declare:
- Capabilities
- Max context length
- Streaming support
- Known limitations

Per rule.md:
- Liskov Substitution: Any adapter can replace another without breaking consumers
- Interface Segregation: Minimal interface tailored to provider operations
- Dependency Inversion: Core depends on this abstract contract, not implementations
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from gateway.config import ProviderConfig
from gateway.models.common import (
    HealthStatus,
    ModelCapability,
    ModelInfo,
    ProviderInfo,
    ProviderType,
)
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk


class ProviderAdapter(ABC):
    """Abstract base class for all provider adapters.

    Implementations must be stateless and thread-safe.
    All methods are async for non-blocking I/O.

    Adapters are instantiated from validated ProviderConfig objects,
    ensuring all URLs and parameters have been validated at load time.
    """

    def __init__(self, config: ProviderConfig, provider_type: ProviderType):
        """Initialize the provider adapter from validated config.

        Args:
            config: Validated provider configuration (URL already validated)
            provider_type: Type of provider (ollama, vllm, etc.)
        """
        self.name = config.name
        self.provider_type = provider_type
        self.base_url = config.base_url  # Already validated by ProviderConfig
        self.timeout = config.timeout
        self.max_retries = config.max_retries

    # =========================================================================
    # Required Methods (per PRD Section 12)
    # =========================================================================

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Check provider health status.

        Returns:
            HealthStatus indicating if provider is healthy, degraded, or unhealthy
        """
        pass

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]:
        """List available models from this provider.

        Returns:
            List of ModelInfo with capabilities, context length, etc.
        """
        pass

    @abstractmethod
    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute a chat completion request.

        Args:
            request: Normalized internal request with messages

        Returns:
            InternalResponse with generated content
        """
        pass

    # =========================================================================
    # Optional Methods (per PRD Section 12)
    # =========================================================================

    async def generate(self, request: InternalRequest) -> InternalResponse:
        """Execute a text generation/completion request.

        Default implementation converts to chat format.
        Override for providers with native completion support.

        Args:
            request: Normalized internal request with prompt

        Returns:
            InternalResponse with generated content
        """
        # Default: convert prompt to chat message and use chat()
        from gateway.models.internal import Message, MessageRole

        if request.prompt and not request.messages:
            request = request.model_copy(
                update={
                    "messages": [Message(role=MessageRole.USER, content=request.prompt)]
                }
            )
        return await self.chat(request)

    async def embeddings(self, request: InternalRequest) -> InternalResponse:
        """Generate embeddings for input text.

        Default implementation raises NotImplementedError.
        Override for providers with embedding support.

        Args:
            request: Normalized internal request with input_data

        Returns:
            InternalResponse with embeddings list
        """
        raise NotImplementedError(f"{self.name} does not support embeddings")

    # =========================================================================
    # Streaming Support
    # =========================================================================

    async def chat_stream(
        self, request: InternalRequest
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion response.

        Default implementation yields single chunk from non-streaming chat().
        Override for providers with native streaming support.

        Args:
            request: Normalized internal request with messages

        Yields:
            StreamChunk with incremental content
        """
        # Default: non-streaming fallback
        response = await self.chat(request)
        yield StreamChunk(
            request_id=response.request_id,
            index=0,
            delta=response.get_output_text(),
            finish_reason=response.finish_reason,
            usage=response.usage,
        )

    # =========================================================================
    # Provider Info
    # =========================================================================

    async def get_info(self) -> ProviderInfo:
        """Get full provider information including health and models.

        Returns:
            ProviderInfo with current status and available models
        """
        health = await self.health()
        models = await self.list_models() if health == HealthStatus.HEALTHY else []

        return ProviderInfo(
            name=self.name,
            type=self.provider_type,
            base_url=self.base_url,
            health=health,
            capabilities=self._aggregate_capabilities(models),
            supports_streaming=self.supports_streaming,
            models=models,
            limitations=self.limitations,
        )

    def _aggregate_capabilities(self, models: list[ModelInfo]) -> list[ModelCapability]:
        """Aggregate capabilities from all models."""
        caps = set()
        for model in models:
            caps.update(model.capabilities)
        return list(caps)

    # =========================================================================
    # Provider Metadata (per PRD Section 12)
    # =========================================================================

    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming responses."""
        return False  # Override in subclasses

    @property
    def limitations(self) -> list[str]:
        """Known limitations of this provider."""
        return []  # Override in subclasses

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    async def close(self) -> None:
        """Close any resources held by the adapter.

        Override in subclasses that maintain persistent connections.
        Called during application shutdown.
        """
        pass  # Default: no-op

    # =========================================================================
    # Error Handling (DRY - shared across all adapters)
    # =========================================================================

    def _error_response(
        self, request: InternalRequest, error: str, error_code: str
    ) -> InternalResponse:
        """Create standardized error response.

        Per rule.md DRY principle: Centralized error response creation
        ensures consistent error handling across all adapters.
        """
        from gateway.models.common import FinishReason

        return InternalResponse(
            request_id=request.request_id,
            task=request.task,
            provider=self.name,
            model=request.model or "unknown",
            error=error,
            error_code=error_code,
            finish_reason=FinishReason.ERROR,
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, base_url={self.base_url!r})"

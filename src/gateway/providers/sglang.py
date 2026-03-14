"""SGLang provider adapter.

SGLang API reference: https://github.com/sgl-project/sglang

Per PRD Section 6: SGLang is a stubbed runtime for future implementation.
This adapter provides the interface but methods return NotImplementedError
until full support is added.
"""

from gateway.config import ProviderConfig
from gateway.models.common import (
    HealthStatus,
    ModelInfo,
    ProviderType,
)
from gateway.models.internal import InternalRequest, InternalResponse
from gateway.providers.base import ProviderAdapter


class SGLangAdapter(ProviderAdapter):
    """Adapter for SGLang inference runtime.

    SGLang provides fast inference with RadixAttention and optimized scheduling.
    Currently stubbed - full implementation planned for future release.
    """

    def __init__(self, config: ProviderConfig):
        """Initialize SGLang adapter from validated config.

        Args:
            config: Validated provider configuration with base_url, timeout, etc.
        """
        super().__init__(config=config, provider_type=ProviderType.SGLANG)

    # =========================================================================
    # Required Methods (Stubbed)
    # =========================================================================

    async def health(self) -> HealthStatus:
        """Check SGLang server health.

        Returns UNKNOWN since this adapter is stubbed.
        """
        return HealthStatus.UNKNOWN

    async def list_models(self) -> list[ModelInfo]:
        """List models available in SGLang.

        Returns empty list since this adapter is stubbed.
        """
        return []

    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute chat completion via SGLang.

        Raises:
            NotImplementedError: SGLang adapter is not yet implemented
        """
        raise NotImplementedError(
            "SGLang adapter is stubbed. Full implementation planned for future release."
        )

    # =========================================================================
    # Provider Metadata
    # =========================================================================

    @property
    def supports_streaming(self) -> bool:
        return False  # Will be True when implemented

    @property
    def limitations(self) -> list[str]:
        return [
            "STUBBED - Not yet implemented",
            "Requires compatible GPU",
            "RadixAttention for efficient KV cache sharing",
        ]

"""TensorRT-LLM provider adapter.

TRT-LLM API reference: https://github.com/NVIDIA/TensorRT-LLM

Per PRD Section 6: TRT-LLM is a stubbed runtime for future implementation.
This adapter provides the interface but methods return NotImplementedError
until full support is added.
"""

from typing import AsyncIterator

from gateway.config import ProviderConfig
from gateway.models.common import (
    HealthStatus,
    ModelInfo,
    ProviderType,
)
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk
from gateway.providers.base import ProviderAdapter


class TRTLLMAdapter(ProviderAdapter):
    """Adapter for TensorRT-LLM inference runtime.

    TRT-LLM provides optimized inference for NVIDIA GPUs.
    Currently stubbed - full implementation planned for future release.
    """

    def __init__(self, config: ProviderConfig):
        """Initialize TRT-LLM adapter from validated config.

        Args:
            config: Validated provider configuration with base_url, timeout, etc.
        """
        super().__init__(config=config, provider_type=ProviderType.TRTLLM)

    # =========================================================================
    # Required Methods (Stubbed)
    # =========================================================================

    async def health(self) -> HealthStatus:
        """Check TRT-LLM server health.

        Returns UNKNOWN since this adapter is stubbed.
        """
        return HealthStatus.UNKNOWN

    async def list_models(self) -> list[ModelInfo]:
        """List models available in TRT-LLM.

        Returns empty list since this adapter is stubbed.
        """
        return []

    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute chat completion via TRT-LLM.

        Raises:
            NotImplementedError: TRT-LLM adapter is not yet implemented
        """
        raise NotImplementedError(
            "TRT-LLM adapter is stubbed. Full implementation planned for future release."
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
            "Requires NVIDIA GPU with TensorRT",
            "Model must be converted to TRT-LLM format",
        ]

"""Provider adapter factory.

Creates provider adapters from validated configuration.
Per rule.md: Single Responsibility - this module only handles adapter instantiation.
"""

from gateway.config import ProviderConfig
from gateway.models.common import ProviderType
from gateway.providers.base import ProviderAdapter
from gateway.providers.ollama import OllamaAdapter
from gateway.providers.vllm import VLLMAdapter
from gateway.providers.trtllm import TRTLLMAdapter
from gateway.providers.sglang import SGLangAdapter
from gateway.providers.openai import OpenAIAdapter


# Registry mapping ProviderType enum to adapter classes
_ADAPTER_REGISTRY: dict[ProviderType, type[ProviderAdapter]] = {
    ProviderType.OLLAMA: OllamaAdapter,
    ProviderType.VLLM: VLLMAdapter,
    ProviderType.TRTLLM: TRTLLMAdapter,
    ProviderType.SGLANG: SGLangAdapter,
    ProviderType.OPENAI: OpenAIAdapter,
}


def create_adapter(config: ProviderConfig) -> ProviderAdapter:
    """Create a provider adapter from validated configuration.

    Args:
        config: Validated ProviderConfig with type (ProviderType enum), base_url, etc.

    Returns:
        Instantiated ProviderAdapter subclass

    Raises:
        ValueError: If provider type is not supported (shouldn't happen with enum)
    """
    adapter_class = _ADAPTER_REGISTRY.get(config.type)
    if adapter_class is None:
        # This should never happen if config.type is properly validated as enum
        supported = ", ".join(pt.value for pt in _ADAPTER_REGISTRY.keys())
        raise ValueError(
            f"Unknown provider type: {config.type}. Supported types: {supported}"
        )

    return adapter_class(config)


def get_supported_provider_types() -> list[str]:
    """Get list of supported provider type strings."""
    return sorted(pt.value for pt in _ADAPTER_REGISTRY.keys())


def register_adapter(provider_type: ProviderType, adapter_class: type[ProviderAdapter]) -> None:
    """Register a custom adapter class for a provider type.

    Allows extending the gateway with custom provider adapters.

    Args:
        provider_type: ProviderType enum value
        adapter_class: ProviderAdapter subclass to use for this type
    """
    _ADAPTER_REGISTRY[provider_type] = adapter_class

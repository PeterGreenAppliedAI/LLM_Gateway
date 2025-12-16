"""Provider adapters for inference runtimes.

Per PRD Section 12: Each adapter must implement health(), list_models(),
chat(), generate() (optional), and embeddings() (optional).

Providers are plugins, not core logic (PRD Section 5).
"""

from gateway.providers.base import ProviderAdapter
from gateway.providers.factory import create_adapter, get_supported_provider_types, register_adapter
from gateway.providers.ollama import OllamaAdapter
from gateway.providers.vllm import VLLMAdapter
from gateway.providers.trtllm import TRTLLMAdapter
from gateway.providers.sglang import SGLangAdapter

__all__ = [
    # Base class
    "ProviderAdapter",
    # Factory
    "create_adapter",
    "get_supported_provider_types",
    "register_adapter",
    # Adapters
    "OllamaAdapter",
    "VLLMAdapter",
    "TRTLLMAdapter",
    "SGLangAdapter",
]

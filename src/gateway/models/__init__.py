"""Gateway data models - internal format and external dialects."""

from gateway.models.common import (
    FinishReason,
    HealthStatus,
    ModelCapability,
    ModelInfo,
    ProviderInfo,
    ProviderType,
    TaskType,
    UsageStats,
)
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
)
from gateway.models.openai import (
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIChatStreamResponse,
    OpenAICompletionRequest,
    OpenAICompletionResponse,
    OpenAIEmbeddingRequest,
    OpenAIEmbeddingResponse,
)

__all__ = [
    # Common types
    "TaskType",
    "FinishReason",
    "UsageStats",
    "ModelCapability",
    "ProviderType",
    "HealthStatus",
    "ModelInfo",
    "ProviderInfo",
    # Internal format
    "InternalRequest",
    "InternalResponse",
    "Message",
    "MessageRole",
    "StreamChunk",
    # OpenAI dialect
    "OpenAIChatRequest",
    "OpenAIChatResponse",
    "OpenAIChatStreamResponse",
    "OpenAICompletionRequest",
    "OpenAICompletionResponse",
    "OpenAIEmbeddingRequest",
    "OpenAIEmbeddingResponse",
]

"""Common types shared across internal and external models."""

from enum import Enum

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """Supported task types for the gateway.

    These represent the semantic intent of a request, independent of
    the API format or provider used.
    """
    CHAT = "chat"
    COMPLETION = "completion"
    SUMMARIZE = "summarize"
    EXTRACT = "extract"
    CLASSIFY = "classify"
    EMBEDDINGS = "embeddings"
    GENERATE = "generate"  # Generic text generation


class FinishReason(str, Enum):
    """Reasons why generation stopped."""
    STOP = "stop"              # Natural stop or stop sequence hit
    LENGTH = "length"          # Max tokens reached
    CONTENT_FILTER = "content_filter"  # Content was filtered
    TOOL_CALLS = "tool_calls"  # Model wants to call tools
    ERROR = "error"            # Error during generation


class UsageStats(BaseModel):
    """Token usage statistics."""
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @classmethod
    def from_counts(cls, prompt: int, completion: int) -> "UsageStats":
        """Create usage stats from prompt and completion counts."""
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )


class ModelCapability(str, Enum):
    """Capabilities that a model/provider may support."""
    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDINGS = "embeddings"
    STREAMING = "streaming"
    FUNCTION_CALLING = "function_calling"
    VISION = "vision"
    JSON_MODE = "json_mode"


class ProviderType(str, Enum):
    """Supported provider runtime types."""
    OLLAMA = "ollama"
    VLLM = "vllm"
    TRTLLM = "trtllm"
    SGLANG = "sglang"


class HealthStatus(str, Enum):
    """Provider health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Partially working (e.g., some models unavailable)
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ModelInfo(BaseModel):
    """Information about a model available from a provider.

    Per PRD Section 12: Adapters must declare capabilities,
    max context length, streaming support, and known limitations.
    """
    name: str  # Model identifier (e.g., "llama3.2", "mistral")
    provider: str  # Provider name this model is from

    # Capabilities (per PRD)
    capabilities: list[ModelCapability] = Field(default_factory=list)
    max_context_length: int = Field(default=4096, gt=0)
    supports_streaming: bool = True

    # Additional metadata
    description: str | None = None
    size_bytes: int | None = None  # Model size if known
    quantization: str | None = None  # e.g., "Q4_0", "FP16"

    # Known limitations (per PRD)
    limitations: list[str] = Field(default_factory=list)

    def supports(self, capability: ModelCapability) -> bool:
        """Check if model supports a capability."""
        return capability in self.capabilities

    def supports_task(self, task: "TaskType") -> bool:
        """Check if model supports a task type."""
        task_to_capability = {
            TaskType.CHAT: ModelCapability.CHAT,
            TaskType.COMPLETION: ModelCapability.COMPLETION,
            TaskType.GENERATE: ModelCapability.COMPLETION,
            TaskType.EMBEDDINGS: ModelCapability.EMBEDDINGS,
            # These tasks use chat/completion capability
            TaskType.SUMMARIZE: ModelCapability.CHAT,
            TaskType.EXTRACT: ModelCapability.CHAT,
            TaskType.CLASSIFY: ModelCapability.CHAT,
        }
        required = task_to_capability.get(task)
        if required is None:
            return False
        return self.supports(required)


class ProviderInfo(BaseModel):
    """Information about a provider runtime.

    Per PRD Section 12: Adapters must declare capabilities,
    max context length, streaming support, and known limitations.
    """
    name: str  # Provider identifier (e.g., "ollama", "vllm-local")
    type: ProviderType
    base_url: str

    # Status
    health: HealthStatus = HealthStatus.UNKNOWN
    health_message: str | None = None

    # Capabilities (aggregate of all models, or provider-level)
    capabilities: list[ModelCapability] = Field(default_factory=list)
    supports_streaming: bool = True

    # Available models
    models: list[ModelInfo] = Field(default_factory=list)

    # Known limitations (per PRD)
    limitations: list[str] = Field(default_factory=list)

    def get_model(self, name: str) -> ModelInfo | None:
        """Get model info by name."""
        for model in self.models:
            if model.name == name:
                return model
        return None

    def get_models_for_task(self, task: TaskType) -> list[ModelInfo]:
        """Get all models that support a given task."""
        return [m for m in self.models if m.supports_task(task)]

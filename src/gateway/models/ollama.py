"""Ollama API request/response models.

These models provide compatibility with the native Ollama API format,
allowing clients that use the Ollama SDK to work with the gateway.
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


# =============================================================================
# Ollama Request Models
# =============================================================================


class OllamaToolCallFunction(BaseModel):
    """Function details within an Ollama tool call."""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class OllamaToolCall(BaseModel):
    """A tool call in Ollama format."""
    function: OllamaToolCallFunction


class OllamaMessage(BaseModel):
    """A single message in Ollama chat format."""
    role: str
    content: str = ""
    images: list[str] | None = None
    tool_calls: list[OllamaToolCall] | None = None
    thinking: str | None = None  # Reasoning model thinking tokens


class OllamaChatRequest(BaseModel):
    """Ollama /api/chat request format."""
    model: str
    messages: list[OllamaMessage]
    stream: bool = True
    format: str | None = None
    options: dict[str, Any] | None = None
    keep_alive: str | None = None
    tools: list[dict[str, Any]] | None = None


class OllamaGenerateRequest(BaseModel):
    """Ollama /api/generate request format."""
    model: str
    prompt: str
    stream: bool = True
    format: str | None = None
    options: dict[str, Any] | None = None
    system: str | None = None
    template: str | None = None
    context: list[int] | None = None
    keep_alive: str | None = None


class OllamaEmbeddingsRequest(BaseModel):
    """Ollama /api/embeddings request format."""
    model: str
    prompt: str | list[str]
    options: dict[str, Any] | None = None
    keep_alive: str | None = None


# =============================================================================
# Ollama Response Models
# =============================================================================


class OllamaChatResponse(BaseModel):
    """Ollama /api/chat response format (non-streaming)."""
    model: str
    created_at: str
    message: OllamaMessage
    done: bool = True
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None


class OllamaChatStreamChunk(BaseModel):
    """Ollama /api/chat streaming chunk."""
    model: str
    created_at: str
    message: OllamaMessage
    done: bool = False


class OllamaGenerateResponse(BaseModel):
    """Ollama /api/generate response format (non-streaming)."""
    model: str
    created_at: str
    response: str
    done: bool = True
    context: list[int] | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None


class OllamaEmbeddingsResponse(BaseModel):
    """Ollama /api/embeddings response format."""
    embedding: list[float] | list[list[float]]


class OllamaModelInfo(BaseModel):
    """Model info for /api/tags response."""
    name: str
    model: str
    modified_at: str
    size: int
    digest: str
    details: dict[str, Any] = Field(default_factory=dict)


class OllamaTagsResponse(BaseModel):
    """Ollama /api/tags response format."""
    models: list[OllamaModelInfo]

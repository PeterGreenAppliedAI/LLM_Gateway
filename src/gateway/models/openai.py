"""OpenAI-compatible API models.

These models match the OpenAI API specification for compatibility with
existing tooling and clients. They convert to/from the internal format.

References:
- https://platform.openai.com/docs/api-reference/chat
- https://platform.openai.com/docs/api-reference/completions
- https://platform.openai.com/docs/api-reference/embeddings
"""

import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from gateway.models.common import FinishReason, TaskType, UsageStats
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
)


# =============================================================================
# Chat Completion Models (POST /v1/chat/completions)
# =============================================================================


class OpenAIChatMessage(BaseModel):
    """OpenAI chat message format."""
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None


class OpenAIChatRequest(BaseModel):
    """OpenAI-compatible chat completion request."""
    model: str
    messages: list[OpenAIChatMessage]
    max_tokens: int | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    stream: bool = False
    user: str | None = None
    # Extended fields
    response_format: dict[str, Any] | None = None

    def to_internal(
        self,
        client_id: str = "default",
        task: TaskType = TaskType.CHAT,
    ) -> InternalRequest:
        """Convert to internal request format."""
        messages = [
            Message(
                role=MessageRole(msg.role),
                content=msg.content or "",
                name=msg.name,
            )
            for msg in self.messages
        ]

        stop_sequences = None
        if self.stop:
            stop_sequences = [self.stop] if isinstance(self.stop, str) else self.stop

        return InternalRequest(
            client_id=client_id,
            user_id=self.user or "anonymous",
            task=task,
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens or 1024,
            temperature=self.temperature if self.temperature is not None else 0.7,
            top_p=self.top_p if self.top_p is not None else 1.0,
            stop=stop_sequences,
            stream=self.stream,
            response_format=self.response_format,
        )


class OpenAIChatChoice(BaseModel):
    """A single choice in chat completion response."""
    index: int
    message: OpenAIChatMessage
    finish_reason: str | None = None


class OpenAIChatUsage(BaseModel):
    """Token usage in chat completion response."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAIChatResponse(BaseModel):
    """OpenAI-compatible chat completion response."""
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid4().hex[:24]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[OpenAIChatChoice]
    usage: OpenAIChatUsage

    @classmethod
    def from_internal(cls, response: InternalResponse) -> "OpenAIChatResponse":
        """Create from internal response format."""
        content = response.get_output_text()

        choice = OpenAIChatChoice(
            index=0,
            message=OpenAIChatMessage(role="assistant", content=content),
            finish_reason=_map_finish_reason(response.finish_reason),
        )

        return cls(
            id=f"chatcmpl-{response.request_id[:24]}",
            model=response.model,
            choices=[choice],
            usage=OpenAIChatUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            ),
        )


class OpenAIChatStreamDelta(BaseModel):
    """Delta content in streaming chat response."""
    role: str | None = None
    content: str | None = None


class OpenAIChatStreamChoice(BaseModel):
    """Streaming choice in chat completion."""
    index: int
    delta: OpenAIChatStreamDelta
    finish_reason: str | None = None


class OpenAIChatStreamResponse(BaseModel):
    """OpenAI-compatible streaming chat completion chunk."""
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[OpenAIChatStreamChoice]

    @classmethod
    def from_chunk(cls, chunk: StreamChunk, model: str) -> "OpenAIChatStreamResponse":
        """Create from internal stream chunk."""
        return cls(
            id=f"chatcmpl-{chunk.request_id[:24]}",
            model=model,
            choices=[
                OpenAIChatStreamChoice(
                    index=chunk.index,
                    delta=OpenAIChatStreamDelta(content=chunk.delta),
                    finish_reason=_map_finish_reason(chunk.finish_reason)
                    if chunk.finish_reason
                    else None,
                )
            ],
        )


# =============================================================================
# Completion Models (POST /v1/completions)
# =============================================================================


class OpenAICompletionRequest(BaseModel):
    """OpenAI-compatible completion request."""
    model: str
    prompt: str | list[str]
    max_tokens: int | None = Field(default=None, le=32768)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    stream: bool = False
    user: str | None = None
    echo: bool = False
    suffix: str | None = None

    def to_internal(
        self,
        client_id: str = "default",
        task: TaskType = TaskType.COMPLETION,
    ) -> InternalRequest:
        """Convert to internal request format."""
        # Handle single string or list of prompts
        prompt_text = self.prompt if isinstance(self.prompt, str) else self.prompt[0]

        stop_sequences = None
        if self.stop:
            stop_sequences = [self.stop] if isinstance(self.stop, str) else self.stop

        return InternalRequest(
            client_id=client_id,
            user_id=self.user or "anonymous",
            task=task,
            model=self.model,
            prompt=prompt_text,
            max_tokens=self.max_tokens or 1024,
            temperature=self.temperature if self.temperature is not None else 0.7,
            top_p=self.top_p if self.top_p is not None else 1.0,
            stop=stop_sequences,
            stream=self.stream,
        )


class OpenAICompletionChoice(BaseModel):
    """A single choice in completion response."""
    index: int
    text: str
    finish_reason: str | None = None


class OpenAICompletionResponse(BaseModel):
    """OpenAI-compatible completion response."""
    id: str = Field(default_factory=lambda: f"cmpl-{uuid4().hex[:24]}")
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[OpenAICompletionChoice]
    usage: OpenAIChatUsage

    @classmethod
    def from_internal(cls, response: InternalResponse) -> "OpenAICompletionResponse":
        """Create from internal response format."""
        choice = OpenAICompletionChoice(
            index=0,
            text=response.get_output_text(),
            finish_reason=_map_finish_reason(response.finish_reason),
        )

        return cls(
            id=f"cmpl-{response.request_id[:24]}",
            model=response.model,
            choices=[choice],
            usage=OpenAIChatUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            ),
        )


# =============================================================================
# Embedding Models (POST /v1/embeddings)
# =============================================================================


class OpenAIEmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request."""
    model: str
    input: str | list[str]
    user: str | None = None
    encoding_format: Literal["float", "base64"] = "float"

    def to_internal(self, client_id: str = "default") -> InternalRequest:
        """Convert to internal request format."""
        # Normalize input to list
        input_data = [self.input] if isinstance(self.input, str) else self.input

        return InternalRequest(
            client_id=client_id,
            user_id=self.user or "anonymous",
            task=TaskType.EMBEDDINGS,
            model=self.model,
            input_data=input_data,
            max_tokens=0,  # Not applicable for embeddings
        )


class OpenAIEmbeddingData(BaseModel):
    """Single embedding in response."""
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float]


class OpenAIEmbeddingResponse(BaseModel):
    """OpenAI-compatible embedding response."""
    object: Literal["list"] = "list"
    model: str
    data: list[OpenAIEmbeddingData]
    usage: OpenAIChatUsage

    @classmethod
    def from_internal(cls, response: InternalResponse) -> "OpenAIEmbeddingResponse":
        """Create from internal response format."""
        data = []
        if response.embeddings:
            for i, embedding in enumerate(response.embeddings):
                data.append(
                    OpenAIEmbeddingData(
                        index=i,
                        embedding=embedding,
                    )
                )

        return cls(
            model=response.model,
            data=data,
            usage=OpenAIChatUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=0,
                total_tokens=response.usage.prompt_tokens,
            ),
        )


# =============================================================================
# Helpers
# =============================================================================


def _map_finish_reason(reason: FinishReason | None) -> str | None:
    """Map internal finish reason to OpenAI format."""
    if reason is None:
        return None
    mapping = {
        FinishReason.STOP: "stop",
        FinishReason.LENGTH: "length",
        FinishReason.CONTENT_FILTER: "content_filter",
        FinishReason.TOOL_CALLS: "tool_calls",
        FinishReason.ERROR: "stop",  # OpenAI doesn't have error as finish reason
    }
    return mapping.get(reason, "stop")

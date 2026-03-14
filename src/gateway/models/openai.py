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

from pydantic import BaseModel, Field, model_serializer

from gateway.models.common import FinishReason, TaskType
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
    ToolCall,
)

# =============================================================================
# Chat Completion Models (POST /v1/chat/completions)
# =============================================================================


class OpenAIToolCallFunction(BaseModel):
    """Function details in an OpenAI tool call."""

    name: str
    arguments: str  # JSON string of arguments


class OpenAIToolCall(BaseModel):
    """A tool call in OpenAI format."""

    id: str
    type: Literal["function"] = "function"
    function: OpenAIToolCallFunction


class OpenAIChatMessage(BaseModel):
    """OpenAI chat message format."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None  # string, content parts array, or null
    name: str | None = None
    tool_calls: list[OpenAIToolCall] | None = None  # Assistant tool calls
    tool_call_id: str | None = None  # For tool role responses

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        """Match OpenAI response format: omit null optional fields except content."""
        d = handler(self)
        for key in ("name", "tool_calls", "tool_call_id"):
            if d.get(key) is None:
                del d[key]
        return d

    def content_as_str(self) -> str:
        """Normalize content to a plain string."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        # Content parts array: [{"type": "text", "text": "..."}, ...]
        parts = []
        for part in self.content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(parts)


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
    # Tool calling
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    # Extended fields
    response_format: dict[str, Any] | None = None

    def to_internal(
        self,
        client_id: str = "default",
        task: TaskType = TaskType.CHAT,
    ) -> InternalRequest:
        """Convert to internal request format."""
        import json as _json

        messages = []
        for msg in self.messages:
            msg_kwargs: dict[str, Any] = {
                "role": MessageRole(msg.role),
                "content": msg.content_as_str() or None,
                "name": msg.name,
            }
            # Preserve raw content parts for multimodal messages (images, etc.)
            if isinstance(msg.content, list):
                msg_kwargs["content_parts"] = msg.content
            if msg.tool_calls:
                msg_kwargs["tool_calls"] = [
                    ToolCall(
                        id=tc.id,
                        type="function",
                        function={
                            "name": tc.function.name,
                            "arguments": _json.loads(tc.function.arguments)
                            if isinstance(tc.function.arguments, str)
                            else tc.function.arguments,
                        },
                    )
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                msg_kwargs["tool_call_id"] = msg.tool_call_id
            messages.append(Message(**msg_kwargs))

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
            tools=self.tools,
            tool_choice=self.tool_choice,
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
        import json as _json

        content = response.get_output_text()

        # Convert internal tool_calls to OpenAI format
        openai_tool_calls = None
        if response.tool_calls:
            openai_tool_calls = [
                OpenAIToolCall(
                    id=tc.id or f"call_{i}",
                    type="function",
                    function=OpenAIToolCallFunction(
                        name=tc.function.get("name", ""),
                        arguments=_json.dumps(tc.function.get("arguments", {}))
                        if isinstance(tc.function.get("arguments"), dict)
                        else str(tc.function.get("arguments", "{}")),
                    ),
                )
                for i, tc in enumerate(response.tool_calls)
            ]

        msg = OpenAIChatMessage(
            role="assistant",
            content=content or None,
            tool_calls=openai_tool_calls,
        )

        choice = OpenAIChatChoice(
            index=0,
            message=msg,
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

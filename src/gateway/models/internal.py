"""Internal normalized request/response format.

This is the gateway's lingua franca - all external formats (OpenAI, Anthropic, etc.)
convert to/from this internal format. Providers also work with this format.

Design principles:
- Task-agnostic: supports chat, completion, summarization, classification, etc.
- Provider-agnostic: no provider-specific fields in the core model
- Extensible: optional fields for advanced features
- Secure: all inputs validated and bounded
"""

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, Field

from gateway.models.common import FinishReason, TaskType, UsageStats


# =============================================================================
# Security Validation
# =============================================================================

# Safe identifier pattern for client_id, user_id (prevents log injection)
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

# Maximum content length per message (1MB - prevents DoS)
MAX_CONTENT_LENGTH = 1_000_000


def validate_safe_id(value: str) -> str:
    """Validate ID is safe for logging and metrics.

    Security: Prevents log injection, metric label injection.
    """
    if not value:
        return "anonymous"
    value = value.strip()[:128]  # Truncate to max length
    if not SAFE_ID_PATTERN.match(value):
        # Sanitize instead of reject - replace unsafe chars
        value = re.sub(r"[^a-zA-Z0-9_.-]", "_", value)
        if not value or not value[0].isalnum():
            value = "user_" + value
    return value


def validate_content_length(value: str) -> str:
    """Validate content length is within bounds.

    Security: Prevents memory exhaustion attacks.
    """
    if len(value) > MAX_CONTENT_LENGTH:
        raise ValueError(f"Content exceeds maximum length of {MAX_CONTENT_LENGTH} characters")
    return value


SafeId = Annotated[str, AfterValidator(validate_safe_id)]
BoundedContent = Annotated[str, AfterValidator(validate_content_length)]


class MessageRole(str, Enum):
    """Role of a message in a conversation."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    """A single message in a conversation."""
    role: MessageRole
    content: BoundedContent  # Length-limited for security
    name: str | None = Field(default=None, max_length=64)  # Bounded name


class InternalRequest(BaseModel):
    """Normalized internal request format.

    All external API formats (OpenAI, Anthropic, etc.) are converted
    to this format before routing and processing.

    Required fields per PRD Section 8:
    - request_id, task, input/messages, max_tokens, temperature
    - client_id, user_id

    Optional fields:
    - preferred_provider, fallback_allowed, schema

    Security:
    - All IDs validated to prevent log injection
    - Content bounded to prevent DoS
    - Lists bounded to prevent memory exhaustion
    """

    # Identity and tracking (validated to prevent injection)
    request_id: str = Field(default_factory=lambda: str(uuid4()), max_length=64)
    client_id: SafeId = Field(default="default")
    user_id: SafeId = Field(default="anonymous")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Task specification
    task: TaskType
    model: str | None = Field(default=None, max_length=128)  # Bounded model name

    # Input - flexible to support different task types (all bounded)
    messages: list[Message] | None = Field(default=None, max_length=1000)  # Max 1000 messages
    prompt: BoundedContent | None = None  # For completion/generate tasks
    input_text: BoundedContent | None = None  # For summarize/extract/classify tasks
    input_data: list[str] | None = Field(default=None, max_length=1000)  # For embeddings

    # Generation parameters (max_tokens=0 valid for embeddings)
    max_tokens: int = Field(default=1024, ge=0, le=32768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    stop: list[str] | None = Field(default=None, max_length=10)  # Max 10 stop sequences
    stream: bool = False

    # Routing hints
    preferred_provider: str | None = Field(default=None, max_length=64)
    fallback_allowed: bool = True

    # Advanced options
    response_format: dict[str, Any] | None = None  # JSON schema for structured output
    metadata: dict[str, Any] = Field(default_factory=dict)  # Pass-through metadata

    def get_input_text(self) -> str:
        """Get the primary input text regardless of task type."""
        if self.prompt:
            return self.prompt
        if self.input_text:
            return self.input_text
        if self.messages:
            # Concatenate message contents for text-based operations
            return "\n".join(m.content for m in self.messages if m.content)
        if self.input_data:
            return "\n".join(self.input_data)
        return ""

    def get_last_user_message(self) -> str | None:
        """Get the last user message content."""
        if not self.messages:
            return self.prompt or self.input_text
        for msg in reversed(self.messages):
            if msg.role == MessageRole.USER:
                return msg.content
        return None


class InternalResponse(BaseModel):
    """Normalized internal response format.

    All provider responses are converted to this format before
    being transformed back to the client's expected format.
    """

    # Tracking (echoed from request)
    request_id: str
    task: TaskType
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Provider info
    provider: str
    model: str

    # Output - flexible to support different task types
    content: str | None = None  # Primary text output
    messages: list[Message] | None = None  # For chat responses
    embeddings: list[list[float]] | None = None  # For embedding responses
    data: dict[str, Any] | None = None  # For structured outputs (extract, classify)

    # Generation metadata
    finish_reason: FinishReason = FinishReason.STOP
    usage: UsageStats = Field(default_factory=UsageStats)

    # Timing
    latency_ms: float = 0.0

    # Error handling
    error: str | None = None
    error_code: str | None = None

    @property
    def is_error(self) -> bool:
        """Check if this response represents an error."""
        return self.error is not None

    def get_output_text(self) -> str:
        """Get the primary output text regardless of response type."""
        if self.content:
            return self.content
        if self.messages:
            # Get last assistant message
            for msg in reversed(self.messages):
                if msg.role == MessageRole.ASSISTANT:
                    return msg.content
        return ""


class StreamChunk(BaseModel):
    """A single chunk in a streaming response."""
    request_id: str
    index: int = 0
    delta: str  # Incremental content
    finish_reason: FinishReason | None = None
    usage: UsageStats | None = None  # Only in final chunk

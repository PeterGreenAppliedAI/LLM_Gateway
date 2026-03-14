"""Tests for gateway data models."""

from datetime import datetime

import pytest

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
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAICompletionRequest,
    OpenAICompletionResponse,
    OpenAIEmbeddingRequest,
    OpenAIEmbeddingResponse,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_messages() -> list[Message]:
    """Sample conversation messages."""
    return [
        Message(role=MessageRole.SYSTEM, content="You are a helpful assistant."),
        Message(role=MessageRole.USER, content="What is the capital of France?"),
    ]


@pytest.fixture
def sample_internal_request(sample_messages: list[Message]) -> InternalRequest:
    """Sample internal request."""
    return InternalRequest(
        request_id="test-123",
        client_id="test-client",
        user_id="test-user",
        task=TaskType.CHAT,
        model="llama3",
        messages=sample_messages,
        max_tokens=100,
        temperature=0.7,
    )


@pytest.fixture
def sample_internal_response() -> InternalResponse:
    """Sample internal response."""
    return InternalResponse(
        request_id="test-123",
        task=TaskType.CHAT,
        provider="ollama",
        model="llama3",
        content="The capital of France is Paris.",
        finish_reason=FinishReason.STOP,
        usage=UsageStats(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        latency_ms=150.5,
    )


@pytest.fixture
def sample_openai_chat_request() -> OpenAIChatRequest:
    """Sample OpenAI chat request."""
    return OpenAIChatRequest(
        model="gpt-3.5-turbo",
        messages=[
            OpenAIChatMessage(role="system", content="You are helpful."),
            OpenAIChatMessage(role="user", content="Hello"),
        ],
        max_tokens=100,
        temperature=0.8,
    )


# =============================================================================
# Common Types Tests
# =============================================================================


class TestTaskType:
    """Tests for TaskType enum."""

    def test_all_task_types_exist(self) -> None:
        """Verify all expected task types are defined."""
        expected = [
            "chat",
            "completion",
            "summarize",
            "extract",
            "classify",
            "embeddings",
            "generate",
        ]
        actual = [t.value for t in TaskType]
        for task in expected:
            assert task in actual

    def test_task_type_string_value(self) -> None:
        """Task types should be usable as strings."""
        assert TaskType.CHAT == "chat"
        assert TaskType.EMBEDDINGS == "embeddings"


class TestModelInfo:
    """Tests for ModelInfo model (PRD Section 12)."""

    @pytest.fixture
    def chat_model(self) -> ModelInfo:
        """A model with chat capabilities."""
        return ModelInfo(
            name="llama3.2",
            provider="ollama",
            capabilities=[ModelCapability.CHAT, ModelCapability.STREAMING],
            max_context_length=8192,
            supports_streaming=True,
        )

    @pytest.fixture
    def embedding_model(self) -> ModelInfo:
        """A model with embedding capabilities."""
        return ModelInfo(
            name="nomic-embed-text",
            provider="ollama",
            capabilities=[ModelCapability.EMBEDDINGS],
            max_context_length=2048,
            supports_streaming=False,
        )

    def test_model_info_basic(self, chat_model: ModelInfo) -> None:
        """Basic model info creation."""
        assert chat_model.name == "llama3.2"
        assert chat_model.provider == "ollama"
        assert chat_model.max_context_length == 8192
        assert chat_model.supports_streaming is True

    def test_model_supports_capability(self, chat_model: ModelInfo) -> None:
        """Check capability support."""
        assert chat_model.supports(ModelCapability.CHAT) is True
        assert chat_model.supports(ModelCapability.STREAMING) is True
        assert chat_model.supports(ModelCapability.EMBEDDINGS) is False

    def test_model_supports_task_chat(self, chat_model: ModelInfo) -> None:
        """Chat model supports chat-based tasks."""
        assert chat_model.supports_task(TaskType.CHAT) is True
        assert chat_model.supports_task(TaskType.SUMMARIZE) is True
        assert chat_model.supports_task(TaskType.EXTRACT) is True
        assert chat_model.supports_task(TaskType.CLASSIFY) is True

    def test_model_supports_task_embeddings(self, embedding_model: ModelInfo) -> None:
        """Embedding model supports embedding task only."""
        assert embedding_model.supports_task(TaskType.EMBEDDINGS) is True
        assert embedding_model.supports_task(TaskType.CHAT) is False

    def test_model_info_limitations(self) -> None:
        """Model can declare limitations per PRD."""
        model = ModelInfo(
            name="test-model",
            provider="test",
            capabilities=[ModelCapability.CHAT],
            limitations=["No function calling", "English only"],
        )
        assert len(model.limitations) == 2
        assert "No function calling" in model.limitations


class TestProviderInfo:
    """Tests for ProviderInfo model (PRD Section 12)."""

    @pytest.fixture
    def provider_with_models(self) -> ProviderInfo:
        """Provider with multiple models."""
        return ProviderInfo(
            name="ollama",
            type=ProviderType.OLLAMA,
            base_url="http://localhost:11434",
            health=HealthStatus.HEALTHY,
            capabilities=[ModelCapability.CHAT, ModelCapability.EMBEDDINGS],
            models=[
                ModelInfo(
                    name="llama3.2",
                    provider="ollama",
                    capabilities=[ModelCapability.CHAT],
                ),
                ModelInfo(
                    name="nomic-embed-text",
                    provider="ollama",
                    capabilities=[ModelCapability.EMBEDDINGS],
                ),
            ],
        )

    def test_provider_info_basic(self, provider_with_models: ProviderInfo) -> None:
        """Basic provider info."""
        assert provider_with_models.name == "ollama"
        assert provider_with_models.type == ProviderType.OLLAMA
        assert provider_with_models.health == HealthStatus.HEALTHY

    def test_provider_get_model(self, provider_with_models: ProviderInfo) -> None:
        """Get model by name."""
        model = provider_with_models.get_model("llama3.2")
        assert model is not None
        assert model.name == "llama3.2"

        missing = provider_with_models.get_model("nonexistent")
        assert missing is None

    def test_provider_get_models_for_task(self, provider_with_models: ProviderInfo) -> None:
        """Get models that support a task."""
        chat_models = provider_with_models.get_models_for_task(TaskType.CHAT)
        assert len(chat_models) == 1
        assert chat_models[0].name == "llama3.2"

        embed_models = provider_with_models.get_models_for_task(TaskType.EMBEDDINGS)
        assert len(embed_models) == 1
        assert embed_models[0].name == "nomic-embed-text"

    def test_provider_health_status(self) -> None:
        """Provider health statuses."""
        assert HealthStatus.HEALTHY == "healthy"
        assert HealthStatus.DEGRADED == "degraded"
        assert HealthStatus.UNHEALTHY == "unhealthy"
        assert HealthStatus.UNKNOWN == "unknown"

    def test_provider_types(self) -> None:
        """All provider types per PRD Section 6."""
        assert ProviderType.OLLAMA == "ollama"
        assert ProviderType.VLLM == "vllm"
        assert ProviderType.TRTLLM == "trtllm"
        assert ProviderType.SGLANG == "sglang"


class TestUsageStats:
    """Tests for UsageStats model."""

    def test_usage_stats_defaults(self) -> None:
        """Default values should be zero."""
        usage = UsageStats()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0

    def test_usage_stats_from_counts(self) -> None:
        """from_counts should calculate total correctly."""
        usage = UsageStats.from_counts(prompt=100, completion=50)
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_usage_stats_validation(self) -> None:
        """Token counts cannot be negative."""
        with pytest.raises(ValueError):
            UsageStats(prompt_tokens=-1)


# =============================================================================
# Internal Model Tests
# =============================================================================


class TestMessage:
    """Tests for Message model."""

    def test_message_basic(self) -> None:
        """Basic message creation."""
        msg = Message(role=MessageRole.USER, content="Hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"
        assert msg.name is None

    def test_message_with_name(self) -> None:
        """Message with optional name field."""
        msg = Message(role=MessageRole.TOOL, content="Result", name="calculator")
        assert msg.name == "calculator"


class TestInternalRequest:
    """Tests for InternalRequest model."""

    def test_request_generates_id(self) -> None:
        """Request should auto-generate ID if not provided."""
        req = InternalRequest(task=TaskType.CHAT, messages=[])
        assert req.request_id is not None
        assert len(req.request_id) > 0

    def test_request_generates_timestamp(self) -> None:
        """Request should auto-generate timestamp."""
        req = InternalRequest(task=TaskType.CHAT, messages=[])
        assert isinstance(req.timestamp, datetime)

    def test_request_defaults(self) -> None:
        """Check default values."""
        req = InternalRequest(task=TaskType.CHAT)
        assert req.client_id == "default"
        assert req.user_id == "anonymous"
        assert req.max_tokens == 1024
        assert req.temperature == 0.7
        assert req.fallback_allowed is True
        assert req.stream is False

    def test_request_with_messages(self, sample_messages: list[Message]) -> None:
        """Request with chat messages."""
        req = InternalRequest(
            task=TaskType.CHAT,
            messages=sample_messages,
        )
        assert len(req.messages) == 2
        assert req.messages[0].role == MessageRole.SYSTEM

    def test_request_with_prompt(self) -> None:
        """Request with completion prompt."""
        req = InternalRequest(
            task=TaskType.COMPLETION,
            prompt="Once upon a time",
        )
        assert req.prompt == "Once upon a time"

    def test_get_input_text_from_prompt(self) -> None:
        """get_input_text returns prompt for completion tasks."""
        req = InternalRequest(task=TaskType.COMPLETION, prompt="Test prompt")
        assert req.get_input_text() == "Test prompt"

    def test_get_input_text_from_messages(self, sample_messages: list[Message]) -> None:
        """get_input_text concatenates messages for chat tasks."""
        req = InternalRequest(task=TaskType.CHAT, messages=sample_messages)
        text = req.get_input_text()
        assert "helpful assistant" in text
        assert "capital of France" in text

    def test_get_last_user_message(self, sample_messages: list[Message]) -> None:
        """get_last_user_message finds the last user message."""
        req = InternalRequest(task=TaskType.CHAT, messages=sample_messages)
        assert req.get_last_user_message() == "What is the capital of France?"

    def test_max_tokens_validation(self) -> None:
        """max_tokens must be within valid range."""
        # 0 is valid (for embeddings)
        req = InternalRequest(task=TaskType.EMBEDDINGS, max_tokens=0)
        assert req.max_tokens == 0
        # Negative is invalid
        with pytest.raises(ValueError):
            InternalRequest(task=TaskType.CHAT, max_tokens=-1)
        # Too large is invalid
        with pytest.raises(ValueError):
            InternalRequest(task=TaskType.CHAT, max_tokens=50000)

    def test_temperature_validation(self) -> None:
        """temperature must be between 0 and 2."""
        with pytest.raises(ValueError):
            InternalRequest(task=TaskType.CHAT, temperature=-0.1)
        with pytest.raises(ValueError):
            InternalRequest(task=TaskType.CHAT, temperature=2.5)


class TestInternalResponse:
    """Tests for InternalResponse model."""

    def test_response_basic(self, sample_internal_response: InternalResponse) -> None:
        """Basic response creation."""
        resp = sample_internal_response
        assert resp.request_id == "test-123"
        assert resp.provider == "ollama"
        assert resp.model == "llama3"
        assert resp.content == "The capital of France is Paris."

    def test_response_is_error(self) -> None:
        """is_error property."""
        resp = InternalResponse(
            request_id="test",
            task=TaskType.CHAT,
            provider="test",
            model="test",
        )
        assert resp.is_error is False

        resp_with_error = InternalResponse(
            request_id="test",
            task=TaskType.CHAT,
            provider="test",
            model="test",
            error="Something went wrong",
        )
        assert resp_with_error.is_error is True

    def test_get_output_text(self, sample_internal_response: InternalResponse) -> None:
        """get_output_text returns content."""
        assert sample_internal_response.get_output_text() == "The capital of France is Paris."

    def test_get_output_text_from_messages(self) -> None:
        """get_output_text extracts from messages if no content."""
        resp = InternalResponse(
            request_id="test",
            task=TaskType.CHAT,
            provider="test",
            model="test",
            messages=[
                Message(role=MessageRole.ASSISTANT, content="Hello from assistant"),
            ],
        )
        assert resp.get_output_text() == "Hello from assistant"


class TestStreamChunk:
    """Tests for StreamChunk model."""

    def test_stream_chunk_basic(self) -> None:
        """Basic stream chunk."""
        chunk = StreamChunk(
            request_id="test-123",
            index=0,
            delta="Hello",
        )
        assert chunk.delta == "Hello"
        assert chunk.finish_reason is None

    def test_stream_chunk_final(self) -> None:
        """Final stream chunk with finish reason."""
        chunk = StreamChunk(
            request_id="test-123",
            index=5,
            delta="",
            finish_reason=FinishReason.STOP,
            usage=UsageStats.from_counts(10, 20),
        )
        assert chunk.finish_reason == FinishReason.STOP
        assert chunk.usage.total_tokens == 30


# =============================================================================
# OpenAI Model Tests
# =============================================================================


class TestOpenAIChatRequest:
    """Tests for OpenAI chat request conversion."""

    def test_to_internal_basic(self, sample_openai_chat_request: OpenAIChatRequest) -> None:
        """Convert OpenAI request to internal format."""
        internal = sample_openai_chat_request.to_internal(client_id="my-client")

        assert internal.client_id == "my-client"
        assert internal.task == TaskType.CHAT
        assert internal.model == "gpt-3.5-turbo"
        assert len(internal.messages) == 2
        assert internal.messages[0].role == MessageRole.SYSTEM
        assert internal.max_tokens == 100
        assert internal.temperature == 0.8

    def test_to_internal_defaults(self) -> None:
        """Conversion applies correct defaults."""
        req = OpenAIChatRequest(
            model="test-model",
            messages=[OpenAIChatMessage(role="user", content="Hi")],
        )
        internal = req.to_internal()

        assert internal.max_tokens == 1024  # default
        assert internal.temperature == 0.7  # default
        assert internal.user_id == "anonymous"

    def test_to_internal_with_stop_string(self) -> None:
        """Stop can be a single string."""
        req = OpenAIChatRequest(
            model="test",
            messages=[OpenAIChatMessage(role="user", content="Hi")],
            stop="END",
        )
        internal = req.to_internal()
        assert internal.stop == ["END"]

    def test_to_internal_with_stop_list(self) -> None:
        """Stop can be a list of strings."""
        req = OpenAIChatRequest(
            model="test",
            messages=[OpenAIChatMessage(role="user", content="Hi")],
            stop=["END", "STOP"],
        )
        internal = req.to_internal()
        assert internal.stop == ["END", "STOP"]


class TestOpenAIChatResponse:
    """Tests for OpenAI chat response conversion."""

    def test_from_internal(self, sample_internal_response: InternalResponse) -> None:
        """Convert internal response to OpenAI format."""
        openai_resp = OpenAIChatResponse.from_internal(sample_internal_response)

        assert openai_resp.object == "chat.completion"
        assert openai_resp.model == "llama3"
        assert len(openai_resp.choices) == 1
        assert openai_resp.choices[0].message.role == "assistant"
        assert openai_resp.choices[0].message.content == "The capital of France is Paris."
        assert openai_resp.choices[0].finish_reason == "stop"
        assert openai_resp.usage.prompt_tokens == 20
        assert openai_resp.usage.completion_tokens == 10


class TestOpenAICompletionRequest:
    """Tests for OpenAI completion request conversion."""

    def test_to_internal_string_prompt(self) -> None:
        """Convert completion request with string prompt."""
        req = OpenAICompletionRequest(
            model="text-davinci-003",
            prompt="Complete this: ",
            max_tokens=50,
        )
        internal = req.to_internal()

        assert internal.task == TaskType.COMPLETION
        assert internal.prompt == "Complete this: "
        assert internal.max_tokens == 50

    def test_to_internal_list_prompt(self) -> None:
        """Convert completion request with list prompt (uses first)."""
        req = OpenAICompletionRequest(
            model="test",
            prompt=["First prompt", "Second prompt"],
        )
        internal = req.to_internal()
        assert internal.prompt == "First prompt"


class TestOpenAICompletionResponse:
    """Tests for OpenAI completion response conversion."""

    def test_from_internal(self) -> None:
        """Convert internal response to completion format."""
        internal = InternalResponse(
            request_id="test-123",
            task=TaskType.COMPLETION,
            provider="vllm",
            model="llama2",
            content="The answer is 42.",
            usage=UsageStats.from_counts(10, 5),
        )
        openai_resp = OpenAICompletionResponse.from_internal(internal)

        assert openai_resp.object == "text_completion"
        assert openai_resp.choices[0].text == "The answer is 42."


class TestOpenAIEmbeddingRequest:
    """Tests for OpenAI embedding request conversion."""

    def test_to_internal_string_input(self) -> None:
        """Convert embedding request with string input."""
        req = OpenAIEmbeddingRequest(
            model="text-embedding-ada-002",
            input="Hello world",
        )
        internal = req.to_internal()

        assert internal.task == TaskType.EMBEDDINGS
        assert internal.input_data == ["Hello world"]

    def test_to_internal_list_input(self) -> None:
        """Convert embedding request with list input."""
        req = OpenAIEmbeddingRequest(
            model="test",
            input=["First", "Second", "Third"],
        )
        internal = req.to_internal()
        assert internal.input_data == ["First", "Second", "Third"]


class TestOpenAIEmbeddingResponse:
    """Tests for OpenAI embedding response conversion."""

    def test_from_internal(self) -> None:
        """Convert internal response to embedding format."""
        internal = InternalResponse(
            request_id="test-123",
            task=TaskType.EMBEDDINGS,
            provider="ollama",
            model="nomic-embed-text",
            embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
            usage=UsageStats(prompt_tokens=10),
        )
        openai_resp = OpenAIEmbeddingResponse.from_internal(internal)

        assert openai_resp.object == "list"
        assert len(openai_resp.data) == 2
        assert openai_resp.data[0].embedding == [0.1, 0.2, 0.3]
        assert openai_resp.data[1].index == 1

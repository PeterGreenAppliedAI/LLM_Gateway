"""Tests for OpenAI-compatible provider adapter."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway.config import ProviderConfig
from gateway.models.common import (
    FinishReason,
    HealthStatus,
    ModelCapability,
    ProviderType,
    TaskType,
)
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.providers import OpenAIAdapter, create_adapter


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def openai_config() -> ProviderConfig:
    """Valid OpenAI provider configuration."""
    return ProviderConfig(
        name="test-openai",
        type=ProviderType.OPENAI,
        base_url="https://api.openai.com",
        enabled=True,
        timeout=30.0,
        max_retries=2,
        api_key="sk-test-key-12345678901234567890",
    )


@pytest.fixture
def groq_config() -> ProviderConfig:
    """Valid Groq provider configuration."""
    return ProviderConfig(
        name="groq",
        type=ProviderType.OPENAI,
        base_url="https://api.groq.com/openai",
        enabled=True,
        timeout=30.0,
        api_key="${GROQ_API_KEY}",
    )


@pytest.fixture
def anthropic_config() -> ProviderConfig:
    """Valid Anthropic provider configuration with custom headers."""
    return ProviderConfig(
        name="anthropic",
        type=ProviderType.OPENAI,
        base_url="https://api.anthropic.com/v1",
        enabled=True,
        timeout=60.0,
        api_key_env="ANTHROPIC_API_KEY",
        headers={"anthropic-version": "2024-01-01"},
    )


@pytest.fixture
def sample_chat_request() -> InternalRequest:
    """Sample chat request for testing."""
    return InternalRequest(
        task=TaskType.CHAT,
        model="gpt-4",
        messages=[
            Message(role=MessageRole.USER, content="Hello, how are you?")
        ],
        max_tokens=100,
        temperature=0.7,
    )


@pytest.fixture
def sample_embedding_request() -> InternalRequest:
    """Sample embedding request for testing."""
    return InternalRequest(
        task=TaskType.EMBEDDINGS,
        model="text-embedding-ada-002",
        input_data=["Hello world"],
    )


# =============================================================================
# Adapter Creation Tests
# =============================================================================


class TestOpenAIAdapterCreation:
    """Tests for OpenAI adapter creation."""

    def test_create_adapter_from_config(self, openai_config):
        """Adapter is created correctly from config."""
        adapter = OpenAIAdapter(openai_config)

        assert adapter.name == "test-openai"
        assert adapter.provider_type == ProviderType.OPENAI
        assert adapter.base_url == "https://api.openai.com"
        assert adapter.timeout == 30.0
        assert adapter._api_key == "sk-test-key-12345678901234567890"

    def test_create_adapter_via_factory(self, openai_config):
        """Adapter can be created via factory function."""
        adapter = create_adapter(openai_config)

        assert isinstance(adapter, OpenAIAdapter)
        assert adapter.name == "test-openai"

    def test_api_key_from_env_var_syntax(self, groq_config):
        """API key resolved from ${ENV_VAR} syntax."""
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test-key"}):
            adapter = OpenAIAdapter(groq_config)
            assert adapter._api_key == "gsk-test-key"

    def test_api_key_from_env_var_name(self, anthropic_config):
        """API key resolved from api_key_env config."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            adapter = OpenAIAdapter(anthropic_config)
            assert adapter._api_key == "sk-ant-test"

    def test_api_key_auto_detect_openai(self):
        """API key auto-detected for OpenAI provider."""
        config = ProviderConfig(
            name="openai-provider",
            type=ProviderType.OPENAI,
            base_url="https://api.openai.com",
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-auto-detected"}):
            adapter = OpenAIAdapter(config)
            assert adapter._api_key == "sk-auto-detected"


# =============================================================================
# Request Building Tests
# =============================================================================


class TestOpenAIRequestBuilding:
    """Tests for building OpenAI API requests."""

    def test_build_chat_request(self, openai_config, sample_chat_request):
        """Chat request is built correctly."""
        adapter = OpenAIAdapter(openai_config)
        request = adapter._build_chat_request(sample_chat_request)

        assert request["model"] == "gpt-4"
        assert len(request["messages"]) == 1
        assert request["messages"][0]["role"] == "user"
        assert request["messages"][0]["content"] == "Hello, how are you?"
        assert request["max_tokens"] == 100
        assert request["temperature"] == 0.7
        assert request["stream"] is False

    def test_build_chat_request_with_stop_sequences(self, openai_config):
        """Stop sequences are included in request."""
        adapter = OpenAIAdapter(openai_config)
        request = InternalRequest(
            task=TaskType.CHAT,
            model="gpt-4",
            messages=[Message(role=MessageRole.USER, content="Test")],
            stop=["STOP", "END"],
        )

        built = adapter._build_chat_request(request)
        assert built["stop"] == ["STOP", "END"]

    def test_build_completion_request(self, openai_config):
        """Completion request is built correctly."""
        adapter = OpenAIAdapter(openai_config)
        request = InternalRequest(
            task=TaskType.COMPLETION,
            model="gpt-3.5-turbo-instruct",
            prompt="Once upon a time",
            max_tokens=50,
        )

        built = adapter._build_completion_request(request)

        assert built["model"] == "gpt-3.5-turbo-instruct"
        assert built["prompt"] == "Once upon a time"
        assert built["max_tokens"] == 50


# =============================================================================
# Response Parsing Tests
# =============================================================================


class TestOpenAIResponseParsing:
    """Tests for parsing OpenAI API responses."""

    def test_parse_chat_response(self, openai_config, sample_chat_request):
        """Chat response is parsed correctly."""
        adapter = OpenAIAdapter(openai_config)

        api_response = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1677652288,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "I'm doing great, thanks!",
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "total_tokens": 18,
            },
        }

        response = adapter._parse_chat_response(sample_chat_request, api_response, 150.0)

        assert response.content == "I'm doing great, thanks!"
        assert response.model == "gpt-4"
        assert response.finish_reason == FinishReason.STOP
        assert response.usage.prompt_tokens == 10
        assert response.usage.completion_tokens == 8
        assert response.latency_ms == 150.0

    def test_parse_completion_response(self, openai_config):
        """Completion response is parsed correctly."""
        adapter = OpenAIAdapter(openai_config)

        request = InternalRequest(
            task=TaskType.COMPLETION,
            model="gpt-3.5-turbo-instruct",
            prompt="Once upon a time",
        )

        api_response = {
            "id": "cmpl-123",
            "object": "text_completion",
            "model": "gpt-3.5-turbo-instruct",
            "choices": [{
                "text": " there was a brave knight",
                "finish_reason": "length",
            }],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 6,
                "total_tokens": 11,
            },
        }

        response = adapter._parse_completion_response(request, api_response, 100.0)

        assert response.content == " there was a brave knight"
        assert response.finish_reason == FinishReason.LENGTH

    def test_map_finish_reasons(self, openai_config):
        """Finish reasons are mapped correctly."""
        adapter = OpenAIAdapter(openai_config)

        assert adapter._map_finish_reason("stop") == FinishReason.STOP
        assert adapter._map_finish_reason("length") == FinishReason.LENGTH
        assert adapter._map_finish_reason("content_filter") == FinishReason.CONTENT_FILTER
        assert adapter._map_finish_reason("tool_calls") == FinishReason.TOOL_CALLS
        assert adapter._map_finish_reason("function_call") == FinishReason.TOOL_CALLS
        assert adapter._map_finish_reason(None) == FinishReason.STOP
        assert adapter._map_finish_reason("unknown") == FinishReason.STOP


# =============================================================================
# Capability Inference Tests
# =============================================================================


class TestOpenAICapabilityInference:
    """Tests for inferring model capabilities."""

    def test_infer_gpt4_capabilities(self, openai_config):
        """GPT-4 capabilities are inferred correctly."""
        adapter = OpenAIAdapter(openai_config)
        caps = adapter._infer_capabilities("gpt-4-turbo")

        assert ModelCapability.CHAT in caps
        assert ModelCapability.STREAMING in caps
        assert ModelCapability.VISION in caps
        assert ModelCapability.FUNCTION_CALLING in caps
        assert ModelCapability.JSON_MODE in caps

    def test_infer_embedding_capabilities(self, openai_config):
        """Embedding model capabilities are inferred correctly."""
        adapter = OpenAIAdapter(openai_config)
        caps = adapter._infer_capabilities("text-embedding-ada-002")

        assert ModelCapability.EMBEDDINGS in caps
        assert ModelCapability.CHAT not in caps

    def test_infer_context_length_gpt4_turbo(self, openai_config):
        """GPT-4 Turbo context length is inferred correctly."""
        adapter = OpenAIAdapter(openai_config)

        assert adapter._infer_context_length("gpt-4-turbo") == 128000
        assert adapter._infer_context_length("gpt-4o") == 128000

    def test_infer_context_length_gpt4(self, openai_config):
        """GPT-4 context length is inferred correctly."""
        adapter = OpenAIAdapter(openai_config)

        assert adapter._infer_context_length("gpt-4") == 8192
        assert adapter._infer_context_length("gpt-4-32k") == 32768

    def test_infer_context_length_claude(self, openai_config):
        """Claude context length is inferred correctly."""
        adapter = OpenAIAdapter(openai_config)

        assert adapter._infer_context_length("claude-3-opus") == 200000
        assert adapter._infer_context_length("claude-2.1") == 100000


# =============================================================================
# Adapter Properties Tests
# =============================================================================


class TestOpenAIAdapterProperties:
    """Tests for adapter properties."""

    def test_supports_streaming(self, openai_config):
        """Adapter supports streaming."""
        adapter = OpenAIAdapter(openai_config)
        assert adapter.supports_streaming is True

    def test_limitations(self, openai_config):
        """Adapter reports limitations."""
        adapter = OpenAIAdapter(openai_config)
        limitations = adapter.limitations

        assert len(limitations) > 0
        assert any("API key" in l for l in limitations)

    def test_get_capabilities(self, openai_config):
        """Adapter reports capabilities."""
        adapter = OpenAIAdapter(openai_config)
        caps = adapter.get_capabilities()

        assert "chat" in caps
        assert "streaming" in caps
        assert "embeddings" in caps


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestOpenAIErrorHandling:
    """Tests for error handling."""

    def test_parse_error_response_json(self, openai_config):
        """JSON error response is parsed correctly."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "error": {
                "message": "Invalid API key",
                "type": "invalid_request_error",
            }
        }

        error = adapter._parse_error_response(mock_response)
        assert "Invalid API key" in error

    def test_parse_error_response_text(self, openai_config):
        """Text error response is handled correctly."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.json.side_effect = json.JSONDecodeError("", "", 0)
        mock_response.text = "Internal Server Error"

        error = adapter._parse_error_response(mock_response)
        assert "Internal Server Error" in error


# =============================================================================
# Integration-style Tests (mocked HTTP)
# =============================================================================


class TestOpenAIAdapterIntegration:
    """Integration-style tests with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, openai_config):
        """Health check returns healthy on 200."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            status = await adapter.health()
            assert status == HealthStatus.HEALTHY

        await adapter.close()

    @pytest.mark.asyncio
    async def test_health_check_unauthorized(self, openai_config):
        """Health check returns degraded on 401."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            status = await adapter.health()
            assert status == HealthStatus.DEGRADED

        await adapter.close()

    @pytest.mark.asyncio
    async def test_health_check_timeout(self, openai_config):
        """Health check returns unhealthy on timeout."""
        adapter = OpenAIAdapter(openai_config)

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_get_client.return_value = mock_client

            status = await adapter.health()
            assert status == HealthStatus.UNHEALTHY

        await adapter.close()

    @pytest.mark.asyncio
    async def test_chat_success(self, openai_config, sample_chat_request):
        """Chat completion succeeds with valid response."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "model": "gpt-4",
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            response = await adapter.chat(sample_chat_request)

            assert response.content == "Hello!"
            assert response.error is None
            assert response.usage.total_tokens == 7

        await adapter.close()

    @pytest.mark.asyncio
    async def test_chat_timeout_error(self, openai_config, sample_chat_request):
        """Chat completion returns error response on timeout."""
        adapter = OpenAIAdapter(openai_config)

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_get_client.return_value = mock_client

            response = await adapter.chat(sample_chat_request)

            assert response.error is not None
            assert "Timeout" in response.error

        await adapter.close()

    @pytest.mark.asyncio
    async def test_embeddings_success(self, openai_config, sample_embedding_request):
        """Embeddings request succeeds with valid response."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "model": "text-embedding-ada-002",
            "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            response = await adapter.embeddings(sample_embedding_request)

            assert response.embeddings is not None
            assert len(response.embeddings) == 1
            assert response.embeddings[0] == [0.1, 0.2, 0.3]

        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_models_success(self, openai_config):
        """List models returns model info."""
        adapter = OpenAIAdapter(openai_config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"id": "gpt-4", "object": "model", "owned_by": "openai"},
                {"id": "gpt-3.5-turbo", "object": "model", "owned_by": "openai"},
            ]
        }

        with patch.object(adapter, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            models = await adapter.list_models()

            assert len(models) == 2
            assert models[0].name == "gpt-4"
            assert models[1].name == "gpt-3.5-turbo"

        await adapter.close()

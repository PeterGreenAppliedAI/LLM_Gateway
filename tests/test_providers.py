"""Tests for provider adapters."""

import pytest

from gateway.config import ProviderConfig
from gateway.models.common import HealthStatus, ModelCapability, ProviderType, TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.providers import (
    ProviderAdapter,
    OllamaAdapter,
    VLLMAdapter,
    TRTLLMAdapter,
    SGLangAdapter,
    create_adapter,
    get_supported_provider_types,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def ollama_config() -> ProviderConfig:
    """Valid Ollama provider configuration."""
    return ProviderConfig(
        name="test-ollama",
        type=ProviderType.OLLAMA,
        base_url="http://ollama.local:11434",
        enabled=True,
        timeout=30.0,
        max_retries=2,
    )


@pytest.fixture
def vllm_config() -> ProviderConfig:
    """Valid vLLM provider configuration."""
    return ProviderConfig(
        name="test-vllm",
        type=ProviderType.VLLM,
        base_url="https://vllm.internal:8000",
        enabled=True,
        timeout=60.0,
        max_retries=3,
    )


@pytest.fixture
def trtllm_config() -> ProviderConfig:
    """Valid TRT-LLM provider configuration."""
    return ProviderConfig(
        name="test-trtllm",
        type=ProviderType.TRTLLM,
        base_url="http://trtllm.gpu:8080",
        enabled=True,
        timeout=45.0,
    )


@pytest.fixture
def sglang_config() -> ProviderConfig:
    """Valid SGLang provider configuration."""
    return ProviderConfig(
        name="test-sglang",
        type=ProviderType.SGLANG,
        base_url="http://sglang.internal:30000",
        enabled=True,
    )


@pytest.fixture
def sample_chat_request() -> InternalRequest:
    """Sample chat request for testing."""
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[
            Message(role=MessageRole.USER, content="Hello, how are you?")
        ],
        max_tokens=100,
        temperature=0.7,
    )


# =============================================================================
# URL Validation Tests
# =============================================================================


class TestProviderUrlValidation:
    """Tests for provider URL validation in config."""

    def test_valid_http_url(self):
        """HTTP URLs are accepted."""
        config = ProviderConfig(
            name="test",
            type=ProviderType.OLLAMA,
            base_url="http://localhost:11434",
        )
        assert config.base_url == "http://localhost:11434"

    def test_valid_https_url(self):
        """HTTPS URLs are accepted."""
        config = ProviderConfig(
            name="test",
            type=ProviderType.OLLAMA,
            base_url="https://secure.provider.com:8443",
        )
        assert config.base_url == "https://secure.provider.com:8443"

    def test_url_trailing_slash_stripped(self):
        """Trailing slashes are stripped from URLs."""
        config = ProviderConfig(
            name="test",
            type=ProviderType.OLLAMA,
            base_url="http://localhost:11434/",
        )
        assert config.base_url == "http://localhost:11434"

    def test_rejects_file_scheme(self):
        """file:// scheme is rejected for security."""
        with pytest.raises(ValueError, match="http or https"):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="file:///etc/passwd",
            )

    def test_rejects_data_scheme(self):
        """data: scheme is rejected for security."""
        with pytest.raises(ValueError):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="data:text/plain,malicious",
            )

    def test_rejects_javascript_scheme(self):
        """javascript: scheme is rejected for security."""
        with pytest.raises(ValueError):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="javascript:alert(1)",
            )

    def test_rejects_ftp_scheme(self):
        """ftp:// scheme is rejected (only http/https allowed)."""
        with pytest.raises(ValueError, match="http or https"):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="ftp://server.local/file",
            )

    def test_rejects_missing_scheme(self):
        """URLs without scheme are rejected."""
        with pytest.raises(ValueError, match="must include scheme"):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="localhost:11434",
            )

    def test_rejects_empty_url(self):
        """Empty URLs are rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="   ",
            )

    def test_rejects_missing_host(self):
        """URLs without host are rejected."""
        with pytest.raises(ValueError, match="must include host"):
            ProviderConfig(
                name="test",
                type=ProviderType.OLLAMA,
                base_url="http:///path",
            )


# =============================================================================
# Factory Tests
# =============================================================================


class TestProviderFactory:
    """Tests for provider adapter factory."""

    def test_create_ollama_adapter(self, ollama_config):
        """Factory creates OllamaAdapter from config."""
        adapter = create_adapter(ollama_config)
        assert isinstance(adapter, OllamaAdapter)
        assert adapter.name == "test-ollama"
        assert adapter.base_url == "http://ollama.local:11434"
        assert adapter.timeout == 30.0
        assert adapter.max_retries == 2
        assert adapter.provider_type == ProviderType.OLLAMA

    def test_create_vllm_adapter(self, vllm_config):
        """Factory creates VLLMAdapter from config."""
        adapter = create_adapter(vllm_config)
        assert isinstance(adapter, VLLMAdapter)
        assert adapter.name == "test-vllm"
        assert adapter.base_url == "https://vllm.internal:8000"
        assert adapter.provider_type == ProviderType.VLLM

    def test_create_trtllm_adapter(self, trtllm_config):
        """Factory creates TRTLLMAdapter from config."""
        adapter = create_adapter(trtllm_config)
        assert isinstance(adapter, TRTLLMAdapter)
        assert adapter.name == "test-trtllm"
        assert adapter.provider_type == ProviderType.TRTLLM

    def test_create_sglang_adapter(self, sglang_config):
        """Factory creates SGLangAdapter from config."""
        adapter = create_adapter(sglang_config)
        assert isinstance(adapter, SGLangAdapter)
        assert adapter.name == "test-sglang"
        assert adapter.provider_type == ProviderType.SGLANG

    def test_unsupported_provider_type_rejected_at_config(self):
        """Invalid provider type rejected at config validation (not factory).

        Security: Using enum ensures invalid types are caught at config load time,
        preventing invalid configs from reaching the factory.
        """
        with pytest.raises(ValueError):
            ProviderConfig(
                name="test",
                type="unknown_provider",  # Not a valid ProviderType enum
                base_url="http://localhost:8000",
            )

    def test_get_supported_provider_types(self):
        """Returns list of supported provider types."""
        types = get_supported_provider_types()
        assert "ollama" in types
        assert "vllm" in types
        assert "trtllm" in types
        assert "sglang" in types
        assert "openai" in types
        assert len(types) == 5


# =============================================================================
# Ollama Adapter Tests
# =============================================================================


class TestOllamaAdapter:
    """Tests for OllamaAdapter."""

    def test_adapter_initialization(self, ollama_config):
        """Adapter initializes correctly from config."""
        adapter = OllamaAdapter(ollama_config)
        assert adapter.name == "test-ollama"
        assert adapter.base_url == "http://ollama.local:11434"
        assert adapter.timeout == 30.0
        assert adapter.max_retries == 2
        assert adapter.provider_type == ProviderType.OLLAMA

    def test_supports_streaming(self, ollama_config):
        """Ollama adapter supports streaming."""
        adapter = OllamaAdapter(ollama_config)
        assert adapter.supports_streaming is True

    def test_limitations(self, ollama_config):
        """Ollama adapter declares its limitations."""
        adapter = OllamaAdapter(ollama_config)
        limitations = adapter.limitations
        assert len(limitations) > 0
        assert any("local" in lim.lower() for lim in limitations)

    def test_repr(self, ollama_config):
        """Adapter has useful string representation."""
        adapter = OllamaAdapter(ollama_config)
        repr_str = repr(adapter)
        assert "OllamaAdapter" in repr_str
        assert "test-ollama" in repr_str


# =============================================================================
# vLLM Adapter Tests
# =============================================================================


class TestVLLMAdapter:
    """Tests for VLLMAdapter."""

    def test_adapter_initialization(self, vllm_config):
        """Adapter initializes correctly from config."""
        adapter = VLLMAdapter(vllm_config)
        assert adapter.name == "test-vllm"
        assert adapter.base_url == "https://vllm.internal:8000"
        assert adapter.timeout == 60.0
        assert adapter.provider_type == ProviderType.VLLM

    def test_supports_streaming(self, vllm_config):
        """vLLM adapter supports streaming."""
        adapter = VLLMAdapter(vllm_config)
        assert adapter.supports_streaming is True

    def test_limitations(self, vllm_config):
        """vLLM adapter declares its limitations."""
        adapter = VLLMAdapter(vllm_config)
        limitations = adapter.limitations
        assert len(limitations) > 0
        assert any("gpu" in lim.lower() for lim in limitations)


# =============================================================================
# TRT-LLM Adapter Tests (Stubbed)
# =============================================================================


class TestTRTLLMAdapter:
    """Tests for TRTLLMAdapter (stubbed implementation)."""

    def test_adapter_initialization(self, trtllm_config):
        """Adapter initializes correctly from config."""
        adapter = TRTLLMAdapter(trtllm_config)
        assert adapter.name == "test-trtllm"
        assert adapter.provider_type == ProviderType.TRTLLM

    @pytest.mark.asyncio
    async def test_health_returns_unknown(self, trtllm_config):
        """Stubbed adapter returns UNKNOWN health status."""
        adapter = TRTLLMAdapter(trtllm_config)
        status = await adapter.health()
        assert status == HealthStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_list_models_returns_empty(self, trtllm_config):
        """Stubbed adapter returns empty models list."""
        adapter = TRTLLMAdapter(trtllm_config)
        models = await adapter.list_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_chat_raises_not_implemented(self, trtllm_config, sample_chat_request):
        """Stubbed adapter raises NotImplementedError for chat."""
        adapter = TRTLLMAdapter(trtllm_config)
        with pytest.raises(NotImplementedError, match="stubbed"):
            await adapter.chat(sample_chat_request)

    def test_limitations_indicate_stubbed(self, trtllm_config):
        """Stubbed adapter indicates it's not implemented."""
        adapter = TRTLLMAdapter(trtllm_config)
        limitations = adapter.limitations
        assert any("stubbed" in lim.lower() or "not yet" in lim.lower() for lim in limitations)


# =============================================================================
# SGLang Adapter Tests (Stubbed)
# =============================================================================


class TestSGLangAdapter:
    """Tests for SGLangAdapter (stubbed implementation)."""

    def test_adapter_initialization(self, sglang_config):
        """Adapter initializes correctly from config."""
        adapter = SGLangAdapter(sglang_config)
        assert adapter.name == "test-sglang"
        assert adapter.provider_type == ProviderType.SGLANG

    @pytest.mark.asyncio
    async def test_health_returns_unknown(self, sglang_config):
        """Stubbed adapter returns UNKNOWN health status."""
        adapter = SGLangAdapter(sglang_config)
        status = await adapter.health()
        assert status == HealthStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_list_models_returns_empty(self, sglang_config):
        """Stubbed adapter returns empty models list."""
        adapter = SGLangAdapter(sglang_config)
        models = await adapter.list_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_chat_raises_not_implemented(self, sglang_config, sample_chat_request):
        """Stubbed adapter raises NotImplementedError for chat."""
        adapter = SGLangAdapter(sglang_config)
        with pytest.raises(NotImplementedError, match="stubbed"):
            await adapter.chat(sample_chat_request)

    def test_limitations_indicate_stubbed(self, sglang_config):
        """Stubbed adapter indicates it's not implemented."""
        adapter = SGLangAdapter(sglang_config)
        limitations = adapter.limitations
        assert any("stubbed" in lim.lower() or "not yet" in lim.lower() for lim in limitations)


# =============================================================================
# Base Adapter Contract Tests
# =============================================================================


class TestProviderAdapterContract:
    """Tests verifying all adapters follow the base contract."""

    @pytest.fixture(params=["ollama", "vllm", "trtllm", "sglang"])
    def adapter_config(self, request, ollama_config, vllm_config, trtllm_config, sglang_config):
        """Parametrized fixture for all adapter configs."""
        configs = {
            "ollama": ollama_config,
            "vllm": vllm_config,
            "trtllm": trtllm_config,
            "sglang": sglang_config,
        }
        return configs[request.param]

    def test_all_adapters_inherit_from_base(self, adapter_config):
        """All adapters inherit from ProviderAdapter."""
        adapter = create_adapter(adapter_config)
        assert isinstance(adapter, ProviderAdapter)

    def test_all_adapters_have_required_attributes(self, adapter_config):
        """All adapters have required attributes."""
        adapter = create_adapter(adapter_config)
        assert hasattr(adapter, "name")
        assert hasattr(adapter, "base_url")
        assert hasattr(adapter, "timeout")
        assert hasattr(adapter, "max_retries")
        assert hasattr(adapter, "provider_type")

    def test_all_adapters_have_required_methods(self, adapter_config):
        """All adapters implement required methods."""
        adapter = create_adapter(adapter_config)
        assert callable(getattr(adapter, "health", None))
        assert callable(getattr(adapter, "list_models", None))
        assert callable(getattr(adapter, "chat", None))

    def test_all_adapters_have_optional_methods(self, adapter_config):
        """All adapters have optional methods (may be inherited defaults)."""
        adapter = create_adapter(adapter_config)
        assert callable(getattr(adapter, "generate", None))
        assert callable(getattr(adapter, "embeddings", None))
        assert callable(getattr(adapter, "chat_stream", None))

    def test_all_adapters_have_metadata_properties(self, adapter_config):
        """All adapters expose metadata properties."""
        adapter = create_adapter(adapter_config)
        assert isinstance(adapter.supports_streaming, bool)
        assert isinstance(adapter.limitations, list)

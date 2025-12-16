"""Tests for configuration loading."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gateway.config import (
    ConfigLoader,
    GatewayConfig,
    ProviderConfig,
    RateLimitConfig,
    RoutingConfig,
    RoutingRule,
    load_config,
)


# ============================================================================
# Test Fixtures - Centralized test data
# ============================================================================


@pytest.fixture
def provider_ollama_data() -> dict[str, Any]:
    """Minimal Ollama provider data."""
    return {
        "name": "test-ollama",
        "type": "ollama",
        "base_url": "http://localhost:11434",
    }


@pytest.fixture
def provider_vllm_data() -> dict[str, Any]:
    """Minimal vLLM provider data."""
    return {
        "name": "test-vllm",
        "type": "vllm",
        "base_url": "http://localhost:8001",
    }


@pytest.fixture
def provider_full_data() -> dict[str, Any]:
    """Provider data with all fields populated."""
    return {
        "name": "test-full-provider",
        "type": "vllm",
        "base_url": "http://localhost:8001",
        "enabled": False,
        "timeout": 120.0,
        "max_retries": 5,
        "capabilities": ["chat", "generate"],
        "models": ["llama3", "mistral"],
    }


@pytest.fixture
def minimal_gateway_config_data(provider_ollama_data: dict[str, Any]) -> dict[str, Any]:
    """Minimal valid gateway config data."""
    return {"providers": [provider_ollama_data]}


@pytest.fixture
def full_gateway_config_data(
    provider_ollama_data: dict[str, Any], provider_vllm_data: dict[str, Any]
) -> dict[str, Any]:
    """Complete gateway config data with all sections."""
    return {
        "providers": [
            {**provider_ollama_data, "enabled": True, "capabilities": ["chat", "generate"]},
            {**provider_vllm_data, "enabled": True},
        ],
        "rate_limits": {
            "requests_per_minute_global": 1000,
            "requests_per_minute_per_user": 100,
            "max_tokens_per_request": 4096,
        },
        "routing": {
            "default_provider": provider_ollama_data["name"],
            "rules": [
                {
                    "task": "chat",
                    "provider": provider_ollama_data["name"],
                    "fallback_providers": [provider_vllm_data["name"]],
                },
            ],
        },
    }


def write_yaml_config(path: Path, data: dict[str, Any]) -> Path:
    """Helper to write YAML config file."""
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


# ============================================================================
# ProviderConfig Tests
# ============================================================================


class TestProviderConfig:
    """Test suite for ProviderConfig."""

    def test_provider_config_required_fields(self, provider_ollama_data: dict[str, Any]) -> None:
        """Test provider config with required fields only."""
        config = ProviderConfig(**provider_ollama_data)

        assert config.name == provider_ollama_data["name"]
        assert config.type == provider_ollama_data["type"]
        assert config.base_url == provider_ollama_data["base_url"]
        # Check defaults
        assert config.enabled is True
        assert config.timeout == 30.0
        assert config.max_retries == 3
        assert config.capabilities == []
        assert config.models == []

    def test_provider_config_all_fields(self, provider_full_data: dict[str, Any]) -> None:
        """Test provider config with all fields."""
        config = ProviderConfig(**provider_full_data)

        assert config.name == provider_full_data["name"]
        assert config.enabled == provider_full_data["enabled"]
        assert config.timeout == provider_full_data["timeout"]
        assert config.max_retries == provider_full_data["max_retries"]
        assert config.capabilities == provider_full_data["capabilities"]
        assert config.models == provider_full_data["models"]

    def test_provider_config_timeout_must_be_positive(
        self, provider_ollama_data: dict[str, Any]
    ) -> None:
        """Test that timeout must be positive."""
        with pytest.raises(ValueError):
            ProviderConfig(**{**provider_ollama_data, "timeout": 0})

        with pytest.raises(ValueError):
            ProviderConfig(**{**provider_ollama_data, "timeout": -1})

    def test_provider_config_max_retries_cannot_be_negative(
        self, provider_ollama_data: dict[str, Any]
    ) -> None:
        """Test that max_retries cannot be negative."""
        with pytest.raises(ValueError):
            ProviderConfig(**{**provider_ollama_data, "max_retries": -1})

        # Zero is valid (no retries)
        config = ProviderConfig(**{**provider_ollama_data, "max_retries": 0})
        assert config.max_retries == 0


# ============================================================================
# RateLimitConfig Tests
# ============================================================================


class TestRateLimitConfig:
    """Test suite for RateLimitConfig."""

    def test_rate_limit_defaults(self) -> None:
        """Test rate limit default values."""
        config = RateLimitConfig()

        assert config.requests_per_minute_global == 1000
        assert config.requests_per_minute_per_user == 100
        assert config.max_tokens_per_request == 4096

    def test_rate_limit_custom_values(self) -> None:
        """Test rate limit with custom values."""
        config = RateLimitConfig(
            requests_per_minute_global=500,
            requests_per_minute_per_user=50,
            max_tokens_per_request=8192,
        )

        assert config.requests_per_minute_global == 500
        assert config.requests_per_minute_per_user == 50
        assert config.max_tokens_per_request == 8192

    def test_rate_limit_values_must_be_positive(self) -> None:
        """Test that rate limit values must be positive."""
        with pytest.raises(ValueError):
            RateLimitConfig(requests_per_minute_global=0)

        with pytest.raises(ValueError):
            RateLimitConfig(requests_per_minute_per_user=0)

        with pytest.raises(ValueError):
            RateLimitConfig(max_tokens_per_request=0)


# ============================================================================
# RoutingConfig Tests
# ============================================================================


class TestRoutingConfig:
    """Test suite for RoutingConfig."""

    def test_routing_config_minimal(self, provider_ollama_data: dict[str, Any]) -> None:
        """Test routing config with minimal fields."""
        config = RoutingConfig(default_provider=provider_ollama_data["name"])

        assert config.default_provider == provider_ollama_data["name"]
        assert config.rules == []

    def test_routing_config_with_rules(
        self, provider_ollama_data: dict[str, Any], provider_vllm_data: dict[str, Any]
    ) -> None:
        """Test routing config with rules."""
        rules = [
            RoutingRule(task="chat", provider=provider_ollama_data["name"]),
            RoutingRule(
                task="summarize",
                provider=provider_vllm_data["name"],
                model="llama3",
                fallback_providers=[provider_ollama_data["name"]],
            ),
        ]
        config = RoutingConfig(default_provider=provider_ollama_data["name"], rules=rules)

        assert len(config.rules) == 2
        assert config.rules[0].task == "chat"
        assert config.rules[1].fallback_providers == [provider_ollama_data["name"]]


# ============================================================================
# GatewayConfig Tests
# ============================================================================


class TestGatewayConfig:
    """Test suite for GatewayConfig."""

    def test_gateway_config_minimal(self, provider_ollama_data: dict[str, Any]) -> None:
        """Test gateway config with minimal required fields."""
        providers = [ProviderConfig(**provider_ollama_data)]
        config = GatewayConfig(providers=providers)

        assert len(config.providers) == 1
        assert config.rate_limits is not None
        assert config.routing is None

    def test_gateway_config_empty_providers_raises(self) -> None:
        """Test that empty providers list raises validation error."""
        with pytest.raises(ValueError, match="At least one provider must be configured"):
            GatewayConfig(providers=[])

    def test_get_provider_by_name(
        self, provider_ollama_data: dict[str, Any], provider_vllm_data: dict[str, Any]
    ) -> None:
        """Test getting provider by name."""
        providers = [
            ProviderConfig(**provider_ollama_data),
            ProviderConfig(**provider_vllm_data),
        ]
        config = GatewayConfig(providers=providers)

        found = config.get_provider(provider_ollama_data["name"])
        assert found is not None
        assert found.name == provider_ollama_data["name"]

        unknown = config.get_provider("nonexistent-provider")
        assert unknown is None

    def test_get_enabled_providers(
        self, provider_ollama_data: dict[str, Any], provider_vllm_data: dict[str, Any]
    ) -> None:
        """Test getting only enabled providers."""
        providers = [
            ProviderConfig(**{**provider_ollama_data, "enabled": True}),
            ProviderConfig(**{**provider_vllm_data, "enabled": False}),
            ProviderConfig(
                name="test-sglang",
                type="sglang",
                base_url="http://localhost:8002",
                enabled=True,
            ),
        ]
        config = GatewayConfig(providers=providers)

        enabled = config.get_enabled_providers()
        assert len(enabled) == 2
        assert all(p.enabled for p in enabled)
        assert provider_vllm_data["name"] not in [p.name for p in enabled]


# ============================================================================
# ConfigLoader Tests
# ============================================================================


class TestConfigLoader:
    """Test suite for ConfigLoader."""

    def test_load_config_from_yaml(
        self, tmp_path: Path, minimal_gateway_config_data: dict[str, Any]
    ) -> None:
        """Test loading config from YAML file."""
        config_file = write_yaml_config(tmp_path / "gateway.yaml", minimal_gateway_config_data)

        config = load_config(config_file)

        assert len(config.providers) == 1
        assert config.providers[0].name == minimal_gateway_config_data["providers"][0]["name"]

    def test_load_config_with_separate_providers_file(
        self, tmp_path: Path, provider_vllm_data: dict[str, Any]
    ) -> None:
        """Test loading providers from separate file."""
        main_config = {"rate_limits": {"max_tokens_per_request": 2048}}
        providers_config = {"providers": [provider_vllm_data]}

        main_file = write_yaml_config(tmp_path / "gateway.yaml", main_config)
        providers_file = write_yaml_config(tmp_path / "providers.yaml", providers_config)

        config = load_config(main_file, providers_file)

        assert len(config.providers) == 1
        assert config.providers[0].name == provider_vllm_data["name"]
        assert config.rate_limits.max_tokens_per_request == 2048

    def test_load_config_file_not_found(self, tmp_path: Path) -> None:
        """Test that missing config file raises clear error."""
        with pytest.raises(FileNotFoundError, match="Configuration file not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_config_empty_file(self, tmp_path: Path) -> None:
        """Test handling of empty config file."""
        config_file = tmp_path / "empty.yaml"
        config_file.touch()

        with pytest.raises(ValueError, match="At least one provider must be configured"):
            load_config(config_file)

    def test_load_full_config(
        self, tmp_path: Path, full_gateway_config_data: dict[str, Any]
    ) -> None:
        """Test loading complete configuration."""
        config_file = write_yaml_config(tmp_path / "gateway.yaml", full_gateway_config_data)

        config = load_config(config_file)

        assert len(config.providers) == 2
        assert config.routing is not None
        assert config.routing.default_provider == full_gateway_config_data["routing"]["default_provider"]
        assert len(config.routing.rules) == 1

    def test_providers_file_overrides_main_config(
        self,
        tmp_path: Path,
        provider_ollama_data: dict[str, Any],
        provider_vllm_data: dict[str, Any],
    ) -> None:
        """Test that providers file takes precedence over main config."""
        main_config = {"providers": [provider_ollama_data]}
        providers_config = {"providers": [provider_vllm_data]}

        main_file = write_yaml_config(tmp_path / "gateway.yaml", main_config)
        providers_file = write_yaml_config(tmp_path / "providers.yaml", providers_config)

        config = load_config(main_file, providers_file)

        # Provider from providers.yaml should be used
        assert len(config.providers) == 1
        assert config.providers[0].name == provider_vllm_data["name"]

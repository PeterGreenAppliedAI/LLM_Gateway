"""Tests for application settings."""

import pytest

from gateway.settings import Settings, get_settings


class TestSettings:
    """Test suite for Settings class."""

    def test_default_settings(self) -> None:
        """Test that default settings are correctly applied.

        Security: Default host is 127.0.0.1 (localhost only) to prevent
        accidental external exposure. Use GATEWAY_HOST=0.0.0.0 to enable.
        """
        settings = Settings()

        # Security: Default to localhost only
        assert settings.host == "127.0.0.1"
        assert settings.port == 8000
        assert settings.debug is False
        assert settings.log_level == "INFO"
        assert settings.log_format == "json"
        assert settings.api_key is None
        assert settings.api_key_header == "X-API-Key"
        assert settings.require_api_key is False
        assert settings.log_requests is True
        assert settings.redact_prompts is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override defaults."""
        monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
        monkeypatch.setenv("GATEWAY_PORT", "9000")
        monkeypatch.setenv("GATEWAY_DEBUG", "true")
        monkeypatch.setenv("GATEWAY_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("GATEWAY_API_KEY", "test-key-123")

        settings = Settings()

        assert settings.host == "0.0.0.0"
        assert settings.port == 9000
        assert settings.debug is True
        assert settings.log_level == "DEBUG"
        # SecretStr: access via .get_secret_value()
        assert settings.api_key is not None
        assert settings.api_key.get_secret_value() == "test-key-123"

    def test_api_key_is_secret_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that API key is protected by SecretStr.

        Security: SecretStr prevents accidental logging of sensitive values.
        """
        monkeypatch.setenv("GATEWAY_API_KEY", "super-secret-key")
        settings = Settings()

        # String representation should NOT contain the key
        assert "super-secret-key" not in str(settings.api_key)
        assert "super-secret-key" not in repr(settings.api_key)
        assert "super-secret-key" not in repr(settings)

        # But we can still access it when needed
        assert settings.api_key.get_secret_value() == "super-secret-key"

    def test_get_settings_returns_settings(self) -> None:
        """Test that get_settings returns a Settings instance."""
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_config_paths_default(self) -> None:
        """Test default config path values."""
        settings = Settings()

        assert settings.config_path == "config/gateway.yaml"
        assert settings.providers_config_path == "config/providers.yaml"

    def test_config_paths_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config path overrides via environment."""
        monkeypatch.setenv("GATEWAY_CONFIG_PATH", "/custom/config.yaml")
        monkeypatch.setenv("GATEWAY_PROVIDERS_CONFIG_PATH", "/custom/providers.yaml")

        settings = Settings()

        assert settings.config_path == "/custom/config.yaml"
        assert settings.providers_config_path == "/custom/providers.yaml"

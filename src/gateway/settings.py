"""Application settings using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway application settings.

    Settings are loaded from environment variables and can be overridden
    by a .env file. Environment variables take precedence over config files.

    Security considerations:
    - API key stored as SecretStr to prevent accidental logging
    - Default host is 127.0.0.1 (localhost only) for security
    - Set GATEWAY_HOST=0.0.0.0 explicitly to expose externally
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server settings
    # Security: Default to localhost only - explicit opt-in for external access
    host: str = Field(default="127.0.0.1", description="Server host (use 0.0.0.0 for external)")
    port: int = Field(default=8000, ge=1, le=65535, description="Server port")
    debug: bool = Field(default=False, description="Debug mode (never enable in production)")

    # Config paths
    config_path: str = Field(default="config/gateway.yaml", description="Main config file path")
    providers_config_path: str = Field(
        default="config/providers.yaml", description="Providers config file path"
    )

    # Security
    # SecretStr prevents accidental exposure in logs, repr, etc.
    api_key: SecretStr | None = Field(default=None, description="API key for authentication")
    api_key_header: str = Field(default="X-API-Key", description="Header name for API key")
    require_api_key: bool = Field(default=False, description="Require API key for all requests")

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="Log format")
    log_requests: bool = Field(default=True, description="Log all requests")
    redact_prompts: bool = Field(default=True, description="Redact prompts from logs")

    def __repr__(self) -> str:
        """Safe repr that doesn't expose secrets."""
        return (
            f"Settings(host={self.host!r}, port={self.port}, debug={self.debug}, "
            f"api_key={'***' if self.api_key else None})"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get application settings singleton (cached)."""
    return Settings()

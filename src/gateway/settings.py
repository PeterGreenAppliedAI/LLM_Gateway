"""Application settings using Pydantic Settings."""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database configuration settings.

    Supports SQLite (default, zero config) and PostgreSQL (production).
    Configure via environment variables with GATEWAY_DB_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_DB_",
        extra="ignore",
    )

    # Connection URL
    # SQLite: sqlite:///./data/gateway.db (default)
    # PostgreSQL: postgresql://user:pass@localhost:5432/gateway
    url: str = Field(
        default="sqlite:///./data/gateway.db",
        description="Database connection URL",
    )

    # Connection pool settings (ignored for SQLite)
    pool_size: int = Field(default=5, ge=1, le=50, description="Connection pool size")
    max_overflow: int = Field(default=10, ge=0, le=100, description="Max pool overflow")
    pool_timeout: int = Field(default=30, ge=1, le=300, description="Pool timeout seconds")
    pool_recycle: int = Field(default=3600, ge=60, description="Connection recycle seconds")

    # Privacy controls - what to store
    store_request_body: bool = Field(
        default=False,
        description="Store request bodies (prompts) - privacy sensitive",
    )
    store_response_body: bool = Field(
        default=False,
        description="Store response bodies (completions) - privacy sensitive",
    )

    # Table creation
    create_tables: bool = Field(default=True, description="Auto-create tables on startup")

    # Debug
    echo: bool = Field(default=False, description="Echo SQL statements (debug only)")


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

    # Database (nested settings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)

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

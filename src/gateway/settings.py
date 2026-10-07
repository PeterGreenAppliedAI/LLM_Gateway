"""Application settings using Pydantic Settings."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Audit durability
    required: bool = Field(
        default=True,
        description="Refuse to start if the database can't be initialized. Set false only "
        "for deployments that accept running with no audit trail.",
    )
    audit_durability: Literal["auto", "process", "grouped", "sync"] = Field(
        default="auto",
        description="Audit intent log (D-038). process: rows go to a local log and the "
        "response returns; a background task writes them to the DB (survives any gateway "
        "crash; a power cut can lose up to ~30 s the OS hadn't written yet). grouped: the "
        "response also waits for the log to reach disk (~50 ms batches; nothing "
        "acknowledged is lost). sync: each response waits for the DB commit. auto: process "
        "on SQLite, sync on PostgreSQL.",
    )
    audit_journal_path: str = Field(
        default="data/audit-journal",
        description="Directory for the audit intent log (local disk, not a network share)",
    )
    audit_journal_max_mb: int = Field(
        default=1024,
        ge=16,
        le=1_048_576,
        description="Cap on the intent log while the DB is unreachable; past it the "
        "oldest records are dropped (logged as critical)",
    )
    audit_spill_path: str = Field(
        default="data/audit-spill.jsonl",
        description="Where audit rows go when the database is unreachable; replayed on "
        "startup. Empty string disables spilling.",
    )

    # API key cache (D-040)
    key_cache_seconds: float = Field(
        default=30.0,
        ge=0,
        le=3600,
        description="Remember validated DB-backed keys this long (no DB query per request). "
        "Revocation is immediate in the process that revoked; other processes follow within "
        "this time. 0 disables the cache.",
    )

    # Data retention
    retention_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="Days to retain audit log entries (0 = no cleanup)",
    )

    # Debug
    echo: bool = Field(default=False, description="Echo SQL statements (debug only)")


class GuardModelSettings(BaseSettings):
    """Guard model shadow analyzer configuration.

    Supports Llama Guard 3 and Granite Guardian 3.2.
    Auto-detects backend from model_name.
    Configure via environment variables with GATEWAY_GUARD_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_GUARD_",
        extra="ignore",
    )

    enabled: bool = Field(default=False, description="Enable guard model shadow analysis")
    base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server URL hosting guard model",
    )
    model_name: str = Field(
        default="ibm/granite3.2-guardian:5b",
        description="Guard model name (e.g. ibm/granite3.2-guardian:5b, llama-guard3:8b)",
    )
    timeout: float = Field(default=15.0, ge=1.0, le=60.0, description="Inference timeout seconds")


class PIISettings(BaseSettings):
    """PII detection and scrubbing configuration.

    Detection always runs when enabled (flags PII in security alerts).
    Scrubbing (replacing PII with placeholders) is optional and per-route.
    Configure via environment variables with GATEWAY_PII_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_PII_",
        extra="ignore",
    )

    enabled: bool = Field(default=False, description="Enable PII detection (always flags)")
    scrub_enabled: bool = Field(
        default=False, description="Enable PII scrubbing (replace with placeholders)"
    )
    scrub_routes: list[str] = Field(
        default_factory=list,
        description="Routes where scrubbing is active (e.g. ['/v1/chat/completions', '/api/chat']). Empty = all routes.",
    )


class SecuritySettings(BaseSettings):
    """Security scanning configuration.

    Configure via environment variables with GATEWAY_SECURITY_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_SECURITY_",
        extra="ignore",
    )

    scan_allowlist_ips: list[str] = Field(
        default_factory=list,
        description="Source IPs to skip security scanning (trusted internal services)",
    )
    # What security scans keep of the request itself (D-041). Verdicts and
    # metadata are always kept; message content only by explicit opt-in.
    store_messages: Literal["none", "flagged", "all"] = Field(
        default="none",
        description="none: verdicts only. flagged: messages of requests the regex scanner "
        "or guard model flagged (for review). all: every request's messages (training data "
        "collection). Stored messages are always PII-redacted.",
    )
    retention_days: int = Field(
        default=90,
        ge=0,
        le=3650,
        description="Delete security scans and PII events older than this (0 = keep)",
    )


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
    profiles_path: str = Field(
        default="config/profiles", description="Directory of engine profiles (<name>.yaml)"
    )

    # Security
    # SecretStr prevents accidental exposure in logs, repr, etc.
    api_key: SecretStr | None = Field(default=None, description="API key for authentication")
    admin_api_key: SecretStr | None = Field(
        default=None, description="Admin API key for key management endpoints"
    )
    api_key_header: str = Field(default="X-API-Key", description="Header name for API key")
    require_api_key: bool = Field(default=False, description="Require API key for all requests")

    # Shared state (D-035). Unset: rate limits and concurrency slots live in
    # this process (nothing else to run). Set to share them across gateway
    # processes/replicas, e.g. redis://:password@redis:6379/0. SecretStr
    # because the URL may carry a password.
    redis_url: SecretStr | None = Field(
        default=None, description="Redis URL for shared rate limits and slots (optional)"
    )
    redis_prefix: str = Field(
        default="devmesh",
        pattern=r"^[a-zA-Z0-9_-]{1,32}$",
        description="Key prefix, so several gateways can share one Redis",
    )

    # Access (D-042)
    dev_mode: bool = Field(
        default=False,
        description="Test mode: keyless inference and dashboard from dev_networks, whatever "
        "the auth config says. Never in production.",
    )
    dev_networks: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.0/8", "::1/128"],
        description="Where test mode accepts keyless requests (comma-separated CIDRs)",
    )
    profile: Literal["default", "production"] = Field(
        default="default",
        description="production: refuse to start without auth, an admin key and restricted "
        "keyless access, or with test mode on",
    )

    @field_validator("dev_networks", mode="before")
    @classmethod
    def _split_networks(cls, value):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        from gateway.security.access import parse_networks

        parse_networks(value)
        return value

    # CORS
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowed CORS origins (dashboard URLs)",
    )

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="Log format")
    log_requests: bool = Field(default=True, description="Log all requests")
    redact_prompts: bool = Field(default=True, description="Redact prompts from logs")

    # Database (nested settings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)

    # Guard model (shadow analyzer)
    guard: GuardModelSettings = Field(default_factory=GuardModelSettings)

    # PII detection and scrubbing
    pii: PIISettings = Field(default_factory=PIISettings)

    # Security scanning
    security: SecuritySettings = Field(default_factory=SecuritySettings)

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

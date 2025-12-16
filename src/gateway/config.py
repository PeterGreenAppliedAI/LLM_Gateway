"""Configuration loading from YAML files."""

import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AfterValidator, BaseModel, Field, model_validator

from gateway.models.common import ProviderType


# =============================================================================
# Validation Helpers
# =============================================================================

# Safe identifier pattern: alphanumeric, hyphens, underscores only
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def validate_safe_identifier(value: str) -> str:
    """Validate string is a safe identifier (no injection risk).

    Security: Prevents log injection, metric label injection, and similar attacks.
    Allows: letters, numbers, hyphens, underscores. Must start with letter.
    Max length: 64 characters.
    """
    value = value.strip()
    if not value:
        raise ValueError("Identifier cannot be empty")
    if not SAFE_IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"Invalid identifier '{value}': must start with letter, contain only "
            "alphanumeric, hyphens, underscores, max 64 chars"
        )
    return value


# Type alias for validated identifiers
SafeIdentifier = Annotated[str, AfterValidator(validate_safe_identifier)]


def validate_provider_url(url: str) -> str:
    """Validate provider URL is well-formed and uses allowed scheme.

    Security: Only allow http/https schemes to prevent injection attacks
    via file://, data://, or other schemes.
    """
    url = url.strip().rstrip("/")
    if not url:
        raise ValueError("URL cannot be empty")

    # Parse and validate scheme
    if "://" not in url:
        raise ValueError("URL must include scheme (http:// or https://)")

    scheme = url.split("://")[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got: {scheme}")

    # Basic structure validation
    rest = url.split("://", 1)[1]
    if not rest or rest.startswith("/"):
        raise ValueError("URL must include host")

    return url


# Type alias for validated provider URLs
ProviderUrl = Annotated[str, AfterValidator(validate_provider_url)]


class ProviderConfig(BaseModel):
    """Configuration for a single provider."""

    name: SafeIdentifier  # Validated to prevent injection
    type: ProviderType  # Enum validation ensures valid provider type
    base_url: ProviderUrl
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0, le=300.0)  # Max 5 minutes
    max_retries: int = Field(default=3, ge=0, le=10)  # Bounded retries
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    models: list[str] = Field(default_factory=list, max_length=100)
    default_model: str | None = None  # Default model for this provider


class ApiKeyConfig(BaseModel):
    """Configuration for a single API key."""

    key: str = Field(min_length=16, max_length=128)
    client_id: SafeIdentifier
    description: str = ""


class AuthConfig(BaseModel):
    """Authentication configuration."""

    enabled: bool = Field(default=False, description="Enable API key authentication")
    api_keys: list[ApiKeyConfig] = Field(default_factory=list, max_length=1000)


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    requests_per_minute_global: int = Field(default=1000, gt=0)
    requests_per_minute_per_user: int = Field(default=100, gt=0)
    max_tokens_per_request: int = Field(default=4096, gt=0)


class RoutingRule(BaseModel):
    """A single routing rule."""

    task: SafeIdentifier  # Task type identifier
    provider: SafeIdentifier  # Provider name reference
    model: str | None = None
    fallback_providers: list[SafeIdentifier] = Field(default_factory=list, max_length=10)


class RoutingConfig(BaseModel):
    """Routing configuration."""

    default_provider: SafeIdentifier  # Must reference a valid provider
    rules: list[RoutingRule] = Field(default_factory=list, max_length=100)


class GatewayConfig(BaseModel):
    """Main gateway configuration."""

    providers: list[ProviderConfig] = Field(default_factory=list, max_length=50)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    routing: RoutingConfig | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Policy config imported lazily to avoid circular imports
    policy: Any = None

    @model_validator(mode="after")
    def validate_config(self) -> "GatewayConfig":
        """Validate configuration consistency.

        Security: Ensures all provider references are valid to prevent
        routing to non-existent providers.
        """
        # Allow empty providers for testing (routes create default config)
        if not self.providers:
            return self

        # Build set of valid provider names
        provider_names = {p.name for p in self.providers}

        # Validate routing references
        if self.routing:
            if self.routing.default_provider not in provider_names:
                raise ValueError(
                    f"Routing default_provider '{self.routing.default_provider}' "
                    f"not found in providers: {sorted(provider_names)}"
                )

            for rule in self.routing.rules:
                if rule.provider not in provider_names:
                    raise ValueError(
                        f"Routing rule references unknown provider '{rule.provider}'"
                    )
                for fallback in rule.fallback_providers:
                    if fallback not in provider_names:
                        raise ValueError(
                            f"Routing rule fallback references unknown provider '{fallback}'"
                        )

        return self

    def get_provider(self, name: str) -> ProviderConfig | None:
        """Get provider config by name."""
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def get_enabled_providers(self) -> list[ProviderConfig]:
        """Get all enabled providers."""
        return [p for p in self.providers if p.enabled]


class ConfigLoader:
    """Loads and validates configuration from YAML files."""

    def __init__(self, config_path: str | Path, providers_path: str | Path | None = None):
        self.config_path = Path(config_path)
        self.providers_path = Path(providers_path) if providers_path else None

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        """Load a YAML file."""
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        if data is None:
            return {}

        return data

    def load(self) -> GatewayConfig:
        """Load and validate the gateway configuration."""
        config_data = self._load_yaml(self.config_path)

        # Load providers from separate file if specified
        if self.providers_path and self.providers_path.exists():
            providers_data = self._load_yaml(self.providers_path)
            if "providers" in providers_data:
                config_data["providers"] = providers_data["providers"]

        return GatewayConfig(**config_data)


def load_config(
    config_path: str | Path, providers_path: str | Path | None = None
) -> GatewayConfig:
    """Convenience function to load gateway configuration."""
    loader = ConfigLoader(config_path, providers_path)
    return loader.load()

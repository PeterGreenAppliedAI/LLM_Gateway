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
    """Configuration for a single provider (legacy format)."""

    name: SafeIdentifier  # Validated to prevent injection
    type: ProviderType  # Enum validation ensures valid provider type
    base_url: ProviderUrl
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0, le=300.0)  # Max 5 minutes
    max_retries: int = Field(default=3, ge=0, le=10)  # Bounded retries
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    models: list[str] = Field(default_factory=list, max_length=100)
    default_model: str | None = None  # Default model for this provider
    # API key authentication (for cloud providers)
    api_key: str | None = None  # Direct key or ${ENV_VAR} syntax
    api_key_env: str | None = None  # Environment variable name for API key
    headers: dict[str, str] = Field(default_factory=dict)  # Custom headers (e.g., anthropic-version)


# =============================================================================
# Endpoint Configuration (New Architecture)
# =============================================================================


class EndpointConfig(BaseModel):
    """Configuration for a single endpoint (physical runtime).

    Endpoints represent actual inference runtimes like gpunode-ollama,
    dgxspark-vllm, etc. Labels enable flexible filtering for environments.
    """

    name: SafeIdentifier  # e.g., gpunode-ollama
    type: ProviderType  # ollama, vllm, etc.
    url: ProviderUrl  # http://192.168.1.216:11434
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0, le=300.0)
    max_retries: int = Field(default=3, ge=0, le=10)
    labels: dict[str, str] = Field(default_factory=dict)  # cold_flexible, prod_eligible, etc.
    api_key_env: str | None = None  # Environment variable name for API key


class EnvironmentConfig(BaseModel):
    """Configuration for an environment (dev, prod, etc.).

    Environments define which endpoints and models are available,
    enabling dev/prod separation with approval controls.
    """

    name: SafeIdentifier  # dev, prod
    endpoint_filter: dict[str, str] = Field(default_factory=dict)  # Label filters
    allowed_endpoints: list[SafeIdentifier] = Field(default_factory=list)
    approved_models: list[str] = Field(default_factory=list, max_length=500)
    allow_all_discovered: bool = False


class ModelDefault(BaseModel):
    """Maps a model pattern to a preferred endpoint."""

    model: str  # phi4:14b or phi4:* (supports glob patterns)
    endpoint: SafeIdentifier


class ResolutionConfig(BaseModel):
    """Configuration for model→endpoint resolution.

    Defines how the gateway resolves which endpoint to use
    when multiple endpoints have the same model.
    """

    model_defaults: list[ModelDefault] = Field(default_factory=list, max_length=500)
    endpoint_priority: list[SafeIdentifier] = Field(default_factory=list, max_length=50)
    ambiguous_behavior: str = Field(
        default="error",
        pattern="^(error|first_priority)$"
    )  # error or first_priority


class ApiKeyConfig(BaseModel):
    """Configuration for a single API key."""

    key: str = Field(min_length=16, max_length=128)
    client_id: SafeIdentifier
    description: str = ""
    environment: SafeIdentifier | None = None  # dev, prod - determines which env this key uses


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
    """Main gateway configuration.

    Supports both legacy providers config and new endpoints architecture.
    When using endpoints, providers is auto-populated for backward compatibility.
    """

    # Legacy provider config (still supported, auto-migrated from endpoints)
    providers: list[ProviderConfig] = Field(default_factory=list, max_length=50)
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    routing: RoutingConfig | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Policy config imported lazily to avoid circular imports
    policy: Any = None

    # New endpoints architecture
    endpoints: list[EndpointConfig] = Field(default_factory=list, max_length=50)
    environments: list[EnvironmentConfig] = Field(default_factory=list, max_length=20)
    resolution: ResolutionConfig = Field(default_factory=ResolutionConfig)

    @model_validator(mode="after")
    def validate_config(self) -> "GatewayConfig":
        """Validate configuration consistency.

        Security: Ensures all provider/endpoint references are valid to prevent
        routing to non-existent providers.
        """
        # Auto-migrate: If endpoints are configured but providers are empty,
        # create provider configs from endpoints for backward compatibility
        if self.endpoints and not self.providers:
            self.providers = [
                ProviderConfig(
                    name=ep.name,
                    type=ep.type,
                    base_url=ep.url,
                    enabled=ep.enabled,
                    timeout=ep.timeout,
                    max_retries=ep.max_retries,
                )
                for ep in self.endpoints
            ]

        # Auto-migrate: If providers are configured but endpoints are empty,
        # create endpoint configs from providers
        if self.providers and not self.endpoints:
            self.endpoints = [
                EndpointConfig(
                    name=p.name,
                    type=p.type,
                    url=p.base_url,
                    enabled=p.enabled,
                    timeout=p.timeout,
                    max_retries=p.max_retries,
                )
                for p in self.providers
            ]

        # Allow empty providers/endpoints for testing
        if not self.providers and not self.endpoints:
            return self

        # Build set of valid provider/endpoint names
        provider_names = {p.name for p in self.providers}
        endpoint_names = {e.name for e in self.endpoints}

        # Validate routing references (legacy)
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

        # Validate resolution config references
        for model_default in self.resolution.model_defaults:
            if model_default.endpoint not in endpoint_names:
                raise ValueError(
                    f"Model default references unknown endpoint '{model_default.endpoint}'"
                )

        for priority_endpoint in self.resolution.endpoint_priority:
            if priority_endpoint not in endpoint_names:
                raise ValueError(
                    f"Endpoint priority references unknown endpoint '{priority_endpoint}'"
                )

        # Validate environment references
        for env in self.environments:
            for allowed_ep in env.allowed_endpoints:
                if allowed_ep not in endpoint_names:
                    raise ValueError(
                        f"Environment '{env.name}' references unknown endpoint '{allowed_ep}'"
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

    def get_endpoint(self, name: str) -> EndpointConfig | None:
        """Get endpoint config by name."""
        for endpoint in self.endpoints:
            if endpoint.name == name:
                return endpoint
        return None

    def get_enabled_endpoints(self) -> list[EndpointConfig]:
        """Get all enabled endpoints."""
        return [e for e in self.endpoints if e.enabled]

    def get_environment(self, name: str) -> EnvironmentConfig | None:
        """Get environment config by name."""
        for env in self.environments:
            if env.name == name:
                return env
        return None

    def get_default_environment(self) -> EnvironmentConfig | None:
        """Get the default environment (first one, or 'dev' if named)."""
        if not self.environments:
            return None
        # Prefer 'dev' if it exists
        for env in self.environments:
            if env.name == "dev":
                return env
        # Otherwise return first
        return self.environments[0]


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

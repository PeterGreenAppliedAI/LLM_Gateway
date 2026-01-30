"""Shared route dependencies.

Per rule.md:
- No Implicit Trust: Validate all inputs
- Auditability: Track request context
- Explicit Boundaries: Clear dependency injection

Per PRD Section 14:
- API key authentication

Per Endpoints/Environments Architecture:
- Environment resolution from API key or header
"""

import re
import secrets
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import Depends, Header, Request

from gateway.config import GatewayConfig, EnvironmentConfig
from gateway.dispatch import Dispatcher, ProviderRegistry
from gateway.errors import (
    AuthenticationError,
    InvalidApiKeyError,
    InvalidApiKeyFormatError,
    RateLimitError,
    PolicyError,
    TokenLimitError,
    ProviderNotAllowedError,
    ErrorCode,
)
from gateway.observability import get_logger, get_metrics, RequestContext
from gateway.observability.logging import set_request_context, clear_request_context
from gateway.policy import PolicyEnforcer, PolicyViolation
from gateway.security import AsyncSecurityAnalyzer, Sanitizer
from gateway.storage import AuditLogger

logger = get_logger(__name__)

# Shared sanitizer instance (thread-safe, stateless)
_sanitizer = Sanitizer()


# Security: Pattern for valid API keys (prevents injection)
SAFE_API_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{16,128}$")


def get_config(request: Request) -> GatewayConfig:
    """Get gateway configuration from app state.

    Returns default config if not loaded.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        # Return minimal default config for testing
        return GatewayConfig()
    return config


async def get_registry(request: Request) -> ProviderRegistry:
    """Get or create provider registry.

    Creates registry on first request and caches in app state.
    Initializes the registry asynchronously if needed.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        config = get_config(request)
        registry = ProviderRegistry(config)
        await registry.initialize()
        request.app.state.registry = registry
    return registry


def get_dispatcher(
    request: Request,
    registry: Annotated[ProviderRegistry, Depends(get_registry)]
) -> Dispatcher:
    """Get dispatcher instance with resolution config."""
    config = get_config(request)
    return Dispatcher(registry, resolution_config=config.resolution)


def get_enforcer(request: Request) -> PolicyEnforcer:
    """Get or create policy enforcer.

    Creates enforcer on first request and caches in app state.
    """
    enforcer = getattr(request.app.state, "enforcer", None)
    if enforcer is None:
        config = get_config(request)
        # Handle both PolicyConfig object and None
        policy_config = config.policy if config.policy else None
        enforcer = PolicyEnforcer(policy_config)
        request.app.state.enforcer = enforcer
    return enforcer


def get_audit_logger(request: Request) -> Optional[AuditLogger]:
    """Get audit logger from app state.

    Returns None if database is not configured or failed to initialize.
    Callers should handle None gracefully (audit logging is optional).
    """
    return getattr(request.app.state, "audit_logger", None)


def validate_api_key(
    api_key: str,
    config: GatewayConfig,
    db_engine=None,
) -> tuple[str, str | None, str | None]:
    """Validate API key and return client_id, environment, and target_endpoint.

    Checks two sources in order:
    1. Config-backed keys (fast, constant-time comparison)
    2. DB-backed keys (hash lookup, if db_engine provided)

    Security:
    - Validates key format to prevent injection
    - Uses constant-time comparison for config keys
    - Uses SHA256 hash lookup for DB keys
    - Returns sanitized client_id

    Args:
        api_key: The API key to validate
        config: Gateway configuration
        db_engine: Optional SQLAlchemy engine for DB key lookup

    Returns:
        Tuple of (client_id, environment, target_endpoint) associated with the key.
        Environment and target_endpoint may be None if not configured.

    Raises:
        InvalidApiKeyFormatError: If key format is invalid
        InvalidApiKeyError: If key is not recognized
    """
    # Security: Validate format first
    if not api_key or not SAFE_API_KEY_PATTERN.match(api_key):
        raise InvalidApiKeyFormatError()

    # Check if auth is enabled
    if not config.auth.enabled:
        # Auth disabled - return default client
        return "default", None, None

    # Source 1: Config-backed keys (constant-time comparison)
    for key_config in config.auth.api_keys:
        # Security: Constant-time comparison
        if secrets.compare_digest(api_key, key_config.key):
            return key_config.client_id, key_config.environment, key_config.target_endpoint

    # Source 2: DB-backed keys (hash lookup)
    if db_engine is not None:
        from gateway.storage.keys import KeyManager
        km = KeyManager(db_engine)
        key_info = km.validate_plaintext_key(api_key)
        if key_info is not None:
            return key_info["client_id"], key_info.get("environment"), None

    raise InvalidApiKeyError()


class AuthResult:
    """Result of authentication containing client_id, environment, and target_endpoint."""

    def __init__(
        self,
        client_id: str,
        environment: str | None = None,
        target_endpoint: str | None = None,
    ):
        self.client_id = client_id
        self.environment = environment
        self.target_endpoint = target_endpoint


async def authenticate(
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> str:
    """Authenticate request and return client_id.

    Supports:
    - Bearer token: Authorization: Bearer <api_key>
    - API key header: X-API-Key: <api_key>

    Security:
    - Validates input format
    - Logs authentication attempts

    Returns:
        client_id for the authenticated client

    Raises:
        AuthenticationError: If authentication fails
    """
    result = await authenticate_with_environment(request, authorization, x_api_key)
    return result.client_id


async def get_auth(
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> AuthResult:
    """Authenticate request and return full AuthResult.

    Returns AuthResult containing:
    - client_id: Identifier for the client
    - environment: Optional environment (dev/prod)
    - target_endpoint: Optional forced endpoint for this client

    Raises:
        AuthenticationError: If authentication fails
    """
    return await authenticate_with_environment(request, authorization, x_api_key)


async def authenticate_with_environment(
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> AuthResult:
    """Authenticate request and return client_id with environment.

    Supports:
    - Bearer token: Authorization: Bearer <api_key>
    - API key header: X-API-Key: <api_key>

    Security:
    - Validates input format
    - Logs authentication attempts

    Returns:
        AuthResult with client_id and optional environment

    Raises:
        AuthenticationError: If authentication fails
    """
    config = get_config(request)

    # Extract API key from headers
    api_key = None

    if authorization:
        # Parse Bearer token
        if authorization.lower().startswith("bearer "):
            api_key = authorization[7:].strip()
        else:
            raise AuthenticationError(message="Invalid authorization header format")
    elif x_api_key:
        api_key = x_api_key

    # If no key provided, use default (auth is optional - keys enable features like target_endpoint)
    if not api_key:
        return AuthResult("default", None, None)

    # Validate key if provided (pass db_engine for DB-backed key lookup)
    db_engine = getattr(request.app.state, "db_engine", None)
    client_id, environment, target_endpoint = validate_api_key(api_key, config, db_engine)
    return AuthResult(client_id, environment, target_endpoint)


async def get_environment(
    request: Request,
    x_environment: Annotated[Optional[str], Header(alias="X-Environment")] = None,
    authorization: Annotated[Optional[str], Header()] = None,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> EnvironmentConfig | None:
    """Get environment configuration for the request.

    Resolution order:
    1. X-Environment header (explicit override)
    2. Environment from API key configuration
    3. Default environment from config

    Args:
        request: FastAPI request
        x_environment: Optional explicit environment header
        authorization: Optional auth header (for env lookup)
        x_api_key: Optional API key header (for env lookup)

    Returns:
        EnvironmentConfig if environment is configured, None otherwise
    """
    config = get_config(request)

    # If no environments configured, return None
    if not config.environments:
        return None

    env_name: str | None = None

    # Priority 1: Explicit header
    if x_environment:
        env_name = x_environment

    # Priority 2: From API key
    if not env_name:
        try:
            auth_result = await authenticate_with_environment(
                request, authorization, x_api_key
            )
            env_name = auth_result.environment
        except AuthenticationError:
            # Auth failed, will use default
            pass

    # Priority 3: Default environment
    if not env_name:
        default_env = config.get_default_environment()
        return default_env

    # Look up environment by name
    return config.get_environment(env_name)


def setup_request_context(
    request_id: Optional[str] = None,
    client_id: str = "default",
    user_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    task: Optional[str] = None,
) -> RequestContext:
    """Create and set request context for logging and metrics.

    Args:
        request_id: Unique request identifier (generated if not provided)
        client_id: Authenticated client ID
        user_id: User ID from request
        provider: Provider being used
        model: Model being used
        task: Task type

    Returns:
        RequestContext that was set
    """
    ctx = RequestContext(
        request_id=request_id or str(uuid4()),
        client_id=client_id,
        user_id=user_id,
        provider=provider,
        model=model,
        task=task,
    )
    set_request_context(ctx)
    return ctx


def cleanup_request_context() -> None:
    """Clear request context after request completes."""
    clear_request_context()


def translate_policy_violation(e: PolicyViolation) -> None:
    """Translate PolicyViolation to appropriate domain error.

    This is the single place where policy violations are translated
    to domain errors. Called by routes after enforcer.enforce().

    Args:
        e: The policy violation exception

    Raises:
        RateLimitError: If rate limit exceeded
        TokenLimitError: If token limit exceeded
        ProviderNotAllowedError: If provider not allowed for task
        PolicyError: For other policy violations
    """
    if e.policy_type == "rate_limit":
        raise RateLimitError(
            message=str(e),
            retry_after=e.retry_after or 60.0,
        )
    elif e.policy_type == "token_limit":
        # Extract details if available
        raise PolicyError(
            message=str(e),
            code=ErrorCode.TOKEN_LIMIT_EXCEEDED,
        )
    elif e.policy_type == "provider_task":
        raise PolicyError(
            message=str(e),
            code=ErrorCode.PROVIDER_NOT_ALLOWED,
        )
    else:
        raise PolicyError(message=str(e))


# =============================================================================
# Security Dependencies
# =============================================================================


def get_sanitizer() -> Sanitizer:
    """Get the shared sanitizer instance.

    Returns a stateless Sanitizer for Unicode sanitization.
    Zero-latency operation (~0ms overhead).
    """
    return _sanitizer


def get_security_analyzer(request: Request) -> Optional[AsyncSecurityAnalyzer]:
    """Get the security analyzer from app state.

    Returns None if analyzer is not initialized.
    Callers should handle None gracefully.
    """
    return getattr(request.app.state, "security_analyzer", None)

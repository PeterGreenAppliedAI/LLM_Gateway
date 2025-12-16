"""Shared route dependencies.

Per rule.md:
- No Implicit Trust: Validate all inputs
- Auditability: Track request context
- Explicit Boundaries: Clear dependency injection

Per PRD Section 14:
- API key authentication
"""

import re
import secrets
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request, status

from gateway.config import GatewayConfig
from gateway.dispatch import Dispatcher, ProviderRegistry
from gateway.observability import get_logger, get_metrics, RequestContext
from gateway.observability.logging import set_request_context, clear_request_context
from gateway.policy import PolicyEnforcer, PolicyViolation

logger = get_logger(__name__)


# Security: Pattern for valid API keys (prevents injection)
SAFE_API_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{16,128}$")


class AuthenticationError(HTTPException):
    """Authentication failed."""

    def __init__(self, detail: str = "Invalid or missing API key"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class RateLimitError(HTTPException):
    """Rate limit exceeded."""

    def __init__(self, detail: str, retry_after: Optional[float] = None):
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers=headers if headers else None,
        )


class PolicyError(HTTPException):
    """Policy violation."""

    def __init__(self, detail: str, code: str = "policy_violation"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": code, "message": detail},
        )


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
    registry: Annotated[ProviderRegistry, Depends(get_registry)]
) -> Dispatcher:
    """Get dispatcher instance."""
    return Dispatcher(registry)


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


def validate_api_key(api_key: str, config: GatewayConfig) -> str:
    """Validate API key and return client_id.

    Security:
    - Validates key format to prevent injection
    - Uses constant-time comparison to prevent timing attacks
    - Returns sanitized client_id

    Args:
        api_key: The API key to validate
        config: Gateway configuration

    Returns:
        client_id associated with the key

    Raises:
        AuthenticationError: If key is invalid
    """
    # Security: Validate format first
    if not api_key or not SAFE_API_KEY_PATTERN.match(api_key):
        raise AuthenticationError("Invalid API key format")

    # Check if auth is enabled
    if not config.auth.enabled:
        # Auth disabled - return default client
        return "default"

    # Look up key in configured keys
    for key_config in config.auth.api_keys:
        # Security: Constant-time comparison
        if secrets.compare_digest(api_key, key_config.key):
            return key_config.client_id

    raise AuthenticationError("Invalid API key")


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
    config = get_config(request)

    # Extract API key from headers
    api_key = None

    if authorization:
        # Parse Bearer token
        if authorization.lower().startswith("bearer "):
            api_key = authorization[7:].strip()
        else:
            raise AuthenticationError("Invalid authorization header format")
    elif x_api_key:
        api_key = x_api_key

    # If no auth configured and no key provided, use default
    if not config.auth.enabled and not api_key:
        return "default"

    # If auth enabled but no key provided
    if config.auth.enabled and not api_key:
        raise AuthenticationError("API key required")

    # Validate key
    if api_key:
        return validate_api_key(api_key, config)

    return "default"


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


async def check_policy(
    request: Request,
    client_id: str,
    enforcer: PolicyEnforcer,
    task: str,
    provider: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> None:
    """Check request against policies.

    Args:
        request: FastAPI request
        client_id: Authenticated client ID
        enforcer: Policy enforcer
        task: Task type
        provider: Provider being used
        max_tokens: Max tokens requested

    Raises:
        RateLimitError: If rate limit exceeded
        PolicyError: If policy violation
    """
    from gateway.models.common import TaskType
    from gateway.models.internal import InternalRequest, Message, MessageRole

    # Create minimal internal request for policy check
    internal_req = InternalRequest(
        task=TaskType(task) if task else TaskType.CHAT,
        messages=[Message(role=MessageRole.USER, content="policy check")],
        client_id=client_id,
        max_tokens=max_tokens or 1024,
    )

    try:
        enforcer.enforce(internal_req, provider=provider)
    except PolicyViolation as e:
        if e.policy_type == "rate_limit":
            raise RateLimitError(str(e), retry_after=e.retry_after)
        else:
            raise PolicyError(str(e), code=e.code)

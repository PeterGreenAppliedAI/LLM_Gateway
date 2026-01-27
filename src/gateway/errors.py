"""Canonical domain errors for the gateway.

Per API Error Handling Architecture (Design Principles Section 8):
- Domain Truth: Define canonical error codes independent of transport
- All errors inherit from GatewayError base class
- Error codes are defined as enums for consistency
- No HTTP/transport coupling at this layer

These domain errors are translated to HTTP responses at the boundary
by the exception handler middleware.
"""

from enum import Enum
from typing import Optional


class ErrorCode(str, Enum):
    """Canonical error codes for the gateway.

    These codes are transport-agnostic and used for:
    - Logging and metrics
    - Client error identification
    - Consistent error categorization
    """

    # Authentication & Authorization
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_API_KEY = "invalid_api_key"
    INVALID_API_KEY_FORMAT = "invalid_api_key_format"

    # Rate Limiting
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    BURST_LIMIT_EXCEEDED = "burst_limit_exceeded"

    # Token Limits
    TOKEN_LIMIT_EXCEEDED = "token_limit_exceeded"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"

    # Policy
    POLICY_VIOLATION = "policy_violation"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"

    # Dispatch & Routing
    NO_PROVIDER = "no_provider"
    PROVIDER_NOT_FOUND = "provider_not_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    ALL_PROVIDERS_UNAVAILABLE = "all_providers_unavailable"
    DISPATCH_ERROR = "dispatch_error"
    AMBIGUOUS_MODEL = "ambiguous_model"
    MODEL_NOT_FOUND = "model_not_found"
    ENDPOINT_NOT_FOUND = "endpoint_not_found"

    # Validation
    VALIDATION_ERROR = "validation_error"
    INVALID_REQUEST = "invalid_request"

    # Provider Errors
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"

    # Internal
    INTERNAL_ERROR = "internal_error"
    STREAM_ERROR = "stream_error"
    CONFIGURATION_ERROR = "configuration_error"

    # Resource
    NOT_FOUND = "not_found"


class ErrorCategory(str, Enum):
    """Error categories for grouping and metrics."""

    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    POLICY = "policy"
    DISPATCH = "dispatch"
    VALIDATION = "validation"
    PROVIDER = "provider"
    INTERNAL = "internal"


class GatewayError(Exception):
    """Base exception for all gateway domain errors.

    All gateway errors should inherit from this class.
    The exception handler middleware catches these and translates
    them to appropriate HTTP responses.

    Attributes:
        message: Human-readable error message
        code: Canonical error code from ErrorCode enum
        category: Error category for grouping
        details: Optional additional details (must be JSON-serializable)
        retry_after: Optional seconds until retry is allowed (for rate limits)
    """

    def __init__(
        self,
        message: str,
        code: ErrorCode,
        category: ErrorCategory,
        details: Optional[dict] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.category = category
        self.details = details or {}
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        """Convert error to dictionary for JSON response."""
        result = {
            "error": {
                "code": self.code.value,
                "message": self.message,
            }
        }
        if self.details:
            result["error"]["details"] = self.details
        return result


# =============================================================================
# Authentication Errors
# =============================================================================


class AuthenticationError(GatewayError):
    """Authentication failed."""

    def __init__(
        self,
        message: str = "Authentication required",
        code: ErrorCode = ErrorCode.AUTHENTICATION_REQUIRED,
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=code,
            category=ErrorCategory.AUTHENTICATION,
            details=details,
        )


class InvalidApiKeyError(AuthenticationError):
    """Invalid API key provided."""

    def __init__(self, message: str = "Invalid API key"):
        super().__init__(
            message=message,
            code=ErrorCode.INVALID_API_KEY,
        )


class InvalidApiKeyFormatError(AuthenticationError):
    """API key format is invalid."""

    def __init__(self, message: str = "Invalid API key format"):
        super().__init__(
            message=message,
            code=ErrorCode.INVALID_API_KEY_FORMAT,
        )


# =============================================================================
# Rate Limit Errors
# =============================================================================


class RateLimitError(GatewayError):
    """Rate limit exceeded."""

    def __init__(
        self,
        message: str,
        retry_after: float,
        code: ErrorCode = ErrorCode.RATE_LIMIT_EXCEEDED,
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=code,
            category=ErrorCategory.RATE_LIMIT,
            details=details,
            retry_after=retry_after,
        )


# =============================================================================
# Policy Errors
# =============================================================================


class PolicyError(GatewayError):
    """Policy violation."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.POLICY_VIOLATION,
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=code,
            category=ErrorCategory.POLICY,
            details=details,
        )


class TokenLimitError(PolicyError):
    """Token limit exceeded."""

    def __init__(
        self,
        message: str,
        requested: int,
        limit: int,
        limit_type: str,
    ):
        super().__init__(
            message=message,
            code=ErrorCode.TOKEN_LIMIT_EXCEEDED,
            details={
                "requested": requested,
                "limit": limit,
                "limit_type": limit_type,
            },
        )


class ProviderNotAllowedError(PolicyError):
    """Provider not allowed for task."""

    def __init__(self, message: str, provider: str, task: str):
        super().__init__(
            message=message,
            code=ErrorCode.PROVIDER_NOT_ALLOWED,
            details={"provider": provider, "task": task},
        )


# =============================================================================
# Dispatch Errors
# =============================================================================


class DispatchError(GatewayError):
    """Error during request dispatch."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.DISPATCH_ERROR,
        provider: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        details = details or {}
        if provider:
            details["provider"] = provider
        super().__init__(
            message=message,
            code=code,
            category=ErrorCategory.DISPATCH,
            details=details,
        )


class NoProviderError(DispatchError):
    """No provider available."""

    def __init__(self, message: str = "No provider specified and no default configured"):
        super().__init__(
            message=message,
            code=ErrorCode.NO_PROVIDER,
        )


class ProviderNotFoundError(DispatchError):
    """Provider not found."""

    def __init__(self, provider: str):
        super().__init__(
            message=f"Provider '{provider}' not found",
            code=ErrorCode.PROVIDER_NOT_FOUND,
            provider=provider,
        )


class ProviderUnavailableError(DispatchError):
    """Provider unavailable."""

    def __init__(self, provider: str, fallback_disabled: bool = False):
        message = f"Provider '{provider}' unavailable"
        if fallback_disabled:
            message += " and fallback disabled"
        super().__init__(
            message=message,
            code=ErrorCode.PROVIDER_UNAVAILABLE,
            provider=provider,
            details={"fallback_disabled": fallback_disabled},
        )


class AllProvidersUnavailableError(DispatchError):
    """All providers unavailable."""

    def __init__(self, attempted: list[str]):
        super().__init__(
            message=f"All providers unavailable. Tried: {', '.join(attempted)}",
            code=ErrorCode.ALL_PROVIDERS_UNAVAILABLE,
            details={"attempted_providers": attempted},
        )


class AmbiguousModelError(DispatchError):
    """Model is available on multiple endpoints with no default configured."""

    def __init__(self, model: str, endpoints: list[str]):
        super().__init__(
            message=f"Model '{model}' is available on multiple endpoints: {endpoints}. "
            "Configure a model_default or use explicit endpoint/model syntax.",
            code=ErrorCode.AMBIGUOUS_MODEL,
            details={"model": model, "available_endpoints": endpoints},
        )


class ModelNotFoundError(DispatchError):
    """Model not found on any endpoint."""

    def __init__(self, model: str, searched_endpoints: list[str] | None = None):
        details: dict = {"model": model}
        if searched_endpoints:
            details["searched_endpoints"] = searched_endpoints
        super().__init__(
            message=f"Model '{model}' not found on any available endpoint",
            code=ErrorCode.MODEL_NOT_FOUND,
            details=details,
        )


class EndpointNotFoundError(DispatchError):
    """Explicitly requested endpoint not found."""

    def __init__(self, endpoint: str):
        super().__init__(
            message=f"Endpoint '{endpoint}' not found",
            code=ErrorCode.ENDPOINT_NOT_FOUND,
            details={"endpoint": endpoint},
        )


# =============================================================================
# Validation Errors
# =============================================================================


class ValidationError(GatewayError):
    """Request validation failed."""

    def __init__(
        self,
        message: str,
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=ErrorCode.VALIDATION_ERROR,
            category=ErrorCategory.VALIDATION,
            details=details,
        )


# =============================================================================
# Provider Errors
# =============================================================================


class ProviderError(GatewayError):
    """Error from provider."""

    def __init__(
        self,
        message: str,
        provider: str,
        code: ErrorCode = ErrorCode.PROVIDER_ERROR,
        details: Optional[dict] = None,
    ):
        details = details or {}
        details["provider"] = provider
        super().__init__(
            message=message,
            code=code,
            category=ErrorCategory.PROVIDER,
            details=details,
        )


# =============================================================================
# Internal Errors
# =============================================================================


class InternalError(GatewayError):
    """Internal server error."""

    def __init__(
        self,
        message: str = "An unexpected error occurred",
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=ErrorCode.INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            details=details,
        )


class StreamError(GatewayError):
    """Error during streaming."""

    def __init__(
        self,
        message: str = "Stream interrupted",
        details: Optional[dict] = None,
    ):
        super().__init__(
            message=message,
            code=ErrorCode.STREAM_ERROR,
            category=ErrorCategory.INTERNAL,
            details=details,
        )

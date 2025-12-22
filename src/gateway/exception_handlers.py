"""Exception handlers for FastAPI application.

Per API Error Handling Architecture (Design Principles Section 8):
- Boundary Translation: Map domain errors to transport-specific responses
- Enforced Consistency: Single choke point for all error handling
- No custom error shapes scattered across routes

This module provides FastAPI exception handlers that translate
domain errors (GatewayError subclasses) to HTTP responses.
"""

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from gateway.errors import (
    GatewayError,
    ErrorCategory,
    ErrorCode,
    AuthenticationError,
    RateLimitError,
    PolicyError,
    DispatchError,
    ValidationError,
    ProviderError,
    InternalError,
)
from gateway.observability import get_logger, get_metrics
from gateway.observability.logging import get_request_context, clear_request_context

logger = get_logger(__name__)
metrics = get_metrics()


# =============================================================================
# Error Category to HTTP Status Code Mapping
# =============================================================================

CATEGORY_STATUS_MAP: dict[ErrorCategory, int] = {
    ErrorCategory.AUTHENTICATION: status.HTTP_401_UNAUTHORIZED,
    ErrorCategory.RATE_LIMIT: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCategory.POLICY: status.HTTP_403_FORBIDDEN,
    ErrorCategory.DISPATCH: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCategory.VALIDATION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ErrorCategory.PROVIDER: status.HTTP_502_BAD_GATEWAY,
    ErrorCategory.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
}

# Override specific error codes that need different status codes
CODE_STATUS_OVERRIDES: dict[ErrorCode, int] = {
    ErrorCode.NO_PROVIDER: status.HTTP_400_BAD_REQUEST,
    ErrorCode.PROVIDER_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
}


def get_status_code(error: GatewayError) -> int:
    """Get HTTP status code for a domain error.

    Uses error code override if available, otherwise falls back
    to category-based mapping.
    """
    if error.code in CODE_STATUS_OVERRIDES:
        return CODE_STATUS_OVERRIDES[error.code]
    return CATEGORY_STATUS_MAP.get(error.category, status.HTTP_500_INTERNAL_SERVER_ERROR)


# =============================================================================
# Exception Handlers
# =============================================================================


async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    """Handle all GatewayError exceptions.

    This is the single choke point for domain error → HTTP translation.
    Provides consistent error response format across all endpoints.
    """
    status_code = get_status_code(exc)

    # Log the error with context
    ctx = get_request_context()
    if ctx:
        ctx.record_error(exc.code.value, exc.message)

    # Record metrics
    metrics.record_error(
        provider=exc.details.get("provider", "unknown"),
        error_type=exc.code.value,
    )

    # Log based on severity
    if exc.category == ErrorCategory.INTERNAL:
        logger.exception(f"Internal error: {exc.message}")
    elif exc.category in (ErrorCategory.DISPATCH, ErrorCategory.PROVIDER):
        logger.warning(f"{exc.category.value} error: {exc.message}")
    else:
        logger.info(f"{exc.category.value} error: {exc.message}")

    # Build response headers
    headers = {}
    if exc.category == ErrorCategory.AUTHENTICATION:
        headers["WWW-Authenticate"] = "Bearer"
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(exc.retry_after))

    # Clear request context
    clear_request_context()

    return JSONResponse(
        status_code=status_code,
        content=exc.to_dict(),
        headers=headers if headers else None,
    )


async def pydantic_validation_error_handler(
    request: Request, exc: PydanticValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors.

    Converts Pydantic validation errors to our standard error format.
    """
    ctx = get_request_context()
    if ctx:
        ctx.record_error("validation_error", str(exc))

    logger.info(f"Validation error: {exc}")

    clear_request_context()

    # Convert Pydantic errors to JSON-serializable format
    # exc.errors() may contain non-serializable objects
    validation_errors = []
    for error in exc.errors():
        clean_error = {
            "loc": error.get("loc", []),
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        validation_errors.append(clean_error)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": ErrorCode.VALIDATION_ERROR.value,
                "message": "Request validation failed",
                "details": {"validation_errors": validation_errors},
            }
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle any unhandled exceptions.

    This is the fallback handler for any exception not caught by
    more specific handlers. Logs the full exception and returns
    a generic error to avoid leaking internals.
    """
    ctx = get_request_context()
    if ctx:
        ctx.record_error("internal_error", str(exc))

    logger.exception(f"Unhandled exception: {exc}")

    clear_request_context()

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "An unexpected error occurred",
            }
        },
    )


# =============================================================================
# Registration Helper
# =============================================================================


def register_exception_handlers(app) -> None:
    """Register all exception handlers with a FastAPI app.

    Call this from main.py to set up centralized error handling.

    Args:
        app: FastAPI application instance
    """
    # Register domain error handler for base class (catches all subclasses)
    app.add_exception_handler(GatewayError, gateway_error_handler)

    # Register Pydantic validation error handler
    app.add_exception_handler(PydanticValidationError, pydantic_validation_error_handler)

    # Register fallback handler for unhandled exceptions
    app.add_exception_handler(Exception, unhandled_exception_handler)

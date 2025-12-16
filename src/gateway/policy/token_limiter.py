"""Token limiter - enforces max tokens per request.

Per rule.md:
- Single Responsibility: Only handles token limit enforcement
- Explicit Boundaries: Clear validation contract
- No Implicit Trust: Validate all token counts

Per PRD Section 10: Max tokens per request is a required policy.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field


class TokenLimitExceeded(Exception):
    """Token limit has been exceeded."""

    def __init__(
        self,
        message: str,
        requested: int,
        limit: int,
        limit_type: str,
    ):
        super().__init__(message)
        self.requested = requested
        self.limit = limit
        self.limit_type = limit_type


class TokenLimitConfig(BaseModel):
    """Configuration for token limits."""

    enabled: bool = Field(default=True, description="Whether token limiting is enabled")
    max_tokens_per_request: int = Field(
        default=4096,
        ge=1,
        le=128000,
        description="Maximum tokens allowed per request",
    )
    max_context_tokens: int = Field(
        default=32000,
        ge=1,
        le=256000,
        description="Maximum context/input tokens allowed",
    )
    default_max_tokens: int = Field(
        default=1024,
        ge=1,
        le=32000,
        description="Default max_tokens if not specified in request",
    )


@dataclass
class TokenLimitResult:
    """Result of token limit check."""

    allowed: bool
    max_tokens: int
    context_limit: int
    adjusted_max_tokens: Optional[int] = None  # If we capped the request


class TokenLimiter:
    """Enforces token limits on requests.

    Validates:
    - max_tokens doesn't exceed configured limit
    - Provides default max_tokens if not specified
    - Context length validation (when token counts are known)
    """

    def __init__(self, config: Optional[TokenLimitConfig] = None):
        """Initialize token limiter.

        Args:
            config: Token limit configuration. Uses defaults if not provided.
        """
        self._config = config or TokenLimitConfig()

    @property
    def enabled(self) -> bool:
        """Check if token limiting is enabled."""
        return self._config.enabled

    @property
    def default_max_tokens(self) -> int:
        """Get default max_tokens value."""
        return self._config.default_max_tokens

    @property
    def max_tokens_per_request(self) -> int:
        """Get maximum allowed tokens per request."""
        return self._config.max_tokens_per_request

    def validate_max_tokens(self, requested_max_tokens: Optional[int]) -> int:
        """Validate and return max_tokens for a request.

        Args:
            requested_max_tokens: max_tokens from request, or None

        Returns:
            Validated max_tokens value (possibly capped or defaulted)

        Raises:
            TokenLimitExceeded: If requested tokens exceed limit and limiting is strict
        """
        if not self._config.enabled:
            return requested_max_tokens or self._config.default_max_tokens

        # Use default if not specified
        if requested_max_tokens is None:
            return self._config.default_max_tokens

        # Cap at maximum allowed
        if requested_max_tokens > self._config.max_tokens_per_request:
            raise TokenLimitExceeded(
                f"Requested max_tokens ({requested_max_tokens}) exceeds limit ({self._config.max_tokens_per_request})",
                requested=requested_max_tokens,
                limit=self._config.max_tokens_per_request,
                limit_type="max_tokens_per_request",
            )

        # Ensure positive
        if requested_max_tokens < 1:
            return self._config.default_max_tokens

        return requested_max_tokens

    def validate_context_length(self, token_count: int) -> None:
        """Validate context/input token count.

        Args:
            token_count: Number of tokens in context/input

        Raises:
            TokenLimitExceeded: If context exceeds limit
        """
        if not self._config.enabled:
            return

        if token_count > self._config.max_context_tokens:
            raise TokenLimitExceeded(
                f"Context length ({token_count} tokens) exceeds limit ({self._config.max_context_tokens})",
                requested=token_count,
                limit=self._config.max_context_tokens,
                limit_type="max_context_tokens",
            )

    def check(
        self,
        requested_max_tokens: Optional[int] = None,
        context_tokens: Optional[int] = None,
    ) -> TokenLimitResult:
        """Check token limits without raising exceptions.

        Args:
            requested_max_tokens: max_tokens from request
            context_tokens: Known context token count (optional)

        Returns:
            TokenLimitResult with validation status
        """
        if not self._config.enabled:
            return TokenLimitResult(
                allowed=True,
                max_tokens=requested_max_tokens or self._config.default_max_tokens,
                context_limit=self._config.max_context_tokens,
            )

        # Check max_tokens
        effective_max_tokens = requested_max_tokens or self._config.default_max_tokens
        adjusted = None

        if effective_max_tokens > self._config.max_tokens_per_request:
            adjusted = self._config.max_tokens_per_request
            effective_max_tokens = adjusted

        # Check context if provided
        context_ok = True
        if context_tokens is not None:
            context_ok = context_tokens <= self._config.max_context_tokens

        return TokenLimitResult(
            allowed=context_ok and (adjusted is None or requested_max_tokens is None),
            max_tokens=effective_max_tokens,
            context_limit=self._config.max_context_tokens,
            adjusted_max_tokens=adjusted,
        )

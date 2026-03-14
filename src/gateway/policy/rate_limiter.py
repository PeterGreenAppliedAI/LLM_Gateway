"""Rate limiter - sliding window rate limiting per client/user.

Per rule.md:
- Single Responsibility: Only handles rate limiting logic
- Explicit Boundaries: Clear input (key, window) and output (allowed/denied)
- Contracts: RateLimitExceeded exception for violations
- No Implicit Trust: Validate keys to prevent injection attacks

Uses a simple in-memory sliding window approach. For distributed deployments,
swap in a Redis-backed implementation.
"""

import re
import time
from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, Field

# Maximum unique keys to track (prevents memory exhaustion)
MAX_TRACKED_KEYS = 10000

# Safe key pattern - alphanumeric, hyphens, underscores, max 128 chars
SAFE_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class RateLimitExceeded(Exception):
    """Rate limit has been exceeded."""

    def __init__(
        self,
        message: str,
        key: str,
        limit: int,
        window_seconds: int,
        retry_after: float,
    ):
        super().__init__(message)
        self.key = key
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after


class RateLimitConfig(BaseModel):
    """Configuration for rate limiting."""

    enabled: bool = Field(default=True, description="Whether rate limiting is enabled")
    requests_per_minute: int = Field(
        default=60,
        ge=1,
        le=10000,
        description="Max requests per minute per key",
    )
    requests_per_hour: int = Field(
        default=1000,
        ge=1,
        le=100000,
        description="Max requests per hour per key",
    )
    burst_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max requests in short burst (10 seconds)",
    )


@dataclass
class RateLimitState:
    """Current rate limit state for a key."""

    requests_remaining_minute: int
    requests_remaining_hour: int
    burst_remaining: int
    reset_minute: float
    reset_hour: float
    reset_burst: float


class RateLimiter:
    """In-memory sliding window rate limiter.

    Tracks requests per key (typically client_id or user_id) across
    multiple time windows: burst (10s), minute, and hour.

    Security:
    - Keys are validated against SAFE_KEY_PATTERN to prevent injection
    - Maximum tracked keys is bounded to prevent memory exhaustion

    Thread-safe for single-process deployments. For distributed,
    use a Redis-backed implementation.
    """

    # Window sizes in seconds
    BURST_WINDOW = 10
    MINUTE_WINDOW = 60
    HOUR_WINDOW = 3600

    def __init__(self, config: RateLimitConfig | None = None):
        """Initialize rate limiter.

        Args:
            config: Rate limit configuration. Uses defaults if not provided.
        """
        self._config = config or RateLimitConfig()

        # Track request timestamps per key
        # Key -> list of timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _sanitize_key(self, key: str) -> str:
        """Sanitize rate limit key to prevent injection attacks.

        Security: Keys are used in logs and potentially metrics.
        Invalid keys are replaced with a hash to prevent injection.
        """
        if SAFE_KEY_PATTERN.match(key):
            return key
        # For invalid keys, use a safe hash representation
        import hashlib

        return f"hashed_{hashlib.sha256(key.encode()).hexdigest()[:16]}"

    def _check_key_limit(self) -> None:
        """Check if we've hit the maximum tracked keys limit.

        Security: Prevents memory exhaustion from tracking too many unique keys.
        When limit is reached, oldest entries are cleaned up.
        """
        if len(self._requests) >= MAX_TRACKED_KEYS:
            # Clean up oldest entries (those with oldest last request)
            now = time.time()
            # Remove keys with no recent requests
            stale_keys = [
                k
                for k, timestamps in self._requests.items()
                if not timestamps or (now - max(timestamps)) > self.HOUR_WINDOW
            ]
            for k in stale_keys[: len(stale_keys) // 2 + 1]:  # Remove at least half
                del self._requests[k]

    @property
    def enabled(self) -> bool:
        """Check if rate limiting is enabled."""
        return self._config.enabled

    def check(self, key: str) -> RateLimitState:
        """Check current rate limit state without recording a request.

        Args:
            key: Identifier for rate limiting (client_id, user_id, IP)

        Returns:
            Current rate limit state for the key
        """
        key = self._sanitize_key(key)
        now = time.time()
        self._cleanup_old_requests(key, now)

        requests = self._requests[key]

        # Count requests in each window
        burst_count = sum(1 for ts in requests if now - ts < self.BURST_WINDOW)
        minute_count = sum(1 for ts in requests if now - ts < self.MINUTE_WINDOW)
        hour_count = sum(1 for ts in requests if now - ts < self.HOUR_WINDOW)

        return RateLimitState(
            requests_remaining_minute=max(0, self._config.requests_per_minute - minute_count),
            requests_remaining_hour=max(0, self._config.requests_per_hour - hour_count),
            burst_remaining=max(0, self._config.burst_limit - burst_count),
            reset_minute=now + self.MINUTE_WINDOW,
            reset_hour=now + self.HOUR_WINDOW,
            reset_burst=now + self.BURST_WINDOW,
        )

    def acquire(self, key: str, rpm_override: int | None = None) -> RateLimitState:
        """Record a request and check if it's allowed.

        Args:
            key: Identifier for rate limiting
            rpm_override: Per-key requests-per-minute override (from API key config)

        Returns:
            Updated rate limit state

        Raises:
            RateLimitExceeded: If any rate limit is exceeded
        """
        if not self._config.enabled:
            rpm = rpm_override or self._config.requests_per_minute
            return RateLimitState(
                requests_remaining_minute=rpm,
                requests_remaining_hour=self._config.requests_per_hour,
                burst_remaining=self._config.burst_limit,
                reset_minute=time.time() + self.MINUTE_WINDOW,
                reset_hour=time.time() + self.HOUR_WINDOW,
                reset_burst=time.time() + self.BURST_WINDOW,
            )

        key = self._sanitize_key(key)
        self._check_key_limit()

        now = time.time()
        self._cleanup_old_requests(key, now)

        requests = self._requests[key]

        # Use per-key RPM override if provided, otherwise config default
        effective_rpm = rpm_override or self._config.requests_per_minute

        # Count requests in each window
        burst_count = sum(1 for ts in requests if now - ts < self.BURST_WINDOW)
        minute_count = sum(1 for ts in requests if now - ts < self.MINUTE_WINDOW)
        hour_count = sum(1 for ts in requests if now - ts < self.HOUR_WINDOW)

        # Check burst limit first (most restrictive for spikes)
        if burst_count >= self._config.burst_limit:
            oldest_burst = min((ts for ts in requests if now - ts < self.BURST_WINDOW), default=now)
            retry_after = self.BURST_WINDOW - (now - oldest_burst)
            raise RateLimitExceeded(
                f"Burst limit exceeded: {self._config.burst_limit} requests per {self.BURST_WINDOW}s",
                key=key,
                limit=self._config.burst_limit,
                window_seconds=self.BURST_WINDOW,
                retry_after=max(0.1, retry_after),
            )

        # Check minute limit (uses per-key override)
        if minute_count >= effective_rpm:
            oldest_minute = min(
                (ts for ts in requests if now - ts < self.MINUTE_WINDOW), default=now
            )
            retry_after = self.MINUTE_WINDOW - (now - oldest_minute)
            raise RateLimitExceeded(
                f"Rate limit exceeded: {effective_rpm} requests per minute",
                key=key,
                limit=effective_rpm,
                window_seconds=self.MINUTE_WINDOW,
                retry_after=max(0.1, retry_after),
            )

        # Check hour limit
        if hour_count >= self._config.requests_per_hour:
            oldest_hour = min((ts for ts in requests if now - ts < self.HOUR_WINDOW), default=now)
            retry_after = self.HOUR_WINDOW - (now - oldest_hour)
            raise RateLimitExceeded(
                f"Rate limit exceeded: {self._config.requests_per_hour} requests per hour",
                key=key,
                limit=self._config.requests_per_hour,
                window_seconds=self.HOUR_WINDOW,
                retry_after=max(0.1, retry_after),
            )

        # Record this request
        requests.append(now)

        return RateLimitState(
            requests_remaining_minute=effective_rpm - minute_count - 1,
            requests_remaining_hour=self._config.requests_per_hour - hour_count - 1,
            burst_remaining=self._config.burst_limit - burst_count - 1,
            reset_minute=now + self.MINUTE_WINDOW,
            reset_hour=now + self.HOUR_WINDOW,
            reset_burst=now + self.BURST_WINDOW,
        )

    def reset(self, key: str) -> None:
        """Reset rate limit state for a key.

        Args:
            key: Identifier to reset
        """
        key = self._sanitize_key(key)
        if key in self._requests:
            del self._requests[key]

    def reset_all(self) -> None:
        """Reset all rate limit state."""
        self._requests.clear()

    def _cleanup_old_requests(self, key: str, now: float) -> None:
        """Remove requests older than the largest window.

        Args:
            key: Identifier to clean up
            now: Current timestamp
        """
        if key in self._requests:
            cutoff = now - self.HOUR_WINDOW
            self._requests[key] = [ts for ts in self._requests[key] if ts > cutoff]

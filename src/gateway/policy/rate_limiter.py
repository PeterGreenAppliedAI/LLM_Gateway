"""Rate limiter - sliding window rate limiting per client/user.

Per rule.md:
- Single Responsibility: Only handles rate limiting logic
- Explicit Boundaries: Clear input (key, window) and output (allowed/denied)
- Contracts: RateLimitExceeded exception for violations
- No Implicit Trust: Validate keys to prevent injection attacks

The limiter owns the policy (limits per key, messages, retry hints); a
RateWindowStore (gateway.state) does the counting: in this process by
default, or in Redis with GATEWAY_REDIS_URL so every gateway process
shares the same windows (D-035).
"""

import hashlib
import math
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from gateway.state.ratelimit import InMemoryRateStore, RateWindowStore

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
    """Sliding-window rate limiter: burst (10s), minute and hour windows per key.

    Security:
    - Keys are validated against SAFE_KEY_PATTERN to prevent injection
    - The in-memory store bounds how many keys it tracks
    """

    # Window sizes in seconds
    BURST_WINDOW = 10
    MINUTE_WINDOW = 60
    HOUR_WINDOW = 3600

    def __init__(self, config: RateLimitConfig | None = None, store: RateWindowStore | None = None):
        self._config = config or RateLimitConfig()
        self._store: RateWindowStore = store or InMemoryRateStore()

    def _sanitize_key(self, key: str) -> str:
        """Sanitize rate limit key to prevent injection attacks.

        Security: Keys are used in logs, metrics and store keys.
        Invalid keys are replaced with a hash to prevent injection.
        """
        if SAFE_KEY_PATTERN.match(key):
            return key
        return f"hashed_{hashlib.sha256(key.encode()).hexdigest()[:16]}"

    @property
    def enabled(self) -> bool:
        """Check if rate limiting is enabled."""
        return self._config.enabled

    def limits_for(self, rpm_override: int | None) -> tuple[int, int, int]:
        """(burst, per-minute, per-hour) limits for a key (D-033).

        A per-key RPM override scales the burst and hourly limits by the
        same factor. Before, a key granted 600 RPM still hit the global
        10-per-10s burst cap (60 RPM in practice) and the 1000/hour cap.
        """
        base_rpm = self._config.requests_per_minute
        rpm = rpm_override or base_rpm
        if rpm == base_rpm:
            return self._config.burst_limit, rpm, self._config.requests_per_hour
        scale = rpm / base_rpm
        burst = max(1, math.ceil(self._config.burst_limit * scale))
        hour = max(rpm, math.ceil(self._config.requests_per_hour * scale))
        return burst, rpm, hour

    def _state(self, limits: tuple[int, int, int], counts: list[int]) -> RateLimitState:
        now = time.time()
        burst, rpm, hour = limits
        return RateLimitState(
            requests_remaining_minute=max(0, rpm - counts[1]),
            requests_remaining_hour=max(0, hour - counts[2]),
            burst_remaining=max(0, burst - counts[0]),
            reset_minute=now + self.MINUTE_WINDOW,
            reset_hour=now + self.HOUR_WINDOW,
            reset_burst=now + self.BURST_WINDOW,
        )

    async def check(self, key: str, rpm_override: int | None = None) -> RateLimitState:
        """Current rate limit state for a key, without recording a request."""
        counts = await self._store.counts(
            self._sanitize_key(key), (self.BURST_WINDOW, self.MINUTE_WINDOW, self.HOUR_WINDOW)
        )
        return self._state(self.limits_for(rpm_override), counts)

    async def acquire(self, key: str, rpm_override: int | None = None) -> RateLimitState:
        """Record a request and check if it's allowed.

        Args:
            key: Identifier for rate limiting
            rpm_override: Per-key requests-per-minute override (from API key config)

        Raises:
            RateLimitExceeded: If any rate limit is exceeded
        """
        limits = self.limits_for(rpm_override)
        if not self._config.enabled:
            return self._state(limits, [0, 0, 0])

        key = self._sanitize_key(key)
        burst, rpm, hour = limits
        windows = (
            (self.BURST_WINDOW, burst),
            (self.MINUTE_WINDOW, rpm),
            (self.HOUR_WINDOW, hour),
        )
        result = await self._store.hit(key, windows)
        if not result.allowed:
            window, limit = windows[result.exceeded or 0]
            if window == self.BURST_WINDOW:
                message = f"Burst limit exceeded: {limit} requests per {window}s"
            elif window == self.MINUTE_WINDOW:
                message = f"Rate limit exceeded: {limit} requests per minute"
            else:
                message = f"Rate limit exceeded: {limit} requests per hour"
            raise RateLimitExceeded(
                message,
                key=key,
                limit=limit,
                window_seconds=window,
                retry_after=max(0.1, result.retry_after),
            )
        # The request just recorded counts against what's left
        return self._state(limits, [c + 1 for c in result.counts])

    async def reset(self, key: str) -> None:
        """Reset rate limit state for a key."""
        await self._store.reset(self._sanitize_key(key))

    async def reset_all(self) -> None:
        """Reset all rate limit state."""
        await self._store.reset_all()

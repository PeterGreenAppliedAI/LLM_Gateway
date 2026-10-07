"""Per-endpoint circuit breaker.

Replaces the in-request health check: an endpoint marked unhealthy used to
get a blocking health probe (up to 10 s) inside every request routed to
it, so N queued requests meant N probes against a dead box, each adding
latency. Streaming didn't check health at all.

States:
- closed: requests flow. Consecutive retryable failures (connection
  errors, timeouts, upstream 5xx; never 4xx) count up; at the threshold
  the circuit opens.
- open: requests skip the endpoint instantly. After the cooldown the next
  request is let through as a probe (half-open).
- half-open: exactly one probe in flight; its success closes the circuit,
  its failure reopens it for another cooldown. A probe that never reports
  back (a bug, a hung client) stops blocking after one more cooldown, so
  the endpoint can't be stranded half-open.

The background health loop feeds the same breaker: a healthy check closes
it, an unhealthy check opens it.
"""

import time
from dataclasses import dataclass, field
from enum import Enum

from gateway.config import CircuitBreakerConfig


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False
    probe_started_at: float = 0.0

    def allow_request(self) -> bool:
        """Whether a request may use this endpoint now (claims the probe if half-open)."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.opened_at < self.config.cooldown_seconds:
                return False
            self.state = CircuitState.HALF_OPEN
            self.probe_in_flight = False
        now = time.monotonic()
        if self.probe_in_flight and now - self.probe_started_at < self.config.cooldown_seconds:
            return False
        self.probe_in_flight = True
        self.probe_started_at = now
        return True

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.probe_in_flight = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if (
            self.state == CircuitState.HALF_OPEN
            or self.consecutive_failures >= self.config.failure_threshold
        ):
            self.trip()

    def trip(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = time.monotonic()
        self.probe_in_flight = False

    def release_probe(self) -> None:
        """A half-open probe ended without a verdict (e.g. upstream 4xx, client left)."""
        self.probe_in_flight = False

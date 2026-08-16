"""Admission queue for embedding requests.

Embedding traffic is bursty (graph-memory pipelines fire hundreds of
small requests), cheap, and latency-tolerant — rejecting bursts with 429
punishes exactly the workload that could simply wait. This queue holds
embedding requests and drains them at the rate limiter's pace instead of
bouncing them.

Chat/generate traffic is NOT queued: an interactive request that can't
run now should fail fast, not sit in line.

Bounds (both configurable):
- max_pending: waiting-room size; beyond it requests get an immediate
  429 with Retry-After (backpressure, not memory growth)
- max_wait_seconds: per-request deadline in the queue; exceeded -> 429

FIFO fairness comes from serializing admission attempts on an asyncio
lock — waiters acquire in arrival order.
"""

import asyncio
import time
from collections.abc import Callable

from pydantic import BaseModel, Field

from gateway.observability import get_logger
from gateway.policy.enforcer import PolicyViolation
from gateway.policy.rate_limiter import RateLimitExceeded

logger = get_logger(__name__)


def _rate_limit_retry_after(exc: Exception) -> float | None:
    """retry_after if exc is a rate-limit rejection, else None."""
    if isinstance(exc, RateLimitExceeded):
        return exc.retry_after or 1.0
    if isinstance(exc, PolicyViolation) and exc.policy_type == "rate_limit":
        return exc.retry_after or 1.0
    return None


class EmbeddingQueueConfig(BaseModel):
    """Configuration for the embedding admission queue."""

    enabled: bool = Field(default=True)
    max_pending: int = Field(
        default=500, ge=1, le=10000, description="Waiting-room size before immediate 429"
    )
    max_wait_seconds: float = Field(
        default=30.0, gt=0, le=600.0, description="Per-request queue deadline before 429"
    )


class QueueSaturatedError(Exception):
    """Queue full or wait deadline exceeded — caller should return 429."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class EmbeddingQueue:
    """Paces embedding admissions at the rate limiter's drain rate.

    `admit(acquire)` calls `acquire()` (which must raise
    RateLimitExceeded with a retry_after when the rate limit is hit) and,
    instead of propagating the rejection, sleeps and retries until it
    succeeds or the deadline passes. Non-rate-limit errors from
    `acquire()` propagate immediately (budget/allowlist violations must
    not be retried into acceptance).
    """

    def __init__(self, config: EmbeddingQueueConfig | None = None):
        self._config = config or EmbeddingQueueConfig()
        self._pending = 0
        # Serializes admission attempts -> approximate FIFO drain
        self._order_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def pending(self) -> int:
        return self._pending

    async def admit(self, acquire: Callable[[], None]) -> None:
        """Admit a request, waiting for rate-limit headroom if needed.

        Raises:
            QueueSaturatedError: Queue full or deadline exceeded (-> 429)
            Exception: whatever non-rate-limit error `acquire` raises
        """
        if not self._config.enabled:
            acquire()
            return

        if self._pending >= self._config.max_pending:
            raise QueueSaturatedError(
                f"Embedding queue full ({self._config.max_pending} pending)",
                retry_after=self._config.max_wait_seconds,
            )

        self._pending += 1
        started = time.monotonic()
        deadline = started + self._config.max_wait_seconds
        try:
            async with self._order_lock:
                while True:
                    try:
                        acquire()
                        waited = time.monotonic() - started
                        if waited > 0.5:
                            logger.info(
                                "Embedding request admitted after queue wait",
                                waited_seconds=round(waited, 2),
                                pending=self._pending,
                            )
                        return
                    except (RateLimitExceeded, PolicyViolation) as e:
                        retry_after = _rate_limit_retry_after(e)
                        if retry_after is None:
                            # Budget/allowlist violations must fail, not wait
                            raise
                        now = time.monotonic()
                        pause = min(max(retry_after, 0.1), 5.0)
                        if now + pause > deadline:
                            raise QueueSaturatedError(
                                f"Embedding request waited {now - started:.1f}s "
                                f"(limit {self._config.max_wait_seconds}s) without headroom",
                                retry_after=pause,
                            ) from e
                        await asyncio.sleep(pause)
        finally:
            self._pending -= 1

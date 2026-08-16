"""Tests for the embedding admission queue.

Embedding bursts wait for rate-limit headroom (bounded) instead of
getting 429s; non-rate policy violations still fail immediately.
"""

import pytest

from gateway.policy.embedding_queue import (
    EmbeddingQueue,
    EmbeddingQueueConfig,
    QueueSaturatedError,
)
from gateway.policy.enforcer import PolicyViolation
from gateway.policy.rate_limiter import RateLimitExceeded


def rate_limited(retry_after: float = 0.05) -> RateLimitExceeded:
    return RateLimitExceeded(
        "Rate limit exceeded",
        key="test",
        limit=100,
        window_seconds=60,
        retry_after=retry_after,
    )


class TestEmbeddingQueue:
    @pytest.mark.asyncio
    async def test_immediate_admission(self):
        queue = EmbeddingQueue()
        calls = []
        await queue.admit(lambda: calls.append(1))
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_waits_for_headroom_then_admits(self):
        """A rate-limited request waits and retries instead of 429ing."""
        queue = EmbeddingQueue(EmbeddingQueueConfig(max_wait_seconds=5.0))
        attempts = []

        def acquire():
            attempts.append(1)
            if len(attempts) < 3:
                raise rate_limited(retry_after=0.02)

        await queue.admit(acquire)
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_policy_violation_rate_limit_also_waits(self):
        """Routes raise PolicyViolation wrappers — same waiting behavior."""
        queue = EmbeddingQueue(EmbeddingQueueConfig(max_wait_seconds=5.0))
        attempts = []

        def acquire():
            attempts.append(1)
            if len(attempts) < 2:
                raise PolicyViolation(
                    message="Rate limit exceeded",
                    policy_type="rate_limit",
                    code="rate_limit_exceeded",
                    retry_after=0.02,
                )

        await queue.admit(acquire)
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_non_rate_violations_fail_immediately(self):
        """Budget/allowlist violations must not be retried into acceptance."""
        queue = EmbeddingQueue()
        attempts = []

        def acquire():
            attempts.append(1)
            raise PolicyViolation(
                message="Model not allowed",
                policy_type="model_not_allowed",
                code="model_not_allowed",
            )

        with pytest.raises(PolicyViolation):
            await queue.admit(acquire)
        assert len(attempts) == 1

    @pytest.mark.asyncio
    async def test_deadline_exceeded_raises_saturated(self):
        """Persistent rate limiting past the deadline -> 429-shaped error."""
        queue = EmbeddingQueue(EmbeddingQueueConfig(max_wait_seconds=0.05))

        def acquire():
            raise rate_limited(retry_after=0.2)

        with pytest.raises(QueueSaturatedError) as exc_info:
            await queue.admit(acquire)
        assert exc_info.value.retry_after > 0

    @pytest.mark.asyncio
    async def test_queue_full_raises_saturated(self):
        queue = EmbeddingQueue(EmbeddingQueueConfig(max_pending=1, max_wait_seconds=5.0))
        queue._pending = 1  # simulate an occupied waiting room

        with pytest.raises(QueueSaturatedError):
            await queue.admit(lambda: None)

    @pytest.mark.asyncio
    async def test_disabled_queue_passes_through(self):
        """Disabled queue preserves old behavior: rejection propagates."""
        queue = EmbeddingQueue(EmbeddingQueueConfig(enabled=False))

        def acquire():
            raise rate_limited()

        with pytest.raises(RateLimitExceeded):
            await queue.admit(acquire)

    @pytest.mark.asyncio
    async def test_pending_count_returns_to_zero(self):
        queue = EmbeddingQueue()
        await queue.admit(lambda: None)
        assert queue.pending == 0

        with pytest.raises(QueueSaturatedError):
            bounded = EmbeddingQueue(EmbeddingQueueConfig(max_wait_seconds=0.05))

            def acquire():
                raise rate_limited(retry_after=0.2)

            await bounded.admit(acquire)
        assert bounded.pending == 0

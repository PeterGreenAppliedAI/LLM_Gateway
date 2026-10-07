"""Shared state: rate-limit windows and concurrency slots (D-010, D-035).

Default: in-memory, nothing else to run; each gateway process enforces its
own limits. Set GATEWAY_REDIS_URL to share them across processes and
replicas. If Redis becomes unreachable, each process falls back to its own
counts until it's back (limits stay enforced, per process), and /health
reports the store as degraded.
"""

from dataclasses import dataclass, field
from typing import Any

from gateway.observability import get_logger
from gateway.state.concurrency import (
    ConcurrencyBackend,
    FallbackConcurrency,
    InMemoryConcurrency,
    Lease,
    Priority,
    RedisConcurrency,
    StoreHealth,
    slot_limit,
)
from gateway.state.ratelimit import (
    FallbackRateStore,
    InMemoryRateStore,
    RateWindowStore,
    RedisRateStore,
    WindowResult,
)

logger = get_logger(__name__)


@dataclass
class SharedState:
    backend: str  # "memory" | "redis"
    endpoint_slots: ConcurrencyBackend
    key_slots: ConcurrencyBackend
    rate_windows: RateWindowStore
    health: StoreHealth | None = None
    _client: Any = field(default=None, repr=False)

    @property
    def status(self) -> dict:
        """For /health: which store, and whether it's currently usable."""
        degraded = self.health.degraded if self.health else False
        return {"backend": self.backend, "status": "degraded" if degraded else "ok"}

    async def close(self) -> None:
        await self.endpoint_slots.close()
        await self.key_slots.close()
        if self._client is not None:
            await self._client.aclose()


def in_memory_state(batch_max_share: float = 1.0) -> SharedState:
    return SharedState(
        backend="memory",
        endpoint_slots=InMemoryConcurrency(batch_max_share=batch_max_share),
        key_slots=InMemoryConcurrency(),
        rate_windows=InMemoryRateStore(),
    )


def create_shared_state(
    redis_url: str | None,
    prefix: str = "devmesh",
    batch_max_share: float = 1.0,
    lease_ttl_seconds: float = 30.0,
) -> SharedState:
    """In-memory state, or Redis-backed with in-memory fallback when a URL is given."""
    if not redis_url:
        return in_memory_state(batch_max_share)

    try:
        import redis.asyncio as redis
    except ImportError as e:
        raise RuntimeError(
            "GATEWAY_REDIS_URL is set but the redis package is not installed: "
            "pip install 'devmesh-gateway[redis]'"
        ) from e

    # Short timeouts: a slow Redis must not add seconds to every request;
    # after a failure the store is skipped for a cooldown (StoreHealth)
    client = redis.from_url(
        redis_url, socket_connect_timeout=0.5, socket_timeout=0.5, health_check_interval=30
    )
    health = StoreHealth("redis")
    memory = in_memory_state(batch_max_share)
    state = SharedState(
        backend="redis",
        endpoint_slots=FallbackConcurrency(
            RedisConcurrency(
                client, prefix, "slots", batch_max_share, lease_ttl_seconds=lease_ttl_seconds
            ),
            memory.endpoint_slots,  # type: ignore[arg-type]
            health,
        ),
        key_slots=FallbackConcurrency(
            RedisConcurrency(client, prefix, "keyslots", lease_ttl_seconds=lease_ttl_seconds),
            memory.key_slots,  # type: ignore[arg-type]
            health,
        ),
        rate_windows=FallbackRateStore(RedisRateStore(client, prefix), InMemoryRateStore(), health),
        health=health,
        _client=client,
    )
    logger.info("Shared state in Redis", prefix=prefix)
    return state


__all__ = [
    "ConcurrencyBackend",
    "InMemoryConcurrency",
    "InMemoryRateStore",
    "Lease",
    "Priority",
    "RateWindowStore",
    "RedisConcurrency",
    "RedisRateStore",
    "SharedState",
    "StoreHealth",
    "WindowResult",
    "create_shared_state",
    "in_memory_state",
    "slot_limit",
]

"""Concurrency slots: endpoint admission (D-032) and per-key limits (D-034).

One interface, three implementations (D-035):

- InMemoryConcurrency: the default. Counts live in this process; nothing to
  run. With several gateway processes each enforces its own limits.
- RedisConcurrency: opt-in with GATEWAY_REDIS_URL. Every process shares the
  same counts, so `max_concurrent: 4` means 4 across all of them.
- FallbackConcurrency: Redis first; if Redis is unreachable, the in-memory
  counts take over (limits become per process again) until it's back.

A freed slot goes to waiting interactive requests before batch ones, and
batch may hold at most `batch_max_share` of a limited endpoint (D-034).
"""

import asyncio
import contextlib
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from gateway.observability import get_logger

logger = get_logger(__name__)

Priority = Literal["interactive", "batch"]
PRIORITIES: tuple[Priority, ...] = ("interactive", "batch")


def slot_limit(capacity: int | None, priority: Priority, batch_share: float) -> int | None:
    """Slots a request of this priority may fill: all of them, or batch's share (at least 1)."""
    if capacity is None or priority == "interactive":
        return capacity
    return max(1, int(capacity * batch_share))


class Lease(ABC):
    """One held slot. Release it exactly once when the work ends (idempotent)."""

    endpoint: str

    @property
    @abstractmethod
    def released(self) -> bool: ...

    @abstractmethod
    async def release(self) -> None: ...


class ConcurrencyBackend(Protocol):
    def set_capacity(self, name: str, capacity: int | None) -> None: ...

    def capacity(self, name: str) -> int | None:
        """max_concurrent, or None for unlimited."""

    async def try_acquire(self, name: str, priority: Priority = "interactive") -> Lease | None:
        """A slot now, or None if full for this priority."""

    async def acquire_any(
        self, names: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        """The first slot to free up on any of the names; None on timeout.

        Interactive waiters are served before batch waiters, each in arrival order.
        """

    async def in_flight_counts(self, names: Sequence[str]) -> dict[str, int]: ...

    def waiting(self) -> int:
        """Requests in this process queued for a slot."""

    async def close(self) -> None: ...


# =============================================================================
# In-memory (default)
# =============================================================================


class _MemoryLease(Lease):
    __slots__ = ("endpoint", "_on_release", "_released")

    def __init__(self, endpoint: str, on_release: Callable[[str], None]):
        self.endpoint = endpoint
        self._on_release = on_release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release_now(self) -> None:
        if self._released:
            return
        self._released = True
        self._on_release(self.endpoint)

    async def release(self) -> None:
        self.release_now()

    def __del__(self) -> None:
        # Backstop for a stream or response dropped without being closed
        # (e.g. the client left before the body started): never strand a slot
        if not self._released:
            logger.debug("Admission lease released by garbage collection", endpoint=self.endpoint)
            self.release_now()


@dataclass
class _Waiter:
    names: frozenset[str]
    priority: Priority
    future: asyncio.Future = field(repr=False)


class InMemoryConcurrency:
    """Per-process counts. A freed slot passes straight to the next waiter
    that can use it, so a newcomer can't jump the queue."""

    def __init__(
        self, capacities: dict[str, int | None] | None = None, batch_max_share: float = 1.0
    ):
        self._capacity: dict[str, int | None] = dict(capacities or {})
        self._in_flight: dict[str, int] = {}
        self._waiters: dict[Priority, deque[_Waiter]] = {p: deque() for p in PRIORITIES}
        self._batch_share = batch_max_share

    def set_capacity(self, name: str, capacity: int | None) -> None:
        self._capacity[name] = capacity

    def capacity(self, name: str) -> int | None:
        return self._capacity.get(name)

    def in_flight(self, name: str) -> int:
        return self._in_flight.get(name, 0)

    async def in_flight_counts(self, names: Sequence[str]) -> dict[str, int]:
        return {name: self.in_flight(name) for name in names}

    def waiting(self) -> int:
        return sum(1 for q in self._waiters.values() for w in q if not w.future.done())

    def _limit(self, name: str, priority: Priority) -> int | None:
        return slot_limit(self._capacity.get(name), priority, self._batch_share)

    def try_acquire_now(self, name: str, priority: Priority = "interactive") -> Lease | None:
        limit = self._limit(name, priority)
        if limit is not None and self.in_flight(name) >= limit:
            return None
        self._in_flight[name] = self.in_flight(name) + 1
        return _MemoryLease(name, self._release)

    async def try_acquire(self, name: str, priority: Priority = "interactive") -> Lease | None:
        return self.try_acquire_now(name, priority)

    async def acquire_any(
        self, names: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        for name in names:
            if lease := self.try_acquire_now(name, priority):
                return lease
        if timeout <= 0:
            return None

        waiter = _Waiter(frozenset(names), priority, asyncio.get_running_loop().create_future())
        self._waiters[priority].append(waiter)
        try:
            name = await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except asyncio.TimeoutError:  # TimeoutError on 3.11+
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                return _MemoryLease(waiter.future.result(), self._release)  # handed over in time
            return None
        except BaseException:
            # Cancelled (client left): a slot handed over in the same
            # instant must be passed on, not lost
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                self._release(waiter.future.result())
            raise
        return _MemoryLease(name, self._release)

    def _abandon(self, waiter: _Waiter) -> None:
        if not waiter.future.done():
            waiter.future.cancel()
        with contextlib.suppress(ValueError):
            self._waiters[waiter.priority].remove(waiter)

    def _release(self, name: str) -> None:
        # Hand the slot over, interactive waiters first. The count stays
        # the same: the slot changes hands. A batch waiter only takes it if
        # the endpoint would still be within the batch share without it.
        others = self.in_flight(name) - 1
        for priority in PRIORITIES:
            limit = self._limit(name, priority)
            if limit is not None and others >= limit:
                continue
            queue = self._waiters[priority]
            for waiter in list(queue):
                if waiter.future.done():
                    queue.remove(waiter)
                    continue
                if name in waiter.names:
                    queue.remove(waiter)
                    waiter.future.set_result(name)
                    return
        self._in_flight[name] = max(0, others)

    async def close(self) -> None:
        return None


# =============================================================================
# Redis (opt-in, shared across gateway processes)
# =============================================================================

# Slots are a sorted set per name: member = lease id, score = expiry (ms, the
# Redis server's clock so gateway hosts' clocks don't matter). Expired
# members are dropped before every count, so a crashed gateway's slots free
# themselves after the lease TTL; live leases are renewed by a heartbeat.
_NOW_MS = "local t = redis.call('TIME'); local now = t[1] * 1000 + math.floor(t[2] / 1000)\n"

_ACQUIRE = (
    _NOW_MS
    + """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local limit = tonumber(ARGV[1])
if limit >= 0 and redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[2])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
return 1
"""
)

_RENEW = (
    _NOW_MS
    + """
local renewed = redis.call('ZADD', KEYS[1], 'XX', 'CH', now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return renewed
"""
)

_COUNT = (
    _NOW_MS
    + """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""
)


class _RedisLease(Lease):
    def __init__(self, backend: "RedisConcurrency", endpoint: str, lease_id: str):
        self.endpoint = endpoint
        self.lease_id = lease_id
        self._backend = backend
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        # Releases often run while the request is being cancelled (client
        # left). Shielded, the Redis call completes even if this await is
        # interrupted, instead of leaving the slot held until the TTL.
        task = asyncio.ensure_future(self._backend._release(self))
        self._backend._pending.add(task)
        task.add_done_callback(self._backend._pending.discard)
        await asyncio.shield(task)

    def __del__(self) -> None:
        # Dropped without release: stop renewing it, so it expires after the TTL
        if not self._released:
            self._backend._live.pop(self.lease_id, None)


@dataclass
class _RedisWaiter:
    names: tuple[str, ...]
    priority: Priority
    future: asyncio.Future = field(repr=False)


class RedisConcurrency:
    """Slot counts shared by every gateway process using the same Redis.

    Waiting: requests queue in this process (interactive first, then
    arrival order) and retry when any process frees a slot (pub/sub) or
    every `poll_interval` seconds as a backstop. Ordering is exact within a
    process; across processes, the first to retry after a release wins.
    """

    def __init__(
        self,
        client: Any,
        prefix: str,
        namespace: str,
        batch_max_share: float = 1.0,
        lease_ttl_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ):
        self._client = client
        self._base = f"{prefix}:{namespace}"
        self._channel = f"{self._base}:freed"
        self._batch_share = batch_max_share
        self._ttl_ms = int(lease_ttl_seconds * 1000)
        self._poll = poll_interval
        self._capacity: dict[str, int | None] = {}
        self._acquire = client.register_script(_ACQUIRE)
        self._renew = client.register_script(_RENEW)
        self._count = client.register_script(_COUNT)
        self._live: dict[str, _RedisLease] = {}
        self._pending: set[asyncio.Task] = set()
        self._waiters: dict[Priority, deque[_RedisWaiter]] = {p: deque() for p in PRIORITIES}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._pump_task: asyncio.Task | None = None

    def _key(self, name: str) -> str:
        return f"{self._base}:{name}"

    def set_capacity(self, name: str, capacity: int | None) -> None:
        self._capacity[name] = capacity

    def capacity(self, name: str) -> int | None:
        return self._capacity.get(name)

    def waiting(self) -> int:
        return sum(1 for q in self._waiters.values() for w in q if not w.future.done())

    async def in_flight_counts(self, names: Sequence[str]) -> dict[str, int]:
        counts = {}
        for name in names:
            counts[name] = int(await self._count(keys=[self._key(name)]))
        return counts

    async def try_acquire(self, name: str, priority: Priority = "interactive") -> Lease | None:
        limit = slot_limit(self._capacity.get(name), priority, self._batch_share)
        lease_id = uuid.uuid4().hex
        admitted = await self._acquire(
            keys=[self._key(name)], args=[-1 if limit is None else limit, lease_id, self._ttl_ms]
        )
        if not admitted:
            return None
        lease = _RedisLease(self, name, lease_id)
        self._live[lease_id] = lease
        self._start_background()
        return lease

    async def _try_any(self, names: Sequence[str], priority: Priority) -> Lease | None:
        for name in names:
            if lease := await self.try_acquire(name, priority):
                return lease
        return None

    async def acquire_any(
        self, names: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        # Don't overtake requests already queued here for the same endpoints
        if not self._queued_ahead(names, priority):
            if lease := await self._try_any(names, priority):
                return lease
        if timeout <= 0:
            return None

        waiter = _RedisWaiter(tuple(names), priority, asyncio.get_running_loop().create_future())
        self._waiters[priority].append(waiter)
        self._start_background()
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except asyncio.TimeoutError:
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                return waiter.future.result()  # admitted just in time
            return None
        except BaseException:
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                await waiter.future.result().release()
            raise

    def _queued_ahead(self, names: Sequence[str], priority: Priority) -> bool:
        wanted = set(names)
        ahead = PRIORITIES[: PRIORITIES.index(priority) + 1]
        return any(
            not w.future.done() and wanted & set(w.names) for p in ahead for w in self._waiters[p]
        )

    def _abandon(self, waiter: _RedisWaiter) -> None:
        if not waiter.future.done():
            waiter.future.cancel()
        with contextlib.suppress(ValueError):
            self._waiters[waiter.priority].remove(waiter)

    async def _release(self, lease: _RedisLease) -> None:
        self._live.pop(lease.lease_id, None)
        try:
            await self._client.zrem(self._key(lease.endpoint), lease.lease_id)
            await self._client.publish(self._channel, lease.endpoint)
        except Exception as e:  # the TTL frees it if Redis is unreachable
            logger.warning("Redis slot release failed", endpoint=lease.endpoint, error=str(e))
        self._wake.set()

    # -- background: heartbeat, release notifications, waiter pump ---------

    def _start_background(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._heartbeat(), name="redis-slots-heartbeat"),
            asyncio.create_task(self._listen(), name="redis-slots-listen"),
        ]
        self._pump_task = asyncio.create_task(self._pump(), name="redis-slots-pump")
        self._tasks.append(self._pump_task)

    async def _heartbeat(self) -> None:
        interval = self._ttl_ms / 3000
        while True:
            await asyncio.sleep(interval)
            for lease in list(self._live.values()):
                try:
                    renewed = await self._renew(
                        keys=[self._key(lease.endpoint)], args=[lease.lease_id, self._ttl_ms]
                    )
                except Exception as e:
                    logger.warning("Redis lease renewal failed", error=str(e))
                    break
                if not renewed and not lease.released:
                    # Expired (this process stalled past the TTL): the slot
                    # may already be someone else's. Stop tracking it.
                    self._live.pop(lease.lease_id, None)
                    logger.warning(
                        "Redis slot lease expired before release", endpoint=lease.endpoint
                    )

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(self._channel)
                try:
                    while True:
                        message = await pubsub.get_message(timeout=self._poll)
                        if message is not None:
                            self._wake.set()
                finally:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Redis release channel unavailable; polling", error=str(e))
                await asyncio.sleep(self._poll)

    async def _pump(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll)
            self._wake.clear()
            for priority in PRIORITIES:
                queue = self._waiters[priority]
                for waiter in list(queue):
                    if waiter.future.done():
                        with contextlib.suppress(ValueError):
                            queue.remove(waiter)
                        continue
                    try:
                        lease = await self._try_any(waiter.names, priority)
                    except Exception as e:
                        logger.warning("Redis slot retry failed", error=str(e))
                        continue
                    if lease is None:
                        continue
                    with contextlib.suppress(ValueError):
                        queue.remove(waiter)
                    if waiter.future.done():  # gave up meanwhile: pass the slot on
                        await lease.release()
                    else:
                        waiter.future.set_result(lease)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks = []
        for lease in list(self._live.values()):
            with contextlib.suppress(Exception):
                await lease.release()


# =============================================================================
# Redis with in-memory fallback
# =============================================================================


def is_backend_error(exc: BaseException) -> bool:
    """Connection-level failures of the shared store (not logic errors)."""
    try:
        from redis.exceptions import RedisError
    except ImportError:  # pragma: no cover - redis not installed
        RedisError = ()  # type: ignore[assignment]  # noqa: N806
    return isinstance(exc, (RedisError, OSError, asyncio.TimeoutError))


class StoreHealth:
    """Whether the shared store is usable. After a failure it is skipped for
    `cooldown_seconds` (no per-request connection timeouts while it's down),
    then tried again."""

    def __init__(self, name: str, cooldown_seconds: float = 5.0):
        self.name = name
        self._cooldown = cooldown_seconds
        self._failed_at: float | None = None

    @property
    def degraded(self) -> bool:
        return self._failed_at is not None

    def use_primary(self) -> bool:
        return self._failed_at is None or time.monotonic() - self._failed_at >= self._cooldown

    def ok(self) -> None:
        if self._failed_at is not None:
            logger.info("Shared state store reachable again", store=self.name)
        self._failed_at = None

    def failed(self, exc: BaseException) -> None:
        if self._failed_at is None:
            logger.error(
                "Shared state store unreachable; enforcing limits per process until it's back",
                store=self.name,
                error=str(exc),
            )
        self._failed_at = time.monotonic()


class FallbackConcurrency:
    """Redis when reachable, else this process's own counts (as without Redis)."""

    def __init__(
        self, primary: RedisConcurrency, fallback: InMemoryConcurrency, health: StoreHealth
    ):
        self._primary = primary
        self._fallback = fallback
        self.health = health

    def set_capacity(self, name: str, capacity: int | None) -> None:
        self._primary.set_capacity(name, capacity)
        self._fallback.set_capacity(name, capacity)

    def capacity(self, name: str) -> int | None:
        return self._primary.capacity(name)

    def waiting(self) -> int:
        return self._primary.waiting() + self._fallback.waiting()

    async def _call(self, method: str, *args):
        if self.health.use_primary():
            try:
                result = await getattr(self._primary, method)(*args)
            except Exception as e:
                if not is_backend_error(e):
                    raise
                self.health.failed(e)
            else:
                self.health.ok()
                return result
        return await getattr(self._fallback, method)(*args)

    async def try_acquire(self, name: str, priority: Priority = "interactive") -> Lease | None:
        return await self._call("try_acquire", name, priority)

    async def acquire_any(
        self, names: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        return await self._call("acquire_any", names, timeout, priority)

    async def in_flight_counts(self, names: Sequence[str]) -> dict[str, int]:
        return await self._call("in_flight_counts", names)

    async def close(self) -> None:
        await self._primary.close()

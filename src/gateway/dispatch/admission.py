"""Per-endpoint admission control: concurrency limits with a FIFO wait (D-032).

An endpoint with `max_concurrent: N` serves at most N requests from this
gateway at once. A request routed to a full endpoint overflows to the next
candidate with room; when every candidate is full it waits, first come
first served, until one frees up or `admission.max_queue_wait_seconds`
passes (then 503 + Retry-After). Endpoints without `max_concurrent` are
unlimited but still counted, for least-loaded routing and metrics.

A slot is held for the whole request: until a non-streaming response
returns, a stream closes, or a media body is fully relayed.

Priority (D-034): a freed slot goes to the oldest *interactive* waiter
first, then the oldest batch waiter. Batch requests may only hold up to
`batch_max_share` of an endpoint's slots, so there is always room for
interactive traffic even while a batch job saturates the queue. Without
that reserve, priority ordering alone wouldn't help: a long batch
generation already holding every slot can't be preempted.

The backend is an interface so the counts can move to a shared store
(Redis, D-010) when several gateway processes front the same engines;
in-memory counts are per process.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from gateway.observability import get_logger

logger = get_logger(__name__)


class Lease:
    """One admitted slot on an endpoint. Release exactly once (idempotent)."""

    __slots__ = ("endpoint", "_on_release", "_released")

    def __init__(self, endpoint: str, on_release: Callable[[str], None]):
        self.endpoint = endpoint
        self._on_release = on_release
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._on_release(self.endpoint)

    def __del__(self) -> None:
        # Backstop for a stream or response dropped without being closed
        # (e.g. the client left before the body started): never strand a slot
        if not self._released:
            logger.debug("Admission lease released by garbage collection", endpoint=self.endpoint)
            self.release()


Priority = Literal["interactive", "batch"]


class ConcurrencyBackend(Protocol):
    def try_acquire(self, endpoint: str, priority: Priority = "interactive") -> Lease | None:
        """A slot now, or None if the endpoint is full (for this priority)."""

    async def acquire_any(
        self, endpoints: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        """The first slot to free up on any of the endpoints; None on timeout.

        Interactive waiters are served before batch waiters, each in arrival order.
        """

    def in_flight(self, endpoint: str) -> int: ...

    def capacity(self, endpoint: str) -> int | None:
        """max_concurrent, or None for unlimited."""

    def waiting(self) -> int:
        """Requests currently queued for a slot."""


@dataclass
class _Waiter:
    endpoints: frozenset[str]
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
        self._waiters: dict[Priority, deque[_Waiter]] = {"interactive": deque(), "batch": deque()}
        self._batch_share = batch_max_share

    def set_capacity(self, endpoint: str, capacity: int | None) -> None:
        self._capacity[endpoint] = capacity

    def capacity(self, endpoint: str) -> int | None:
        return self._capacity.get(endpoint)

    def in_flight(self, endpoint: str) -> int:
        return self._in_flight.get(endpoint, 0)

    def waiting(self) -> int:
        return sum(1 for q in self._waiters.values() for w in q if not w.future.done())

    def _limit(self, endpoint: str, priority: Priority) -> int | None:
        cap = self._capacity.get(endpoint)
        if cap is None or priority == "interactive":
            return cap
        return max(1, int(cap * self._batch_share))

    def try_acquire(self, endpoint: str, priority: Priority = "interactive") -> Lease | None:
        limit = self._limit(endpoint, priority)
        if limit is not None and self.in_flight(endpoint) >= limit:
            return None
        self._in_flight[endpoint] = self.in_flight(endpoint) + 1
        return Lease(endpoint, self._release)

    async def acquire_any(
        self, endpoints: Sequence[str], timeout: float, priority: Priority = "interactive"
    ) -> Lease | None:
        for name in endpoints:
            if lease := self.try_acquire(name, priority):
                return lease
        if timeout <= 0:
            return None

        waiter = _Waiter(frozenset(endpoints), priority, asyncio.get_running_loop().create_future())
        self._waiters[priority].append(waiter)
        try:
            endpoint = await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except asyncio.TimeoutError:  # TimeoutError on 3.11+
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                return Lease(waiter.future.result(), self._release)  # handed over just in time
            return None
        except BaseException:
            # Cancelled (client left): a slot handed over in the same
            # instant must be passed on, not lost
            self._abandon(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                self._release(waiter.future.result())
            raise
        return Lease(endpoint, self._release)

    def _abandon(self, waiter: _Waiter) -> None:
        if not waiter.future.done():
            waiter.future.cancel()
        try:
            self._waiters[waiter.priority].remove(waiter)
        except ValueError:
            pass

    def _release(self, endpoint: str) -> None:
        # Hand the slot over, interactive waiters first. The count stays
        # the same: the slot changes hands. A batch waiter only takes it if
        # the endpoint would still be within the batch share without it.
        others = self.in_flight(endpoint) - 1
        for priority in ("interactive", "batch"):
            limit = self._limit(endpoint, priority)
            if limit is not None and others >= limit:
                continue
            queue = self._waiters[priority]
            for waiter in list(queue):
                if waiter.future.done():
                    queue.remove(waiter)
                    continue
                if endpoint in waiter.endpoints:
                    queue.remove(waiter)
                    waiter.future.set_result(endpoint)
                    return
        self._in_flight[endpoint] = max(0, others)

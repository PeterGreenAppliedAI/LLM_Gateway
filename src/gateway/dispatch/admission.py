"""Per-endpoint admission control: concurrency limits with a FIFO wait (D-032).

An endpoint with `max_concurrent: N` serves at most N requests at once. A
request routed to a full endpoint overflows to the next candidate with
room; when every candidate is full it waits, first come first served,
until one frees up or `admission.max_queue_wait_seconds` passes (then
503 + Retry-After). Endpoints without `max_concurrent` are unlimited but
still counted, for least-loaded routing and metrics.

A slot is held for the whole request: until a non-streaming response
returns, a stream closes, or a media body is fully relayed.

Priority (D-034): a freed slot goes to the oldest *interactive* waiter
first, then the oldest batch waiter. Batch requests may only hold up to
`batch_max_share` of an endpoint's slots, so there is always room for
interactive traffic even while a batch job saturates the queue.

The counts live behind gateway.state (D-035): in this process by default,
or in Redis (shared by every gateway process) with GATEWAY_REDIS_URL.
"""

from gateway.state.concurrency import (
    ConcurrencyBackend,
    InMemoryConcurrency,
    Lease,
    Priority,
)

__all__ = ["ConcurrencyBackend", "InMemoryConcurrency", "Lease", "Priority"]

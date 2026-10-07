"""asyncio helpers."""

import asyncio


async def wait_event(event: asyncio.Event, timeout: float) -> bool:
    """Wait up to `timeout` for `event`; True if it was set.

    Use this instead of `asyncio.wait_for(event.wait(), timeout)` in background
    loops. On Python before 3.12, wait_for returns normally when the event is
    set at the moment the task is cancelled (CPython gh-86296). The
    cancellation is lost, the loop keeps running, and whatever awaits the
    cancelled task (a `close()`) waits forever. `asyncio.wait` doesn't swallow
    cancellation.
    """
    if event.is_set():
        return True
    waiter = asyncio.ensure_future(event.wait())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
    finally:
        waiter.cancel()
    return bool(done)

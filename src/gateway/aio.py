"""asyncio helpers."""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


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


class Uninterruptible:
    """Database work in a background loop that cancelling the loop doesn't cut off.

    Cancelling a task in the middle of a database call is unsafe (D-046):

    - On Python 3.11 with SQLAlchemy and aiosqlite, the connection can be left
      unclosed, kept alive only by a reference cycle. On SQLite it holds the
      write lock until the garbage collector runs, so every other writer waits
      out the busy timeout and fails with "database is locked".
    - Work interrupted between taking rows and confirming them is lost.

    A loop wraps each unit of database work in `run()`. Its owner cancels the
    loop, then awaits `finish()`, so shutdown waits for the transaction in
    progress instead of abandoning it.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        self._task = asyncio.ensure_future(coro)
        return await asyncio.shield(self._task)

    async def finish(self, timeout: float | None = None) -> None:
        """Wait for work in progress, if any (its errors were the loop's to handle).

        With a timeout, work still running after it is cancelled after all:
        for shutdown, where the process exits next and SQLite releases its
        locks with it.
        """
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: no "exception never retrieved" warning

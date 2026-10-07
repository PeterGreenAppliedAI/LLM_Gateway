"""wait_event never loses a cancellation (Python < 3.12, CPython gh-86296)."""

import asyncio

import pytest

from gateway.aio import wait_event


@pytest.mark.asyncio
async def test_set_and_timeout():
    event = asyncio.Event()
    assert await wait_event(event, 0.01) is False
    event.set()
    assert await wait_event(event, 1) is True


@pytest.mark.asyncio
async def test_cancel_at_the_moment_the_event_is_set_stops_the_loop():
    """The Redis slot pump and the audit drainer hung close() this way on 3.10."""
    event = asyncio.Event()
    rounds = 0

    async def loop():
        nonlocal rounds
        while True:
            await wait_event(event, 5)
            event.clear()
            rounds += 1

    task = asyncio.create_task(loop())
    await asyncio.sleep(0.01)  # the loop is waiting
    event.set()
    task.cancel()  # same tick as the wake-up
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert task.cancelled()

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


@pytest.mark.asyncio
async def test_cancelling_the_loop_doesnt_cut_off_its_work():
    """D-046: database work must not be abandoned mid-transaction."""
    from gateway.aio import Uninterruptible

    work = Uninterruptible()
    started, finished = asyncio.Event(), []

    async def unit():
        started.set()
        await asyncio.sleep(0.05)
        finished.append(True)

    async def loop():
        while True:
            await work.run(unit())

    task = asyncio.create_task(loop())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == []  # the loop stopped; its work is still going
    await work.finish()
    assert finished == [True]


@pytest.mark.asyncio
async def test_finish_timeout_cancels_after_all():
    from gateway.aio import Uninterruptible

    work = Uninterruptible()
    runner = asyncio.create_task(work.run(asyncio.sleep(10)))
    await asyncio.sleep(0)
    runner.cancel()
    await work.finish(timeout=0.01)  # returns instead of waiting 10 s
    with pytest.raises(asyncio.CancelledError):
        await runner

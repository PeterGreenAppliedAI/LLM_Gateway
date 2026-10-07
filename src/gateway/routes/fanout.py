"""Fan one client request out into several upstream generations.

The internal request/response model carries one output, but clients can
ask for several: `n` completions, or a list of prompts on /v1/completions.
Before this module both were silently reduced to one (extra prompts were
dropped, `n` was ignored). Each requested choice is now its own upstream
request, run concurrently and merged in order by the route.

Concurrent requests are what continuous-batching engines (vLLM, and Ollama
up to OLLAMA_NUM_PARALLEL) batch together, so this works the same on every
engine without adapter changes.
"""

import asyncio

from fastapi import Request

from gateway.dispatch import Dispatcher, DispatchResult
from gateway.errors import ValidationError
from gateway.models.internal import InternalRequest
from gateway.models.openai import MAX_CHOICES
from gateway.routes.dependencies import run_unless_disconnected

# Upstream requests one fan-out runs at once. Bounds how much of an
# endpoint a single client request can occupy.
MAX_CONCURRENT_PER_REQUEST = 8


def check_choice_count(count: int, *, stream: bool = False) -> None:
    """Validate how many choices a request asks for.

    Raises:
        ValidationError: Zero, more than MAX_CHOICES, or more than one while
            streaming (multiple choices are only supported non-streaming).
    """
    if count < 1:
        raise ValidationError(message="Request must contain at least one prompt")
    if count > MAX_CHOICES:
        raise ValidationError(
            message=f"Request asks for {count} choices; the maximum is {MAX_CHOICES} "
            "(number of prompts x n)"
        )
    if stream and count > 1:
        raise ValidationError(
            message="Streaming supports a single choice; use stream: false for n > 1"
        )


async def dispatch_choices(
    request: Request,
    dispatcher: Dispatcher,
    internal_requests: list[InternalRequest],
) -> list[DispatchResult]:
    """Dispatch requests concurrently; results are in input order.

    All-or-nothing, like a single request: if any choice fails, the rest
    are cancelled and the error propagates. A client disconnect cancels
    every in-flight upstream request.
    """
    if len(internal_requests) == 1:
        return [await run_unless_disconnected(request, dispatcher.dispatch(internal_requests[0]))]

    slots = asyncio.Semaphore(MAX_CONCURRENT_PER_REQUEST)

    async def one(internal_request: InternalRequest) -> DispatchResult:
        async with slots:
            return await dispatcher.dispatch(internal_request)

    tasks = [asyncio.ensure_future(one(r)) for r in internal_requests]
    try:
        return await run_unless_disconnected(request, asyncio.gather(*tasks))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

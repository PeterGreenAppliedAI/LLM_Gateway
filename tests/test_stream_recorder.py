"""Streamed responses record budget, audit and status on every exit path.

Regressions covered:
- streamed usage was never charged to token budgets
- a provider error chunk mid-stream was audited as "success"
- a client disconnect left no audit row and no budget charge
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.models.common import FinishReason, TaskType, UsageStats
from gateway.models.internal import InternalRequest, Message, MessageRole, StreamChunk
from gateway.observability.logging import RequestContext
from gateway.routes.ollama import _stream_ollama_chat, _stream_ollama_generate
from gateway.routes.openai import _stream_chat_response


def _request() -> InternalRequest:
    return InternalRequest(
        task=TaskType.CHAT,
        model="phi4:14b",
        client_id="app-1",
        stream=True,
        messages=[Message(role=MessageRole.USER, content="hi")],
    )


def _chunk(delta="", finish=None, usage=None) -> StreamChunk:
    return StreamChunk(request_id="r", index=0, delta=delta, finish_reason=finish, usage=usage)


def _audit_logger() -> AsyncMock:
    """Audit logger whose write suspends, like a real DB round-trip."""

    audit = AsyncMock()
    audit.completed = []

    async def write(*args, **kwargs):
        await asyncio.sleep(0.01)
        audit.completed.append(kwargs)

    audit.log_request = AsyncMock(side_effect=write)
    return audit


def _dispatcher(chunks, hang_after: bool = False):
    async def stream():
        for c in chunks:
            yield c
        if hang_after:
            await asyncio.Event().wait()

    dispatcher = MagicMock()
    dispatcher.dispatch_stream = AsyncMock(return_value=("gpu-1", stream()))
    return dispatcher


STREAM_FACTORIES = {
    "openai_chat": lambda d, audit, enforcer: _stream_chat_response(
        d, _request(), "phi4:14b", RequestContext(request_id="req-1"), audit, enforcer=enforcer
    ),
    "ollama_chat": lambda d, audit, enforcer: _stream_ollama_chat(
        d, _request(), "phi4:14b", RequestContext(request_id="req-1"), audit, enforcer=enforcer
    ),
    "ollama_generate": lambda d, audit, enforcer: _stream_ollama_generate(
        d, _request(), "phi4:14b", RequestContext(request_id="req-1"), audit, enforcer=enforcer
    ),
}


async def _drain(response) -> list:
    return [part async for part in response.body_iterator]


def _audit_kwargs(audit) -> dict:
    assert audit.log_request.await_count == 1
    assert len(audit.completed) == 1, "audit write started but did not complete"
    return audit.log_request.await_args.kwargs | dict(
        zip(
            ["request_id", "client_id", "task", "model", "endpoint", "status"],
            audit.log_request.await_args.args,
        )
    )


@pytest.mark.parametrize("route", STREAM_FACTORIES)
@pytest.mark.asyncio
async def test_success_charges_budget_and_audits(route):
    audit, enforcer = _audit_logger(), MagicMock()
    d = _dispatcher(
        [
            _chunk("Hel"),
            _chunk("lo"),
            _chunk(finish=FinishReason.STOP, usage=UsageStats.from_counts(prompt=5, completion=7)),
        ]
    )
    await _drain(await STREAM_FACTORIES[route](d, audit, enforcer))

    enforcer.record_token_usage.assert_called_once_with("app-1", "phi4:14b", 12)
    kwargs = _audit_kwargs(audit)
    assert kwargs["status"] == "success"
    assert kwargs["endpoint"] == "gpu-1"
    assert kwargs["response_body"] == {"content": "Hello"}


@pytest.mark.parametrize("route", STREAM_FACTORIES)
@pytest.mark.asyncio
async def test_midstream_error_is_not_success(route):
    audit, enforcer = _audit_logger(), MagicMock()
    d = _dispatcher([_chunk("partial"), _chunk(finish=FinishReason.ERROR)])
    await _drain(await STREAM_FACTORIES[route](d, audit, enforcer))

    kwargs = _audit_kwargs(audit)
    assert kwargs["status"] == "error"
    assert kwargs["error_code"] == "stream_error"
    # Generated tokens are still charged (estimated: one per content chunk)
    enforcer.record_token_usage.assert_called_once_with("app-1", "phi4:14b", 1)


@pytest.mark.parametrize("route", STREAM_FACTORIES)
@pytest.mark.asyncio
async def test_disconnect_via_aclose_is_recorded(route):
    """Client goes away: the server closes the body iterator early."""
    audit, enforcer = _audit_logger(), MagicMock()
    d = _dispatcher([_chunk("a"), _chunk("b"), _chunk("c")], hang_after=True)
    body = (await STREAM_FACTORIES[route](d, audit, enforcer)).body_iterator
    await body.__anext__()
    await body.aclose()

    kwargs = _audit_kwargs(audit)
    assert kwargs["status"] == "error"
    assert kwargs["error_code"] == "client_disconnected"
    enforcer.record_token_usage.assert_called_once()


@pytest.mark.parametrize("route", STREAM_FACTORIES)
@pytest.mark.asyncio
async def test_disconnect_via_cancellation_is_recorded(route):
    """Client goes away while the gateway awaits upstream: task is cancelled."""
    audit, enforcer = _audit_logger(), MagicMock()
    d = _dispatcher([_chunk("a")], hang_after=True)
    response = await STREAM_FACTORIES[route](d, audit, enforcer)

    task = asyncio.ensure_future(_drain(response))
    await asyncio.sleep(0.05)  # first chunk forwarded, now awaiting upstream
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    kwargs = _audit_kwargs(audit)
    assert kwargs["error_code"] == "client_disconnected"
    assert kwargs["response_body"]["content"] == "a"
    assert kwargs["response_body"]["completion_tokens_estimated"] is True


@pytest.mark.parametrize("route", STREAM_FACTORIES)
@pytest.mark.asyncio
async def test_disconnect_via_cancel_scope_is_recorded(route):
    """Starlette (ASGI < 2.4) cancels the stream through an anyio cancel
    scope, which re-cancels every later await: the audit write must be
    shielded or it never happens."""
    import anyio

    audit, enforcer = _audit_logger(), MagicMock()
    d = _dispatcher([_chunk("a")], hang_after=True)
    response = await STREAM_FACTORIES[route](d, audit, enforcer)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_drain, response)
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert _audit_kwargs(audit)["error_code"] == "client_disconnected"

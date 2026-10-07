"""Shared streaming helpers for provider adapters.

Before this module each adapter carried its own copy of the line reader
and the OpenAI-style SSE parser, and every failure became an anonymous
error chunk. That lost the cause ("model not found" looked the same as a
dead box, so 4xx errors failed over to every endpoint) and the copies
drifted (vLLM ignored finish_reason "tool_calls"; neither OpenAI-style
adapter parsed streamed tool calls).

Timeouts: the first line may take as long as the endpoint timeout (cold
model loads are slow). After that, a gap longer than the stream idle
timeout means the stream is dead, not slow.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx

from gateway.models.common import FinishReason, UsageStats
from gateway.models.internal import InternalRequest, StreamChunk, ToolCall


class StreamStalled(Exception):
    """No data arrived within the allowed window."""


async def iter_lines_with_timeouts(
    response: httpx.Response,
    first_timeout: float,
    idle_timeout: float,
) -> AsyncIterator[str]:
    """Yield response lines; the first non-empty line gets first_timeout,
    every later line idle_timeout.

    Raises:
        StreamStalled: If a deadline passes with no data.
    """
    lines = response.aiter_lines().__aiter__()
    timeout = first_timeout
    while True:
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            phase = "first data" if timeout == first_timeout else "next data"
            raise StreamStalled(f"Stream stalled: no {phase} for {timeout:g}s") from None
        if line.strip():
            timeout = idle_timeout
        yield line


async def upstream_http_error(response: httpx.Response) -> tuple[str, str]:
    """(error_code, message) for an upstream error status, from its body."""
    body = (await response.aread()).decode("utf-8", errors="replace")
    message = body[:500]
    try:
        data = json.loads(body)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            err = err.get("message") or err
        message = str(err)[:500]
    except (json.JSONDecodeError, AttributeError):
        pass
    return f"http_{response.status_code}", message or f"HTTP {response.status_code}"


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """(error_code, message) for an exception raised while streaming.

    Codes match the non-streaming adapters so the dispatcher applies the
    same failover rules: timeout/connection_error/unknown_error retry,
    http_4xx does not.
    """
    if isinstance(exc, StreamStalled):
        return "timeout", str(exc)
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout", f"Gateway connection pool exhausted: {exc}"
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "timeout", f"Timeout: {exc}" if str(exc) else "Timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error", f"Connection failed: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}", str(exc)
    return "unknown_error", f"{type(exc).__name__}: {exc}"


def error_chunk(request: InternalRequest, code: str, message: str, index: int = 0) -> StreamChunk:
    """Terminal error chunk carrying the failure's cause."""
    return StreamChunk(
        request_id=request.request_id,
        index=index,
        delta="",
        finish_reason=FinishReason.ERROR,
        error=message[:1000],
        error_code=code[:64],
    )


def _parse_arguments(arguments: str | dict | None) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": arguments}


async def parse_openai_sse(
    lines: AsyncIterator[str],
    request: InternalRequest,
    map_finish_reason: Callable[[str | None], FinishReason],
) -> AsyncIterator[StreamChunk]:
    """Turn an OpenAI-compatible SSE stream into StreamChunks.

    - Usage arrives after the finish frame (stream_options.include_usage),
      so the finish chunk is held back until usage or end of stream.
    - Tool calls arrive as fragments (id/name once, arguments in pieces)
      keyed by index; they are reassembled and attached to the finish
      chunk as complete calls with parsed arguments, matching Ollama.
    """
    index = 0
    pending_final: StreamChunk | None = None
    tool_fragments: dict[int, dict] = {}

    async for line in lines:
        if not line or line.startswith(":"):
            continue
        data = line[6:] if line.startswith("data: ") else line
        if data == "[DONE]":
            break
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            continue

        usage = None
        if frame.get("usage"):
            u = frame["usage"]
            usage = UsageStats(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
            )

        if frame.get("error"):
            err = frame["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            yield error_chunk(request, "upstream_error", message[:500], index)
            return

        choices = frame.get("choices") or []
        if not choices:
            # Usage-only frame
            if usage and pending_final:
                pending_final = pending_final.model_copy(update={"usage": usage})
            continue

        choice = choices[0]
        delta = choice.get("delta") or {}

        for frag in delta.get("tool_calls") or []:
            slot = tool_fragments.setdefault(
                frag.get("index", len(tool_fragments)), {"id": None, "name": "", "arguments": ""}
            )
            if frag.get("id"):
                slot["id"] = frag["id"]
            function = frag.get("function") or {}
            if function.get("name"):
                slot["name"] += function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]

        finish = choice.get("finish_reason")
        finish_reason = map_finish_reason(finish) if finish else None

        # Chat frames carry delta.content; /v1/completions frames carry text
        content = delta.get("content") or choice.get("text") or ""
        thinking = delta.get("reasoning_content") or delta.get("reasoning") or None
        if finish_reason is None and not content and not thinking:
            continue  # role-only or tool-fragment frame: nothing to forward yet

        chunk = StreamChunk(
            request_id=request.request_id,
            index=index,
            delta=content,
            thinking=thinking,
            finish_reason=finish_reason,
            usage=usage,
        )
        index += 1
        if finish_reason is not None:
            pending_final = chunk
            continue
        yield chunk

    if tool_fragments:
        calls = [
            ToolCall(
                id=slot["id"],
                type="function",
                function={"name": slot["name"], "arguments": _parse_arguments(slot["arguments"])},
            )
            for _, slot in sorted(tool_fragments.items())
        ]
        if pending_final is None:
            pending_final = StreamChunk(
                request_id=request.request_id,
                index=index,
                delta="",
                finish_reason=FinishReason.TOOL_CALLS,
            )
        pending_final = pending_final.model_copy(update={"tool_calls": calls})

    if pending_final is None:
        # Stream ended cleanly without a finish frame: still close it out,
        # or the route would record the request as a client disconnect
        pending_final = StreamChunk(
            request_id=request.request_id, index=index, delta="", finish_reason=FinishReason.STOP
        )
    yield pending_final

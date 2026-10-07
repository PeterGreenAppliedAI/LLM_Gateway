"""Streaming correctness (capacity plan phase 1).

Regressions covered:
- every stream failure was an anonymous error chunk, so 4xx errors failed
  over to every endpoint and the final 503 had no cause
- streams the dispatcher abandoned were never closed
- failures before the first chunk returned HTTP 200 with an error event
- the gap allowed between chunks was max(120s, endpoint timeout), up to 1h
- OpenAI-style adapters dropped streamed tool calls; vLLM ignored
  finish_reason "tool_calls"; the OpenAI route disabled streaming for tools
- OpenAI stream choice index counted chunks instead of staying 0
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import EndpointConfig, GatewayConfig, ProviderConfig, ResolutionConfig
from gateway.dispatch import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import AllProvidersUnavailableError, ProviderError
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType
from gateway.models.internal import (
    InternalRequest,
    Message,
    MessageRole,
    StreamChunk,
    ToolCall,
)
from gateway.providers.ollama import OllamaAdapter
from gateway.providers.streaming import (
    StreamStalled,
    classify_exception,
    iter_lines_with_timeouts,
    parse_openai_sse,
    upstream_http_error,
)
from gateway.providers.vllm import VLLMAdapter
from gateway.routes import ollama_router, openai_router
from gateway.routes.dependencies import get_dispatcher


def _request(model: str = "m:1", **kw) -> InternalRequest:
    return InternalRequest(
        task=TaskType.CHAT,
        model=model,
        stream=True,
        messages=[Message(role=MessageRole.USER, content="hi")],
        **kw,
    )


async def _aiter(items):
    for item in items:
        yield item


async def _collect(it) -> list:
    return [x async for x in it]


# =============================================================================
# Shared helpers
# =============================================================================


class _SlowLines:
    """Fake response whose lines arrive after given delays."""

    def __init__(self, timed_lines):
        self._timed_lines = timed_lines

    async def aiter_lines(self):
        for delay, line in self._timed_lines:
            await asyncio.sleep(delay)
            yield line


class TestTimeouts:
    @pytest.mark.asyncio
    async def test_slow_first_line_allowed(self):
        lines = iter_lines_with_timeouts(
            _SlowLines([(0.05, "a"), (0.0, "b")]), first_timeout=1.0, idle_timeout=0.01
        )
        assert await _collect(lines) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_stall_after_first_line_fails_fast(self):
        lines = iter_lines_with_timeouts(
            _SlowLines([(0.0, "a"), (0.2, "b")]), first_timeout=5.0, idle_timeout=0.05
        )
        with pytest.raises(StreamStalled, match="next data"):
            await _collect(lines)

    def test_stall_classified_as_retryable_timeout(self):
        assert classify_exception(StreamStalled("x"))[0] == "timeout"


class TestUpstreamHttpError:
    @pytest.mark.asyncio
    async def test_ollama_style_error_body(self):
        response = httpx.Response(404, json={"error": "model 'nope' not found"})
        assert await upstream_http_error(response) == ("http_404", "model 'nope' not found")

    @pytest.mark.asyncio
    async def test_openai_style_error_body(self):
        response = httpx.Response(400, json={"error": {"message": "bad tools", "type": "x"}})
        assert await upstream_http_error(response) == ("http_400", "bad tools")


class TestParseOpenAISSE:
    @staticmethod
    def _frames(*frames) -> list[str]:
        return [f"data: {json.dumps(f)}" for f in frames] + ["data: [DONE]"]

    @staticmethod
    def _finish(reason):
        return FinishReason.TOOL_CALLS if reason == "tool_calls" else FinishReason.STOP

    @pytest.mark.asyncio
    async def test_tool_call_fragments_reassembled(self):
        lines = self._frames(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "get_weather", "arguments": '{"ci'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": 'ty":"Oslo"}'}}]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}},
        )
        chunks = await _collect(parse_openai_sse(_aiter(lines), _request(), self._finish))

        assert len(chunks) == 1
        final = chunks[0]
        assert final.finish_reason == FinishReason.TOOL_CALLS
        assert final.usage.completion_tokens == 9
        assert final.tool_calls == [
            ToolCall(id="call_a", function={"name": "get_weather", "arguments": {"city": "Oslo"}})
        ]

    @pytest.mark.asyncio
    async def test_stream_without_finish_still_closes(self):
        lines = self._frames({"choices": [{"delta": {"content": "hi"}}]})
        chunks = await _collect(parse_openai_sse(_aiter(lines), _request(), self._finish))
        assert [c.delta for c in chunks] == ["hi", ""]
        assert chunks[-1].finish_reason == FinishReason.STOP

    @pytest.mark.asyncio
    async def test_error_frame_becomes_error_chunk(self):
        lines = self._frames({"error": {"message": "CUDA out of memory"}})
        chunks = await _collect(parse_openai_sse(_aiter(lines), _request(), self._finish))
        assert chunks[0].finish_reason == FinishReason.ERROR
        assert chunks[0].error == "CUDA out of memory"


# =============================================================================
# Adapters
# =============================================================================


def _adapter(cls, ptype, handler):
    adapter = cls(ProviderConfig(name="ep", type=ptype, base_url="http://upstream:1"))
    adapter._client = httpx.AsyncClient(
        base_url="http://upstream:1", transport=httpx.MockTransport(handler)
    )
    return adapter


class TestAdapters:
    @pytest.mark.asyncio
    async def test_ollama_404_carries_cause(self):
        adapter = _adapter(
            OllamaAdapter,
            ProviderType.OLLAMA,
            lambda req: httpx.Response(404, json={"error": "model 'nope' not found"}),
        )
        chunks = await _collect(adapter.chat_stream(_request("nope")))
        assert chunks[0].finish_reason == FinishReason.ERROR
        assert chunks[0].error_code == "http_404"
        assert chunks[0].error == "model 'nope' not found"

    @pytest.mark.asyncio
    async def test_ollama_midstream_error_line(self):
        body = (
            json.dumps({"message": {"content": "par"}, "done": False})
            + "\n"
            + json.dumps({"error": "llama runner process has terminated"})
            + "\n"
        )
        adapter = _adapter(
            OllamaAdapter, ProviderType.OLLAMA, lambda req: httpx.Response(200, text=body)
        )
        chunks = await _collect(adapter.chat_stream(_request()))
        assert chunks[0].delta == "par"
        assert chunks[-1].finish_reason == FinishReason.ERROR
        assert "terminated" in chunks[-1].error

    @pytest.mark.asyncio
    async def test_vllm_streams_tool_calls(self):
        frames = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        adapter = _adapter(
            VLLMAdapter, ProviderType.VLLM, lambda req: httpx.Response(200, text=body)
        )
        chunks = await _collect(adapter.chat_stream(_request()))
        assert chunks[-1].finish_reason == FinishReason.TOOL_CALLS
        assert chunks[-1].tool_calls[0].function["name"] == "f"


# =============================================================================
# Dispatcher failover
# =============================================================================


class _TrackedStream:
    """Async iterator over chunks that records whether it was closed."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self):
        self.closed = True


def _err(code: str, message: str) -> StreamChunk:
    return StreamChunk(
        request_id="r",
        delta="",
        finish_reason=FinishReason.ERROR,
        error=message,
        error_code=code,
    )


def _ok() -> list[StreamChunk]:
    return [
        StreamChunk(request_id="r", delta="hi"),
        StreamChunk(request_id="r", delta="", finish_reason=FinishReason.STOP),
    ]


@pytest.fixture
async def two_endpoints():
    config = GatewayConfig(
        endpoints=[
            EndpointConfig(name="a", type=ProviderType.OLLAMA, url="http://a:11434"),
            EndpointConfig(name="b", type=ProviderType.OLLAMA, url="http://b:11434"),
        ],
        resolution=ResolutionConfig(endpoint_priority=["a", "b"]),
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    streams: dict[str, _TrackedStream] = {}

    def install(name, chunks):
        stream = _TrackedStream(chunks)
        streams[name] = stream
        registry.get(name).chat_stream = lambda request: stream

    for name in ("a", "b"):
        from gateway.catalog.models import DiscoveredModel

        registry.catalog.add_model(DiscoveredModel(name="m:1", endpoint=name))
        registry.get_health(name).record_healthy()

    yield Dispatcher(registry, config.resolution), install, streams
    await registry.close()


class TestStreamFailover:
    @pytest.mark.asyncio
    async def test_4xx_does_not_fail_over(self, two_endpoints):
        dispatcher, install, streams = two_endpoints
        install("a", [_err("http_404", "model 'm:1' not found")])
        install("b", _ok())
        with pytest.raises(ProviderError) as exc:
            await dispatcher.dispatch_stream(_request())
        assert exc.value.http_status == 404
        assert "not found" in exc.value.message
        assert streams["a"].closed
        assert streams["b"]._chunks  # never consumed

    @pytest.mark.asyncio
    async def test_retryable_error_fails_over_and_closes_abandoned(self, two_endpoints):
        dispatcher, install, streams = two_endpoints
        install("a", [_err("timeout", "Stream stalled: no first data for 30s")])
        install("b", _ok())
        provider, stream = await dispatcher.dispatch_stream(_request())
        assert provider == "b"
        assert streams["a"].closed
        assert [c.delta for c in await _collect(stream)] == ["hi", ""]

    @pytest.mark.asyncio
    async def test_runtime_error_line_fails_over(self, two_endpoints):
        """An in-band runtime failure (e.g. out of memory) is server-side."""
        dispatcher, install, _ = two_endpoints
        install("a", [_err("upstream_error", "model requires more system memory")])
        install("b", _ok())
        assert (await dispatcher.dispatch_stream(_request()))[0] == "b"

    @pytest.mark.asyncio
    async def test_all_fail_reports_last_cause(self, two_endpoints):
        dispatcher, install, _ = two_endpoints
        install("a", [_err("connection_error", "Connection failed: a")])
        install("b", [_err("http_503", "server busy")])
        with pytest.raises(AllProvidersUnavailableError) as exc:
            await dispatcher.dispatch_stream(_request())
        assert "server busy" in str(exc.value.details)

    @pytest.mark.asyncio
    async def test_stream_honors_priority_and_target_endpoint(self, two_endpoints):
        """Regression: streams tried catalog endpoints (in set order) before
        the resolved primary, ignoring priority and a key's target_endpoint."""
        dispatcher, install, _ = two_endpoints
        install("a", _ok())
        install("b", _ok())
        assert (await dispatcher.dispatch_stream(_request()))[0] == "a"
        install("a", _ok())
        install("b", _ok())
        assert (await dispatcher.dispatch_stream(_request(preferred_provider="b")))[0] == "b"

    @pytest.mark.asyncio
    async def test_fallback_disabled_tries_only_primary(self, two_endpoints):
        dispatcher, install, streams = two_endpoints
        install("a", [_err("timeout", "slow")])
        install("b", _ok())
        with pytest.raises(AllProvidersUnavailableError):
            await dispatcher.dispatch_stream(_request(fallback_allowed=False))
        assert streams["b"]._chunks  # never consumed

    def test_catalog_order_is_deterministic(self, two_endpoints):
        dispatcher, _, _ = two_endpoints
        assert dispatcher._registry.get_endpoints_with_model("m:1") == ["a", "b"]

    @pytest.mark.asyncio
    async def test_closing_returned_stream_closes_upstream(self, two_endpoints):
        dispatcher, install, streams = two_endpoints
        install("a", _ok())
        _, stream = await dispatcher.dispatch_stream(_request())
        await stream.__anext__()
        await stream.aclose()
        assert streams["a"].closed


# =============================================================================
# Routes: real status before streaming; OpenAI tool-call streaming
# =============================================================================


@pytest.fixture
def stream_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.include_router(ollama_router)
    app.state.config = GatewayConfig(
        providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")]
    )
    app.state.registry = None
    app.state.enforcer = None
    return app


def _use(app, dispatch_stream):
    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch_stream = dispatch_stream
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher


CHAT = {"model": "m:1", "messages": [{"role": "user", "content": "hi"}], "stream": True}


class TestPreStreamStatus:
    @pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/chat"])
    def test_no_endpoint_returns_503_not_200(self, stream_app, path):
        _use(
            stream_app,
            AsyncMock(side_effect=AllProvidersUnavailableError(attempted=["ep"], last_error="x")),
        )
        resp = TestClient(stream_app).post(path, json=CHAT)
        assert resp.status_code == 503
        assert resp.headers["content-type"].startswith("application/json")

    @pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/chat"])
    def test_upstream_404_passes_through(self, stream_app, path):
        _use(
            stream_app,
            AsyncMock(
                side_effect=ProviderError(
                    message="model 'm:1' not found", provider="ep", http_status=404
                )
            ),
        )
        resp = TestClient(stream_app).post(path, json=CHAT)
        assert resp.status_code == 404


class TestOpenAIToolStreaming:
    def _frames(self, resp) -> list[dict]:
        return [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    def test_tool_calls_stream_as_deltas(self, stream_app):
        chunks = [
            StreamChunk(request_id="r", delta="Let me check. "),
            StreamChunk(
                request_id="r",
                delta="",
                tool_calls=[
                    ToolCall(function={"name": "get_weather", "arguments": {"city": "Oslo"}})
                ],
            ),
            # Ollama reports "stop" after tool calls
            StreamChunk(request_id="r", delta="", finish_reason=FinishReason.STOP),
        ]
        _use(stream_app, AsyncMock(return_value=("ep", _aiter(chunks))))
        body = {**CHAT, "tools": [{"type": "function", "function": {"name": "get_weather"}}]}
        resp = TestClient(stream_app).post("/v1/chat/completions", json=body)

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = self._frames(resp)
        assert [f["choices"][0]["index"] for f in frames] == [0, 0, 0]
        tool_call = frames[1]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_call["index"] == 0
        assert tool_call["id"] == "call_0"
        assert tool_call["function"]["name"] == "get_weather"
        assert json.loads(tool_call["function"]["arguments"]) == {"city": "Oslo"}
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert resp.text.rstrip().endswith("data: [DONE]")

    def test_midstream_error_sends_cause(self, stream_app):
        chunks = [StreamChunk(request_id="r", delta="par"), _err("upstream_error", "runner died")]
        _use(stream_app, AsyncMock(return_value=("ep", _aiter(chunks))))
        resp = TestClient(stream_app).post("/v1/chat/completions", json=CHAT)
        frames = self._frames(resp)
        assert frames[-1]["error"]["message"] == "runner died"

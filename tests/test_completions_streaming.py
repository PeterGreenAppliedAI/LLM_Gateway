"""/v1/completions streaming, and raw-prompt streams on every adapter.

Regressions covered:
- /v1/completions ignored stream: true and returned plain JSON
- every stream went through the chat endpoint (chat template applied),
  including Ollama /api/generate streams, while non-streaming used the
  raw completion endpoint
- /v1/completions sent the raw client prompt to the model: sanitizing and
  PII scrubbing ran on a copy that was never used (fixed in 0ec8d7b)
"""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import GatewayConfig, ProviderConfig
from gateway.dispatch import Dispatcher, DispatchResult
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType, UsageStats
from gateway.models.internal import InternalRequest, InternalResponse, StreamChunk
from gateway.providers.ollama import OllamaAdapter
from gateway.providers.openai import OpenAIAdapter
from gateway.providers.vllm import VLLMAdapter
from gateway.routes import openai_router
from gateway.routes.dependencies import get_audit_logger, get_dispatcher
from gateway.security.pii import PIIScrubber
from gateway.settings import PIISettings


def _completion_request(prompt="Once upon") -> InternalRequest:
    return InternalRequest(task=TaskType.COMPLETION, model="m:1", prompt=prompt, stream=True)


async def _collect(it) -> list:
    return [x async for x in it]


def _adapter(cls, ptype, handler):
    adapter = cls(ProviderConfig(name="ep", type=ptype, base_url="http://up:1", api_key="k"))
    adapter._client = httpx.AsyncClient(
        base_url="http://up:1", transport=httpx.MockTransport(handler)
    )
    return adapter


# =============================================================================
# Adapters stream from the raw completion endpoint
# =============================================================================


@pytest.mark.asyncio
async def test_vllm_streams_from_v1_completions():
    seen = {}
    frames = [
        {"choices": [{"text": "a time"}]},
        {"choices": [{"text": " there", "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
    ]

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body)

    chunks = await _collect(
        _adapter(VLLMAdapter, ProviderType.VLLM, handler).generate_stream(_completion_request())
    )
    assert seen["path"] == "/v1/completions"
    assert seen["body"]["prompt"] == "Once upon"
    assert seen["body"]["stream"] is True
    assert [c.delta for c in chunks] == ["a time", " there"]
    assert chunks[-1].usage.completion_tokens == 3


@pytest.mark.asyncio
async def test_ollama_streams_from_api_generate():
    seen = {}
    lines = [
        {"response": "a time", "done": False},
        {"response": "", "done": True, "done_reason": "length", "eval_count": 4},
    ]

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, text="".join(json.dumps(x) + "\n" for x in lines))

    chunks = await _collect(
        _adapter(OllamaAdapter, ProviderType.OLLAMA, handler).generate_stream(_completion_request())
    )
    assert seen["path"] == "/api/generate"
    assert seen["body"]["prompt"] == "Once upon"
    assert seen["body"]["stream"] is True
    assert chunks[0].delta == "a time"
    assert chunks[-1].finish_reason == FinishReason.LENGTH


@pytest.mark.asyncio
async def test_openai_without_completions_falls_back_to_chat():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path == "/v1/completions":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        frames = [{"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}]
        return httpx.Response(
            200, text="".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        )

    chunks = await _collect(
        _adapter(OpenAIAdapter, ProviderType.OPENAI, handler).generate_stream(_completion_request())
    )
    assert paths == ["/v1/completions", "/v1/chat/completions"]
    assert chunks[-1].delta == "hi"


@pytest.mark.asyncio
async def test_dispatcher_uses_generate_stream_for_completions():
    from gateway.dispatch.registry import ProviderRegistry

    config = GatewayConfig(
        providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")]
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    adapter = registry.get("ep")

    async def stream(_request):
        yield StreamChunk(request_id="r", delta="raw", finish_reason=FinishReason.STOP)

    adapter.generate_stream = stream
    adapter.chat_stream = MagicMock(side_effect=AssertionError("chat_stream used"))
    _, chunks = await Dispatcher(registry).dispatch_stream(_completion_request())
    assert [c.delta async for c in chunks] == ["raw"]
    await registry.close()


# =============================================================================
# Route
# =============================================================================


@pytest.fixture
def app():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.state.config = GatewayConfig(
        providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")]
    )
    app.state.registry = None
    app.state.enforcer = None
    app.state.test_audit = AsyncMock()
    app.dependency_overrides[get_audit_logger] = lambda: app.state.test_audit
    return app


def _streaming_dispatcher(app, chunks):
    async def stream():
        for c in chunks:
            yield c

    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch_stream = AsyncMock(side_effect=lambda req: ("ep", stream()))
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    return dispatcher


def test_completions_stream_returns_sse(app):
    dispatcher = _streaming_dispatcher(
        app,
        [
            StreamChunk(request_id="r", delta="a time"),
            StreamChunk(
                request_id="r",
                delta="",
                finish_reason=FinishReason.STOP,
                usage=UsageStats.from_counts(prompt=2, completion=2),
            ),
        ],
    )
    resp = TestClient(app).post(
        "/v1/completions", json={"model": "m:1", "prompt": "Once upon", "stream": True}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line[6:])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert frames[0]["object"] == "text_completion"
    assert frames[0]["choices"][0]["text"] == "a time"
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert resp.text.rstrip().endswith("data: [DONE]")

    sent = dispatcher.dispatch_stream.call_args[0][0]
    assert sent.task == TaskType.COMPLETION
    audit = app.state.test_audit.log_request.await_args.kwargs
    assert audit["status"] == "success"
    assert audit["stream"] is True


def test_completions_stream_with_several_prompts_rejected(app):
    _streaming_dispatcher(app, [])
    resp = TestClient(app).post(
        "/v1/completions", json={"model": "m:1", "prompt": ["a", "b"], "stream": True}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("stream", [False, True])
def test_scrubbed_prompt_is_what_the_model_gets(app, stream):
    app.state.pii_scrubber = PIIScrubber()
    app.state.pii_settings = PIISettings(enabled=True, scrub_enabled=True)
    if stream:
        dispatcher = _streaming_dispatcher(
            app, [StreamChunk(request_id="r", delta="ok", finish_reason=FinishReason.STOP)]
        )
    else:
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=InternalResponse(
                    request_id="r",
                    task=TaskType.COMPLETION,
                    provider="ep",
                    model="m:1",
                    content="ok",
                ),
                provider_used="ep",
            )
        )
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher

    resp = TestClient(app).post(
        "/v1/completions",
        json={"model": "m:1", "prompt": "email me at a.b@example.com", "stream": stream},
    )
    assert resp.status_code == 200
    call = dispatcher.dispatch_stream if stream else dispatcher.dispatch
    assert call.call_args[0][0].prompt == "email me at [EMAIL]"

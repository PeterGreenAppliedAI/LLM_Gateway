"""Batch-shaped requests: embedding lists on vLLM, prompt lists, and n.

Regressions covered:
- the vLLM adapter had no embeddings method, so every vLLM embedding
  request failed as "all providers unavailable"
- /v1/completions with a list of prompts used only the first and dropped
  the rest without an error
- `n` was silently ignored on chat and completions
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import GatewayConfig, ProviderConfig
from gateway.dispatch import Dispatcher, DispatchResult
from gateway.errors import ProviderError
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType, UsageStats
from gateway.models.internal import InternalRequest, InternalResponse
from gateway.providers.vllm import VLLMAdapter
from gateway.routes import fanout, openai_router
from gateway.routes.dependencies import get_audit_logger, get_dispatcher

# =============================================================================
# vLLM embeddings
# =============================================================================


@pytest.mark.asyncio
async def test_vllm_embeddings_batch_in_one_call_and_ordered():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        # Upstream returns items out of order: output must follow input order
        return httpx.Response(
            200,
            json={
                "model": "bge-m3",
                "data": [
                    {"index": 1, "embedding": [1.0]},
                    {"index": 0, "embedding": [0.0]},
                    {"index": 2, "embedding": [2.0]},
                ],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            },
        )

    adapter = VLLMAdapter(
        ProviderConfig(name="v", type=ProviderType.VLLM, base_url="http://v:8000")
    )
    adapter._client = httpx.AsyncClient(
        base_url="http://v:8000", transport=httpx.MockTransport(handler)
    )
    response = await adapter.embeddings(
        InternalRequest(task=TaskType.EMBEDDINGS, model="bge-m3", input_data=["a", "b", "c"])
    )

    assert not response.is_error
    assert seen["path"] == "/v1/embeddings"
    assert seen["body"]["input"] == ["a", "b", "c"]  # one upstream call for the batch
    assert response.embeddings == [[0.0], [1.0], [2.0]]
    assert response.usage.prompt_tokens == 9


# =============================================================================
# Fan-out: prompt lists and n
# =============================================================================


def _result(text: str, provider: str = "ep") -> DispatchResult:
    return DispatchResult(
        response=InternalResponse(
            request_id="r",
            task=TaskType.CHAT,
            provider=provider,
            model="m:1",
            content=text,
            finish_reason=FinishReason.STOP,
            usage=UsageStats.from_counts(prompt=2, completion=3),
        ),
        provider_used=provider,
    )


@pytest.fixture
def batch_app():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.state.config = GatewayConfig(
        providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")]
    )
    app.state.registry = None
    app.state.enforcer = None
    audit = AsyncMock()
    app.dependency_overrides[get_audit_logger] = lambda: audit
    app.state.test_audit = audit
    return app


def _use_dispatch(app, side_effect):
    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch = AsyncMock(side_effect=side_effect)
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    return dispatcher


async def _echo_prompt(request: InternalRequest) -> DispatchResult:
    return _result(f"out:{request.prompt}")


class TestCompletionsPromptList:
    def test_every_prompt_answered_in_order(self, batch_app):
        dispatcher = _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post(
            "/v1/completions", json={"model": "m:1", "prompt": ["a", "b", "c"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [c["text"] for c in data["choices"]] == ["out:a", "out:b", "out:c"]
        assert [c["index"] for c in data["choices"]] == [0, 1, 2]
        assert data["usage"] == {"prompt_tokens": 6, "completion_tokens": 9, "total_tokens": 15}
        assert dispatcher.dispatch.await_count == 3

    def test_prompts_times_n_is_prompt_major(self, batch_app):
        _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post(
            "/v1/completions", json={"model": "m:1", "prompt": ["a", "b"], "n": 2}
        )
        assert [c["text"] for c in resp.json()["choices"]] == ["out:a", "out:a", "out:b", "out:b"]

    def test_one_audit_row_with_summed_usage(self, batch_app):
        _use_dispatch(batch_app, _echo_prompt)
        TestClient(batch_app).post("/v1/completions", json={"model": "m:1", "prompt": ["a", "b"]})
        audit = batch_app.state.test_audit
        assert audit.log_request.await_count == 1
        kwargs = audit.log_request.await_args.kwargs
        assert kwargs["prompt_tokens"] == 4
        assert kwargs["completion_tokens"] == 6
        assert kwargs["request_body"]["prompt"] == ["a", "b"]

    def test_empty_prompt_list_is_a_validation_error(self, batch_app):
        _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post("/v1/completions", json={"model": "m:1", "prompt": []})
        assert resp.status_code == 422

    def test_too_many_choices_rejected(self, batch_app):
        _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post(
            "/v1/completions", json={"model": "m:1", "prompt": ["p"] * 40, "n": 2}
        )
        assert resp.status_code == 422
        assert "maximum is 64" in resp.json()["error"]["message"]


CHAT = {"model": "m:1", "messages": [{"role": "user", "content": "hi"}]}


class TestChatN:
    def test_n_returns_n_choices(self, batch_app):
        texts = iter(["one", "two", "three"])

        async def answer(request):
            return _result(next(texts))

        dispatcher = _use_dispatch(batch_app, answer)
        resp = TestClient(batch_app).post("/v1/chat/completions", json={**CHAT, "n": 3})
        assert resp.status_code == 200
        choices = resp.json()["choices"]
        assert [c["index"] for c in choices] == [0, 1, 2]
        assert sorted(c["message"]["content"] for c in choices) == ["one", "three", "two"]
        assert dispatcher.dispatch.await_count == 3

    def test_n_with_stream_rejected(self, batch_app):
        _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post(
            "/v1/chat/completions", json={**CHAT, "n": 2, "stream": True}
        )
        assert resp.status_code == 422

    def test_n_default_unchanged(self, batch_app):
        dispatcher = _use_dispatch(batch_app, _echo_prompt)
        resp = TestClient(batch_app).post("/v1/chat/completions", json=CHAT)
        assert len(resp.json()["choices"]) == 1
        assert dispatcher.dispatch.await_count == 1

    def test_one_failed_choice_fails_the_request(self, batch_app):
        calls = 0

        async def second_fails(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ProviderError(message="model not found", provider="ep", http_status=404)
            await asyncio.sleep(0.01)
            return _result("ok")

        _use_dispatch(batch_app, second_fails)
        resp = TestClient(batch_app).post("/v1/chat/completions", json={**CHAT, "n": 3})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fanout_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr(fanout, "MAX_CONCURRENT_PER_REQUEST", 3)
    running = peak = 0

    async def slow(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return _result("x")

    class _Connected:
        async def receive(self):
            await asyncio.Event().wait()

    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch = AsyncMock(side_effect=slow)
    request = InternalRequest(task=TaskType.COMPLETION, model="m:1", prompt="p")
    results = await fanout.dispatch_choices(_Connected(), dispatcher, [request] * 10)

    assert len(results) == 10
    assert peak == 3

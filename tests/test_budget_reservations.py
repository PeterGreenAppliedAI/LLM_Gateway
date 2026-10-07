"""Budgets as hard limits: reserve at admission, settle on completion (D-043).

The external review found ten concurrent requests could each pass a
100-token check against the same 100-token budget, an exhausted budget
still admitted zero-estimate requests, and interrupted reasoning-only
streams counted as zero tokens.
"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from gateway.config import EndpointConfig, GatewayConfig
from gateway.dispatch.registry import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import ProviderType
from gateway.models.internal import StreamChunk
from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
from gateway.policy.token_budget import (
    TokenBudgetConfig,
    TokenBudgetExceeded,
    TokenBudgetTracker,
)
from gateway.policy.token_limiter import TokenLimitConfig
from gateway.routes import openai_router


def _tracker(limit: int = 100) -> TokenBudgetTracker:
    return TokenBudgetTracker(
        TokenBudgetConfig(enabled=True, default_daily_limit=limit, default_cost_multiplier=1.0)
    )


class TestTracker:
    def test_reservations_count_against_the_budget(self):
        tracker = _tracker(100)
        tracker.reserve("r1", "k", "m", 60)
        with pytest.raises(TokenBudgetExceeded):
            tracker.reserve("r2", "k", "m", 60)  # 60 held + 60 > 100

    def test_settle_replaces_estimate_with_actual(self):
        tracker = _tracker(100)
        tracker.reserve("r1", "k", "m", 90)
        tracker.record_usage("k", "m", 20, reservation_id="r1")
        assert tracker.get_budget_state("k").tokens_used == 20  # not 90, not 110
        tracker.reserve("r2", "k", "m", 70)  # fits now

    def test_release_frees_the_hold(self):
        tracker = _tracker(100)
        tracker.reserve("r1", "k", "m", 100)
        tracker.release("r1")
        tracker.reserve("r2", "k", "m", 100)

    def test_exhausted_budget_refuses_zero_estimate(self):
        tracker = _tracker(100)
        tracker.record_usage("k", "m", 100)
        with pytest.raises(TokenBudgetExceeded):
            tracker.check_budget("k", "m", estimated_tokens=0)

    def test_unsettled_hold_expires(self, monkeypatch):
        import gateway.policy.token_budget as tb

        tracker = _tracker(100)
        tracker.reserve("lost", "k", "m", 100)
        monkeypatch.setattr(tb, "RESERVATION_TTL", -1)
        tracker.reserve("next", "k", "m", 100)  # the lost hold was dropped


class TestEstimates:
    def test_unset_max_tokens_reserves_the_default(self):
        enforcer = PolicyEnforcer(
            PolicyConfig(
                token_budget=TokenBudgetConfig(
                    enabled=True, default_daily_limit=100_000, default_cost_multiplier=1.0
                ),
                token_limit=TokenLimitConfig(default_max_tokens=500),
            )
        )
        from gateway.models.common import TaskType
        from gateway.models.internal import InternalRequest, Message, MessageRole

        chat = InternalRequest(
            task=TaskType.CHAT,
            model="m",
            messages=[Message(role=MessageRole.USER, content="x" * 400)],
        )
        assert enforcer._estimate_tokens(chat, None) == 400 // 4 + 1 + 500
        embed = InternalRequest(task=TaskType.EMBEDDINGS, model="m", input_data=["y" * 40])
        assert enforcer._estimate_tokens(embed, None) == 40 // 4 + 1  # no output

    def test_reasoning_only_stream_is_not_free(self):
        from unittest.mock import MagicMock

        from gateway.routes.stream_recorder import StreamRecorder

        recorder = StreamRecorder.__new__(StreamRecorder)
        recorder._ctx = MagicMock()
        recorder._parts, recorder._content_chunks = [], 0
        recorder._usage, recorder._first_token_seen = None, False
        for _ in range(5):
            recorder.observe(StreamChunk(request_id="r", index=0, delta="", thinking="hmm"))
        assert recorder._token_counts() == (0, 5, True)


# =============================================================================
# Through the real route: concurrency, settlement, release on failure
# =============================================================================


class SlowEngine:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(0.05)  # all requests are in flight together
        if self.fail:
            return httpx.Response(500, json={"error": "engine down"})
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            },
        )


async def _app(engine: SlowEngine, limit: int) -> tuple[FastAPI, ProviderRegistry]:
    config = GatewayConfig(
        endpoints=[EndpointConfig(name="eng", type=ProviderType.OPENAI, url="http://eng:1")]
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    registry.get("eng")._client = httpx.AsyncClient(
        base_url="http://eng:1", transport=httpx.MockTransport(engine)
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.state.config = config
    app.state.registry = registry
    app.state.enforcer = PolicyEnforcer(
        PolicyConfig(
            token_budget=TokenBudgetConfig(
                enabled=True, default_daily_limit=limit, default_cost_multiplier=1.0
            )
        )
    )
    return app, registry


async def _chat(app, n: int):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    body = {"model": "m", "max_tokens": 80, "messages": [{"role": "user", "content": "hi"}]}
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
        return await asyncio.gather(
            *(http.post("/v1/chat/completions", json=body) for _ in range(n))
        )


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overspend():
    """The review's case: 10 concurrent requests against a 100-token budget."""
    engine = SlowEngine()
    app, registry = await _app(engine, limit=100)
    responses = await _chat(app, 10)
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1  # one 80-token reservation fits; the rest don't
    assert engine.calls == 1
    tracker = app.state.enforcer.token_budget
    assert tracker.get_budget_state("default").tokens_used == 10  # settled to actual
    assert tracker._reservations == {}
    await registry.close()


@pytest.mark.asyncio
async def test_failed_requests_release_their_holds():
    engine = SlowEngine(fail=True)
    app, registry = await _app(engine, limit=100)
    responses = await _chat(app, 1)
    assert responses[0].status_code >= 500
    tracker = app.state.enforcer.token_budget
    assert tracker._reservations == {}  # released at the end of the request
    assert tracker.get_budget_state("default").tokens_used == 0
    await registry.close()


@pytest.mark.asyncio
async def test_streamed_request_settles_its_reservation():
    import json as _json

    def sse(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            },
        ]
        body = "".join(
            f"data: {_json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'model': 'm', **c})}\n\n"
            for c in chunks
        )
        return httpx.Response(
            200, content=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    app, registry = await _app(SlowEngine(), limit=1000)
    registry.get("eng")._client = httpx.AsyncClient(
        base_url="http://eng:1", transport=httpx.MockTransport(sse)
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    body = {
        "model": "m",
        "stream": True,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
        resp = await http.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200 and "[DONE]" in resp.text
    tracker = app.state.enforcer.token_budget
    assert tracker._reservations == {}
    assert tracker.get_budget_state("default").tokens_used == 7  # actual, not the 500 estimate
    await registry.close()

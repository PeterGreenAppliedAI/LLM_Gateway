"""Task/endpoint policy applied where endpoints are chosen (D-049).

Second external review: a TaskProviderPolicy denying the selected provider
still let the chat request through. The check needed a provider argument
the routes never passed, and gateway.yaml couldn't configure it at all.
"""

import json

import httpx
import pytest
from fastapi import FastAPI

from gateway.config import EndpointConfig, GatewayConfig, ResolutionConfig, TaskEndpointPolicy
from gateway.dispatch.registry import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import ProviderType, TaskType
from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer, TaskProviderPolicy
from gateway.routes import openai_router
from gateway.routes.dependencies import build_enforcer


class Engine:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "m",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        if json.loads(request.content).get("stream"):
            chunk = {
                "id": "c",
                "object": "chat.completion.chunk",
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": self.name}, "finish_reason": "stop"}],
            }
            return httpx.Response(
                200,
                content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
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
                        "message": {"role": "assistant", "content": self.name},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 2, "completion_tokens": 1},
            },
        )


async def _app(names, task_endpoints=(), policy: PolicyConfig | None = None):
    config = GatewayConfig(
        endpoints=[
            EndpointConfig(name=n, type=ProviderType.OPENAI, url=f"http://{n}:1") for n in names
        ],
        resolution=ResolutionConfig(endpoint_priority=list(names)),
        task_endpoints=list(task_endpoints),
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    engines = {}
    for n in names:
        engines[n] = Engine(n)
        registry.get(n)._client = httpx.AsyncClient(
            base_url=f"http://{n}:1", transport=httpx.MockTransport(engines[n])
        )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.state.config = config
    app.state.registry = registry
    if policy is not None:
        app.state.enforcer = PolicyEnforcer(policy)
    else:
        build_enforcer(app)  # bridges gateway.yaml task_endpoints
    return app, registry, engines


async def _post(app, path, body):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
        return await http.post(path, json=body)


CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_denied_endpoint_is_never_used():
    """The review's case: the policy denies the only endpoint; the request went through."""
    app, registry, engines = await _app(
        ["gpu"],
        policy=PolicyConfig(
            task_policies=[TaskProviderPolicy(task=TaskType.CHAT, denied_providers={"gpu"})]
        ),
    )
    resp = await _post(app, "/v1/chat/completions", CHAT)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "endpoint_not_allowed"
    assert engines["gpu"].calls == []
    await registry.close()


@pytest.mark.asyncio
async def test_routing_skips_a_denied_endpoint_for_an_allowed_one():
    app, registry, engines = await _app(
        ["cloud", "gpu"],
        task_endpoints=[TaskEndpointPolicy(task=TaskType.CHAT, denied_endpoints=["cloud"])],
    )
    resp = await _post(app, "/v1/chat/completions", CHAT)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "gpu"
    assert engines["cloud"].calls == []
    await registry.close()


@pytest.mark.asyncio
async def test_streams_follow_the_policy_too():
    app, registry, engines = await _app(
        ["cloud", "gpu"],
        task_endpoints=[TaskEndpointPolicy(task=TaskType.CHAT, denied_endpoints=["cloud"])],
    )
    resp = await _post(app, "/v1/chat/completions", {**CHAT, "stream": True})
    assert resp.status_code == 200 and "gpu" in resp.text
    assert engines["cloud"].calls == []
    await registry.close()


@pytest.mark.asyncio
async def test_allowed_list_applies_per_task():
    """Embeddings kept on-prem; chat may still use the cloud endpoint."""
    app, registry, engines = await _app(
        ["cloud", "gpu"],
        task_endpoints=[TaskEndpointPolicy(task=TaskType.EMBEDDINGS, allowed_endpoints=["gpu"])],
    )
    assert (await _post(app, "/v1/embeddings", {"model": "m", "input": "x"})).status_code == 200
    assert (await _post(app, "/v1/chat/completions", CHAT)).status_code == 200
    assert engines["gpu"].calls == ["/v1/embeddings"]
    assert engines["cloud"].calls == ["/v1/chat/completions"]
    await registry.close()


@pytest.mark.asyncio
async def test_pinning_a_denied_endpoint_is_refused():
    app, registry, engines = await _app(
        ["cloud", "gpu"],
        task_endpoints=[TaskEndpointPolicy(task=TaskType.CHAT, denied_endpoints=["cloud"])],
    )
    resp = await _post(app, "/v1/chat/completions", {**CHAT, "model": "cloud/m"})
    assert resp.status_code == 403
    assert engines["cloud"].calls == []
    await registry.close()


def test_unknown_endpoint_in_policy_is_a_config_error():
    with pytest.raises(ValueError, match="unknown endpoint 'nope'"):
        GatewayConfig(
            endpoints=[EndpointConfig(name="gpu", type=ProviderType.OPENAI, url="http://g:1")],
            task_endpoints=[TaskEndpointPolicy(task=TaskType.CHAT, denied_endpoints=["nope"])],
        )

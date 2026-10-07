"""Per-key concurrency limits and the batch priority class (D-034)."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse

from gateway.config import AdmissionConfig, GatewayConfig, ProviderConfig, RoutingConfig
from gateway.dispatch.admission import InMemoryConcurrency
from gateway.dispatch.dispatcher import Dispatcher
from gateway.dispatch.registry import ProviderRegistry
from gateway.errors import ErrorCode
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType
from gateway.models.internal import InternalRequest, InternalResponse, Message, MessageRole
from gateway.routes.dependencies import (
    PRIORITY_HEADER,
    AuthResult,
    get_auth,
    get_inference_auth,
    resolve_access_scope,
)
from gateway.storage import DatabaseConfig, create_async_db_engine
from gateway.storage.keys import KeyManager

# =============================================================================
# Backend: priority and batch share
# =============================================================================


class TestBatchScheduling:
    async def test_batch_limited_to_share(self):
        c = InMemoryConcurrency({"a": 4}, batch_max_share=0.75)
        batch = [await c.try_acquire("a", "batch") for _ in range(3)]
        assert all(batch)
        assert await c.try_acquire("a", "batch") is None  # 4th slot is reserved
        assert await c.try_acquire("a", "interactive") is not None

    async def test_single_slot_endpoint_still_serves_batch(self):
        c = InMemoryConcurrency({"a": 1}, batch_max_share=0.75)
        assert await c.try_acquire("a", "batch") is not None

    async def test_unlimited_endpoint_ignores_share(self):
        c = InMemoryConcurrency({"a": None}, batch_max_share=0.5)
        assert all([await c.try_acquire("a", "batch") for _ in range(20)])

    @pytest.mark.asyncio
    async def test_interactive_waiter_served_before_older_batch_waiter(self):
        c = InMemoryConcurrency({"a": 1})
        held = await c.try_acquire("a")
        served: list[str] = []

        async def wait(priority):
            lease = await c.acquire_any(["a"], timeout=5, priority=priority)
            served.append(priority)
            await asyncio.sleep(0)
            await lease.release()

        batch = asyncio.create_task(wait("batch"))
        await asyncio.sleep(0.01)
        interactive = asyncio.create_task(wait("interactive"))
        await asyncio.sleep(0.01)
        await held.release()
        await asyncio.gather(batch, interactive)
        assert served == ["interactive", "batch"]

    @pytest.mark.asyncio
    async def test_batch_waiter_not_handed_reserved_slot(self):
        c = InMemoryConcurrency({"a": 4}, batch_max_share=0.5)  # batch may hold 2
        held = [await c.try_acquire("a", "interactive") for _ in range(4)]
        waiter = asyncio.create_task(c.acquire_any(["a"], timeout=5, priority="batch"))
        await asyncio.sleep(0.01)
        await held[0].release()
        await held[1].release()  # 2 left in flight: batch would make 3, over its share
        await asyncio.sleep(0.01)
        assert not waiter.done()
        assert c.in_flight("a") == 2
        await held[2].release()  # 1 left: batch may take a slot
        lease = await asyncio.wait_for(waiter, 1)
        assert c.in_flight("a") == 2
        for h in (lease, held[3]):
            await h.release()
        assert c.in_flight("a") == 0


class TestDispatcherPriority:
    @pytest.mark.asyncio
    async def test_batch_overflows_past_reserved_slots_and_waits_longer(self):
        config = GatewayConfig(
            providers=[
                ProviderConfig(
                    name="a", type=ProviderType.VLLM, base_url="http://a:8000", max_concurrent=4
                ),
                ProviderConfig(
                    name="b", type=ProviderType.VLLM, base_url="http://b:8000", max_concurrent=4
                ),
            ],
            routing=RoutingConfig(default_provider="a"),
            admission=AdmissionConfig(
                batch_max_share=0.5, max_queue_wait_seconds=1, batch_max_queue_wait_seconds=30
            ),
        )
        registry = ProviderRegistry(config)
        await registry.initialize()
        for name in ("a", "b"):
            adapter = AsyncMock()
            adapter.chat = AsyncMock(
                return_value=InternalResponse(
                    request_id="r",
                    task=TaskType.CHAT,
                    provider=name,
                    model="m",
                    content="ok",
                    finish_reason=FinishReason.STOP,
                )
            )
            registry._adapters[name] = adapter
        dispatcher = Dispatcher(registry)
        held = [await registry.admission.try_acquire("a", "batch") for _ in range(2)]

        def request(priority):
            return InternalRequest(
                task=TaskType.CHAT,
                model="m",
                priority=priority,
                messages=[Message(role=MessageRole.USER, content="x")],
            )

        # a holds 2 batch: batch is at its share there, interactive is not
        assert (await dispatcher.dispatch(request("batch"))).provider_used == "b"
        assert (await dispatcher.dispatch(request("interactive"))).provider_used == "a"
        assert registry.queue_wait_seconds("batch") == 30
        assert registry.queue_wait_seconds("interactive") == 1
        for lease in held:
            await lease.release()
        await registry.close()


# =============================================================================
# Per-key limit and the priority header (route dependency)
# =============================================================================


def _app(auth: AuthResult) -> tuple[FastAPI, asyncio.Event]:
    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_auth] = lambda: auth
    release = asyncio.Event()

    @app.post("/work")
    async def work(a: AuthResult = Depends(get_inference_auth)):
        await release.wait()
        return {"priority": a.priority}

    @app.post("/stream")
    async def stream(a: AuthResult = Depends(get_inference_auth)):
        async def body():
            yield b"one"
            await release.wait()
            yield b"two"

        return StreamingResponse(body())

    @app.post("/priority")
    async def priority(a: AuthResult = Depends(get_inference_auth)):
        return {"priority": a.priority}

    return app, release


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


class TestPerKeyConcurrency:
    @pytest.mark.asyncio
    async def test_over_limit_gets_429_then_frees(self):
        app, release = _app(AuthResult("tenant", max_concurrent=1))
        async with _client(app) as client:
            first = asyncio.create_task(client.post("/work"))
            await asyncio.sleep(0.05)
            second = await client.post("/work")
            assert second.status_code == 429
            assert second.json()["error"]["code"] == ErrorCode.CONCURRENCY_LIMIT_EXCEEDED.value
            assert second.headers["Retry-After"] == "1"
            release.set()
            assert (await first).status_code == 200
            assert (await client.post("/work")).status_code == 200

    @pytest.mark.asyncio
    async def test_stream_holds_slot_until_finished(self):
        """Driven at the ASGI level: httpx's ASGI transport buffers whole bodies."""
        app, release = _app(AuthResult("tenant", max_concurrent=1))
        first_chunk = asyncio.Event()
        sent: list[dict] = []

        async def receive():
            await asyncio.Event().wait()  # never disconnects

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body") == b"one":
                first_chunk.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
            "client": ("t", 1),
            "server": ("t", 80),
            "scheme": "http",
            "root_path": "",
        }
        streaming = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_chunk.wait(), 5)
        async with _client(app) as client:
            assert (await client.post("/priority")).status_code == 429  # stream holds it
            release.set()
            await asyncio.wait_for(streaming, 5)
            assert (await client.post("/priority")).status_code == 200

    @pytest.mark.asyncio
    async def test_no_limit_by_default(self):
        app, release = _app(AuthResult("tenant"))
        release.set()
        async with _client(app) as client:
            results = await asyncio.gather(*(client.post("/work") for _ in range(10)))
        assert all(r.status_code == 200 for r in results)

    @pytest.mark.asyncio
    async def test_keys_counted_separately(self):
        app, release = _app(AuthResult("tenant", max_concurrent=1))
        async with _client(app) as client:
            held = asyncio.create_task(client.post("/work"))
            await asyncio.sleep(0.05)
            app.dependency_overrides[get_auth] = lambda: AuthResult("other", max_concurrent=1)
            release.set()
            assert (await client.post("/work")).status_code == 200
            await held


class TestPriorityHeader:
    @pytest.mark.asyncio
    async def test_client_can_mark_batch(self):
        app, _ = _app(AuthResult("tenant"))
        async with _client(app) as client:
            resp = await client.post("/priority", headers={PRIORITY_HEADER: "batch"})
        assert resp.json() == {"priority": "batch"}

    @pytest.mark.asyncio
    async def test_batch_key_cannot_upgrade(self):
        app, _ = _app(AuthResult("tenant", priority="batch"))
        async with _client(app) as client:
            resp = await client.post("/priority", headers={PRIORITY_HEADER: "interactive"})
        assert resp.json() == {"priority": "batch"}

    @pytest.mark.asyncio
    async def test_unknown_value_rejected(self):
        app, _ = _app(AuthResult("tenant"))
        async with _client(app) as client:
            resp = await client.post("/priority", headers={PRIORITY_HEADER: "urgent"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_scope_carries_priority_to_dispatch(self):
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.app.state.config = GatewayConfig()
        updates = await resolve_access_scope(request, AuthResult("t", priority="batch"), "m")
        assert updates["priority"] == "batch"


# =============================================================================
# DB-backed keys store the new fields
# =============================================================================


@pytest.mark.asyncio
async def test_db_key_stores_concurrency_and_priority():
    engine = await create_async_db_engine(
        DatabaseConfig(url="sqlite:///:memory:"), create_tables=True
    )
    km = KeyManager(engine)
    created = await km.create_key(
        name="evals", client_id="evals", max_concurrent=2, priority="batch"
    )
    info = await km.validate_plaintext_key(created["key"])
    assert info["max_concurrent"] == 2
    assert info["priority"] == "batch"
    listed = (await km.list_keys())[0]
    assert (listed["max_concurrent"], listed["priority"]) == (2, "batch")

    plain = await km.create_key(name="app", client_id="app")
    info = await km.validate_plaintext_key(plain["key"])
    assert (info["max_concurrent"], info["priority"]) == (None, "interactive")
    await engine.dispose()

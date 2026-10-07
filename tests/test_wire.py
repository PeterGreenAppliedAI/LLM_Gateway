"""What actually reaches the engine.

These tests run the real routes, dispatcher and adapter, and replace only
the engine with a recorder of the exact HTTP bodies it receives. A control
that logs success (PII scrubbed, image accepted) is only proven by what the
engine got. Added after an external review found two such gaps.
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import EndpointConfig, GatewayConfig
from gateway.dispatch.registry import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import ProviderType
from gateway.routes import ollama_router, openai_router
from gateway.security.pii import PIIScrubber
from gateway.security.pii_config import PIIScrubConfig

EMAIL = "jane.doe@example.com"
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class Engine:
    """An OpenAI-compatible engine that records every request body."""

    def __init__(self):
        self.bodies: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.bodies.append((request.url.path, body))
        if request.url.path == "/v1/embeddings":
            items = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(
                200,
                json={
                    "data": [{"index": i, "embedding": [0.1]} for i in range(len(items))],
                    "model": body["model"],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "m"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    def last(self, path: str) -> dict:
        return [b for p, b in self.bodies if p == path][-1]


@pytest.fixture
async def wire():
    config = GatewayConfig(
        endpoints=[EndpointConfig(name="eng", type=ProviderType.OPENAI, url="http://eng:1")]
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    engine = Engine()
    registry.get("eng")._client = httpx.AsyncClient(
        base_url="http://eng:1", transport=httpx.MockTransport(engine)
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.include_router(ollama_router)
    app.state.config = config
    app.state.registry = registry
    app.state.pii_scrubber = PIIScrubber()
    app.state.pii_settings = PIIScrubConfig(scrub_enabled=True)
    yield TestClient(app), engine
    await registry.close()


class TestPIIScrubbingReachesTheEngine:
    def test_openai_embeddings_string(self, wire):
        client, engine = wire
        resp = client.post("/v1/embeddings", json={"model": "m", "input": f"mail {EMAIL} now"})
        assert resp.status_code == 200
        sent = json.dumps(engine.last("/v1/embeddings"))
        assert EMAIL not in sent

    def test_openai_embeddings_list(self, wire):
        client, engine = wire
        resp = client.post(
            "/v1/embeddings", json={"model": "m", "input": ["clean text", f"to {EMAIL}"]}
        )
        assert resp.status_code == 200
        sent = engine.last("/v1/embeddings")
        assert EMAIL not in json.dumps(sent)
        assert sent["input"][0] == "clean text"  # untouched items stay as they were

    def test_openai_chat(self, wire):
        client, engine = wire
        client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": f"I am {EMAIL}"}]},
        )
        assert EMAIL not in json.dumps(engine.last("/v1/chat/completions"))

    def test_pii_past_100k_characters_is_scrubbed(self, wire):
        """Second review: an email after character 100,000 reached the engine
        unchanged, because only a prefix was scanned."""
        client, engine = wire
        content = "x " * 60_000 + f"reach me at {EMAIL}"
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": content}]},
        )
        assert resp.status_code == 200
        sent = json.dumps(engine.last("/v1/chat/completions"))
        assert EMAIL not in sent
        assert "[EMAIL]" in sent

    def test_clean_input_unchanged_while_scrubbing(self, wire):
        """Regression: no-PII text scrubbed to None and failed validation (422)."""
        client, engine = wire
        resp = client.post("/v1/embeddings", json={"model": "m", "input": "nothing private"})
        assert resp.status_code == 200
        assert engine.last("/v1/embeddings")["input"] in ("nothing private", ["nothing private"])

    def test_ollama_embed(self, wire):
        client, engine = wire
        client.post("/api/embed", json={"model": "m", "input": f"x {EMAIL}"})
        assert EMAIL not in json.dumps(engine.last("/v1/embeddings"))

    def test_scrubbing_off_passes_text_through(self, wire):
        client, engine = wire
        client.app.state.pii_settings = PIIScrubConfig(scrub_enabled=False)
        client.post("/v1/embeddings", json={"model": "m", "input": f"mail {EMAIL}"})
        assert EMAIL in json.dumps(engine.last("/v1/embeddings"))


def _image_message(text: str = "what is this?", url: str = IMAGE) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


class TestInlineImagesReachTheEngine:
    def test_image_passes_through_unchanged(self, wire):
        client, engine = wire
        resp = client.post(
            "/v1/chat/completions", json={"model": "vision", "messages": [_image_message()]}
        )
        assert resp.status_code == 200
        parts = engine.last("/v1/chat/completions")["messages"][0]["content"]
        assert {"type": "image_url", "image_url": {"url": IMAGE}} in parts
        assert {"type": "text", "text": "what is this?"} in parts

    def test_text_beside_image_still_scrubbed(self, wire):
        client, engine = wire
        client.post(
            "/v1/chat/completions",
            json={"model": "vision", "messages": [_image_message(f"from {EMAIL}")]},
        )
        sent = engine.last("/v1/chat/completions")
        assert EMAIL not in json.dumps(sent)
        assert IMAGE in json.dumps(sent)

    def test_image_url_rejected_not_dropped(self, wire):
        client, engine = wire
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "vision",
                "messages": [_image_message(url="https://example.com/cat.png")],
            },
        )
        assert resp.status_code == 422
        assert "inline" in resp.json()["error"]["message"]
        assert engine.bodies == []

    def test_unsupported_part_rejected(self, wire):
        client, engine = wire
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": "AAAA"}}],
                    }
                ],
            },
        )
        assert resp.status_code == 422
        assert engine.bodies == []

    def test_audit_body_never_holds_image_bytes(self):
        from gateway.routes.content_parts import redact_media

        stored = redact_media({"content_parts": [{"image_url": {"url": IMAGE}}]})
        assert IMAGE not in json.dumps(stored)
        assert stored["content_parts"][0]["image_url"]["url"].startswith("[image/png, ")


@pytest.mark.asyncio
async def test_image_reaches_ollama_in_native_field():
    seen = []

    def ollama(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": "a pixel"},
                "done": True,
                "prompt_eval_count": 3,
                "eval_count": 2,
            },
        )

    config = GatewayConfig(
        endpoints=[EndpointConfig(name="oll", type=ProviderType.OLLAMA, url="http://oll:1")]
    )
    registry = ProviderRegistry(config)
    await registry.initialize()
    registry.get("oll")._client = httpx.AsyncClient(
        base_url="http://oll:1", transport=httpx.MockTransport(ollama)
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.state.config = config
    app.state.registry = registry
    resp = TestClient(app).post(
        "/v1/chat/completions", json={"model": "llava", "messages": [_image_message()]}
    )
    assert resp.status_code == 200
    assert seen[-1]["messages"][0]["images"] == [IMAGE.partition(",")[2]]
    await registry.close()


# =============================================================================
# PII audit events point at the right input (second review)
# =============================================================================


@pytest.fixture
async def audited(wire, tmp_path):
    """The wire app with a real audit logger on SQLite (direct writes)."""
    from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine

    client, engine = wire
    db = await create_async_db_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/a.db"))
    client.app.state.audit_logger = AuditLogger(db)
    yield client, db
    await db.dispose()


async def _pii_events(db) -> list[dict]:
    from sqlalchemy import select

    from gateway.storage.schema import pii_events

    async with db.connect() as conn:
        rows = await conn.execute(select(pii_events).order_by(pii_events.c.id))
        return [dict(r._mapping) for r in rows]


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


class TestPIIAuditMapping:
    @pytest.mark.asyncio
    async def test_email_in_a_content_part_is_audited(self, audited):
        """Produced zero events: content parts weren't mapped back."""
        client, db = audited
        parts = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            {"type": "text", "text": f"who is {EMAIL}?"},
        ]
        messages = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": parts},
        ]
        resp = client.post("/v1/chat/completions", json={"model": "m", "messages": messages})
        assert resp.status_code == 200
        [event] = await _pii_events(db)
        assert (event["message_index"], event["part_index"]) == (1, 1)
        assert event["message_role"] == "user"
        assert event["value_hash"] == _sha(EMAIL)
        text = parts[1]["text"]
        assert text[event["position_start"] : event["position_end"]] == EMAIL

    @pytest.mark.asyncio
    async def test_embedding_batch_events_point_at_their_input(self, audited):
        """Filtering out clean results misaligned detections: wrong index, wrong hash."""
        client, db = audited
        other = "sam@example.org"
        inputs = ["nothing here", f"mail {EMAIL}", "still clean", f"or {other}"]
        resp = client.post("/v1/embeddings", json={"model": "m", "input": inputs})
        assert resp.status_code == 200
        events = await _pii_events(db)
        assert [(e["message_index"], e["value_hash"]) for e in events] == [
            (1, _sha(EMAIL)),
            (3, _sha(other)),
        ]

    @pytest.mark.asyncio
    async def test_completion_prompt_list_events_point_at_their_prompt(self, audited):
        client, db = audited
        prompts = ["clean", "clean too", f"x {EMAIL}"]
        resp = client.post("/v1/completions", json={"model": "m", "prompt": prompts})
        assert resp.status_code == 200
        [event] = await _pii_events(db)
        assert (event["message_index"], event["value_hash"]) == (2, _sha(EMAIL))

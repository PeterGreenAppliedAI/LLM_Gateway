"""Tests for Ollama-compatible routes: passthrough contract and streaming.

Covers the LocalClaw integration requirements (2026-07-06):
- `format` forwarded both as "json" and as a JSON-schema dict
- `options` passthrough verbatim (num_ctx, top_k, ... never dropped)
- keep_alive forwarded
- no invented defaults (num_predict, temperature, num_ctx)
- streamed tool_calls survive with object arguments
- /api/embed (modern) and /api/embeddings (legacy) shapes
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import AuthConfig, GatewayConfig, ProviderConfig
from gateway.dispatch import Dispatcher, DispatchResult
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import (
    FinishReason,
    ProviderType,
    TaskType,
    UsageStats,
)
from gateway.models.internal import InternalResponse, StreamChunk, ToolCall
from gateway.providers.ollama import OllamaAdapter
from gateway.routes import ollama_router
from gateway.routes.dependencies import get_dispatcher

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app() -> FastAPI:
    """Test app with Ollama routes and auth disabled."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(ollama_router)
    app.state.config = GatewayConfig(
        providers=[
            ProviderConfig(
                name="ollama",
                type=ProviderType.OLLAMA,
                base_url="http://localhost:11434",
            )
        ],
        auth=AuthConfig(enabled=False),
    )
    app.state.registry = None
    app.state.enforcer = None
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def make_chat_response(content: str = "hello") -> InternalResponse:
    return InternalResponse(
        request_id="req-1",
        task=TaskType.CHAT,
        provider="ollama",
        model="phi4:14b",
        content=content,
        finish_reason=FinishReason.STOP,
        usage=UsageStats.from_counts(prompt=5, completion=7),
    )


@pytest.fixture
def mock_dispatcher(app: FastAPI):
    """Dispatcher mock capturing the InternalRequest it receives."""
    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=DispatchResult(
            response=make_chat_response(),
            provider_used="ollama",
            was_fallback=False,
            attempted_providers=["ollama"],
        )
    )
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    yield dispatcher
    app.dependency_overrides.pop(get_dispatcher, None)


def dispatched_request(mock_dispatcher):
    return mock_dispatcher.dispatch.call_args.args[0]


# =============================================================================
# format passthrough
# =============================================================================


class TestFormatPassthrough:
    def test_chat_format_json_string(self, client, mock_dispatcher):
        resp = client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "Return JSON"}],
                "format": "json",
                "stream": False,
            },
        )
        assert resp.status_code == 200
        internal = dispatched_request(mock_dispatcher)
        assert internal.response_format == {"type": "json_object"}

    def test_chat_format_schema_object(self, client, mock_dispatcher):
        schema = {"type": "string", "enum": ["chat", "web_search", "memory", "exec"]}
        resp = client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "Classify"}],
                "format": schema,
                "stream": False,
            },
        )
        assert resp.status_code == 200
        internal = dispatched_request(mock_dispatcher)
        assert internal.response_format["type"] == "json_schema"
        assert internal.response_format["json_schema"]["schema"] == schema

    def test_generate_format_schema_object(self, client, mock_dispatcher):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        resp = client.post(
            "/api/generate",
            json={
                "model": "phi4:14b",
                "prompt": "Return ok true",
                "format": schema,
                "stream": False,
            },
        )
        assert resp.status_code == 200
        internal = dispatched_request(mock_dispatcher)
        assert internal.response_format["json_schema"]["schema"] == schema

    def test_no_format_means_no_response_format(self, client, mock_dispatcher):
        client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.response_format is None


# =============================================================================
# options / keep_alive passthrough, no invented defaults
# =============================================================================


class TestOptionsPassthrough:
    def test_options_pass_through_verbatim(self, client, mock_dispatcher):
        options = {
            "num_ctx": 131072,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "stop": ["</answer>"],
            "temperature": 0.1,
            "num_predict": 20,
        }
        client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "options": options,
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.options == options
        # Policed fields mirrored into normalized fields
        assert internal.max_tokens == 20
        assert internal.temperature == 0.1

    def test_keep_alive_forwarded(self, client, mock_dispatcher):
        client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "keep_alive": "30m",
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.extensions["keep_alive"] == "30m"

    def test_no_invented_defaults(self, client, mock_dispatcher):
        client.post(
            "/api/chat",
            json={
                "model": "phi4:14b",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.max_tokens is None
        assert internal.temperature is None
        assert internal.top_p is None
        assert internal.options == {}

    def test_generate_system_template_forwarded(self, client, mock_dispatcher):
        client.post(
            "/api/generate",
            json={
                "model": "phi4:14b",
                "prompt": "hello",
                "system": "You are terse.",
                "keep_alive": "30m",
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.extensions["system"] == "You are terse."
        assert internal.extensions["keep_alive"] == "30m"


# =============================================================================
# Adapter payload construction (what actually reaches the engine)
# =============================================================================


class TestOllamaAdapterPayload:
    @pytest.fixture
    def adapter(self) -> OllamaAdapter:
        return OllamaAdapter(
            ProviderConfig(
                name="ollama", type=ProviderType.OLLAMA, base_url="http://localhost:11434"
            )
        )

    def make_request(self, **kwargs):
        from gateway.models.internal import InternalRequest, Message

        defaults = {
            "task": TaskType.CHAT,
            "model": "phi4:14b",
            "messages": [Message(role="user", content="hi")],
        }
        defaults.update(kwargs)
        return InternalRequest(**defaults)

    def test_client_options_win_and_pass_verbatim(self, adapter):
        req = self.make_request(
            options={"num_ctx": 131072, "top_k": 40, "num_predict": 50},
            max_tokens=999,  # normalized field must NOT override client's num_predict
        )
        payload = adapter._build_chat_request(req)
        assert payload["options"]["num_ctx"] == 131072
        assert payload["options"]["top_k"] == 40
        assert payload["options"]["num_predict"] == 50

    def test_nothing_invented_when_unset(self, adapter):
        payload = adapter._build_chat_request(self.make_request())
        # No options key at all: engine defaults apply
        assert "options" not in payload
        assert "format" not in payload
        assert "keep_alive" not in payload
        assert payload["model"] == "phi4:14b"

    def test_no_hardcoded_num_ctx(self, adapter):
        req = self.make_request(max_tokens=100)
        payload = adapter._build_chat_request(req)
        assert "num_ctx" not in payload["options"]
        assert payload["options"]["num_predict"] == 100

    def test_format_json_object_roundtrip(self, adapter):
        req = self.make_request(response_format={"type": "json_object"})
        payload = adapter._build_chat_request(req)
        assert payload["format"] == "json"

    def test_format_schema_roundtrip(self, adapter):
        schema = {"type": "string", "enum": ["a", "b"]}
        req = self.make_request(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }
        )
        payload = adapter._build_chat_request(req)
        assert payload["format"] == schema

    def test_keep_alive_in_payload(self, adapter):
        req = self.make_request(extensions={"keep_alive": "30m"})
        payload = adapter._build_chat_request(req)
        assert payload["keep_alive"] == "30m"

    def test_generate_extensions_in_payload(self, adapter):
        from gateway.models.internal import InternalRequest

        req = InternalRequest(
            task=TaskType.GENERATE,
            model="phi4:14b",
            prompt="hello",
            extensions={"system": "Be terse.", "keep_alive": "30m", "context": [1, 2]},
        )
        payload = adapter._build_generate_request(req)
        assert payload["system"] == "Be terse."
        assert payload["keep_alive"] == "30m"
        assert payload["context"] == [1, 2]
        assert "options" not in payload


# =============================================================================
# Streamed tool_calls survival
# =============================================================================


class TestStreamedToolCalls:
    def test_tool_calls_survive_streaming(self, app, client):
        tool_chunk = StreamChunk(
            request_id="req-1",
            index=0,
            delta="",
            tool_calls=[
                ToolCall(
                    type="function",
                    function={"name": "get_weather", "arguments": {"city": "Boston"}},
                )
            ],
        )
        final_chunk = StreamChunk(
            request_id="req-1",
            index=1,
            delta="",
            finish_reason=FinishReason.STOP,
            usage=UsageStats.from_counts(prompt=10, completion=5),
        )

        async def fake_stream():
            yield tool_chunk
            yield final_chunk

        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch_stream = AsyncMock(return_value=("ollama", fake_stream()))
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        try:
            resp = client.post(
                "/api/chat",
                json={
                    "model": "qwen3.6:35b",
                    "messages": [{"role": "user", "content": "Weather in Boston?"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                },
                            },
                        }
                    ],
                    "stream": True,
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)

        assert resp.status_code == 200
        lines = [json.loads(line) for line in resp.text.strip().split("\n")]
        tool_lines = [line for line in lines if line.get("message", {}).get("tool_calls")]
        assert tool_lines, f"no tool_calls in streamed chunks: {lines}"
        tc = tool_lines[0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        # Ollama shape: arguments is an OBJECT, not a stringified blob
        assert tc["function"]["arguments"] == {"city": "Boston"}
        # Final chunk carries token counts
        assert lines[-1]["prompt_eval_count"] == 10
        assert lines[-1]["eval_count"] == 5


# =============================================================================
# Embeddings endpoints
# =============================================================================


def make_embed_response(vectors) -> InternalResponse:
    return InternalResponse(
        request_id="req-1",
        task=TaskType.EMBEDDINGS,
        provider="ollama",
        model="qwen3-embedding:8b",
        embeddings=vectors,
        finish_reason=FinishReason.STOP,
        usage=UsageStats(prompt_tokens=3),
    )


class TestEmbeddings:
    def _override(self, app, vectors):
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=make_embed_response(vectors),
                provider_used="ollama",
                was_fallback=False,
                attempted_providers=["ollama"],
            )
        )
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        return dispatcher

    def test_modern_embed_endpoint(self, app, client):
        dispatcher = self._override(app, [[0.1, 0.2], [0.3, 0.4]])
        try:
            resp = client.post(
                "/api/embed",
                json={"model": "qwen3-embedding:8b", "input": ["a", "b"]},
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == "qwen3-embedding:8b"
        assert body["embeddings"] == [[0.1, 0.2], [0.3, 0.4]]
        # Multi-input requests carry all inputs upstream
        internal = dispatcher.dispatch.call_args.args[0]
        assert internal.input_data == ["a", "b"]

    def test_modern_embed_single_string(self, app, client):
        self._override(app, [[0.5, 0.6]])
        try:
            resp = client.post(
                "/api/embed",
                json={"model": "qwen3-embedding:8b", "input": "hello"},
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)
        assert resp.json()["embeddings"] == [[0.5, 0.6]]

    def test_legacy_embeddings_endpoint(self, app, client):
        self._override(app, [[0.7, 0.8]])
        try:
            resp = client.post(
                "/api/embeddings",
                json={"model": "qwen3-embedding:8b", "prompt": "hello"},
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)
        assert resp.status_code == 200
        assert resp.json()["embedding"] == [0.7, 0.8]


# =============================================================================
# Dispatcher error taxonomy
# =============================================================================


class TestRetryableClassification:
    def test_classification(self):
        assert Dispatcher._is_retryable_error("timeout") is True
        assert Dispatcher._is_retryable_error("connection_error") is True
        assert Dispatcher._is_retryable_error("http_503") is True
        assert Dispatcher._is_retryable_error("http_500") is True
        assert Dispatcher._is_retryable_error(None) is True
        # Upstream 4xx means the request is wrong — do not mask via fallback
        assert Dispatcher._is_retryable_error("http_404") is False
        assert Dispatcher._is_retryable_error("http_400") is False
        assert Dispatcher._is_retryable_error("http_422") is False


# =============================================================================
# Auth split: control plane requires a key, inference stays stock-Ollama
# =============================================================================


class TestAuthSplit:
    """With auth enabled: dashboard endpoints demand a key; inference does not.

    LocalClaw (and any stock Ollama client) sends no auth header, so
    /api/chat must keep working anonymously while /api/stats — which
    exposes stored traffic — returns 401 without a valid key.
    """

    @pytest.fixture
    def auth_app(self) -> FastAPI:
        from gateway.config import ApiKeyConfig
        from gateway.routes.dashboard import router as dashboard_router

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(ollama_router)
        app.include_router(dashboard_router)
        app.state.config = GatewayConfig(
            providers=[
                ProviderConfig(
                    name="ollama",
                    type=ProviderType.OLLAMA,
                    base_url="http://localhost:11434",
                )
            ],
            auth=AuthConfig(
                enabled=True,
                api_keys=[ApiKeyConfig(key="test-api-key-12345678", client_id="tester")],
            ),
        )
        app.state.registry = None
        app.state.enforcer = None
        return app

    def test_dashboard_requires_key(self, auth_app):
        client = TestClient(auth_app)
        assert client.get("/api/stats").status_code == 401
        assert (
            client.get("/api/stats", headers={"X-API-Key": "wrong-key-12345678"}).status_code == 401
        )
        assert (
            client.get("/api/stats", headers={"X-API-Key": "test-api-key-12345678"}).status_code
            == 200
        )

    def test_inference_stays_anonymous(self, auth_app):
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=make_chat_response(),
                provider_used="ollama",
                was_fallback=False,
                attempted_providers=["ollama"],
            )
        )
        auth_app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        try:
            client = TestClient(auth_app)
            resp = client.post(
                "/api/chat",
                json={
                    "model": "phi4:14b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        finally:
            auth_app.dependency_overrides.pop(get_dispatcher, None)
        assert resp.status_code == 200

    def _anonymous_chat(self, auth_app, model: str = "phi4:14b"):
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=make_chat_response(),
                provider_used="ollama",
                was_fallback=False,
                attempted_providers=["ollama"],
            )
        )
        auth_app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        try:
            return TestClient(auth_app).post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        finally:
            auth_app.dependency_overrides.pop(get_dispatcher, None)

    def test_anonymous_disabled_requires_key(self, auth_app):
        auth_app.state.config.auth.anonymous.enabled = False
        assert self._anonymous_chat(auth_app).status_code == 401

    def test_anonymous_model_allowlist_applies(self, auth_app):
        """Dropping the key must not escape model restrictions."""
        auth_app.state.config.auth.anonymous.allowed_models = ["llama3.1:*"]
        resp = self._anonymous_chat(auth_app, model="phi4:14b")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "model_not_allowed"
        assert self._anonymous_chat(auth_app, model="llama3.1:8b").status_code == 200

    def test_control_plane_writes_require_admin(self, auth_app, monkeypatch):
        """A client key can read the dashboard but not change budgets."""
        from pydantic import SecretStr

        import gateway.settings
        from gateway.settings import Settings

        admin_key = "admin-key-1234567890"
        monkeypatch.setattr(
            gateway.settings,
            "get_settings",
            lambda: Settings(admin_api_key=SecretStr(admin_key)),
        )
        client = TestClient(auth_app)
        tier = {"name": "cheap", "cost_multiplier": 0.1}

        resp = client.post(
            "/api/budget/tiers", json=tier, headers={"X-API-Key": "test-api-key-12345678"}
        )
        assert resp.status_code == 401
        resp = client.post("/api/budget/tiers", json=tier, headers={"X-API-Key": admin_key})
        assert resp.status_code == 200

        # One dashboard key serves both: admin can read, client can read
        assert client.get("/api/stats", headers={"X-API-Key": admin_key}).status_code == 200
        assert (
            client.get("/api/stats", headers={"X-API-Key": "test-api-key-12345678"}).status_code
            == 200
        )

    def test_anonymous_policy_ignored_when_auth_disabled(self, auth_app):
        auth_app.state.config.auth.enabled = False
        auth_app.state.config.auth.anonymous.enabled = False
        assert self._anonymous_chat(auth_app).status_code == 200


class TestThinkPassthrough:
    """The `think` parameter reaches the engine; `thinking` comes back.

    Regression: think was silently dropped (not declared on the request
    model), so clients could neither disable rumination nor toggle
    thinking-separation — and the non-streaming response dropped the
    separated `thinking` field entirely.
    """

    def test_think_forwarded_to_internal(self, client, mock_dispatcher):
        client.post(
            "/api/chat",
            json={
                "model": "qwen3.6:27b",
                "messages": [{"role": "user", "content": "hi"}],
                "think": False,
                "stream": False,
            },
        )
        internal = dispatched_request(mock_dispatcher)
        assert internal.extensions["think"] is False

    def test_think_in_adapter_payload(self):
        from gateway.config import ProviderConfig
        from gateway.models.internal import InternalRequest, Message

        adapter = OllamaAdapter(
            ProviderConfig(
                name="ollama", type=ProviderType.OLLAMA, base_url="http://localhost:11434"
            )
        )
        req = InternalRequest(
            task=TaskType.CHAT,
            model="qwen3.6:27b",
            messages=[Message(role="user", content="hi")],
            extensions={"think": "high"},
        )
        payload = adapter._build_chat_request(req)
        assert payload["think"] == "high"

        gen_req = InternalRequest(
            task=TaskType.GENERATE,
            model="qwen3.6:27b",
            prompt="hi",
            extensions={"think": False},
        )
        gen_payload = adapter._build_generate_request(gen_req)
        assert gen_payload["think"] is False

    def test_thinking_returned_non_streaming(self, app, client):
        response = InternalResponse(
            request_id="req-1",
            task=TaskType.CHAT,
            provider="ollama",
            model="qwen3.6:27b",
            content="4",
            thinking="The user asks 2+2. That is 4.",
            finish_reason=FinishReason.STOP,
            usage=UsageStats.from_counts(prompt=5, completion=3),
        )
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=response,
                provider_used="ollama",
                was_fallback=False,
                attempted_providers=["ollama"],
            )
        )
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        try:
            resp = client.post(
                "/api/chat",
                json={
                    "model": "qwen3.6:27b",
                    "messages": [{"role": "user", "content": "2+2?"}],
                    "stream": False,
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)
        msg = resp.json()["message"]
        assert msg["content"] == "4"
        assert msg["thinking"] == "The user asks 2+2. That is 4."


# =============================================================================
# Environments scope routing
# =============================================================================


class TestEnvironmentScope:
    """Environments restrict endpoints and models; keys can't escape theirs.

    Regression: get_environment() existed but no route used it, so
    environment allowed_endpoints/endpoint_filter/approved_models had no
    effect, and an unknown X-Environment silently meant "no restrictions".
    """

    PROD_KEY = "prod-key-1234567890"

    @pytest.fixture
    def env_app(self) -> FastAPI:
        from gateway.config import ApiKeyConfig, EndpointConfig, EnvironmentConfig

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(ollama_router)
        app.state.config = GatewayConfig(
            endpoints=[
                EndpointConfig(
                    name="gpu-prod",
                    type=ProviderType.OLLAMA,
                    url="http://gpu-prod:11434",
                    labels={"tier": "prod"},
                ),
                EndpointConfig(
                    name="gpu-dev",
                    type=ProviderType.OLLAMA,
                    url="http://gpu-dev:11434",
                ),
            ],
            environments=[
                EnvironmentConfig(name="dev", allow_all_discovered=True),
                EnvironmentConfig(
                    name="prod",
                    endpoint_filter={"tier": "prod"},
                    approved_models=["phi4:*"],
                ),
            ],
            auth=AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(key=self.PROD_KEY, client_id="prod-app", environment="prod")
                ],
            ),
        )
        app.state.registry = None
        app.state.enforcer = None
        return app

    def _chat(self, app, model="phi4:14b", headers=None):
        dispatcher = AsyncMock(spec=Dispatcher)
        dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=make_chat_response(),
                provider_used="gpu-prod",
                was_fallback=False,
                attempted_providers=["gpu-prod"],
            )
        )
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher
        try:
            resp = TestClient(app).post(
                "/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                headers=headers or {},
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)
        sent = dispatcher.dispatch.call_args[0][0] if dispatcher.dispatch.called else None
        return resp, sent

    def test_key_environment_limits_endpoints(self, env_app):
        resp, sent = self._chat(env_app, headers={"X-API-Key": self.PROD_KEY})
        assert resp.status_code == 200
        assert sent.environment == "prod"
        assert sent.allowed_endpoints == ["gpu-prod"]

    def test_key_environment_cannot_be_overridden(self, env_app):
        resp, sent = self._chat(
            env_app, headers={"X-API-Key": self.PROD_KEY, "X-Environment": "dev"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "environment_not_allowed"
        assert sent is None

    def test_unapproved_model_refused(self, env_app):
        resp, sent = self._chat(env_app, model="llama3.1:8b", headers={"X-API-Key": self.PROD_KEY})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "model_not_allowed"
        assert sent is None

    def test_unknown_environment_refused(self, env_app):
        resp, sent = self._chat(env_app, headers={"X-Environment": "nope"})
        assert resp.status_code == 403
        assert sent is None

    def test_default_environment_for_keyless(self, env_app):
        resp, sent = self._chat(env_app, model="llama3.1:8b")
        assert resp.status_code == 200
        assert sent.environment == "dev"
        assert sent.allowed_endpoints == ["gpu-dev", "gpu-prod"]

"""Regression tests for the 2026-10-09 independent review (one per finding).

1. Tool-call arguments are PII-scanned and scrubbed (dict and JSON-string forms).
2. Budgets reject capless text-generation requests (loud 4xx, no invented cap).
3. A partially failed batch still charges its completed generations.
4. Audit error_message fields are redacted like bodies.
5. Policy denials are written to the durable audit trail.
6. /api/tags follows the anonymous-network policy and scopes listings.
7. Malformed content parts return 4xx, never 500.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import AuthConfig, GatewayConfig, ProviderConfig
from gateway.errors import ValidationError
from gateway.models.common import ProviderType, TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer, PolicyViolation
from gateway.policy.token_budget import TokenBudgetConfig
from gateway.security.pii import PIIScrubber

EMAIL = "a.b@example.com"


# =============================================================================
# 1. Tool-call arguments
# =============================================================================


class TestToolCallArgumentScrubbing:
    def test_dict_arguments_scrubbed(self):
        scrubber = PIIScrubber()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "send_email", "arguments": {"to": EMAIL, "n": 3}}}
                ],
            }
        ]
        out, findings = scrubber.scan_messages(messages, scrub=True)
        args = out[0]["tool_calls"][0]["function"]["arguments"]
        assert EMAIL not in str(args)
        assert args["to"] == "[EMAIL]"
        assert args["n"] == 3  # JSON shape preserved
        assert any(f.result.has_pii for f in findings)

    def test_json_string_arguments_scrubbed(self):
        """OpenAI format keeps arguments as a JSON string."""
        scrubber = PIIScrubber()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "f", "arguments": f'{{"to": "{EMAIL}"}}'}}],
            }
        ]
        out, findings = scrubber.scan_messages(messages, scrub=True)
        args = out[0]["tool_calls"][0]["function"]["arguments"]
        assert EMAIL not in args
        assert "[EMAIL]" in args

    def test_flag_only_mode_detects_without_mutating(self):
        scrubber = PIIScrubber()
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "f", "arguments": {"to": EMAIL}}}],
            }
        ]
        out, findings = scrubber.scan_messages(messages, scrub=False)
        assert out[0]["tool_calls"][0]["function"]["arguments"]["to"] == EMAIL
        assert any(f.result.has_pii for f in findings)


# =============================================================================
# 2. Capless requests under budget enforcement
# =============================================================================


class TestCaplessBudgetRejection:
    def _enforcer(self) -> PolicyEnforcer:
        return PolicyEnforcer(
            PolicyConfig(token_budget=TokenBudgetConfig(enabled=True, default_daily_limit=10_000))
        )

    def _chat(self, **updates) -> InternalRequest:
        base = InternalRequest(
            task=TaskType.CHAT,
            model="m",
            messages=[Message(role=MessageRole.USER, content="hi")],
        )
        return base.model_copy(update=updates) if updates else base

    @pytest.mark.asyncio
    async def test_capless_chat_rejected(self):
        with pytest.raises(PolicyViolation) as exc_info:
            await self._enforcer().enforce(self._chat(), rate_limit_key="k")
        assert exc_info.value.code == "budget_requires_output_bound"

    @pytest.mark.asyncio
    async def test_capped_chat_passes(self):
        await self._enforcer().enforce(self._chat(max_tokens=100), rate_limit_key="k")

    @pytest.mark.asyncio
    async def test_embeddings_exempt(self):
        req = InternalRequest(task=TaskType.EMBEDDINGS, model="e", input_data=["x"])
        await self._enforcer().enforce(req, rate_limit_key="k")

    @pytest.mark.asyncio
    async def test_explicit_estimate_exempt(self):
        """Media routes pass their own admission estimate."""
        req = InternalRequest(task=TaskType.CHAT, model="m", prompt="x")
        await self._enforcer().enforce(req, rate_limit_key="k", estimated_tokens=50)

    @pytest.mark.asyncio
    async def test_budgets_disabled_no_requirement(self):
        enforcer = PolicyEnforcer(PolicyConfig())
        await enforcer.enforce(self._chat(), rate_limit_key="k")


# =============================================================================
# 3. Partial batch charging
# =============================================================================


class TestPartialBatchCharging:
    @pytest.mark.asyncio
    async def test_completed_generations_reported_on_failure(self):
        from gateway.routes.fanout import dispatch_choices

        ok = MagicMock(name="ok_result")
        started = asyncio.Event()

        async def dispatch(internal_request):
            if internal_request.prompt == "good":
                started.set()
                return ok
            await started.wait()  # ensure the good one completes first
            raise RuntimeError("upstream exploded")

        dispatcher = MagicMock()
        dispatcher.dispatch = dispatch
        http_request = MagicMock()

        async def never_disconnects():
            while True:
                await asyncio.sleep(3600)

        http_request.receive = never_disconnects

        charged: list = []
        reqs = [
            InternalRequest(task=TaskType.COMPLETION, model="m", prompt="good"),
            InternalRequest(task=TaskType.COMPLETION, model="m", prompt="bad"),
        ]
        with pytest.raises(RuntimeError):
            await dispatch_choices(http_request, dispatcher, reqs, on_partial_usage=charged.extend)
        assert charged == [ok]


# =============================================================================
# 4. error_message redaction
# =============================================================================


class TestErrorMessageRedaction:
    @pytest.mark.asyncio
    async def test_error_message_redacted_in_audit_row(self, tmp_path):
        from sqlalchemy import select

        from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine
        from gateway.storage.schema import audit_log

        engine = await create_async_db_engine(
            DatabaseConfig(url=f"sqlite:///{tmp_path}/a.db", create_tables=True)
        )
        try:
            logger_ = AuditLogger(engine=engine, body_redactor=PIIScrubber().redact)
            await logger_.log_request(
                request_id="r1",
                client_id="c",
                task="chat",
                model="m",
                endpoint="ep",
                status="error",
                error_code="http_400",
                error_message=f"upstream said: invalid value {EMAIL} in prompt",
            )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(select(audit_log).where(audit_log.c.request_id == "r1"))
                ).fetchone()
            assert EMAIL not in row.error_message
            assert "[EMAIL]" in row.error_message
        finally:
            await engine.dispose()


# =============================================================================
# 5. Denials in the audit trail
# =============================================================================


class TestDenialAuditing:
    @pytest.mark.asyncio
    async def test_policy_denial_written(self):
        from gateway.errors import ErrorCode, PolicyError
        from gateway.exception_handlers import gateway_error_handler
        from gateway.routes.dependencies import setup_request_context

        app = FastAPI()
        audit = AsyncMock()
        audit._redact_text = lambda t: t
        app.state.audit_logger = audit

        scope = {"type": "http", "app": app, "method": "POST", "path": "/api/chat", "headers": []}
        from starlette.requests import Request as StarletteRequest

        request = StarletteRequest(scope)
        setup_request_context(client_id="client-x", model="m", task="chat")

        exc = PolicyError(message="Model 'm' is not in allowed models for this API key")
        response = await gateway_error_handler(request, exc)

        assert response.status_code == 403
        audit.log_request.assert_awaited_once()
        kwargs = audit.log_request.await_args.kwargs
        assert kwargs["status"] == "denied"
        assert kwargs["client_id"] == "client-x"
        assert kwargs["error_code"] == ErrorCode.POLICY_VIOLATION.value


# =============================================================================
# 6. Model listing access
# =============================================================================


class TestListingAccess:
    def _app(self, anonymous_enabled: bool) -> FastAPI:
        from gateway.catalog.models import DiscoveredModel, ModelCatalog
        from gateway.exception_handlers import register_exception_handlers
        from gateway.routes import ollama_router

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(ollama_router)
        app.state.config = GatewayConfig(
            providers=[
                ProviderConfig(name="ep1", type=ProviderType.OLLAMA, base_url="http://x:1"),
            ],
            auth=AuthConfig(
                enabled=True,
                api_keys=[],
                anonymous={
                    "enabled": anonymous_enabled,
                    "allowed_networks": ["127.0.0.0/8", "::1/128"],
                },
            ),
        )
        catalog = ModelCatalog()
        catalog.add_model(DiscoveredModel(name="m1", endpoint="ep1"))
        registry = MagicMock()
        registry.catalog = catalog
        app.state.registry = registry
        app.state.enforcer = None
        return app

    def test_tags_denied_when_anonymous_disabled(self):
        client = TestClient(self._app(anonymous_enabled=False))
        assert client.get("/api/tags").status_code == 401

    def test_tags_allowed_from_permitted_network(self):
        client = TestClient(self._app(anonymous_enabled=True))
        r = client.get("/api/tags")
        assert r.status_code == 200
        assert [m["name"] for m in r.json()["models"]] == ["m1"]


# =============================================================================
# 7. Malformed content parts
# =============================================================================


class TestContentPartValidation:
    def test_string_image_url_data_uri_accepted(self):
        from gateway.routes.content_parts import check_content_parts

        check_content_parts([{"type": "image_url", "image_url": "data:image/png;base64,AAAA"}])

    def test_string_image_url_normalized(self):
        from gateway.routes.content_parts import sanitize_content
        from gateway.security import Sanitizer

        out = sanitize_content(
            Sanitizer(), [{"type": "image_url", "image_url": "data:image/png;base64,AAAA"}]
        )
        assert out[0]["image_url"] == {"url": "data:image/png;base64,AAAA"}

    def test_non_object_image_url_is_validation_error(self):
        from gateway.routes.content_parts import check_content_parts

        with pytest.raises(ValidationError):
            check_content_parts([{"type": "image_url", "image_url": 123}])

    def test_bare_string_part_is_validation_error(self):
        from gateway.routes.content_parts import check_content_parts

        with pytest.raises(ValidationError):
            check_content_parts(["just a string"])

    def test_non_string_text_is_validation_error(self):
        from gateway.routes.content_parts import check_content_parts

        with pytest.raises(ValidationError):
            check_content_parts([{"type": "text", "text": {"nested": "thing"}}])


class TestAuthDenialWithoutContext:
    @pytest.mark.asyncio
    async def test_auth_denial_audited_before_context_exists(self):
        """401s fire in the auth dependency, before any request context —
        the denial row must be written anyway (unattributed is still
        evidence)."""
        from starlette.requests import Request as StarletteRequest

        from gateway.errors import InvalidApiKeyError
        from gateway.exception_handlers import gateway_error_handler
        from gateway.observability.logging import clear_request_context

        clear_request_context()
        app = FastAPI()
        audit = AsyncMock()
        audit._redact_text = lambda t: t
        app.state.audit_logger = audit

        scope = {"type": "http", "app": app, "method": "POST", "path": "/api/chat", "headers": []}
        response = await gateway_error_handler(StarletteRequest(scope), InvalidApiKeyError())

        assert response.status_code == 401
        audit.log_request.assert_awaited_once()
        kwargs = audit.log_request.await_args.kwargs
        assert kwargs["status"] == "denied"
        assert kwargs["client_id"] == "unknown"

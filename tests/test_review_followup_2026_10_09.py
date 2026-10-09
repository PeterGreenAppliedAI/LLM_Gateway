"""Regression tests for the follow-up review of 5b1fe30 (D-056).

1. JSON-string tool arguments are decoded before scanning (escaped PII is
   caught) and numeric PII is scrubbed without corrupting the JSON.
2. Operational logs are redacted at the formatter, on every path.
3. Operational /v1/devmesh/* routes are admin-only.
"""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gateway.settings
from gateway.security.pii import PIIScrubber

EMAIL = "jane.doe@example.com"


def _scrub_args(arguments):
    out, findings = PIIScrubber().scan_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "f", "arguments": arguments}}],
            }
        ],
        scrub=True,
    )
    return out[0]["tool_calls"][0]["function"]["arguments"], findings


class TestToolArgumentDecoding:
    def test_json_escaped_email_is_scrubbed(self):
        """The reviewer's bypass: \\u0040 decodes to '@'."""
        args, findings = _scrub_args('{"email":"jane.doe\\u0040example.com"}')
        decoded = json.loads(args)
        assert EMAIL not in json.dumps(decoded)
        assert decoded["email"] == "[EMAIL]"
        assert any(f.result.has_pii for f in findings)

    def test_numeric_phone_stays_valid_json(self):
        """The regression: an unquoted placeholder made the JSON invalid (500)."""
        args, findings = _scrub_args('{"phone":2025550123,"count":3}')
        decoded = json.loads(args)  # must parse
        assert decoded["phone"] == "[PHONE]"
        assert decoded["count"] == 3
        assert any(f.result.has_pii for f in findings)

    def test_clean_arguments_untouched_byte_for_byte(self):
        original = '{"city":  "Boston", "days": 3}'
        args, _ = _scrub_args(original)
        assert args == original  # no re-serialization when nothing was scrubbed

    def test_booleans_are_not_numbers(self):
        args, _ = _scrub_args('{"flag":true}')
        assert json.loads(args) == {"flag": True}

    def test_non_json_arguments_scanned_as_text(self):
        args, findings = _scrub_args(f"not json, mail {EMAIL}")
        assert EMAIL not in args
        assert any(f.result.has_pii for f in findings)


class TestLogRedaction:
    def _format(self, formatter_cls, record):
        from gateway.observability.logging import LogConfig

        return formatter_cls(LogConfig()).format(record)

    def _record(self, msg, extra_fields=None, exc=None):
        record = logging.LogRecord(
            "gateway.dispatch", logging.WARNING, __file__, 1, msg, None, None
        )
        if extra_fields is not None:
            record.extra_fields = extra_fields
        if exc is not None:
            try:
                raise exc
            except Exception:
                import sys

                record.exc_info = sys.exc_info()
        return record

    def test_dispatcher_style_structured_field_redacted(self):
        """The reviewer's probe: retryable-error log before the central handler."""
        from gateway.observability.logging import StructuredJsonFormatter

        out = self._format(
            StructuredJsonFormatter,
            self._record(
                "Provider returned retryable error",
                extra_fields={"error": f"HTTP 500: Failed for {EMAIL}", "provider": "ep"},
            ),
        )
        assert EMAIL not in out
        assert "[EMAIL]" in out
        assert json.loads(out)["provider"] == "ep"

    def test_message_and_exception_redacted(self):
        from gateway.observability.logging import StructuredJsonFormatter

        out = self._format(
            StructuredJsonFormatter,
            self._record(f"failed for {EMAIL}", exc=RuntimeError(f"upstream echoed {EMAIL}")),
        )
        assert EMAIL not in out

    def test_text_formatter_redacted(self):
        from gateway.observability.logging import StructuredTextFormatter

        out = self._format(
            StructuredTextFormatter,
            self._record(f"failed for {EMAIL}", extra_fields={"error": EMAIL}),
        )
        assert EMAIL not in out


ADMIN = "admin-key-1234567890"
CLIENT = "client-key-1234567890"


class TestOperationalCatalogIsAdminOnly:
    @pytest.fixture
    def client(self, monkeypatch):
        from gateway.config import ApiKeyConfig, AuthConfig, GatewayConfig, ProviderConfig
        from gateway.exception_handlers import register_exception_handlers
        from gateway.models.common import ProviderType
        from gateway.routes import devmesh_router
        from gateway.settings import Settings

        monkeypatch.setattr(
            gateway.settings, "get_settings", lambda: Settings(admin_api_key=SecretStr(ADMIN))
        )
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(devmesh_router)
        app.state.config = GatewayConfig(
            providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")],
            auth=AuthConfig(
                enabled=True,
                api_keys=[ApiKeyConfig(key=CLIENT, client_id="app", allowed_models=["public-*"])],
                anonymous={"enabled": True},
            ),
        )
        app.state.registry = None
        app.state.enforcer = None
        return TestClient(app)

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/v1/devmesh/catalog"),
            ("get", "/v1/devmesh/providers"),
            ("post", "/v1/devmesh/catalog/refresh"),
            ("post", "/v1/devmesh/providers/ep/health"),
        ],
    )
    def test_client_key_and_keyless_refused(self, client, method, path):
        assert getattr(client, method)(path, headers={"X-API-Key": CLIENT}).status_code in (
            401,
            403,
        )
        assert getattr(client, method)(path).status_code in (401, 403)

    def test_route_debug_refused_for_clients(self, client):
        r = client.post("/v1/devmesh/route", json={"model": "m"}, headers={"X-API-Key": CLIENT})
        assert r.status_code in (401, 403)


class TestLogRedactionLiveFindings:
    """Found by the post-deploy check against real upstreams."""

    def test_context_model_field_redacted(self):
        from gateway.observability import RequestContext
        from gateway.observability.logging import (
            LogConfig,
            StructuredJsonFormatter,
            clear_request_context,
            set_request_context,
        )

        set_request_context(
            RequestContext(request_id="r1", client_id="c", model=f"probe-{EMAIL}", task="chat")
        )
        try:
            record = logging.LogRecord(
                "httpx", logging.INFO, __file__, 1, "HTTP Request", None, None
            )
            out = StructuredJsonFormatter(LogConfig()).format(record)
        finally:
            clear_request_context()
        assert EMAIL not in out
        assert json.loads(out)["request_id"] == "r1"  # machine fields untouched

    def test_endpoint_ips_survive_log_redaction(self):
        from gateway.observability.logging import redact_for_log

        line = "HTTP Request: POST http://10.0.0.19:11434/api/chat"
        assert redact_for_log(line) == line

    def test_request_scrubbing_still_catches_ips(self):
        assert PIIScrubber().scan("client at 10.0.0.19", scrub=True).has_pii

"""PII scrubbing is configurable at runtime from the dashboard (admin API)."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gateway.settings
from gateway.config import ApiKeyConfig, AuthConfig, GatewayConfig, ProviderConfig
from gateway.dispatch import Dispatcher, DispatchResult
from gateway.exception_handlers import register_exception_handlers
from gateway.main import _load_saved_pii_scrub
from gateway.models.common import ProviderType, TaskType
from gateway.models.internal import InternalResponse
from gateway.routes import openai_router
from gateway.routes.dashboard import router as dashboard_router
from gateway.routes.dependencies import get_dispatcher
from gateway.security.pii import PIIScrubber
from gateway.security.pii_config import PIIScrubConfig
from gateway.settings import Settings
from gateway.storage import DatabaseConfig, RuntimeSettingsStore, create_async_db_engine

ADMIN = "admin-key-1234567890"
CLIENT = "client-key-1234567890"
EMAIL_CHAT = {
    "model": "m:1",
    "messages": [{"role": "user", "content": "mail a.b@example.com"}],
}


@pytest.fixture
async def engine(tmp_path):
    engine = await create_async_db_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path}/gw.db", create_tables=True)
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def app(engine, monkeypatch):
    monkeypatch.setattr(
        gateway.settings, "get_settings", lambda: Settings(admin_api_key=SecretStr(ADMIN))
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(openai_router)
    app.include_router(dashboard_router)
    app.state.config = GatewayConfig(
        providers=[ProviderConfig(name="ep", type=ProviderType.OLLAMA, base_url="http://x:1")],
        auth=AuthConfig(enabled=True, api_keys=[ApiKeyConfig(key=CLIENT, client_id="app")]),
    )
    app.state.registry = None
    app.state.enforcer = None
    app.state.pii_scrubber = PIIScrubber()
    app.state.pii_settings = PIIScrubConfig()  # env default: detect, don't scrub
    app.state.runtime_settings = RuntimeSettingsStore(engine)

    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=DispatchResult(
            response=InternalResponse(
                request_id="r", task=TaskType.CHAT, provider="ep", model="m:1", content="ok"
            ),
            provider_used="ep",
        )
    )
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    app.state.test_dispatcher = dispatcher
    return app


def _sent_content(app) -> str:
    return app.state.test_dispatcher.dispatch.call_args[0][0].messages[0].content


def _put(client, body, key=ADMIN):
    return client.put("/api/pii/config", json=body, headers={"X-API-Key": key})


def test_config_requires_admin(app):
    client = TestClient(app)
    assert client.get("/api/pii/config", headers={"X-API-Key": CLIENT}).status_code == 401
    assert _put(client, {"scrub_enabled": True}, key=CLIENT).status_code == 401
    view = client.get("/api/pii/config", headers={"X-API-Key": ADMIN}).json()
    assert view["detection_enabled"] is True
    assert view["scrub_enabled"] is False
    assert view["source"] == "environment"
    assert "/v1/chat/completions" in view["available_routes"]


def test_change_applies_to_next_request(app):
    client = TestClient(app)
    client.post("/v1/chat/completions", json=EMAIL_CHAT)
    assert _sent_content(app) == "mail a.b@example.com"  # flag-only: model gets original

    resp = _put(client, {"scrub_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["source"] == "dashboard"
    assert resp.json()["updated_by"] == "admin"

    client.post("/v1/chat/completions", json=EMAIL_CHAT)
    assert _sent_content(app) == "mail [EMAIL]"


def test_selected_routes_only(app):
    client = TestClient(app)
    assert _put(client, {"scrub_enabled": True, "scrub_routes": ["/api/chat"]}).status_code == 200
    client.post("/v1/chat/completions", json=EMAIL_CHAT)
    assert _sent_content(app) == "mail a.b@example.com"  # not a selected route


def test_unknown_route_rejected(app):
    resp = _put(TestClient(app), {"scrub_enabled": True, "scrub_routes": ["/v1/chat"]})
    assert resp.status_code == 422
    assert app.state.pii_settings.scrub_enabled is False  # unchanged


def test_requires_detection(app):
    app.state.pii_scrubber = None
    resp = _put(TestClient(app), {"scrub_enabled": True})
    assert resp.status_code == 422
    assert "GATEWAY_PII_ENABLED" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_saved_policy_survives_restart(engine):
    store = RuntimeSettingsStore(engine)
    await store.set("pii.scrub", {"scrub_enabled": True, "scrub_routes": ["/api/chat"]}, "admin")

    restarted = FastAPI()
    restarted.state.runtime_settings = store
    restarted.state.pii_settings = PIIScrubConfig()  # env default
    await _load_saved_pii_scrub(restarted)

    assert restarted.state.pii_settings.scrub_enabled is True
    assert restarted.state.pii_settings.scrub_routes == ["/api/chat"]
    assert restarted.state.pii_settings.source == "dashboard"


@pytest.mark.asyncio
async def test_invalid_saved_policy_keeps_env_default(engine):
    store = RuntimeSettingsStore(engine)
    await store.set("pii.scrub", {"scrub_enabled": True, "scrub_routes": ["/nope"]}, "admin")
    restarted = FastAPI()
    restarted.state.runtime_settings = store
    restarted.state.pii_settings = PIIScrubConfig(scrub_enabled=False)
    await _load_saved_pii_scrub(restarted)
    assert restarted.state.pii_settings.scrub_enabled is False

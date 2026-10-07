"""Access modes: keys, solo, test mode, production profile (D-042)."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

import gateway.settings
from gateway.config import ApiKeyConfig, AuthConfig, GatewayConfig, ProviderConfig
from gateway.dispatch import Dispatcher, DispatchResult
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, ProviderType, TaskType
from gateway.models.internal import InternalResponse
from gateway.routes import ollama_router
from gateway.routes.dashboard import router as dashboard_router
from gateway.routes.dependencies import get_dispatcher
from gateway.routes.health import router as health_router
from gateway.security.access import production_problems
from gateway.settings import Settings

KEY = "client-key-1234567890"
ADMIN = "admin-key-1234567890"
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)
CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}


def _app(auth: AuthConfig, monkeypatch, **settings) -> FastAPI:
    monkeypatch.setattr(gateway.settings, "get_settings", lambda: Settings(**settings))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(ollama_router)
    app.include_router(dashboard_router)
    app.include_router(health_router)
    app.state.config = GatewayConfig(
        providers=[ProviderConfig(name="p", type=ProviderType.OLLAMA, base_url="http://x:1")],
        auth=auth,
    )
    app.state.registry = None
    app.state.enforcer = None
    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.dispatch = AsyncMock(
        return_value=DispatchResult(
            response=InternalResponse(
                request_id="r",
                task=TaskType.CHAT,
                provider="p",
                model="m",
                content="ok",
                finish_reason=FinishReason.STOP,
            ),
            provider_used="p",
        )
    )
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    app.state.dispatcher = dispatcher
    return app


async def _call(app, method, path, client=LOCAL, key=None, headers=None, json=None):
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http:
        h = dict(headers or {})
        if key:
            h["Authorization"] = f"Bearer {key}"
        return await http.request(method, path, headers=h, json=json)


def _client_id(app) -> str:
    return app.state.dispatcher.dispatch.call_args[0][0].client_id


KEYS = AuthConfig(enabled=True, api_keys=[ApiKeyConfig(key=KEY, client_id="app")])


class TestKeysMode:
    @pytest.mark.asyncio
    async def test_keyless_refused_by_default(self, monkeypatch):
        app = _app(KEYS, monkeypatch)
        assert (await _call(app, "POST", "/api/chat", json=CHAT)).status_code == 401
        assert (await _call(app, "POST", "/api/chat", key=KEY, json=CHAT)).status_code == 200

    @pytest.mark.asyncio
    async def test_keyless_opt_in_only_from_allowed_networks(self, monkeypatch):
        auth = AuthConfig(
            enabled=True,
            api_keys=KEYS.api_keys,
            anonymous={"enabled": True, "allowed_networks": ["10.0.0.0/24"]},
        )
        app = _app(auth, monkeypatch)
        ok = await _call(app, "POST", "/api/chat", client=("10.0.0.20", 1), json=CHAT)
        assert ok.status_code == 200
        far = await _call(app, "POST", "/api/chat", client=REMOTE, json=CHAT)
        assert far.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_routes_need_the_admin_key(self, monkeypatch):
        app = _app(KEYS, monkeypatch)  # no GATEWAY_ADMIN_API_KEY
        resp = await _call(app, "GET", "/api/stats", key=KEY)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "admin_key_required"

        app = _app(KEYS, monkeypatch, admin_api_key=SecretStr(ADMIN))
        assert (await _call(app, "GET", "/api/stats", key=KEY)).status_code == 401
        assert (await _call(app, "GET", "/api/stats", key=ADMIN)).status_code == 200


class TestSoloMode:
    @pytest.mark.asyncio
    async def test_local_only(self, monkeypatch):
        app = _app(AuthConfig(enabled=False), monkeypatch)
        assert (await _call(app, "POST", "/api/chat", json=CHAT)).status_code == 200
        far = await _call(app, "POST", "/api/chat", client=REMOTE, json=CHAT)
        assert far.status_code == 403
        assert far.json()["error"]["code"] == "network_not_allowed"
        assert (await _call(app, "GET", "/api/stats")).status_code == 200  # local operator
        assert (await _call(app, "GET", "/api/stats", client=REMOTE)).status_code == 403

    @pytest.mark.asyncio
    async def test_a_key_does_not_bypass_locality(self, monkeypatch):
        app = _app(AuthConfig(enabled=False), monkeypatch)
        far = await _call(app, "POST", "/api/chat", client=REMOTE, key=KEY, json=CHAT)
        assert far.status_code == 403

    @pytest.mark.asyncio
    async def test_proxied_requests_are_not_local(self, monkeypatch):
        """A reverse proxy on the same host would make every request look local."""
        app = _app(AuthConfig(enabled=False), monkeypatch)
        resp = await _call(
            app, "POST", "/api/chat", headers={"X-Forwarded-For": "198.51.100.9"}, json=CHAT
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_widened_networks(self, monkeypatch):
        auth = AuthConfig(enabled=False, anonymous={"allowed_networks": ["203.0.113.0/24"]})
        app = _app(auth, monkeypatch)
        assert (await _call(app, "POST", "/api/chat", client=REMOTE, json=CHAT)).status_code == 200


class TestTestMode:
    @pytest.mark.asyncio
    async def test_keyless_works_even_with_auth_on(self, monkeypatch):
        app = _app(KEYS, monkeypatch, dev_mode=True)
        assert (await _call(app, "POST", "/api/chat", json=CHAT)).status_code == 200
        assert _client_id(app) == "dev"
        assert (await _call(app, "GET", "/api/stats")).status_code == 200  # dashboard too

    @pytest.mark.asyncio
    async def test_keys_still_checked(self, monkeypatch):
        app = _app(KEYS, monkeypatch, dev_mode=True)
        assert (await _call(app, "POST", "/api/chat", key=KEY, json=CHAT)).status_code == 200
        assert _client_id(app) == "app"
        bad = await _call(app, "POST", "/api/chat", key="wrong-key-123456789", json=CHAT)
        assert bad.status_code == 401

    @pytest.mark.asyncio
    async def test_only_from_test_networks(self, monkeypatch):
        app = _app(KEYS, monkeypatch, dev_mode=True)
        assert (await _call(app, "POST", "/api/chat", client=REMOTE, json=CHAT)).status_code == 401
        app = _app(KEYS, monkeypatch, dev_mode=True, dev_networks=["203.0.113.0/24"])
        assert (await _call(app, "POST", "/api/chat", client=REMOTE, json=CHAT)).status_code == 200

    @pytest.mark.asyncio
    async def test_health_reports_the_mode(self, monkeypatch):
        app = _app(KEYS, monkeypatch, dev_mode=True)
        access = (await _call(app, "GET", "/health")).json()["access"]
        assert access["mode"] == "dev"


def test_dev_networks_from_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_DEV_NETWORKS", "192.168.1.0/24, 10.0.0.5")
    assert Settings().dev_networks == ["192.168.1.0/24", "10.0.0.5"]
    monkeypatch.setenv("GATEWAY_DEV_NETWORKS", "not-a-network")
    with pytest.raises(ValueError):
        Settings()


class TestProductionProfile:
    def test_lists_every_unsafe_setting(self):
        config = GatewayConfig(auth=AuthConfig(enabled=False))
        problems = production_problems(config, Settings(dev_mode=True))
        text = " ".join(problems)
        assert "GATEWAY_DEV_MODE" in text
        assert "auth.enabled" in text
        assert "GATEWAY_ADMIN_API_KEY" in text

    def test_unrestricted_keyless_access_refused(self):
        config = GatewayConfig(auth=AuthConfig(enabled=True, anonymous={"enabled": True}))
        problems = production_problems(config, Settings(admin_api_key=SecretStr(ADMIN)))
        assert any("auth.anonymous" in p for p in problems)

    def test_safe_configuration_passes(self):
        config = GatewayConfig(
            auth=AuthConfig(
                enabled=True, anonymous={"enabled": True, "allowed_models": ["llama3.1:*"]}
            )
        )
        assert production_problems(config, Settings(admin_api_key=SecretStr(ADMIN))) == []

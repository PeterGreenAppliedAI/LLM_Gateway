"""Routing policy is configurable at runtime from the dashboard (admin API)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gateway.settings
from gateway.config import ApiKeyConfig, AuthConfig, GatewayConfig, ProviderConfig
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import ProviderType, TaskType
from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
from gateway.routes.dashboard import router as dashboard_router
from gateway.routing_config import RoutingState, RoutingUpdate, apply_routing
from gateway.settings import Settings
from gateway.storage import DatabaseConfig, RuntimeSettingsStore, create_async_db_engine

ADMIN = "admin-key-1234567890"


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
    app.include_router(dashboard_router)
    app.state.config = GatewayConfig(
        providers=[
            ProviderConfig(name="the-mini", type=ProviderType.OLLAMA, base_url="http://m:1"),
            ProviderConfig(name="gpu-node", type=ProviderType.OLLAMA, base_url="http://g:1"),
        ],
        auth=AuthConfig(enabled=True, api_keys=[ApiKeyConfig(key=ADMIN + "x", client_id="c")]),
    )
    app.state.registry = None
    app.state.enforcer = PolicyEnforcer(PolicyConfig())
    app.state.runtime_settings = RuntimeSettingsStore(engine)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def admin(client, method, url, **kw):
    return getattr(client, method)(url, headers={"X-API-Key": ADMIN}, **kw)


PIN_EMBEDDINGS = {
    "strategy": "priority",
    "task_endpoints": [{"task": "embeddings", "allowed_endpoints": ["the-mini"]}],
    "model_defaults": [{"model": "qwen3-embedding*", "endpoint": "the-mini"}],
}


class TestRoutingConfigApi:
    def test_get_requires_admin(self, client):
        assert client.get("/api/routing/config").status_code in (401, 403)

    def test_get_shows_defaults(self, client):
        r = admin(client, "get", "/api/routing/config")
        assert r.status_code == 200
        body = r.json()
        assert body["strategy"] == "priority"
        assert body["task_endpoints"] == []
        assert body["source"] == "config"
        assert set(body["available_endpoints"]) == {"the-mini", "gpu-node"}
        assert "embeddings" in body["available_tasks"]

    def test_put_applies_to_live_enforcer_and_resolution(self, app, client):
        r = admin(client, "put", "/api/routing/config", json=PIN_EMBEDDINGS)
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "dashboard"
        assert body["task_endpoints"][0]["allowed_endpoints"] == ["the-mini"]

        enforcer = app.state.enforcer
        assert enforcer.check_provider_allowed(TaskType.EMBEDDINGS, "the-mini")
        assert not enforcer.check_provider_allowed(TaskType.EMBEDDINGS, "gpu-node")
        # Other tasks untouched
        assert enforcer.check_provider_allowed(TaskType.CHAT, "gpu-node")

        res = app.state.config.resolution
        assert res.model_defaults[0].endpoint == "the-mini"

    def test_put_strategy_switch(self, app, client):
        payload = dict(PIN_EMBEDDINGS, strategy="least_loaded")
        assert admin(client, "put", "/api/routing/config", json=payload).status_code == 200
        assert app.state.config.resolution.strategy == "least_loaded"

    def test_put_rejects_unknown_endpoint(self, client):
        payload = {
            "strategy": "priority",
            "task_endpoints": [{"task": "embeddings", "allowed_endpoints": ["no-such-box"]}],
            "model_defaults": [],
        }
        r = admin(client, "put", "/api/routing/config", json=payload)
        assert r.status_code == 422
        assert "no-such-box" in r.text

    def test_put_rejects_duplicate_task(self, client):
        payload = {
            "strategy": "priority",
            "task_endpoints": [
                {"task": "embeddings", "allowed_endpoints": ["the-mini"]},
                {"task": "embeddings", "denied_endpoints": ["gpu-node"]},
            ],
            "model_defaults": [],
        }
        assert admin(client, "put", "/api/routing/config", json=payload).status_code == 422

    @pytest.mark.asyncio
    async def test_saved_policy_survives_restart(self, app, client, engine):
        admin(client, "put", "/api/routing/config", json=PIN_EMBEDDINGS)

        # Simulate a restart: fresh app, same database
        from gateway.routing_config import load_saved_routing

        app2 = FastAPI()
        app2.state.config = GatewayConfig(
            providers=[
                ProviderConfig(name="the-mini", type=ProviderType.OLLAMA, base_url="http://m:1"),
                ProviderConfig(name="gpu-node", type=ProviderType.OLLAMA, base_url="http://g:1"),
            ]
        )
        app2.state.enforcer = PolicyEnforcer(PolicyConfig())
        app2.state.runtime_settings = RuntimeSettingsStore(engine)
        await load_saved_routing(app2)

        assert app2.state.config.resolution.model_defaults[0].endpoint == "the-mini"
        assert not app2.state.enforcer.check_provider_allowed(TaskType.EMBEDDINGS, "gpu-node")
        assert app2.state.routing_state.source == "dashboard"


class TestApplyRouting:
    def test_apply_preserves_yaml_only_fields(self, app):
        base = app.state.config.resolution
        base.endpoint_priority = ["gpu-node", "the-mini"]
        update = RoutingUpdate.model_validate(PIN_EMBEDDINGS)
        apply_routing(app, update, RoutingState(source="dashboard"))
        assert app.state.config.resolution.endpoint_priority == ["gpu-node", "the-mini"]
        assert app.state.config.resolution.strategy == "priority"

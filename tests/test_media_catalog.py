"""Voice registry (M1b): discovery, profiles, voice-aware routing, validation."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gateway.settings
from gateway.config import ApiKeyConfig, AuthConfig, EndpointConfig, GatewayConfig
from gateway.dispatch.registry import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.media.catalog import MediaCatalog, normalize_voices
from gateway.media.profiles import load_profiles, voice_components
from gateway.models.common import ProviderType
from gateway.routes import audio_router
from gateway.routes.dashboard import router as dashboard_router
from gateway.routes.dependencies import get_audit_logger
from gateway.settings import Settings

PROFILES = load_profiles("config/profiles")


# =============================================================================
# Voice list shapes
# =============================================================================


@pytest.mark.parametrize(
    "payload,expected",
    [
        # Kokoro-FastAPI
        (
            {"voices": [{"id": "af_heart", "name": "af_heart"}], "default_voice": "af_heart"},
            [{"id": "af_heart", "name": "af_heart"}],
        ),
        # Kokoro legacy / plain strings
        (
            {"voices": ["af_sky", "am_adam"]},
            [{"id": "af_sky", "name": "af_sky"}, {"id": "am_adam", "name": "am_adam"}],
        ),
        # speaches: explicit language and gender
        (
            {
                "voices": [{"name": "bf_emma", "language": "en-gb", "gender": "female"}],
                "object": "list",
            },
            [{"id": "bf_emma", "name": "bf_emma", "language": "en-gb", "gender": "female"}],
        ),
        # vLLM-Omni: built-in names plus uploaded clones
        (
            {"voices": ["vivian"], "uploaded_voices": [{"name": "my-clone", "consent": True}]},
            [{"id": "vivian", "name": "vivian"}, {"id": "my-clone", "name": "my-clone"}],
        ),
        # Piper: {id: config}
        (
            {"en_US-lessac-medium": {"language": {"code": "en_US", "name_english": "English"}}},
            [{"id": "en_US-lessac-medium", "name": "en_US-lessac-medium", "language": "English"}],
        ),
        # Bare list, duplicates collapsed
        (["a", "a", {"voice_id": "b"}], [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}]),
    ],
)
def test_normalize_voices(payload, expected):
    assert normalize_voices(payload) == expected


def test_voice_mix_components():
    assert voice_components("af_bella(2)+af_sky(1)-am_adam", blending=True) == [
        "af_bella",
        "af_sky",
        "am_adam",
    ]
    # Without blending support, a '+' is just part of the name
    assert voice_components("a+b", blending=False) == ["a+b"]


def test_invalid_profile_fails_loudly(tmp_path):
    (tmp_path / "bad.yaml").write_text("tts:\n  params:\n    speed: {type: enum}\n")
    with pytest.raises(ValueError, match="bad.yaml"):
        load_profiles(tmp_path)


# =============================================================================
# A fake engine fleet behind the real registry
# =============================================================================


def engine_handler(voices, models=("kokoro",), audio=b"audio"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(200, json=voices) if voices is not None else httpx.Response(404)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})

    handler.seen = seen
    return handler


async def build_app(endpoints: list[EndpointConfig], handlers: dict, **config_kwargs):
    config = GatewayConfig(endpoints=endpoints, **config_kwargs)
    registry = ProviderRegistry(config)
    await registry.initialize()
    for ep in endpoints:
        registry.get(ep.name)._media_client = httpx.AsyncClient(
            base_url=f"http://{ep.name}:1", transport=httpx.MockTransport(handlers[ep.name])
        )
    catalog = MediaCatalog(registry, PROFILES)
    await catalog.refresh()

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(audio_router)
    app.include_router(dashboard_router)
    app.state.config = config
    app.state.registry = registry
    app.state.enforcer = None
    app.state.media_catalog = catalog
    app.dependency_overrides[get_audit_logger] = lambda: AsyncMock()
    return app


def tts(name, profile=None, voices=()):
    return EndpointConfig(
        name=name,
        type=ProviderType.OPENAI,
        url=f"http://{name}:1",
        capabilities=["tts"],
        profile=profile,
        voices=list(voices),
    )


KOKORO_VOICES = {"voices": [{"id": v, "name": v} for v in ("af_heart", "af_bella", "bm_george")]}


def speak(app, **overrides):
    body = {"model": "kokoro", "input": "Hello", "voice": "af_heart", **overrides}
    return TestClient(app).post("/v1/audio/speech", json=body)


# =============================================================================
# Discovery
# =============================================================================


@pytest.mark.asyncio
async def test_profile_enriches_discovered_voices():
    app = await build_app(
        [tts("kokoro-box", profile="kokoro")], {"kokoro-box": engine_handler(KOKORO_VOICES)}
    )
    [entry] = app.state.media_catalog.snapshot()
    george = next(v for v in entry["voices"] if v["id"] == "bm_george")
    assert george["language"] == "British English"
    assert george["gender"] == "male"
    assert entry["voices_source"] == "engine"
    assert entry["blending"] is True
    assert entry["tts_params"]["speed"] == {
        "type": "number",
        "min": 0.25,
        "max": 4.0,
        "default": 1.0,
    }
    assert entry["models"] == [{"id": "kokoro"}]


@pytest.mark.asyncio
async def test_declared_voices_when_engine_lists_none():
    app = await build_app([tts("quiet", voices=["alpha", "beta"])], {"quiet": engine_handler(None)})
    [entry] = app.state.media_catalog.snapshot()
    assert [v["id"] for v in entry["voices"]] == ["alpha", "beta"]
    assert entry["voices_source"] == "config"


# =============================================================================
# Voice-aware routing and validation
# =============================================================================


@pytest.mark.asyncio
async def test_voice_routes_to_the_endpoint_that_has_it():
    a = engine_handler({"voices": ["af_heart"]}, audio=b"from-a")
    b = engine_handler({"voices": ["bm_george"]}, audio=b"from-b")
    app = await build_app(
        [tts("a", profile="kokoro"), tts("b", profile="kokoro")], {"a": a, "b": b}
    )
    resp = speak(app, voice="bm_george")
    assert resp.status_code == 200
    assert resp.content == b"from-b"


@pytest.mark.asyncio
async def test_unknown_voice_rejected_with_choices():
    handler = engine_handler(KOKORO_VOICES)
    app = await build_app([tts("k", profile="kokoro")], {"k": handler})
    resp = speak(app, voice="zz_nobody")
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert "zz_nobody" in message and "af_heart" in message
    assert not [r for r in handler.seen if r.url.path == "/v1/audio/speech"]


@pytest.mark.asyncio
async def test_blend_components_validated():
    app = await build_app([tts("k", profile="kokoro")], {"k": engine_handler(KOKORO_VOICES)})
    assert speak(app, voice="af_heart(2)+af_bella(1)").status_code == 200
    resp = speak(app, voice="af_heart+af_ghost")
    assert resp.status_code == 422
    assert "af_ghost" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_setting_out_of_profile_range_rejected():
    app = await build_app([tts("k", profile="kokoro")], {"k": engine_handler(KOKORO_VOICES)})
    resp = speak(app, speed=9.0)
    assert resp.status_code == 422
    assert "speed must be at most 4" in resp.json()["error"]["message"]
    assert speak(app, lang_code="q").status_code == 422
    assert speak(app, speed=1.5, lang_code="b").status_code == 200


@pytest.mark.asyncio
async def test_unknown_engine_is_never_blocked():
    """No voice list and no profile: unknown is not invalid."""
    app = await build_app([tts("mystery")], {"mystery": engine_handler(None)})
    assert speak(app, voice="anything", speed=99).status_code == 200


# =============================================================================
# Listings
# =============================================================================


@pytest.mark.asyncio
async def test_voices_listing_merges_endpoints():
    a = engine_handler({"voices": ["af_heart", "bm_george"]})
    b = engine_handler({"voices": ["bm_george"]})
    app = await build_app(
        [tts("a", profile="kokoro"), tts("b", profile="kokoro")], {"a": a, "b": b}
    )
    voices = TestClient(app).get("/v1/audio/voices").json()["voices"]
    george = next(v for v in voices if v["id"] == "bm_george")
    assert george["endpoints"] == ["a", "b"]
    assert george["language"] == "British English"


@pytest.mark.asyncio
async def test_voices_listing_respects_key_scope():
    a = engine_handler({"voices": ["af_heart"]})
    b = engine_handler({"voices": ["bm_george"]})
    app = await build_app(
        [tts("a"), tts("b")],
        {"a": a, "b": b},
        auth=AuthConfig(
            enabled=True,
            api_keys=[ApiKeyConfig(key="client-key-1234567890", client_id="app")],
            anonymous={"allowed_endpoints": ["a"]},
        ),
    )
    voices = TestClient(app).get("/v1/audio/voices").json()["voices"]
    assert [v["id"] for v in voices] == ["af_heart"]


@pytest.mark.asyncio
async def test_admin_catalog_requires_admin(monkeypatch):
    monkeypatch.setattr(
        gateway.settings,
        "get_settings",
        lambda: Settings(admin_api_key=SecretStr("admin-key-1234567890")),
    )
    app = await build_app(
        [tts("k", profile="kokoro")],
        {"k": engine_handler(KOKORO_VOICES)},
        auth=AuthConfig(
            enabled=True, api_keys=[ApiKeyConfig(key="client-key-1234567890", client_id="app")]
        ),
    )
    client = TestClient(app)
    assert (
        client.get("/api/media/catalog", headers={"X-API-Key": "client-key-1234567890"}).status_code
        == 401
    )
    resp = client.post("/api/media/catalog/refresh", headers={"X-API-Key": "admin-key-1234567890"})
    assert resp.status_code == 200
    assert resp.json()["endpoints"][0]["profile"] == "kokoro"


def test_startup_fails_on_missing_profile(monkeypatch, tmp_path):
    import gateway.main

    config_file = tmp_path / "gateway.yaml"
    config_file.write_text(
        json.dumps(
            {
                "endpoints": [
                    {
                        "name": "k",
                        "type": "openai",
                        "url": "http://k:1",
                        "capabilities": ["tts"],
                        "profile": "no-such-profile",
                    }
                ]
            }
        )
    )
    settings = Settings(config_path=str(config_file), db={"url": f"sqlite:///{tmp_path}/gw.db"})
    monkeypatch.setattr(gateway.main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="no-such-profile"):
        with TestClient(gateway.main.create_app(settings)):
            pass

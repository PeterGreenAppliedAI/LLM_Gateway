"""Voice routes (/v1/audio/*) over OpenAI-compatible engines (D-020).

A fake engine stands in for Kokoro-FastAPI / speaches / vLLM via
httpx.MockTransport, behind the real registry and adapters.
"""

import io
import json
import struct
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import (
    ApiKeyConfig,
    AuthConfig,
    EndpointConfig,
    GatewayConfig,
    MediaConfig,
)
from gateway.dispatch.registry import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import ProviderType
from gateway.policy import PolicyEnforcer
from gateway.routes import audio_router
from gateway.routes.dependencies import get_audit_logger, get_enforcer
from gateway.security.pii import PIIScrubber
from gateway.security.pii_config import PIIScrubConfig


def _wav(seconds: float, rate: int = 16000) -> bytes:
    """A silent mono 16-bit PCM WAV of the given length."""
    data = b"\x00\x00" * int(seconds * rate)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


class FakeEngine:
    """Records upstream requests; answers with a configurable handler."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)


def _tts_ok(request):
    return httpx.Response(200, content=b"ID3-audio-bytes", headers={"content-type": "audio/mpeg"})


def _stt_ok(request):
    return httpx.Response(200, json={"text": "hello world"})


@pytest.fixture
def make_app():
    """Build an app with the given endpoints; each gets a FakeEngine."""
    registries = []

    async def build(endpoints: dict[str, tuple[list[str], object]], **config_kwargs):
        config = GatewayConfig(
            endpoints=[
                EndpointConfig(
                    name=name, type=ProviderType.OPENAI, url=f"http://{name}:1", capabilities=caps
                )
                for name, (caps, _) in endpoints.items()
            ],
            **config_kwargs,
        )
        registry = ProviderRegistry(config)
        await registry.initialize()
        registries.append(registry)
        engines = {}
        for name, (_, handler) in endpoints.items():
            engine = FakeEngine(handler)
            engines[name] = engine
            registry.get(name)._media_client = httpx.AsyncClient(
                base_url=f"http://{name}:1", transport=httpx.MockTransport(engine)
            )

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(audio_router)
        app.state.config = config
        app.state.registry = registry
        enforcer = PolicyEnforcer()
        enforcer.record_token_usage = MagicMock()
        audit = AsyncMock()
        app.dependency_overrides[get_enforcer] = lambda: enforcer
        app.dependency_overrides[get_audit_logger] = lambda: audit
        app.state.test = {"enforcer": enforcer, "audit": audit, "engines": engines}
        return app

    yield build
    for registry in registries:
        for adapter in registry._adapters.values():
            adapter._media_client = None


def _audit(app) -> dict:
    audit = app.state.test["audit"]
    assert audit.log_request.await_count == 1
    return audit.log_request.await_args.kwargs


SPEECH = {"model": "kokoro", "input": "Hello there", "voice": "af_heart"}


# =============================================================================
# Text-to-speech
# =============================================================================


class TestSpeech:
    @pytest.mark.asyncio
    async def test_audio_relayed_and_recorded(self, make_app):
        app = await make_app({"kokoro-box": (["tts"], _tts_ok)})
        resp = TestClient(app).post(
            "/v1/audio/speech", json={**SPEECH, "lang_code": "b", "speed": 1.2}
        )
        assert resp.status_code == 200
        assert resp.content == b"ID3-audio-bytes"
        assert resp.headers["content-type"] == "audio/mpeg"

        sent = json.loads(app.state.test["engines"]["kokoro-box"].requests[0].content)
        assert sent == {**SPEECH, "lang_code": "b", "speed": 1.2}  # extras pass through

        audit = _audit(app)
        assert audit["status"] == "success"
        assert audit["endpoint"] == "kokoro-box"
        assert audit["task"] == "speech"
        assert audit["media_usage"]["characters"] == len("Hello there")
        assert audit["media_usage"]["bytes_out"] == len(b"ID3-audio-bytes")
        # 11 characters x 0.25 token-equivalents, rounded up
        app.state.test["enforcer"].record_token_usage.assert_called_once_with(
            "default", "kokoro", 3
        )

    @pytest.mark.asyncio
    async def test_only_tts_endpoints_are_used(self, make_app):
        app = await make_app({"whisper": (["stt"], _stt_ok), "kokoro-box": (["tts"], _tts_ok)})
        assert TestClient(app).post("/v1/audio/speech", json=SPEECH).status_code == 200
        assert not app.state.test["engines"]["whisper"].requests

    @pytest.mark.asyncio
    async def test_no_tts_endpoint_is_no_provider(self, make_app):
        # Gateway convention: "nothing can serve this request" is 400 no_provider
        app = await make_app({"whisper": (["stt"], _stt_ok)})
        resp = TestClient(app).post("/v1/audio/speech", json=SPEECH)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "no_provider"

    @pytest.mark.asyncio
    async def test_pin_strips_endpoint_prefix(self, make_app):
        app = await make_app({"a": (["tts"], _tts_ok), "b": (["tts"], _tts_ok)})
        TestClient(app).post("/v1/audio/speech", json={**SPEECH, "model": "b/kokoro"})
        assert not app.state.test["engines"]["a"].requests
        assert json.loads(app.state.test["engines"]["b"].requests[0].content)["model"] == "kokoro"

    @pytest.mark.asyncio
    async def test_pin_to_non_tts_endpoint_rejected(self, make_app):
        app = await make_app({"whisper": (["stt"], _stt_ok), "a": (["tts"], _tts_ok)})
        resp = TestClient(app).post("/v1/audio/speech", json={**SPEECH, "model": "whisper/x"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_upstream_4xx_passes_through_without_failover(self, make_app):
        unknown_voice = lambda r: httpx.Response(400, json={"detail": "Voice 'zz' not found"})  # noqa: E731
        app = await make_app({"a": (["tts"], unknown_voice), "b": (["tts"], _tts_ok)})
        resp = TestClient(app).post("/v1/audio/speech", json={**SPEECH, "voice": "zz"})
        assert resp.status_code == 400
        assert not app.state.test["engines"]["b"].requests
        assert _audit(app)["status"] == "error"

    @pytest.mark.asyncio
    async def test_upstream_5xx_fails_over(self, make_app):
        down = lambda r: httpx.Response(503, json={"error": "loading"})  # noqa: E731
        app = await make_app(
            {"a": (["tts"], down), "b": (["tts"], _tts_ok)},
        )
        app.state.config.resolution.endpoint_priority = ["a", "b"]
        resp = TestClient(app).post("/v1/audio/speech", json=SPEECH)
        assert resp.status_code == 200
        assert _audit(app)["endpoint"] == "b"

    @pytest.mark.asyncio
    async def test_open_circuit_skips_engine(self, make_app):
        app = await make_app({"a": (["tts"], _tts_ok), "b": (["tts"], _tts_ok)})
        app.state.config.resolution.endpoint_priority = ["a", "b"]
        app.state.registry.trip("a")
        resp = TestClient(app).post("/v1/audio/speech", json=SPEECH)
        assert resp.status_code == 200
        assert _audit(app)["endpoint"] == "b"
        assert not app.state.test["engines"]["a"].requests

    @pytest.mark.asyncio
    async def test_input_over_limit_rejected(self, make_app):
        app = await make_app({"a": (["tts"], _tts_ok)}, media=MediaConfig(max_tts_characters=5))
        assert TestClient(app).post("/v1/audio/speech", json=SPEECH).status_code == 422
        assert not app.state.test["engines"]["a"].requests

    @pytest.mark.asyncio
    async def test_pii_scrubbed_before_synthesis(self, make_app):
        app = await make_app({"a": (["tts"], _tts_ok)})
        app.state.pii_scrubber = PIIScrubber()
        app.state.pii_settings = PIIScrubConfig(scrub_enabled=True)
        TestClient(app).post(
            "/v1/audio/speech", json={**SPEECH, "input": "Mail a.b@example.com now"}
        )
        sent = json.loads(app.state.test["engines"]["a"].requests[0].content)
        assert sent["input"] == "Mail [EMAIL] now"

    @pytest.mark.asyncio
    async def test_key_endpoint_restriction_applies(self, make_app):
        key = "client-key-1234567890"
        app = await make_app(
            {"a": (["tts"], _tts_ok)},
            auth=AuthConfig(
                enabled=True,
                api_keys=[ApiKeyConfig(key=key, client_id="app")],
                anonymous={"allowed_endpoints": ["somewhere-else"]},
            ),
        )
        resp = TestClient(app).post("/v1/audio/speech", json=SPEECH)
        assert resp.status_code == 403
        assert not app.state.test["engines"]["a"].requests


def test_media_capability_needs_openai_contract():
    with pytest.raises(ValueError, match="OpenAI-compatible media API"):
        EndpointConfig(name="o", type=ProviderType.OLLAMA, url="http://o:1", capabilities=["tts"])


# =============================================================================
# Speech-to-text
# =============================================================================


class TestTranscription:
    @pytest.mark.asyncio
    async def test_multipart_forwarded_and_metered(self, make_app):
        app = await make_app({"whisper": (["stt"], _stt_ok)})
        audio = _wav(2.5)
        resp = TestClient(app).post(
            "/v1/audio/transcriptions",
            files={"file": ("clip.wav", io.BytesIO(audio), "audio/wav")},
            data={
                "model": "whisper/Systran/faster-whisper-small",
                "language": "en",
                "timestamp_granularities[]": ["word", "segment"],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"text": "hello world"}

        upstream = app.state.test["engines"]["whisper"].requests[0]
        assert upstream.url.path == "/v1/audio/transcriptions"
        assert upstream.headers["content-type"].startswith("multipart/form-data")
        body = upstream.content
        assert audio in body  # the file itself, not a re-encoding
        assert b"Systran/faster-whisper-small" in body and b"whisper/Systran" not in body
        assert body.count(b'name="timestamp_granularities[]"') == 2

        audit = _audit(app)
        assert audit["task"] == "transcription"
        assert audit["media_usage"]["duration_seconds"] == 2.5
        assert audit["media_usage"]["duration_source"] == "wav_header"
        assert audit["response_body"] == {"text": "hello world"}
        # 2.5 s x 10 token-equivalents
        app.state.test["enforcer"].record_token_usage.assert_called_once_with(
            "default", "Systran/faster-whisper-small", 25
        )

    @pytest.mark.asyncio
    async def test_engine_reported_duration_wins(self, make_app):
        verbose = lambda r: httpx.Response(200, json={"text": "hi", "duration": 7.0})  # noqa: E731
        app = await make_app({"w": (["stt"], verbose)})
        TestClient(app).post(
            "/v1/audio/transcriptions",
            files={"file": ("a.mp3", io.BytesIO(b"mp3-bytes"), "audio/mpeg")},
            data={"model": "whisper-1", "response_format": "verbose_json"},
        )
        usage = _audit(app)["media_usage"]
        assert usage["duration_seconds"] == 7.0
        assert usage["duration_source"] == "engine"

    @pytest.mark.asyncio
    async def test_translation_route(self, make_app):
        app = await make_app({"w": (["stt"], _stt_ok)})
        TestClient(app).post(
            "/v1/audio/translations",
            files={"file": ("a.wav", io.BytesIO(_wav(0.1)), "audio/wav")},
            data={"model": "whisper-1"},
        )
        assert app.state.test["engines"]["w"].requests[0].url.path == "/v1/audio/translations"
        assert _audit(app)["task"] == "translation"

    @pytest.mark.asyncio
    async def test_upload_over_limit_is_413(self, make_app):
        app = await make_app({"w": (["stt"], _stt_ok)}, media=MediaConfig(max_upload_mb=0.001))
        resp = TestClient(app).post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", io.BytesIO(_wav(1.0)), "audio/wav")},
            data={"model": "whisper-1"},
        )
        assert resp.status_code == 413
        assert not app.state.test["engines"]["w"].requests

    @pytest.mark.asyncio
    async def test_missing_file_rejected(self, make_app):
        app = await make_app({"w": (["stt"], _stt_ok)})
        resp = TestClient(app).post("/v1/audio/transcriptions", data={"model": "whisper-1"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_streaming_transcript_relayed(self, make_app):
        sse = (
            'data: {"type":"transcript.text.delta","delta":"hel"}\n\n'
            'data: {"type":"transcript.text.done","text":"hello"}\n\n'
        )
        streaming = lambda r: httpx.Response(  # noqa: E731
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )
        app = await make_app({"w": (["stt"], streaming)})
        resp = TestClient(app).post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", io.BytesIO(_wav(1.0)), "audio/wav")},
            data={"model": "whisper-1", "stream": "true"},
        )
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text == sse
        assert _audit(app)["status"] == "success"


@pytest.mark.asyncio
async def test_multipart_parses_on_a_real_fastapi_engine(make_app):
    """The hand-built multipart body must parse on a real server (speaches and
    Kokoro-FastAPI are FastAPI apps), including repeated fields and the file."""
    from fastapi import File, Form, UploadFile

    engine = FastAPI()
    received = {}

    @engine.post("/v1/audio/transcriptions")
    async def transcribe(
        file: UploadFile = File(...),
        model: str = Form(...),
        language: str | None = Form(None),
        timestamp_granularities: list[str] = Form([], alias="timestamp_granularities[]"),
    ):
        received.update(
            model=model,
            language=language,
            granularities=timestamp_granularities,
            filename=file.filename,
            content_type=file.content_type,
            audio=await file.read(),
        )
        return {"text": "parsed"}

    app = await make_app({"speaches": (["stt"], _stt_ok)})
    app.state.registry.get("speaches")._media_client = httpx.AsyncClient(
        base_url="http://speaches:1", transport=httpx.ASGITransport(app=engine)
    )
    audio = _wav(0.5)
    resp = TestClient(app).post(
        "/v1/audio/transcriptions",
        files={"file": ('we"ird\r\nname.wav', io.BytesIO(audio), "audio/wav")},
        data={
            "model": "speaches/whisper-small",
            "language": "fr",
            "timestamp_granularities[]": ["word", "segment"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "parsed"}
    assert received["model"] == "whisper-small"
    assert received["language"] == "fr"
    assert received["granularities"] == ["word", "segment"]
    assert received["audio"] == audio
    assert received["content_type"] == "audio/wav"
    assert "\r" not in received["filename"] and '"' not in received["filename"]

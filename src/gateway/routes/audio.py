"""OpenAI-compatible voice routes: /v1/audio/speech, /transcriptions, /translations.

Any engine speaking OpenAI's audio API works behind these (Kokoro-FastAPI,
speaches/faster-whisper, vLLM, vLLM-Omni, ...): endpoints declare
`capabilities: [tts]` / `[stt]` (D-020). Requests get the same guarantees
as chat: auth, key/environment scope, rate limits, PII handling, a real
status before the first byte, failover, audit (metadata only, D-023),
metering and budgets (D-021).
"""

import json
import math
import struct
import time
from collections.abc import AsyncGenerator
from typing import Annotated, Any, Literal
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from gateway.config import GatewayConfig, MediaCapability
from gateway.dispatch import ProviderRegistry
from gateway.dispatch.media import MediaDispatcher, UpstreamMedia
from gateway.errors import GatewayError, PayloadTooLargeError, ValidationError
from gateway.media.catalog import MediaCatalog
from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest
from gateway.observability import get_logger, get_metrics
from gateway.policy import PolicyEnforcer, PolicyViolation
from gateway.routes.dependencies import (
    AuthResult,
    ClientDisconnected,
    get_audit_logger,
    get_auth,
    get_config,
    get_enforcer,
    get_inference_auth,
    get_pii_scrubber,
    get_registry,
    resolve_access_scope,
    run_unless_disconnected,
    setup_request_context,
    should_scrub_pii,
    translate_policy_violation,
)
from gateway.security import PIIScrubber
from gateway.security.pii import PIIFinding
from gateway.storage import AuditLogger

logger = get_logger(__name__)
metrics = get_metrics()

router = APIRouter(prefix="/v1/audio", tags=["audio"])

# Transcript text kept for the audit row (redacted at rest, D-005)
_AUDIT_TEXT_LIMIT = 100_000


def get_media_catalog(request: Request) -> MediaCatalog | None:
    return getattr(request.app.state, "media_catalog", None)


def get_media_dispatcher(
    request: Request,
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> MediaDispatcher:
    return MediaDispatcher(
        registry,
        get_config(request).resolution,
        endpoint_allowed=enforcer.check_provider_allowed,
    )


class SpeechRequest(BaseModel):
    """OpenAI /v1/audio/speech body. Engine-native extras (e.g. Kokoro's
    lang_code, normalization_options) pass through untouched."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(max_length=128)
    input: str = Field(min_length=1)
    voice: str | dict[str, Any]
    response_format: str | None = None
    speed: float | None = None
    stream_format: Literal["audio", "sse"] | None = None
    instructions: str | None = None


# =============================================================================
# Shared plumbing
# =============================================================================


class _MediaOutcome:
    """Audit, metrics and budget for one media request, recorded once."""

    def __init__(
        self,
        *,
        ctx,
        task: TaskType,
        internal_request: InternalRequest,
        audit_logger: AuditLogger | None,
        enforcer: PolicyEnforcer,
        request_body: dict | None = None,
    ):
        self.ctx = ctx
        self.task = task
        self.request = internal_request
        self.audit_logger = audit_logger
        self.enforcer = enforcer
        self.request_body = request_body
        self.endpoint: str | None = None
        self.model = internal_request.model or "unknown"
        self.started = time.perf_counter()
        self.done = False

    async def record(
        self,
        status: str,
        *,
        usage: dict,
        budget_tokens: int = 0,
        response_body: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.done:
            return
        self.done = True
        latency_ms = (time.perf_counter() - self.started) * 1000

        if budget_tokens > 0:
            try:
                self.enforcer.record_token_usage(self.request.client_id, self.model, budget_tokens)
            except Exception:
                logger.exception("Failed to record media budget usage")
        try:
            metrics.record_request(
                provider=self.endpoint or "unknown",
                model=self.model,
                task=self.task.value,
                status=status,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.exception("Failed to record media metrics")

        if self.audit_logger is None:
            return
        await self.audit_logger.log_request(
            request_id=self.ctx.request_id,
            client_id=self.request.client_id,
            task=self.task.value,
            model=self.model,
            endpoint=self.endpoint or "unknown",
            status=status,
            environment=self.request.environment,
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
            request_body=self.request_body,
            response_body=response_body,
            media_usage=usage,
        )

    async def record_shielded(self, *args, **kwargs) -> None:
        """record() that survives the client's cancellation (see D-004)."""
        with anyio.CancelScope(shield=True):
            try:
                await self.record(*args, **kwargs)
            except Exception:
                logger.exception("Failed to record media request outcome")


async def _prepare(
    request: Request,
    auth: AuthResult,
    enforcer: PolicyEnforcer,
    task: TaskType,
    model: str,
    estimated_tokens: int,
) -> InternalRequest:
    """Scope and policy for a media request (same rules as text routes).

    The budget reservation is the media cost in token equivalents, known (or
    bounded) before the upstream request opens. Before, admission checked an
    empty request: a 400-character speech request passed a 10-token budget
    and was charged 100 afterwards.
    """
    internal = InternalRequest(task=task, model=model, client_id=auth.client_id)
    internal = internal.model_copy(update=await resolve_access_scope(request, auth, model))
    try:
        await enforcer.enforce(
            internal,
            rate_limit_key=auth.client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
            estimated_tokens=estimated_tokens,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)
    return internal


async def _open_upstream(
    request: Request,
    dispatcher: MediaDispatcher,
    capability: MediaCapability,
    internal: InternalRequest,
    outcome: _MediaOutcome,
    build,
    usage: dict,
    compatible=None,
) -> UpstreamMedia:
    """Pick an endpoint and get its status before responding (D-013)."""
    try:
        upstream = await run_unless_disconnected(
            request, dispatcher.send(capability, internal, build, compatible)
        )
    except ClientDisconnected:
        await outcome.record(
            "error",
            usage=usage,
            error_code="client_disconnected",
            error_message="Client disconnected before the upstream responded",
        )
        raise
    except GatewayError as e:
        await outcome.record("error", usage=usage, error_code=e.code.value, error_message=str(e))
        raise
    outcome.endpoint = upstream.endpoint
    outcome.model = upstream.model
    return upstream


# Compressed audio's duration isn't known before the engine reports it. For
# the budget reservation, assume 128 kbps (typical MP3/M4A): low-bitrate
# voice files reserve more than they use and are settled down afterwards.
_ASSUMED_BYTES_PER_SECOND = 16_000

_PASSTHROUGH_HEADERS = ("content-type", "content-disposition", "x-download-path")


def _response_headers(upstream: UpstreamMedia) -> dict[str, str]:
    headers = {
        k: v for k, v in upstream.response.headers.items() if k.lower() in _PASSTHROUGH_HEADERS
    }
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"  # let audio chunks through proxies immediately
    return headers


# =============================================================================
# Text-to-speech
# =============================================================================


@router.post("/speech")
async def create_speech(
    request: Request,
    body: SpeechRequest,
    auth: Annotated[AuthResult, Depends(get_inference_auth)],
    dispatcher: Annotated[MediaDispatcher, Depends(get_media_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    config: Annotated[GatewayConfig, Depends(get_config)],
    catalog: Annotated[MediaCatalog | None, Depends(get_media_catalog)],
) -> StreamingResponse:
    """Text-to-speech. Audio is relayed as the engine produces it."""
    limit = config.media.max_tts_characters
    if len(body.input) > limit:
        raise ValidationError(
            message=f"input is {len(body.input)} characters; the limit is {limit}"
        )

    ctx = setup_request_context(client_id=auth.client_id, model=body.model, task="speech")
    text = body.input

    # The text is a prompt like any other: detect, optionally scrub (D-019)
    if pii_scrubber:
        scrub = should_scrub_pii(request)
        result = pii_scrubber.scan(text, scrub=scrub)
        if result.has_pii:
            if audit_logger:
                await audit_logger.log_pii_events(
                    request_id=ctx.request_id,
                    client_id=auth.client_id,
                    task="speech",
                    model=body.model,
                    findings=[PIIFinding(0, "user", text, result)],
                    was_scrubbed=scrub,
                )
            if scrub and result.scrubbed_text is not None:
                text = result.scrubbed_text

    budget_tokens = math.ceil(len(text) * config.media.token_equivalents.tts_character)
    internal = await _prepare(
        request, auth, enforcer, TaskType.SPEECH, body.model, estimated_tokens=budget_tokens
    )
    voice = body.voice if isinstance(body.voice, str) else json.dumps(body.voice)[:200]
    usage: dict[str, Any] = {
        "characters": len(text),
        "voice": voice,
        "format": body.response_format or "engine default",
    }
    outcome = _MediaOutcome(
        ctx=ctx,
        task=TaskType.SPEECH,
        internal_request=internal,
        audit_logger=audit_logger,
        enforcer=enforcer,
        request_body={"input": text, "voice": voice},
    )

    def build(client, model: str):
        payload = body.model_dump(exclude_none=True)
        payload.update(model=model, input=text)
        return client.build_request("POST", "/v1/audio/speech", json=payload)

    compatible = None
    if catalog is not None:
        params = body.model_dump(exclude={"model", "input", "voice"}, exclude_none=True)

        def compatible(endpoint: str) -> str | None:
            return catalog.check_speech(endpoint, body.voice, params)

    upstream = await _open_upstream(
        request, dispatcher, "tts", internal, outcome, build, usage, compatible
    )

    async def relay() -> AsyncGenerator[bytes, None]:
        sent = 0
        completed = False
        try:
            async for chunk in upstream.response.aiter_bytes():
                sent += len(chunk)
                yield chunk
            completed = True
        except Exception as e:
            logger.warning("TTS stream failed", endpoint=upstream.endpoint, error=str(e))
            await outcome.record_shielded(
                "error",
                usage={**usage, "bytes_out": sent},
                budget_tokens=budget_tokens,
                error_code="stream_error",
                error_message=str(e),
            )
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
            if completed:
                await outcome.record_shielded(
                    "success", usage={**usage, "bytes_out": sent}, budget_tokens=budget_tokens
                )
            else:
                # Client left mid-audio: the engine still did the work
                await outcome.record_shielded(
                    "error",
                    usage={**usage, "bytes_out": sent},
                    budget_tokens=budget_tokens,
                    error_code="client_disconnected",
                    error_message="Client disconnected before the audio completed",
                )

    return StreamingResponse(relay(), headers=_response_headers(upstream))


@router.get("/voices")
async def list_voices(
    request: Request,
    auth: Annotated[AuthResult, Depends(get_auth)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    catalog: Annotated[MediaCatalog | None, Depends(get_media_catalog)],
) -> dict:
    """Voices on the text-to-speech endpoints this key may use.

    Each voice lists the endpoints that serve it, plus language and gender
    when the engine or its profile provides them. Not part of OpenAI's API,
    but the de-facto path engines use (Kokoro-FastAPI, speaches, vLLM-Omni).
    """
    scope = await resolve_access_scope(request, auth, None)
    allowed = scope.get("allowed_endpoints")
    endpoints = [
        name
        for name in registry.list_providers()
        if "tts" in getattr(registry.get_endpoint_config(name), "capabilities", [])
        and (allowed is None or name in allowed)
    ]
    return {"object": "list", "voices": catalog.voices(endpoints) if catalog else []}


# =============================================================================
# Speech-to-text
# =============================================================================


def _wav_duration_seconds(upload: UploadFile) -> float | None:
    """Duration of a PCM WAV upload from its header, or None if not a parseable WAV."""
    f = upload.file
    try:
        f.seek(0)
        header = f.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        byte_rate = None
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                return None
            chunk_id, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            if chunk_id == b"fmt ":
                fmt = f.read(size)
                byte_rate = struct.unpack("<I", fmt[8:12])[0]
            elif chunk_id == b"data":
                return round(size / byte_rate, 3) if byte_rate else None
            else:
                f.seek(size + (size & 1), 1)
    except (OSError, struct.error):
        return None
    finally:
        f.seek(0)


_UPLOAD_CHUNK = 64 * 1024


def _header_safe(value: str) -> str:
    """Strip characters that could break out of a multipart header line."""
    return value.replace("\r", "").replace("\n", "").replace('"', "")


def _multipart_body(
    fields: list[tuple[str, str]], upload: UploadFile, size: int
) -> tuple[dict[str, str], AsyncGenerator[bytes, None]]:
    """Re-encode the form for upstream, streaming the file from its spool.

    httpx's own multipart encoder treats the upload's (synchronous) file
    object as a sync stream, which an AsyncClient refuses to send. This
    reads it asynchronously in chunks instead, and sets an exact
    Content-Length (the size is known). A fresh generator per attempt
    rewinds the file, so failover can resend it.
    """
    boundary = uuid4().hex
    preamble = b"".join(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_header_safe(name)}"'
            f"\r\n\r\n{value}\r\n"
        ).encode()
        for name, value in fields
    )
    file_head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{_header_safe(upload.filename or "audio")}"\r\n'
        f"Content-Type: {_header_safe(upload.content_type or 'application/octet-stream')}"
        "\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    async def body() -> AsyncGenerator[bytes, None]:
        yield preamble
        yield file_head
        await upload.seek(0)
        while chunk := await upload.read(_UPLOAD_CHUNK):
            yield chunk
        yield tail

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(preamble) + len(file_head) + size + len(tail)),
    }
    return headers, body()


def _transcript_text(body: bytes, content_type: str) -> str:
    """The transcript from an engine response (json/verbose_json/text/srt/vtt)."""
    text = body.decode("utf-8", errors="replace")
    if "json" in content_type:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                return data["text"]
        except json.JSONDecodeError:
            pass
    return text


def _engine_duration(body: bytes) -> float | None:
    """Audio duration reported by the engine (verbose_json), if any."""
    try:
        data = json.loads(body)
        duration = data.get("duration") if isinstance(data, dict) else None
        return float(duration) if duration is not None else None
    except (ValueError, TypeError):
        return None


async def _speech_to_text(
    request: Request,
    task: TaskType,
    path: str,
    auth: AuthResult,
    dispatcher: MediaDispatcher,
    enforcer: PolicyEnforcer,
    audit_logger: AuditLogger | None,
    config: GatewayConfig,
) -> Response:
    max_bytes = int(config.media.max_upload_mb * 1024 * 1024)
    form = await request.form(max_files=1)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ValidationError(message="Multipart field 'file' (the audio) is required")
    model = form.get("model")
    if not isinstance(model, str) or not model:
        raise ValidationError(message="Multipart field 'model' is required")
    size = upload.size
    if size is None:
        upload.file.seek(0, 2)
        size = upload.file.tell()
        upload.file.seek(0)
    if size > max_bytes:
        raise PayloadTooLargeError(
            f"Audio is {size / 1048576:.1f} MB; the limit is {config.media.max_upload_mb:g} MB"
        )

    # Every other field passes through untouched (language, prompt,
    # response_format, timestamp_granularities[], engine extensions...)
    fields = [
        (k, v) for k, v in form.multi_items() if k not in ("file", "model") and isinstance(v, str)
    ]
    streaming = any(k == "stream" and v.lower() == "true" for k, v in fields)

    ctx = setup_request_context(client_id=auth.client_id, model=model, task=task.value)
    wav_duration = _wav_duration_seconds(upload)
    # Reserve for the audio's length: exact for WAV; for compressed formats,
    # bounded from the size (settled to the engine's duration afterwards)
    expected_seconds = (
        wav_duration if wav_duration is not None else size / _ASSUMED_BYTES_PER_SECOND
    )
    internal = await _prepare(
        request,
        auth,
        enforcer,
        task,
        model,
        estimated_tokens=math.ceil(
            expected_seconds * config.media.token_equivalents.stt_audio_second
        ),
    )
    usage: dict[str, Any] = {
        "bytes_in": size,
        "content_type": upload.content_type,
        "language": next((v for k, v in fields if k == "language"), None),
    }
    outcome = _MediaOutcome(
        ctx=ctx, task=task, internal_request=internal, audit_logger=audit_logger, enforcer=enforcer
    )
    equivalents = config.media.token_equivalents

    def budget_for(duration: float | None, transcript: str) -> int:
        if duration is not None:
            return math.ceil(duration * equivalents.stt_audio_second)
        # Duration unknown (non-WAV upload, non-verbose response): fall back to
        # the transcript's approximate token count
        return math.ceil(len(transcript) * equivalents.tts_character)

    def build(client, upstream_model: str):
        headers, body = _multipart_body([("model", upstream_model), *fields], upload, size)
        return client.build_request("POST", path, content=body, headers=headers)

    upstream = await _open_upstream(request, dispatcher, "stt", internal, outcome, build, usage)
    content_type = upstream.response.headers.get("content-type", "")

    if not streaming:
        try:
            body = await upstream.response.aread()
        finally:
            await upstream.aclose()
        transcript = _transcript_text(body, content_type)
        engine_duration = _engine_duration(body)
        duration = engine_duration if engine_duration is not None else wav_duration
        usage.update(
            duration_seconds=duration,
            duration_source="engine"
            if engine_duration is not None
            else ("wav_header" if wav_duration is not None else None),
        )
        await outcome.record(
            "success",
            usage=usage,
            budget_tokens=budget_for(duration, transcript),
            response_body={"text": transcript[:_AUDIT_TEXT_LIMIT]},
        )
        return Response(content=body, status_code=200, headers=_response_headers(upstream))

    async def relay() -> AsyncGenerator[bytes, None]:
        received = bytearray()
        completed = False
        try:
            async for chunk in upstream.response.aiter_bytes():
                if len(received) < _AUDIT_TEXT_LIMIT:
                    received.extend(chunk[: _AUDIT_TEXT_LIMIT - len(received)])
                yield chunk
            completed = True
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
            text = received.decode("utf-8", errors="replace")
            usage.update(
                duration_seconds=wav_duration,
                duration_source="wav_header" if wav_duration else None,
            )
            await outcome.record_shielded(
                "success" if completed else "error",
                usage=usage,
                budget_tokens=budget_for(wav_duration, text),
                response_body={"stream": text},
                error_code=None if completed else "client_disconnected",
                error_message=None if completed else "Stream ended before completion",
            )

    return StreamingResponse(relay(), headers=_response_headers(upstream))


@router.post("/transcriptions")
async def create_transcription(
    request: Request,
    auth: Annotated[AuthResult, Depends(get_inference_auth)],
    dispatcher: Annotated[MediaDispatcher, Depends(get_media_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> Response:
    """Speech-to-text in the spoken language (multipart, OpenAI fields)."""
    return await _speech_to_text(
        request,
        TaskType.TRANSCRIPTION,
        "/v1/audio/transcriptions",
        auth,
        dispatcher,
        enforcer,
        audit_logger,
        config,
    )


@router.post("/translations")
async def create_translation(
    request: Request,
    auth: Annotated[AuthResult, Depends(get_inference_auth)],
    dispatcher: Annotated[MediaDispatcher, Depends(get_media_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> Response:
    """Speech-to-English-text (multipart, OpenAI fields)."""
    return await _speech_to_text(
        request,
        TaskType.TRANSLATION,
        "/v1/audio/translations",
        auth,
        dispatcher,
        enforcer,
        audit_logger,
        config,
    )

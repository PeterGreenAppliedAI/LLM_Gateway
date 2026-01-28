"""Ollama-compatible API endpoints.

Provides native Ollama API compatibility so clients using the Ollama SDK
can work with the gateway without modification.

Endpoints:
- POST /api/chat - Chat completions
- POST /api/generate - Text generation
- GET /api/tags - List available models
- POST /api/embeddings - Generate embeddings
"""

import json
from datetime import datetime, timezone
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway.dispatch import Dispatcher
from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest, Message
from gateway.models.ollama import (
    OllamaChatRequest,
    OllamaChatResponse,
    OllamaChatStreamChunk,
    OllamaGenerateRequest,
    OllamaGenerateResponse,
    OllamaEmbeddingsRequest,
    OllamaEmbeddingsResponse,
    OllamaMessage,
    OllamaTagsResponse,
    OllamaModelInfo,
)
from gateway.observability import get_logger
from gateway.routes.dependencies import (
    AuthResult,
    get_auth,
    get_audit_logger,
    get_dispatcher,
    setup_request_context,
)
from gateway.storage import AuditLogger

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["ollama"])


def _now_iso() -> str:
    """Get current time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Chat Endpoint
# =============================================================================


@router.post("/chat")
async def ollama_chat(
    request: Request,
    body: OllamaChatRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
):
    """Ollama-compatible chat endpoint.

    Accepts Ollama format requests and returns Ollama format responses.
    """
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        model=body.model,
        task="chat",
    )

    # Convert Ollama messages to internal format
    messages = [
        Message(role=m.role, content=m.content)
        for m in body.messages
    ]

    # Extract options - only include if actually set
    options = body.options or {}

    request_kwargs = {
        "task": TaskType.CHAT,
        "model": body.model,
        "messages": messages,
        "client_id": client_id,
        "stream": body.stream,
    }

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        request_kwargs["preferred_provider"] = auth.target_endpoint

    if options.get("temperature") is not None:
        request_kwargs["temperature"] = options["temperature"]
    if options.get("num_predict") is not None:
        request_kwargs["max_tokens"] = options["num_predict"]
    if options.get("top_p") is not None:
        request_kwargs["top_p"] = options["top_p"]

    internal_request = InternalRequest(**request_kwargs)

    if body.stream:
        return await _stream_ollama_chat(
            dispatcher, internal_request, body.model, ctx, audit_logger
        )

    # Non-streaming
    result = await dispatcher.dispatch(internal_request)

    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )

    # Audit log
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="chat",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            stream=False,
            latency_ms=ctx.total_latency_ms,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
        )

    # Convert to Ollama format
    return OllamaChatResponse(
        model=result.response.model,
        created_at=_now_iso(),
        message=OllamaMessage(
            role="assistant",
            content=result.response.content,
        ),
        done=True,
        prompt_eval_count=result.response.usage.prompt_tokens,
        eval_count=result.response.usage.completion_tokens,
    )


async def _stream_ollama_chat(
    dispatcher: Dispatcher,
    internal_request: InternalRequest,
    model: str,
    ctx,
    audit_logger: AuditLogger | None,
) -> StreamingResponse:
    """Stream Ollama chat response."""

    async def generate() -> AsyncGenerator[bytes, None]:
        provider_name = None
        full_content = ""

        try:
            provider_name, stream = await dispatcher.dispatch_stream(internal_request)

            async for chunk in stream:
                full_content += chunk.delta or ""

                response = OllamaChatStreamChunk(
                    model=model,
                    created_at=_now_iso(),
                    message=OllamaMessage(
                        role="assistant",
                        content=chunk.delta or "",
                    ),
                    done=chunk.finish_reason is not None,
                )
                yield json.dumps(response.model_dump()) + "\n"

                if chunk.finish_reason:
                    # Send final done message
                    final = OllamaChatResponse(
                        model=model,
                        created_at=_now_iso(),
                        message=OllamaMessage(role="assistant", content=""),
                        done=True,
                        prompt_eval_count=chunk.usage.prompt_tokens if chunk.usage else 0,
                        eval_count=chunk.usage.completion_tokens if chunk.usage else 0,
                    )
                    yield json.dumps(final.model_dump()) + "\n"

                    # Audit log
                    if audit_logger and provider_name:
                        await audit_logger.log_request(
                            request_id=ctx.request_id,
                            client_id=internal_request.client_id,
                            task="chat",
                            model=model,
                            endpoint=provider_name,
                            status="success",
                            stream=True,
                            latency_ms=ctx.total_latency_ms,
                        )

        except Exception as e:
            logger.exception("Error in Ollama chat stream")
            error_response = {
                "error": str(e),
                "done": True,
            }
            yield json.dumps(error_response) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
    )


# =============================================================================
# Generate Endpoint
# =============================================================================


@router.post("/generate")
async def ollama_generate(
    request: Request,
    body: OllamaGenerateRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
):
    """Ollama-compatible generate endpoint."""
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        model=body.model,
        task="generate",
    )

    # Build messages from prompt and optional system
    messages = []
    if body.system:
        messages.append(Message(role="system", content=body.system))
    messages.append(Message(role="user", content=body.prompt))

    options = body.options or {}

    request_kwargs = {
        "task": TaskType.GENERATE,
        "model": body.model,
        "messages": messages,
        "prompt": body.prompt,
        "client_id": client_id,
        "stream": body.stream,
    }

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        request_kwargs["preferred_provider"] = auth.target_endpoint

    if options.get("temperature") is not None:
        request_kwargs["temperature"] = options["temperature"]
    if options.get("num_predict") is not None:
        request_kwargs["max_tokens"] = options["num_predict"]
    if options.get("top_p") is not None:
        request_kwargs["top_p"] = options["top_p"]

    internal_request = InternalRequest(**request_kwargs)

    if body.stream:
        return await _stream_ollama_generate(
            dispatcher, internal_request, body.model, ctx, audit_logger
        )

    # Non-streaming
    result = await dispatcher.dispatch(internal_request)

    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )

    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="generate",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            stream=False,
            latency_ms=ctx.total_latency_ms,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
        )

    return OllamaGenerateResponse(
        model=result.response.model,
        created_at=_now_iso(),
        response=result.response.content,
        done=True,
        prompt_eval_count=result.response.usage.prompt_tokens,
        eval_count=result.response.usage.completion_tokens,
    )


async def _stream_ollama_generate(
    dispatcher: Dispatcher,
    internal_request: InternalRequest,
    model: str,
    ctx,
    audit_logger: AuditLogger | None,
) -> StreamingResponse:
    """Stream Ollama generate response."""

    async def generate() -> AsyncGenerator[bytes, None]:
        provider_name = None

        try:
            provider_name, stream = await dispatcher.dispatch_stream(internal_request)

            async for chunk in stream:
                response = {
                    "model": model,
                    "created_at": _now_iso(),
                    "response": chunk.delta or "",
                    "done": chunk.finish_reason is not None,
                }

                if chunk.finish_reason:
                    response["prompt_eval_count"] = chunk.usage.prompt_tokens if chunk.usage else 0
                    response["eval_count"] = chunk.usage.completion_tokens if chunk.usage else 0

                    if audit_logger and provider_name:
                        await audit_logger.log_request(
                            request_id=ctx.request_id,
                            client_id=internal_request.client_id,
                            task="generate",
                            model=model,
                            endpoint=provider_name,
                            status="success",
                            stream=True,
                            latency_ms=ctx.total_latency_ms,
                        )

                yield json.dumps(response) + "\n"

        except Exception as e:
            logger.exception("Error in Ollama generate stream")
            yield json.dumps({"error": str(e), "done": True}) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
    )


# =============================================================================
# Tags (List Models) Endpoint
# =============================================================================


@router.get("/tags")
async def ollama_tags(request: Request):
    """Ollama-compatible list models endpoint.

    Returns all discovered models from all endpoints.
    """
    registry = getattr(request.app.state, "registry", None)
    if not registry:
        return OllamaTagsResponse(models=[])

    catalog = registry.catalog
    models = []

    for discovered in catalog.discovered:
        models.append(OllamaModelInfo(
            name=discovered.name,
            model=discovered.name,
            modified_at=discovered.discovered_at.isoformat() if discovered.discovered_at else _now_iso(),
            size=discovered.size_bytes or 0,
            digest="",
            details={
                "family": discovered.family or "",
                "parameter_size": discovered.parameter_size or "",
                "quantization_level": discovered.quantization or "",
            },
        ))

    return OllamaTagsResponse(models=models)


# =============================================================================
# Embeddings Endpoint
# =============================================================================


@router.post("/embeddings")
async def ollama_embeddings(
    request: Request,
    body: OllamaEmbeddingsRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
):
    """Ollama-compatible embeddings endpoint."""
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        model=body.model,
        task="embeddings",
    )

    # Handle both single string and list of strings
    prompts = body.prompt if isinstance(body.prompt, list) else [body.prompt]

    request_kwargs = {
        "task": TaskType.EMBEDDINGS,
        "model": body.model,
        "prompt": prompts[0] if len(prompts) == 1 else None,
        "client_id": client_id,
    }

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        request_kwargs["preferred_provider"] = auth.target_endpoint

    internal_request = InternalRequest(**request_kwargs)

    result = await dispatcher.dispatch(internal_request)

    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="embeddings",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            latency_ms=ctx.total_latency_ms,
        )

    # Return embeddings in Ollama format
    embeddings = result.response.embeddings
    if embeddings and len(embeddings) == 1:
        return OllamaEmbeddingsResponse(embedding=embeddings[0])
    return OllamaEmbeddingsResponse(embedding=embeddings or [])

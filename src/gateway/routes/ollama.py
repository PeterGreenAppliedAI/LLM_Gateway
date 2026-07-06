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
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway.dispatch import Dispatcher
from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest, Message
from gateway.models.ollama import (
    OllamaChatRequest,
    OllamaChatResponse,
    OllamaChatStreamChunk,
    OllamaEmbeddingsRequest,
    OllamaEmbeddingsResponse,
    OllamaEmbedRequest,
    OllamaEmbedResponse,
    OllamaGenerateRequest,
    OllamaGenerateResponse,
    OllamaMessage,
    OllamaModelInfo,
    OllamaTagsResponse,
    OllamaToolCall,
    OllamaToolCallFunction,
)
from gateway.observability import get_logger, get_metrics
from gateway.policy import PolicyEnforcer, PolicyViolation
from gateway.routes.dependencies import (
    AuthResult,
    get_audit_logger,
    get_auth,
    get_dispatcher,
    get_enforcer,
    get_pii_scrubber,
    get_sanitizer,
    get_security_analyzer,
    run_unless_disconnected,
    setup_request_context,
    should_scrub_pii,
    translate_policy_violation,
)
from gateway.security import AsyncSecurityAnalyzer, PIIScrubber, Sanitizer
from gateway.storage import AuditLogger

logger = get_logger(__name__)
metrics = get_metrics()

router = APIRouter(prefix="/api", tags=["ollama"])


def _now_iso() -> str:
    """Get current time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_ollama_format(fmt: str | dict) -> dict:
    """Normalize Ollama's `format` into the internal response_format shape.

    "json" -> {"type": "json_object"}; a JSON-schema dict -> {"type":
    "json_schema", ...}. Adapters translate back to their engine's native
    mechanism, so schema-constrained decoding survives cross-protocol dispatch.
    """
    if isinstance(fmt, dict):
        return {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": fmt, "strict": True},
        }
    return {"type": "json_object"}


# =============================================================================
# Chat Endpoint
# =============================================================================


@router.post("/chat")
async def ollama_chat(
    request: Request,
    body: OllamaChatRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
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

    # Log incoming images for vision debugging
    incoming_images = sum(1 for m in body.messages if m.images)
    if incoming_images:
        logger.info(
            "Received chat request with images",
            model=body.model,
            messages_with_images=incoming_images,
            image_sizes=[[len(img) for img in m.images] for m in body.messages if m.images],
        )

    # Security: Sanitize message content
    sanitized_messages = []
    for m in body.messages:
        msg_dict: dict = {"role": m.role, "content": m.content or ""}
        if m.content:
            result = sanitizer.sanitize(m.content)
            msg_dict["content"] = result.sanitized
        if m.images:
            msg_dict["images"] = m.images
        if m.tool_calls:
            msg_dict["tool_calls"] = [{"function": tc.function.model_dump()} for tc in m.tool_calls]
        sanitized_messages.append(msg_dict)

    # PII detection (always flags) + optional scrubbing (per-route)
    if pii_scrubber:
        scrub = should_scrub_pii(request)
        # Keep pre-scrub messages for hashing raw PII values
        pre_scrub_messages = [dict(m) for m in sanitized_messages]
        sanitized_messages, pii_results = pii_scrubber.scan_messages(
            sanitized_messages, scrub=scrub
        )
        pii_found = sum(r.detection_count for r in pii_results)
        if pii_found:
            logger.warning(
                "PII detected in request",
                request_id=ctx.request_id,
                pii_count=pii_found,
                scrubbed=scrub,
            )
            # Log PII events with hashed values (never raw PII)
            if audit_logger:
                await audit_logger.log_pii_events(
                    request_id=ctx.request_id,
                    client_id=client_id,
                    task="chat",
                    model=body.model,
                    messages=pre_scrub_messages,
                    pii_results=pii_results,
                    was_scrubbed=scrub,
                )

    # Queue for async security analysis
    if security_analyzer:
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=sanitized_messages,
            source_ip=request.client.host if request.client else None,
        )

    # Convert Ollama messages to internal format (using sanitized content)
    from gateway.models.internal import ToolCall

    messages = []
    for m in sanitized_messages:
        msg_kwargs: dict = {"role": m["role"], "content": m["content"]}
        if m.get("images"):
            msg_kwargs["images"] = m["images"]
        if m.get("tool_calls"):
            msg_kwargs["tool_calls"] = [
                ToolCall(type="function", function=tc["function"]) for tc in m["tool_calls"]
            ]
        messages.append(Message(**msg_kwargs))

    # Client options pass through verbatim (num_ctx, top_k, repeat_penalty,
    # ...). The fields the gateway polices are mirrored into normalized
    # fields; everything else must reach the engine untouched.
    options = body.options or {}

    request_kwargs = {
        "task": TaskType.CHAT,
        "model": body.model,
        "messages": messages,
        "client_id": client_id,
        "stream": body.stream,
        "options": options,
    }

    # Pass through tool definitions
    if body.tools:
        request_kwargs["tools"] = body.tools

    # Structured outputs: "json" or a JSON-schema dict (normalized to the
    # OpenAI response_format shape; adapters translate back)
    if body.format is not None:
        request_kwargs["response_format"] = _normalize_ollama_format(body.format)

    if body.keep_alive is not None:
        request_kwargs["extensions"] = {"keep_alive": body.keep_alive}

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

    # Check policies - raises domain errors on violation
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    if body.stream:
        return await _stream_ollama_chat(
            dispatcher,
            internal_request,
            body.model,
            ctx,
            audit_logger,
            request_body={"messages": sanitized_messages},
        )

    # Non-streaming
    result = await run_unless_disconnected(request, dispatcher.dispatch(internal_request))

    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )
    metrics.record_request(
        provider=result.provider_used,
        model=result.response.model,
        task="chat",
        status="success",
        latency_ms=ctx.total_latency_ms or 0,
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )

    # Record token usage for daily budget tracking
    total_tokens = (result.response.usage.prompt_tokens or 0) + (
        result.response.usage.completion_tokens or 0
    )
    if total_tokens > 0:
        enforcer.record_token_usage(client_id, result.response.model, total_tokens)

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
            request_body={"messages": sanitized_messages},
        )

    # Convert to Ollama format - include tool_calls if present
    ollama_tool_calls = None
    if result.response.tool_calls:
        ollama_tool_calls = [
            OllamaToolCall(
                function=OllamaToolCallFunction(
                    name=tc.function.get("name", ""),
                    arguments=tc.function.get("arguments", {}),
                )
            )
            for tc in result.response.tool_calls
        ]

    return OllamaChatResponse(
        model=result.response.model,
        created_at=_now_iso(),
        message=OllamaMessage(
            role="assistant",
            content=result.response.content or "",
            tool_calls=ollama_tool_calls,
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
    request_body: dict | None = None,
) -> StreamingResponse:
    """Stream Ollama chat response."""

    async def generate() -> AsyncGenerator[bytes, None]:
        provider_name = None
        full_content = ""

        try:
            provider_name, stream = await dispatcher.dispatch_stream(internal_request)

            async for chunk in stream:
                full_content += chunk.delta or ""

                # Tool calls stream through untouched (Ollama shape:
                # function.arguments stays an object, never stringified)
                chunk_tool_calls = None
                if chunk.tool_calls:
                    chunk_tool_calls = [
                        OllamaToolCall(
                            function=OllamaToolCallFunction(
                                name=tc.function.get("name", ""),
                                arguments=tc.function.get("arguments") or {},
                            )
                        )
                        for tc in chunk.tool_calls
                    ]

                response = OllamaChatStreamChunk(
                    model=model,
                    created_at=_now_iso(),
                    message=OllamaMessage(
                        role="assistant",
                        content=chunk.delta or "",
                        thinking=chunk.thinking,
                        tool_calls=chunk_tool_calls,
                    ),
                    done=chunk.finish_reason is not None,
                )
                yield json.dumps(response.model_dump(exclude_none=True)) + "\n"

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

                    final_prompt_tokens = chunk.usage.prompt_tokens if chunk.usage else 0
                    final_completion_tokens = chunk.usage.completion_tokens if chunk.usage else 0
                    ctx.record_complete(
                        prompt_tokens=final_prompt_tokens,
                        completion_tokens=final_completion_tokens,
                    )

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
                            time_to_first_token_ms=ctx.time_to_first_token_ms,
                            tokens_per_second=ctx.tokens_per_second,
                            prompt_tokens=final_prompt_tokens,
                            completion_tokens=final_completion_tokens,
                            request_body=request_body,
                        )

        except Exception:
            logger.exception("Error in Ollama chat stream")
            error_response = {
                "error": "Stream interrupted",
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
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
):
    """Ollama-compatible generate endpoint."""
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        model=body.model,
        task="generate",
    )

    # Security: Sanitize prompt and system content
    sanitized_prompt = sanitizer.sanitize(body.prompt).sanitized
    sanitized_system = sanitizer.sanitize(body.system).sanitized if body.system else None

    # PII detection + optional scrubbing
    analysis_messages = []
    if sanitized_system:
        analysis_messages.append({"role": "system", "content": sanitized_system})
    analysis_messages.append({"role": "user", "content": sanitized_prompt})

    if pii_scrubber:
        scrub = should_scrub_pii(request)
        pre_scrub_messages = [dict(m) for m in analysis_messages]
        analysis_messages, pii_results = pii_scrubber.scan_messages(analysis_messages, scrub=scrub)
        pii_found = sum(r.detection_count for r in pii_results)
        if pii_found:
            logger.warning(
                "PII detected in request",
                request_id=ctx.request_id,
                pii_count=pii_found,
                scrubbed=scrub,
            )
            if audit_logger:
                await audit_logger.log_pii_events(
                    request_id=ctx.request_id,
                    client_id=client_id,
                    task="generate",
                    model=body.model,
                    messages=pre_scrub_messages,
                    pii_results=pii_results,
                    was_scrubbed=scrub,
                )
        # Update sanitized values from scrubbed messages
        if scrub:
            sanitized_prompt = analysis_messages[-1]["content"]
            if sanitized_system and len(analysis_messages) > 1:
                sanitized_system = analysis_messages[0]["content"]

    if security_analyzer:
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=analysis_messages,
            source_ip=request.client.host if request.client else None,
        )

    # Build messages from sanitized prompt and optional system
    messages = []
    if sanitized_system:
        messages.append(Message(role="system", content=sanitized_system))
    messages.append(Message(role="user", content=sanitized_prompt))

    # Client options pass through verbatim; see ollama_chat for rationale
    options = body.options or {}

    # Engine-native top-level generate fields, forwarded untouched
    extensions = {
        key: value
        for key, value in (
            ("system", sanitized_system),
            ("template", body.template),
            ("context", body.context),
            ("keep_alive", body.keep_alive),
        )
        if value is not None
    }

    request_kwargs = {
        "task": TaskType.GENERATE,
        "model": body.model,
        "messages": messages,
        "prompt": sanitized_prompt,
        "client_id": client_id,
        "stream": body.stream,
        "options": options,
        "extensions": extensions,
    }

    if body.format is not None:
        request_kwargs["response_format"] = _normalize_ollama_format(body.format)

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

    # Check policies
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    if body.stream:
        return await _stream_ollama_generate(
            dispatcher,
            internal_request,
            body.model,
            ctx,
            audit_logger,
            request_body={"messages": [m for m in analysis_messages]},
        )

    # Non-streaming
    result = await run_unless_disconnected(request, dispatcher.dispatch(internal_request))

    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )
    metrics.record_request(
        provider=result.provider_used,
        model=result.response.model,
        task="generate",
        status="success",
        latency_ms=ctx.total_latency_ms or 0,
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )

    # Record token usage for daily budget tracking
    total_tokens = (result.response.usage.prompt_tokens or 0) + (
        result.response.usage.completion_tokens or 0
    )
    if total_tokens > 0:
        enforcer.record_token_usage(client_id, result.response.model, total_tokens)

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
            request_body={"messages": [m for m in analysis_messages]},
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
    request_body: dict | None = None,
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
                            request_body=request_body,
                        )

                yield json.dumps(response) + "\n"

        except Exception:
            logger.exception("Error in Ollama generate stream")
            yield json.dumps({"error": "Stream interrupted", "done": True}) + "\n"

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
        models.append(
            OllamaModelInfo(
                name=discovered.name,
                model=discovered.name,
                modified_at=discovered.discovered_at.isoformat()
                if discovered.discovered_at
                else _now_iso(),
                size=discovered.size_bytes or 0,
                digest="",
                details={
                    "family": discovered.family or "",
                    "parameter_size": discovered.parameter_size or "",
                    "quantization_level": discovered.quantization or "",
                },
            )
        )

    return OllamaTagsResponse(models=models)


# =============================================================================
# Embeddings Endpoint
# =============================================================================


async def _run_embeddings(
    request: Request,
    *,
    model: str,
    inputs: list[str],
    auth: AuthResult,
    dispatcher: Dispatcher,
    enforcer: PolicyEnforcer,
    pii_scrubber: PIIScrubber | None,
    audit_logger: AuditLogger | None,
    sanitizer: Sanitizer,
    security_analyzer: AsyncSecurityAnalyzer | None,
) -> list[list[float]]:
    """Shared embeddings pipeline for /api/embeddings (legacy) and /api/embed."""
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        model=model,
        task="embeddings",
    )

    prompts = inputs

    # Security: Sanitize prompts
    sanitized_prompts = [sanitizer.sanitize(p).sanitized for p in prompts]

    # PII detection + optional scrubbing
    if pii_scrubber:
        scrub = should_scrub_pii(request)
        embed_messages = [{"role": "user", "content": p} for p in sanitized_prompts]
        pre_scrub_messages = [dict(m) for m in embed_messages]
        embed_messages, pii_results = pii_scrubber.scan_messages(embed_messages, scrub=scrub)
        pii_found = sum(r.detection_count for r in pii_results)
        if pii_found:
            logger.warning(
                "PII detected in request",
                request_id=ctx.request_id,
                pii_count=pii_found,
                scrubbed=scrub,
            )
            if audit_logger:
                await audit_logger.log_pii_events(
                    request_id=ctx.request_id,
                    client_id=client_id,
                    task="embeddings",
                    model=model,
                    messages=pre_scrub_messages,
                    pii_results=pii_results,
                    was_scrubbed=scrub,
                )
        if scrub:
            sanitized_prompts = [m["content"] for m in embed_messages]

    # Queue for async security analysis
    if security_analyzer:
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=model,
            messages=[{"role": "user", "content": "\n".join(sanitized_prompts)}],
            task="embeddings",
            source_ip=request.client.host if request.client else None,
        )

    request_kwargs = {
        "task": TaskType.EMBEDDINGS,
        "model": model,
        "input_data": sanitized_prompts,
        "client_id": client_id,
    }

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        request_kwargs["preferred_provider"] = auth.target_endpoint

    internal_request = InternalRequest(**request_kwargs)

    # Check policies
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    result = await run_unless_disconnected(request, dispatcher.dispatch(internal_request))

    metrics.record_request(
        provider=result.provider_used,
        model=result.response.model,
        task="embeddings",
        status="success",
        latency_ms=ctx.total_latency_ms or 0,
        prompt_tokens=result.response.usage.prompt_tokens,
    )

    # Record token usage for daily budget tracking
    if result.response.usage.prompt_tokens:
        enforcer.record_token_usage(
            client_id, result.response.model, result.response.usage.prompt_tokens
        )

    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="embeddings",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            latency_ms=ctx.total_latency_ms,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
            request_body={"prompts": sanitized_prompts},
        )

    return result.response.embeddings or []


@router.post("/embeddings")
async def ollama_embeddings(
    request: Request,
    body: OllamaEmbeddingsRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
):
    """Ollama-compatible embeddings endpoint (legacy shape)."""
    inputs = body.prompt if isinstance(body.prompt, list) else [body.prompt]
    embeddings = await _run_embeddings(
        request,
        model=body.model,
        inputs=inputs,
        auth=auth,
        dispatcher=dispatcher,
        enforcer=enforcer,
        pii_scrubber=pii_scrubber,
        audit_logger=audit_logger,
        sanitizer=sanitizer,
        security_analyzer=security_analyzer,
    )
    if embeddings and len(embeddings) == 1:
        return OllamaEmbeddingsResponse(embedding=embeddings[0])
    return OllamaEmbeddingsResponse(embedding=embeddings)


@router.post("/embed")
async def ollama_embed(
    request: Request,
    body: OllamaEmbedRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
):
    """Ollama-compatible embed endpoint (modern shape, Ollama >= 0.2.6)."""
    inputs = body.input if isinstance(body.input, list) else [body.input]
    embeddings = await _run_embeddings(
        request,
        model=body.model,
        inputs=inputs,
        auth=auth,
        dispatcher=dispatcher,
        enforcer=enforcer,
        pii_scrubber=pii_scrubber,
        audit_logger=audit_logger,
        sanitizer=sanitizer,
        security_analyzer=security_analyzer,
    )
    return OllamaEmbedResponse(model=body.model, embeddings=embeddings)

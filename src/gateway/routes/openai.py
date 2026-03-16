"""OpenAI-compatible API endpoints.

Per PRD Section 7:
- POST /v1/chat/completions
- POST /v1/completions
- POST /v1/embeddings

These endpoints are designed to be drop-in compatible with OpenAI clients,
enabling existing tooling to work without modification.

Per rule.md:
- No Implicit Trust: Validate all inputs
- Explicit Boundaries: Clear request/response contracts
- Auditability: Log all requests

Per API Error Handling Architecture:
- Routes raise domain errors (GatewayError subclasses)
- Exception handler middleware translates to HTTP responses
- No try/except blocks for error-to-HTTP translation
"""

import json
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway.dispatch import Dispatcher
from gateway.errors import DispatchError, StreamError
from gateway.models.common import TaskType
from gateway.models.openai import (
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIChatStreamResponse,
    OpenAICompletionRequest,
    OpenAICompletionResponse,
    OpenAIEmbeddingRequest,
    OpenAIEmbeddingResponse,
)
from gateway.observability import get_logger, get_metrics
from gateway.observability.logging import clear_request_context
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
    setup_request_context,
    should_scrub_pii,
    translate_policy_violation,
)
from gateway.security import AsyncSecurityAnalyzer, PIIScrubber, Sanitizer
from gateway.storage import AuditLogger

logger = get_logger(__name__)
metrics = get_metrics()

router = APIRouter(prefix="/v1", tags=["openai"])


# =============================================================================
# Chat Completions
# =============================================================================


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    body: OpenAIChatRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
):
    """Create a chat completion.

    OpenAI-compatible endpoint for chat-based interactions.

    Supports both streaming and non-streaming responses.
    Domain errors propagate to exception handler middleware.
    """
    client_id = auth.client_id

    # Setup request context for logging
    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="chat",
    )

    # Security: Sanitize message content (removes invisible Unicode chars)
    sanitized_messages = []
    for msg in body.messages:
        text = msg.content_as_str()
        if text:
            result = sanitizer.sanitize(text)
            sanitized_messages.append({"role": msg.role, "content": result.sanitized})
        else:
            sanitized_messages.append({"role": msg.role, "content": ""})

    # PII detection (always flags) + optional scrubbing (per-route)
    if pii_scrubber:
        scrub = should_scrub_pii(request)
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

    # Queue for async security analysis (non-blocking)
    if security_analyzer:
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=sanitized_messages,
            source_ip=request.client.host if request.client else None,
        )

    # Apply sanitized content back to body before converting to internal format
    # This ensures to_internal() uses sanitized text, not raw user input
    for i, msg in enumerate(body.messages):
        if i < len(sanitized_messages):
            msg.content = sanitized_messages[i]["content"]

    # Convert to internal format
    internal_request = body.to_internal(client_id=client_id, task=TaskType.CHAT)

    # Force non-streaming when tools are present (streaming tool call format
    # requires complex delta fragmentation; non-streaming works with OpenAI SDK)
    if body.tools and body.stream:
        internal_request = internal_request.model_copy(update={"stream": False})

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        internal_request = internal_request.model_copy(
            update={"preferred_provider": auth.target_endpoint}
        )

    # Check policies - raises domain errors on violation
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=auth.client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    # Handle streaming (skip for tool calls - return non-streaming JSON instead)
    if body.stream and not body.tools:
        return await _stream_chat_response(
            dispatcher, internal_request, body.model, ctx, audit_logger
        )

    # Non-streaming: dispatch and wait
    # DispatchError propagates to exception handler
    with metrics.track_request("dispatch"):
        result = await dispatcher.dispatch(internal_request)

    # Record metrics
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
        tokens_per_second=ctx.tokens_per_second,
    )

    # Record token usage for daily budget tracking
    total_tokens = (result.response.usage.prompt_tokens or 0) + (
        result.response.usage.completion_tokens or 0
    )
    if total_tokens > 0:
        enforcer.record_token_usage(client_id, result.response.model, total_tokens)

    # Audit log the request
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="chat",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            user_id=body.user,
            stream=False,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            latency_ms=ctx.total_latency_ms,
            tokens_per_second=ctx.tokens_per_second,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
        )

    # Convert to OpenAI format
    openai_response = OpenAIChatResponse.from_internal(result.response)

    # If client requested streaming + tools, wrap the complete response in SSE
    # so the OpenAI SDK's stream parser is satisfied
    if body.stream and body.tools:
        # Rewrite to streaming chunk format: object → chunk, message → delta
        d = openai_response.model_dump()
        d["object"] = "chat.completion.chunk"
        for choice in d.get("choices", []):
            if "message" in choice:
                choice["delta"] = choice.pop("message")
        response_json = json.dumps(d)
        sse_body = f"data: {response_json}\n\ndata: [DONE]\n\n"
        return StreamingResponse(
            iter([sse_body.encode()]),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return openai_response


async def _stream_chat_response(
    dispatcher: Dispatcher,
    internal_request,
    model: str,
    ctx,
    audit_logger: AuditLogger | None,
) -> StreamingResponse:
    """Create streaming response for chat completions.

    Note: Streaming errors are sent as SSE error events rather than
    raising exceptions, since the HTTP response has already started.
    """

    async def generate() -> AsyncGenerator[bytes, None]:
        provider_name = None
        final_prompt_tokens = 0
        final_completion_tokens = 0

        try:
            provider_name, stream = await dispatcher.dispatch_stream(internal_request)

            first_chunk = True
            async for chunk in stream:
                if first_chunk:
                    ctx.record_first_token()
                    first_chunk = False

                # Convert to OpenAI streaming format
                response = OpenAIChatStreamResponse.from_chunk(chunk, model)
                yield f"data: {response.model_dump_json()}\n\n".encode()

                # If this is the final chunk, record completion
                if chunk.finish_reason:
                    final_prompt_tokens = chunk.usage.prompt_tokens if chunk.usage else 0
                    final_completion_tokens = chunk.usage.completion_tokens if chunk.usage else 0
                    ctx.record_complete(
                        prompt_tokens=final_prompt_tokens,
                        completion_tokens=final_completion_tokens,
                    )
                    metrics.record_request(
                        provider=provider_name,
                        model=model,
                        task="chat",
                        status="success",
                        latency_ms=ctx.total_latency_ms or 0,
                        time_to_first_token_ms=ctx.time_to_first_token_ms,
                        tokens_per_second=ctx.tokens_per_second,
                    )

                    # Audit log for streaming
                    if audit_logger and provider_name:
                        await audit_logger.log_request(
                            request_id=ctx.request_id,
                            client_id=internal_request.client_id,
                            task="chat",
                            model=model,
                            endpoint=provider_name,
                            status="success",
                            user_id=internal_request.user_id,
                            stream=True,
                            max_tokens=internal_request.max_tokens,
                            temperature=internal_request.temperature,
                            latency_ms=ctx.total_latency_ms,
                            time_to_first_token_ms=ctx.time_to_first_token_ms,
                            tokens_per_second=ctx.tokens_per_second,
                            prompt_tokens=final_prompt_tokens,
                            completion_tokens=final_completion_tokens,
                        )

            # Send [DONE] marker
            yield b"data: [DONE]\n\n"

        except DispatchError as e:
            # For streaming, send error as SSE event
            ctx.record_error(e.code.value, str(e))
            error_response = e.to_dict()
            yield f"data: {json.dumps(error_response)}\n\n".encode()

            # Audit log error
            if audit_logger:
                await audit_logger.log_request(
                    request_id=ctx.request_id,
                    client_id=internal_request.client_id,
                    task="chat",
                    model=model,
                    endpoint=provider_name or "unknown",
                    status="error",
                    stream=True,
                    error_code=e.code.value,
                    error_message=str(e),
                )

        except Exception as e:
            # Wrap unexpected errors
            ctx.record_error("stream_error", str(e))
            logger.exception("Error in chat stream")
            stream_error = StreamError(message="Stream interrupted")
            yield f"data: {json.dumps(stream_error.to_dict())}\n\n".encode()

            # Audit log error
            if audit_logger:
                await audit_logger.log_request(
                    request_id=ctx.request_id,
                    client_id=internal_request.client_id,
                    task="chat",
                    model=model,
                    endpoint=provider_name or "unknown",
                    status="error",
                    stream=True,
                    error_code="stream_error",
                    error_message=str(e),
                )
        finally:
            clear_request_context()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# Completions
# =============================================================================


@router.post("/completions")
async def completions(
    request: Request,
    body: OpenAICompletionRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
) -> OpenAICompletionResponse:
    """Create a text completion.

    OpenAI-compatible endpoint for completion-based interactions.
    Domain errors propagate to exception handler middleware.
    """
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="completion",
    )

    # Security: Sanitize prompt content
    sanitized_prompt = body.prompt
    if isinstance(body.prompt, str):
        result = sanitizer.sanitize(body.prompt)
        sanitized_prompt = result.sanitized
    elif isinstance(body.prompt, list):
        sanitized_prompt = [sanitizer.sanitize(p).sanitized for p in body.prompt]

    # PII detection + optional scrubbing
    if pii_scrubber:
        scrub = should_scrub_pii(request)
        if isinstance(sanitized_prompt, str):
            pre_scrub_prompt = sanitized_prompt
            pii_result = pii_scrubber.scan(sanitized_prompt, scrub=scrub)
            if pii_result.has_pii:
                logger.warning(
                    "PII detected in request",
                    request_id=ctx.request_id,
                    pii_count=pii_result.detection_count,
                    scrubbed=scrub,
                )
                if audit_logger:
                    await audit_logger.log_pii_events(
                        request_id=ctx.request_id,
                        client_id=client_id,
                        task="completions",
                        model=body.model,
                        messages=[{"role": "user", "content": pre_scrub_prompt}],
                        pii_results=[pii_result],
                        was_scrubbed=scrub,
                    )
                if scrub and pii_result.scrubbed_text:
                    sanitized_prompt = pii_result.scrubbed_text
        elif isinstance(sanitized_prompt, list):
            all_pii_results = []
            pre_scrub_prompts = list(sanitized_prompt)
            for idx, p in enumerate(sanitized_prompt):
                pii_result = pii_scrubber.scan(p, scrub=scrub)
                if pii_result.has_pii:
                    all_pii_results.append((idx, pii_result))
                    logger.warning(
                        "PII detected in request",
                        request_id=ctx.request_id,
                        pii_count=pii_result.detection_count,
                        scrubbed=scrub,
                    )
                    if scrub and pii_result.scrubbed_text:
                        sanitized_prompt[idx] = pii_result.scrubbed_text
            if all_pii_results and audit_logger:
                await audit_logger.log_pii_events(
                    request_id=ctx.request_id,
                    client_id=client_id,
                    task="completions",
                    model=body.model,
                    messages=[{"role": "user", "content": p} for p in pre_scrub_prompts],
                    pii_results=[r for _, r in all_pii_results],
                    was_scrubbed=scrub,
                )

    # Queue for async security analysis
    if security_analyzer:
        prompt_content = (
            sanitized_prompt if isinstance(sanitized_prompt, str) else "\n".join(sanitized_prompt)
        )
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=[{"role": "user", "content": prompt_content}],
            source_ip=request.client.host if request.client else None,
        )

    # Convert to internal format
    internal_request = body.to_internal(client_id=client_id, task=TaskType.COMPLETION)

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        internal_request = internal_request.model_copy(
            update={"preferred_provider": auth.target_endpoint}
        )

    # Check policies - raises domain errors on violation
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=auth.client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    # Dispatch request - DispatchError propagates to exception handler
    with metrics.track_request("dispatch"):
        result = await dispatcher.dispatch(internal_request)

    # Record metrics
    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
    )
    metrics.record_request(
        provider=result.provider_used,
        model=result.response.model,
        task="completion",
        status="success",
        latency_ms=ctx.total_latency_ms or 0,
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=result.response.usage.completion_tokens,
        tokens_per_second=ctx.tokens_per_second,
    )

    # Record token usage for daily budget tracking
    total_tokens = (result.response.usage.prompt_tokens or 0) + (
        result.response.usage.completion_tokens or 0
    )
    if total_tokens > 0:
        enforcer.record_token_usage(client_id, result.response.model, total_tokens)

    # Audit log the request
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="completion",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            user_id=body.user,
            stream=False,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            latency_ms=ctx.total_latency_ms,
            tokens_per_second=ctx.tokens_per_second,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=result.response.usage.completion_tokens,
        )

    return OpenAICompletionResponse.from_internal(result.response)


# =============================================================================
# Embeddings
# =============================================================================


@router.post("/embeddings")
async def embeddings(
    request: Request,
    body: OpenAIEmbeddingRequest,
    auth: Annotated[AuthResult, Depends(get_auth)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    sanitizer: Annotated[Sanitizer, Depends(get_sanitizer)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
    pii_scrubber: Annotated[PIIScrubber | None, Depends(get_pii_scrubber)],
) -> OpenAIEmbeddingResponse:
    """Create embeddings for the input text.

    OpenAI-compatible endpoint for generating text embeddings.
    Domain errors propagate to exception handler middleware.
    """
    client_id = auth.client_id

    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="embeddings",
    )

    # Security: Sanitize input content
    sanitized_input = body.input
    if isinstance(body.input, str):
        sanitized_input = sanitizer.sanitize(body.input).sanitized
    elif isinstance(body.input, list):
        sanitized_input = [
            sanitizer.sanitize(i).sanitized if isinstance(i, str) else i for i in body.input
        ]

    # PII detection (always flags) + optional scrubbing (per-route)
    if pii_scrubber:
        scrub = should_scrub_pii(request)
        if isinstance(sanitized_input, str):
            pre_scrub_input = sanitized_input
            pii_result = pii_scrubber.scan(sanitized_input, scrub=scrub)
            if pii_result.detection_count:
                logger.warning(
                    "PII detected in request",
                    request_id=ctx.request_id,
                    pii_count=pii_result.detection_count,
                    scrubbed=scrub,
                )
                if audit_logger:
                    await audit_logger.log_pii_events(
                        request_id=ctx.request_id,
                        client_id=client_id,
                        task="embeddings",
                        model=body.model,
                        messages=[{"role": "user", "content": pre_scrub_input}],
                        pii_results=[pii_result],
                        was_scrubbed=scrub,
                    )
            if scrub:
                sanitized_input = pii_result.scrubbed_text
        elif isinstance(sanitized_input, list):
            total_pii = 0
            all_pii_results = []
            pre_scrub_items = list(sanitized_input)
            scrubbed_list = []
            for item in sanitized_input:
                if isinstance(item, str):
                    pii_result = pii_scrubber.scan(item, scrub=scrub)
                    total_pii += pii_result.detection_count
                    if pii_result.has_pii:
                        all_pii_results.append(pii_result)
                    scrubbed_list.append(pii_result.scrubbed_text if scrub else item)
                else:
                    scrubbed_list.append(item)
            if total_pii:
                logger.warning(
                    "PII detected in request",
                    request_id=ctx.request_id,
                    pii_count=total_pii,
                    scrubbed=scrub,
                )
                if audit_logger and all_pii_results:
                    await audit_logger.log_pii_events(
                        request_id=ctx.request_id,
                        client_id=client_id,
                        task="embeddings",
                        model=body.model,
                        messages=[
                            {"role": "user", "content": str(i)}
                            for i in pre_scrub_items
                            if isinstance(i, str)
                        ],
                        pii_results=all_pii_results,
                        was_scrubbed=scrub,
                    )
            if scrub:
                sanitized_input = scrubbed_list

    # Queue for async security analysis
    if security_analyzer:
        input_content = (
            sanitized_input
            if isinstance(sanitized_input, str)
            else "\n".join(str(i) for i in sanitized_input)
        )
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=[{"role": "user", "content": input_content}],
            task="embeddings",
            source_ip=request.client.host if request.client else None,
        )

    # Convert to internal format
    internal_request = body.to_internal(client_id=client_id)

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        internal_request = internal_request.model_copy(
            update={"preferred_provider": auth.target_endpoint}
        )

    # Check policies - raises domain errors on violation
    try:
        enforcer.enforce(
            internal_request,
            rate_limit_key=auth.client_id,
            allowed_models=auth.allowed_models,
            allowed_endpoints=auth.allowed_endpoints,
            rate_limit_rpm=auth.rate_limit_rpm,
        )
    except PolicyViolation as e:
        translate_policy_violation(e)

    # Dispatch request - DispatchError propagates to exception handler
    with metrics.track_request("dispatch"):
        result = await dispatcher.dispatch(internal_request)

    # Record metrics
    ctx.record_complete(
        prompt_tokens=result.response.usage.prompt_tokens,
        completion_tokens=0,
    )
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

    # Audit log the request
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="embeddings",
            model=result.response.model,
            endpoint=result.provider_used,
            status="success",
            user_id=body.user,
            stream=False,
            latency_ms=ctx.total_latency_ms,
            prompt_tokens=result.response.usage.prompt_tokens,
            completion_tokens=0,
        )

    return OpenAIEmbeddingResponse.from_internal(result.response)

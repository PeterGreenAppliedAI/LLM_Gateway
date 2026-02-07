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
from typing import Annotated, AsyncGenerator

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
    get_auth,
    get_audit_logger,
    get_dispatcher,
    get_enforcer,
    get_sanitizer,
    get_security_analyzer,
    setup_request_context,
    translate_policy_violation,
)
from gateway.security import AsyncSecurityAnalyzer, Sanitizer
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
        if msg.content:
            result = sanitizer.sanitize(msg.content)
            sanitized_messages.append({"role": msg.role, "content": result.sanitized})
        else:
            sanitized_messages.append({"role": msg.role, "content": msg.content or ""})

    # Queue for async security analysis (non-blocking)
    if security_analyzer:
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=sanitized_messages,
        )

    # Convert to internal format
    internal_request = body.to_internal(client_id=client_id, task=TaskType.CHAT)

    # Apply per-client target endpoint if configured
    if auth.target_endpoint:
        internal_request = internal_request.model_copy(
            update={"preferred_provider": auth.target_endpoint}
        )

    # Check policies - raises domain errors on violation
    try:
        enforcer.enforce(internal_request)
    except PolicyViolation as e:
        translate_policy_violation(e)

    # Handle streaming
    if body.stream:
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
    return OpenAIChatResponse.from_internal(result.response)


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

    # Queue for async security analysis
    if security_analyzer:
        prompt_content = sanitized_prompt if isinstance(sanitized_prompt, str) else "\n".join(sanitized_prompt)
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=[{"role": "user", "content": prompt_content}],
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
        enforcer.enforce(internal_request)
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
        sanitized_input = [sanitizer.sanitize(i).sanitized if isinstance(i, str) else i for i in body.input]

    # Queue for async security analysis
    if security_analyzer:
        input_content = sanitized_input if isinstance(sanitized_input, str) else "\n".join(str(i) for i in sanitized_input)
        security_analyzer.queue_request(
            request_id=ctx.request_id,
            client_id=client_id,
            model=body.model,
            messages=[{"role": "user", "content": input_content}],
            task="embeddings",
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
        enforcer.enforce(internal_request)
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

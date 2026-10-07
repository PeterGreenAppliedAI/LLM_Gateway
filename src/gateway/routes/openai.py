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

from gateway.dispatch import Dispatcher, DispatchResult
from gateway.errors import StreamError
from gateway.models.common import TaskType
from gateway.models.openai import (
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAIChatStreamResponse,
    OpenAICompletionRequest,
    OpenAICompletionResponse,
    OpenAICompletionStreamResponse,
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
    resolve_access_scope,
    run_unless_disconnected,
    setup_request_context,
    should_scrub_pii,
    translate_policy_violation,
)
from gateway.routes.fanout import check_choice_count, dispatch_choices
from gateway.routes.stream_recorder import StreamRecorder
from gateway.security import AsyncSecurityAnalyzer, PIIScrubber, Sanitizer
from gateway.storage import AuditLogger

logger = get_logger(__name__)
metrics = get_metrics()

router = APIRouter(prefix="/v1", tags=["openai"])


def _audit_response_body(response) -> dict:
    """Model output for the audit log (stored only when the operator
    enables response-body storage; embeddings are never included)."""
    body: dict = {"content": response.content or ""}
    if response.tool_calls:
        body["tool_calls"] = [{"function": tc.function} for tc in response.tool_calls]
    return body


def _audit_choices_body(results: list[DispatchResult]) -> dict:
    """Audit output for one or more choices (one row per client request)."""
    if len(results) == 1:
        return _audit_response_body(results[0].response)
    return {
        "choices": [
            {**_audit_response_body(r.response), "endpoint": r.provider_used} for r in results
        ]
    }


def _summed_usage(results: list[DispatchResult]) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) across all choices."""
    return (
        sum(r.response.usage.prompt_tokens or 0 for r in results),
        sum(r.response.usage.completion_tokens or 0 for r in results),
    )


def _endpoints_used(results: list[DispatchResult]) -> str:
    """Endpoint column for the audit row; choices may land on different endpoints."""
    return ",".join(dict.fromkeys(r.provider_used for r in results))[:64]


def _record_choice_metrics(results: list[DispatchResult], ctx, task: str) -> None:
    """One metrics sample per upstream generation, so per-endpoint metrics stay true."""
    for r in results:
        metrics.record_request(
            provider=r.provider_used,
            model=r.response.model,
            task=task,
            status="success",
            latency_ms=(ctx.total_latency_ms if len(results) == 1 else r.response.latency_ms) or 0,
            prompt_tokens=r.response.usage.prompt_tokens,
            completion_tokens=r.response.usage.completion_tokens,
            tokens_per_second=ctx.tokens_per_second if len(results) == 1 else None,
        )


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
    check_choice_count(body.n, stream=body.stream)

    # Apply per-key and per-environment routing restrictions
    internal_request = internal_request.model_copy(
        update=await resolve_access_scope(request, auth, internal_request.model)
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

    # Handle streaming (tool calls stream too, as complete calls per delta)
    if body.stream:
        return await _stream_chat_response(
            request,
            dispatcher,
            internal_request,
            body.model,
            ctx,
            audit_logger,
            enforcer=enforcer,
            request_body={
                "messages": [
                    m.model_dump(exclude_none=True) for m in internal_request.messages or []
                ],
                "response_format": body.response_format,
                "tool_names": [t.get("function", {}).get("name") for t in (body.tools or [])],
            },
        )

    # Non-streaming: one upstream generation per requested choice (n),
    # run concurrently. DispatchError propagates to exception handler.
    with metrics.track_request("dispatch"):
        results = await dispatch_choices(request, dispatcher, [internal_request] * body.n)

    prompt_tokens, completion_tokens = _summed_usage(results)
    ctx.record_complete(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    _record_choice_metrics(results, ctx, "chat")

    # Record token usage for daily budget tracking
    if prompt_tokens + completion_tokens > 0:
        enforcer.record_token_usage(
            client_id, results[0].response.model, prompt_tokens + completion_tokens
        )

    # Audit log the request (one row, however many choices)
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="chat",
            model=results[0].response.model,
            endpoint=_endpoints_used(results),
            status="success",
            user_id=body.user,
            stream=False,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            latency_ms=ctx.total_latency_ms,
            tokens_per_second=ctx.tokens_per_second,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            request_body={
                "messages": [
                    m.model_dump(exclude_none=True) for m in internal_request.messages or []
                ],
                "response_format": body.response_format,
                "tool_names": [t.get("function", {}).get("name") for t in (body.tools or [])],
                "n": body.n,
            },
            response_body=_audit_choices_body(results),
        )

    # Convert to OpenAI format
    return OpenAIChatResponse.from_internal_many([r.response for r in results])


async def _stream_chat_response(
    request: Request,
    dispatcher: Dispatcher,
    internal_request,
    model: str,
    ctx,
    audit_logger: AuditLogger | None,
    enforcer: PolicyEnforcer | None = None,
    request_body: dict | None = None,
) -> StreamingResponse:
    """Create streaming response for chat completions.

    The endpoint is chosen and its first chunk received before the response
    starts, so failures up to that point raise and return a real HTTP error.
    Failures after the first byte are sent as an SSE error event.
    """
    recorder = StreamRecorder(
        ctx=ctx,
        internal_request=internal_request,
        model=model,
        task="chat",
        audit_logger=audit_logger,
        enforcer=enforcer,
        request_body=request_body,
    )
    stream = await recorder.start(request, dispatcher)

    async def generate() -> AsyncGenerator[bytes, None]:
        tool_calls_sent = 0
        try:
            async for chunk in stream:
                recorder.observe(chunk)

                if recorder.is_error(chunk):
                    # Mid-stream provider failure: OpenAI's own wire format
                    # for this is an error object in a data frame
                    error = StreamError(message=chunk.error or "Stream interrupted")
                    yield f"data: {json.dumps(error.to_dict())}\n\n".encode()
                    await recorder.finish_error_chunk(chunk)
                    break

                finish_reason = None
                if chunk.finish_reason and (chunk.tool_calls or tool_calls_sent):
                    # Ollama reports "stop" after tool calls; OpenAI clients
                    # key off "tool_calls" to run them
                    finish_reason = "tool_calls"
                response = OpenAIChatStreamResponse.from_chunk(
                    chunk, model, tool_call_start=tool_calls_sent, finish_reason=finish_reason
                )
                tool_calls_sent += len(chunk.tool_calls or [])
                yield f"data: {response.model_dump_json()}\n\n".encode()

                if chunk.finish_reason:
                    await recorder.finish()

            # Send [DONE] marker
            yield b"data: [DONE]\n\n"

        except Exception as e:
            # Wrap unexpected errors
            logger.exception("Error in chat stream")
            stream_error = StreamError(message="Stream interrupted")
            yield f"data: {json.dumps(stream_error.to_dict())}\n\n".encode()
            await recorder.finish(status="error", error_code="stream_error", error_message=str(e))
        finally:
            await recorder.finish_disconnected(stream)
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

    # One choice per prompt per n. Validate first: to_internal reads prompt[0]
    prompt_count = 1 if isinstance(body.prompt, str) else len(body.prompt)
    check_choice_count(prompt_count * body.n, stream=body.stream)

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

    # Apply per-key and per-environment routing restrictions
    internal_request = internal_request.model_copy(
        update=await resolve_access_scope(request, auth, internal_request.model)
    )

    # Queue-and-drain instead of 429: embedding bursts wait for rate-limit
    # headroom (bounded); other policy violations still fail immediately
    from gateway.errors import RateLimitError
    from gateway.policy.embedding_queue import QueueSaturatedError
    from gateway.routes.dependencies import get_embedding_queue

    queue = get_embedding_queue(request)
    try:
        await queue.admit(
            lambda: enforcer.enforce(
                internal_request,
                rate_limit_key=auth.client_id,
                allowed_models=auth.allowed_models,
                allowed_endpoints=auth.allowed_endpoints,
                rate_limit_rpm=auth.rate_limit_rpm,
            )
        )
    except QueueSaturatedError as e:
        raise RateLimitError(message=str(e), retry_after=e.retry_after)
    except PolicyViolation as e:
        translate_policy_violation(e)

    # The sanitized/scrubbed prompts are what the model gets (body.prompt is
    # the raw client input; before 0ec8d7b it was sent as-is)
    prompts = [sanitized_prompt] if isinstance(sanitized_prompt, str) else list(sanitized_prompt)

    if body.stream:
        return await _stream_completion_response(
            request,
            dispatcher,
            internal_request.model_copy(update={"prompt": prompts[0], "stream": True}),
            body.model,
            ctx,
            audit_logger,
            enforcer=enforcer,
            request_body={"prompt": prompts[0]},
        )

    # One upstream generation per prompt per n, run concurrently and returned
    # in OpenAI order (prompt-major). Before, extra prompts were dropped.
    choice_requests = [
        internal_request.model_copy(update={"prompt": prompt})
        for prompt in prompts
        for _ in range(body.n)
    ]
    with metrics.track_request("dispatch"):
        results = await dispatch_choices(request, dispatcher, choice_requests)

    prompt_tokens, completion_tokens = _summed_usage(results)
    ctx.record_complete(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    _record_choice_metrics(results, ctx, "completion")

    # Record token usage for daily budget tracking
    if prompt_tokens + completion_tokens > 0:
        enforcer.record_token_usage(
            client_id, results[0].response.model, prompt_tokens + completion_tokens
        )

    # Audit log the request (one row, however many choices)
    if audit_logger:
        await audit_logger.log_request(
            request_id=ctx.request_id,
            client_id=client_id,
            task="completion",
            model=results[0].response.model,
            endpoint=_endpoints_used(results),
            status="success",
            user_id=body.user,
            stream=False,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            latency_ms=ctx.total_latency_ms,
            tokens_per_second=ctx.tokens_per_second,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            request_body={"prompt": prompts if len(prompts) > 1 else prompts[0], "n": body.n},
            response_body=_audit_choices_body(results),
        )

    return OpenAICompletionResponse.from_internal_many([r.response for r in results])


async def _stream_completion_response(
    request: Request,
    dispatcher: Dispatcher,
    internal_request,
    model: str,
    ctx,
    audit_logger: AuditLogger | None,
    enforcer: PolicyEnforcer | None = None,
    request_body: dict | None = None,
) -> StreamingResponse:
    """Stream a text completion as OpenAI text_completion SSE frames.

    Same contract as chat streaming: endpoint chosen and first chunk
    received before headers (real HTTP errors), errors after that sent
    in-band, outcome recorded once by StreamRecorder.
    """
    recorder = StreamRecorder(
        ctx=ctx,
        internal_request=internal_request,
        model=model,
        task="completion",
        audit_logger=audit_logger,
        enforcer=enforcer,
        request_body=request_body,
    )
    stream = await recorder.start(request, dispatcher)

    async def generate() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in stream:
                recorder.observe(chunk)
                if recorder.is_error(chunk):
                    error = StreamError(message=chunk.error or "Stream interrupted")
                    yield f"data: {json.dumps(error.to_dict())}\n\n".encode()
                    await recorder.finish_error_chunk(chunk)
                    break
                response = OpenAICompletionStreamResponse.from_chunk(chunk, model)
                yield f"data: {response.model_dump_json()}\n\n".encode()
                if chunk.finish_reason:
                    await recorder.finish()

            yield b"data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Error in completion stream")
            stream_error = StreamError(message="Stream interrupted")
            yield f"data: {json.dumps(stream_error.to_dict())}\n\n".encode()
            await recorder.finish(status="error", error_code="stream_error", error_message=str(e))
        finally:
            await recorder.finish_disconnected(stream)
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

    # Apply per-key and per-environment routing restrictions
    internal_request = internal_request.model_copy(
        update=await resolve_access_scope(request, auth, internal_request.model)
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
        result = await run_unless_disconnected(request, dispatcher.dispatch(internal_request))

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

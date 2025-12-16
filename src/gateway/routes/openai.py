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
"""

import json
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from gateway.dispatch import Dispatcher, DispatchError
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
    authenticate,
    get_dispatcher,
    get_enforcer,
    setup_request_context,
    RateLimitError,
    PolicyError,
)

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
    client_id: Annotated[str, Depends(authenticate)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
):
    """Create a chat completion.

    OpenAI-compatible endpoint for chat-based interactions.

    Supports both streaming and non-streaming responses.
    """
    # Setup request context for logging
    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="chat",
    )

    try:
        # Convert to internal format
        internal_request = body.to_internal(client_id=client_id, task=TaskType.CHAT)

        # Check policies
        try:
            enforcer.enforce(internal_request)
        except PolicyViolation as e:
            if e.policy_type == "rate_limit":
                raise RateLimitError(str(e), retry_after=e.retry_after)
            raise PolicyError(str(e), code=e.code)

        # Handle streaming
        if body.stream:
            return await _stream_chat_response(
                dispatcher, internal_request, body.model, ctx
            )

        # Non-streaming: dispatch and wait
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

        # Convert to OpenAI format
        return OpenAIChatResponse.from_internal(result.response)

    except DispatchError as e:
        ctx.record_error("dispatch_error", str(e))
        metrics.record_error(e.provider or "unknown", e.code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": e.code, "message": str(e)},
        )
    except ValidationError as e:
        ctx.record_error("validation_error", str(e))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation_error", "message": str(e)},
        )
    except (RateLimitError, PolicyError):
        raise
    except Exception as e:
        ctx.record_error("internal_error", str(e))
        logger.exception("Unexpected error in chat_completions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "message": "An unexpected error occurred"},
        )
    finally:
        clear_request_context()


async def _stream_chat_response(
    dispatcher: Dispatcher,
    internal_request,
    model: str,
    ctx,
) -> StreamingResponse:
    """Create streaming response for chat completions."""

    async def generate() -> AsyncGenerator[bytes, None]:
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
                    ctx.record_complete(
                        prompt_tokens=chunk.usage.prompt_tokens if chunk.usage else 0,
                        completion_tokens=chunk.usage.completion_tokens if chunk.usage else 0,
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

            # Send [DONE] marker
            yield b"data: [DONE]\n\n"

        except DispatchError as e:
            ctx.record_error("dispatch_error", str(e))
            error_response = {"error": {"code": e.code, "message": str(e)}}
            yield f"data: {json.dumps(error_response)}\n\n".encode()
        except Exception as e:
            ctx.record_error("stream_error", str(e))
            logger.exception("Error in chat stream")
            error_response = {"error": {"code": "stream_error", "message": "Stream interrupted"}}
            yield f"data: {json.dumps(error_response)}\n\n".encode()
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
    client_id: Annotated[str, Depends(authenticate)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> OpenAICompletionResponse:
    """Create a text completion.

    OpenAI-compatible endpoint for completion-based interactions.
    """
    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="completion",
    )

    try:
        # Convert to internal format
        internal_request = body.to_internal(client_id=client_id, task=TaskType.COMPLETION)

        # Check policies
        try:
            enforcer.enforce(internal_request)
        except PolicyViolation as e:
            if e.policy_type == "rate_limit":
                raise RateLimitError(str(e), retry_after=e.retry_after)
            raise PolicyError(str(e), code=e.code)

        # Dispatch request
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

        return OpenAICompletionResponse.from_internal(result.response)

    except DispatchError as e:
        ctx.record_error("dispatch_error", str(e))
        metrics.record_error(e.provider or "unknown", e.code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": e.code, "message": str(e)},
        )
    except (RateLimitError, PolicyError):
        raise
    except Exception as e:
        ctx.record_error("internal_error", str(e))
        logger.exception("Unexpected error in completions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "message": "An unexpected error occurred"},
        )
    finally:
        clear_request_context()


# =============================================================================
# Embeddings
# =============================================================================


@router.post("/embeddings")
async def embeddings(
    request: Request,
    body: OpenAIEmbeddingRequest,
    client_id: Annotated[str, Depends(authenticate)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> OpenAIEmbeddingResponse:
    """Create embeddings for the input text.

    OpenAI-compatible endpoint for generating text embeddings.
    """
    ctx = setup_request_context(
        client_id=client_id,
        user_id=body.user,
        model=body.model,
        task="embeddings",
    )

    try:
        # Convert to internal format
        internal_request = body.to_internal(client_id=client_id)

        # Check policies
        try:
            enforcer.enforce(internal_request)
        except PolicyViolation as e:
            if e.policy_type == "rate_limit":
                raise RateLimitError(str(e), retry_after=e.retry_after)
            raise PolicyError(str(e), code=e.code)

        # Dispatch request
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

        return OpenAIEmbeddingResponse.from_internal(result.response)

    except DispatchError as e:
        ctx.record_error("dispatch_error", str(e))
        metrics.record_error(e.provider or "unknown", e.code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": e.code, "message": str(e)},
        )
    except (RateLimitError, PolicyError):
        raise
    except Exception as e:
        ctx.record_error("internal_error", str(e))
        logger.exception("Unexpected error in embeddings")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "internal_error", "message": "An unexpected error occurred"},
        )
    finally:
        clear_request_context()

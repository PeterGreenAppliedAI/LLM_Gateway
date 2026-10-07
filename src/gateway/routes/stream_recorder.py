"""Outcome recording for streamed responses.

Every streaming route needs the same bookkeeping once a stream ends:
audit row, metrics, and token-budget charge. Doing it inline in each
generator let three things slip through:

- budgets were never charged for streamed usage
- a provider error chunk (finish_reason=ERROR) was audited as "success"
- a client disconnect cancelled the generator before anything was recorded

StreamRecorder observes chunks and records the outcome exactly once,
whichever way the stream ends. Routes call `finish()` on normal
completion or a handled error, and `finish_disconnected()` from their
`finally` block, which is a no-op when the stream already finished.
"""

from collections.abc import AsyncIterator

import anyio
from fastapi import Request

from gateway.dispatch import Dispatcher
from gateway.errors import GatewayError
from gateway.models.common import FinishReason
from gateway.models.internal import InternalRequest, StreamChunk
from gateway.observability import get_logger, get_metrics
from gateway.observability.logging import RequestContext
from gateway.policy import PolicyEnforcer
from gateway.routes.dependencies import ClientDisconnected, run_unless_disconnected
from gateway.storage import AuditLogger

logger = get_logger(__name__)


class StreamRecorder:
    """Accumulates a stream's content/usage and records its outcome once."""

    def __init__(
        self,
        *,
        ctx: RequestContext,
        internal_request: InternalRequest,
        model: str,
        task: str,
        audit_logger: AuditLogger | None,
        enforcer: PolicyEnforcer | None,
        request_body: dict | None = None,
    ):
        self._ctx = ctx
        self._request = internal_request
        self._model = model
        self._task = task
        self._audit_logger = audit_logger
        self._enforcer = enforcer
        self._request_body = request_body

        self.provider: str | None = None
        self._parts: list[str] = []
        self._content_chunks = 0
        self._usage = None
        self._first_token_seen = False
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def observe(self, chunk: StreamChunk) -> None:
        """Record a chunk as it is forwarded to the client."""
        if not self._first_token_seen:
            self._ctx.record_first_token()
            self._first_token_seen = True
        if chunk.delta:
            self._parts.append(chunk.delta)
        # Reasoning and tool-call output cost tokens too: a reasoning-only
        # stream cut short used to estimate zero (D-043)
        if chunk.delta or chunk.thinking or chunk.tool_calls:
            self._content_chunks += 1
        if chunk.usage:
            self._usage = chunk.usage

    @staticmethod
    def is_error(chunk: StreamChunk) -> bool:
        return chunk.finish_reason == FinishReason.ERROR

    def _token_counts(self) -> tuple[int, int, bool]:
        """(prompt, completion, estimated). Without upstream usage (stream
        cut short) completion is estimated as one token per content chunk,
        so abandoned streams still count against the budget."""
        if self._usage is not None:
            return (
                self._usage.prompt_tokens or 0,
                self._usage.completion_tokens or 0,
                False,
            )
        return 0, self._content_chunks, True

    async def finish(
        self,
        status: str = "success",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Record the stream's outcome (idempotent)."""
        if self._finished:
            return
        self._finished = True

        prompt_tokens, completion_tokens, estimated = self._token_counts()
        if status == "success":
            self._ctx.record_complete(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
        else:
            self._ctx.record_error(error_code or "stream_error", error_message or "")

        # Budget: charge whatever was generated, success or not — a failed or
        # abandoned stream still consumed the GPU
        total_tokens = prompt_tokens + completion_tokens
        if self._enforcer is not None and total_tokens > 0:
            try:
                # Budget tiers key on the model name without an endpoint pin
                pin = f"{self.provider}/" if self.provider else None
                bare_model = (
                    self._model[len(pin) :] if pin and self._model.startswith(pin) else self._model
                )
                self._enforcer.record_token_usage(self._request.client_id, bare_model, total_tokens)
            except Exception:
                logger.exception("Failed to record streamed token usage")

        try:
            get_metrics().record_request(
                provider=self.provider or "unknown",
                model=self._model,
                task=self._task,
                status=status,
                latency_ms=self._ctx.total_latency_ms or 0,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                time_to_first_token_ms=self._ctx.time_to_first_token_ms,
                tokens_per_second=self._ctx.tokens_per_second if status == "success" else None,
            )
        except Exception:
            logger.exception("Failed to record stream metrics")

        if self._audit_logger is None:
            return
        response_body = {"content": "".join(self._parts)}
        if estimated:
            response_body["completion_tokens_estimated"] = True
        await self._audit_logger.log_request(
            request_id=self._ctx.request_id,
            client_id=self._request.client_id,
            task=self._task,
            model=self._model,
            endpoint=self.provider or "unknown",
            status=status,
            user_id=self._request.user_id,
            environment=self._request.environment,
            stream=True,
            max_tokens=self._request.max_tokens,
            temperature=self._request.temperature,
            latency_ms=self._ctx.total_latency_ms,
            time_to_first_token_ms=self._ctx.time_to_first_token_ms,
            tokens_per_second=self._ctx.tokens_per_second if status == "success" else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=error_code,
            error_message=error_message,
            request_body=self._request_body,
            response_body=response_body,
        )

    async def start(self, request: Request, dispatcher: Dispatcher) -> AsyncIterator[StreamChunk]:
        """Pick an endpoint and wait for its first chunk, before any response is sent.

        Failing here, before the status line goes out, lets the route return
        a real HTTP error (4xx/503) instead of a 200 carrying an error
        event, which load balancers and SDK retry logic can't see. Waiting
        for the first chunk can take a while (cold model load), so a client
        that leaves meanwhile cancels the upstream request.

        Raises:
            GatewayError: No endpoint could start the stream (recorded first).
            ClientDisconnected: The client left while waiting (recorded first).
        """
        try:
            self.provider, stream = await run_unless_disconnected(
                request, dispatcher.dispatch_stream(self._request)
            )
        except ClientDisconnected:
            await self.finish(
                status="error",
                error_code="client_disconnected",
                error_message="Client disconnected before the stream started",
            )
            raise
        except GatewayError as e:
            await self.finish(status="error", error_code=e.code.value, error_message=str(e))
            raise
        return stream

    async def finish_error_chunk(self, chunk: StreamChunk) -> None:
        """Record a provider error that arrived mid-stream, with its cause."""
        await self.finish(
            status="error",
            error_code=chunk.error_code or "stream_error",
            error_message=chunk.error or "Provider stream failed",
        )

    async def finish_disconnected(self, stream: AsyncIterator[StreamChunk] | None) -> None:
        """Close the upstream stream and record a disconnect if not finished.

        Call from the generator's `finally`. After a client disconnect the
        surrounding task may already be cancelled, so the work is shielded;
        otherwise the first await would re-raise and nothing would be saved.
        """
        with anyio.CancelScope(shield=True):
            if stream is not None:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        logger.debug("Error closing upstream stream", exc_info=True)
            if not self._finished:
                try:
                    await self.finish(
                        status="error",
                        error_code="client_disconnected",
                        error_message="Client disconnected before the stream completed",
                    )
                except Exception:
                    logger.exception("Failed to record disconnected stream")

"""vLLM provider adapter.

vLLM API reference: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html

Per PRD Section 6: vLLM is a required runtime with full support.
vLLM exposes an OpenAI-compatible API, so we use those endpoints.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gateway.config import ProviderConfig
from gateway.models.common import (
    FinishReason,
    HealthStatus,
    ModelCapability,
    ModelInfo,
    ProviderType,
    UsageStats,
)
from gateway.models.internal import (
    InternalRequest,
    InternalResponse,
    Message,
    MessageRole,
    StreamChunk,
    ToolCall,
)
from gateway.providers.base import ProviderAdapter


class VLLMAdapter(ProviderAdapter):
    """Adapter for vLLM inference runtime.

    vLLM provides high-throughput LLM serving with OpenAI-compatible API.
    Uses /v1/chat/completions, /v1/completions, and /v1/models endpoints.
    """

    def __init__(self, config: ProviderConfig):
        """Initialize vLLM adapter from validated config.

        Args:
            config: Validated provider configuration with base_url, timeout, etc.
        """
        super().__init__(config=config, provider_type=ProviderType.VLLM)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client (thread-safe)."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
                )
            return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # =========================================================================
    # Required Methods
    # =========================================================================

    async def health(self) -> HealthStatus:
        """Check vLLM server health via /health or /v1/models endpoint."""
        try:
            client = await self._get_client()
            # Try /health first (vLLM >= 0.4.0)
            response = await client.get("/health")
            if response.status_code == 200:
                return HealthStatus.HEALTHY
            # Fallback to /v1/models
            response = await client.get("/v1/models")
            if response.status_code == 200:
                return HealthStatus.HEALTHY
            return HealthStatus.DEGRADED
        except httpx.TimeoutException:
            return HealthStatus.UNHEALTHY
        except httpx.ConnectError:
            return HealthStatus.UNHEALTHY
        except Exception:
            return HealthStatus.UNKNOWN

    async def list_models(self) -> list[ModelInfo]:
        """List models available in vLLM via /v1/models."""
        try:
            client = await self._get_client()
            response = await client.get("/v1/models")
            response.raise_for_status()
            data = response.json()

            models = []
            for model_data in data.get("data", []):
                model_id = model_data.get("id", "")
                models.append(
                    ModelInfo(
                        name=model_id,
                        provider=self.name,
                        capabilities=[
                            ModelCapability.CHAT,
                            ModelCapability.COMPLETION,
                            ModelCapability.STREAMING,
                        ],
                        max_context_length=model_data.get("max_model_len", 4096),
                        supports_streaming=True,
                    )
                )
            return models
        except Exception:
            return []

    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute chat completion via vLLM /v1/chat/completions endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build OpenAI-compatible chat request
            vllm_request = self._build_chat_request(request)

            response = await client.post("/v1/chat/completions", json=vllm_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_chat_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            return self._error_response(
                request,
                f"HTTP {e.response.status_code}: {e.response.text}",
                f"http_{e.response.status_code}",
            )
        except httpx.ConnectError as e:
            return self._error_response(request, f"Connection failed: {e}", "connection_error")
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    # =========================================================================
    # Optional Methods
    # =========================================================================

    async def generate(self, request: InternalRequest) -> InternalResponse:
        """Execute text generation via vLLM /v1/completions endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build OpenAI-compatible completion request
            vllm_request: dict[str, Any] = {
                "model": request.model,
                "prompt": request.prompt or request.get_input_text(),
                "stream": False,
            }
            for key, value in (
                ("max_tokens", request.max_tokens),
                ("temperature", request.temperature),
                ("top_p", request.top_p),
            ):
                if value is not None:
                    vllm_request[key] = value

            response = await client.post("/v1/completions", json=vllm_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_completion_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            return self._error_response(
                request,
                f"HTTP {e.response.status_code}: {e.response.text}",
                f"http_{e.response.status_code}",
            )
        except httpx.ConnectError as e:
            return self._error_response(request, f"Connection failed: {e}", "connection_error")
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    # =========================================================================
    # Streaming
    # =========================================================================

    # Per-chunk timeout: if no chunk arrives within this window, the stream is dead
    STREAM_CHUNK_TIMEOUT = 120.0  # seconds between chunks

    async def _iter_lines_with_timeout(self, response: httpx.Response) -> AsyncIterator[str]:
        """Iterate response lines with a per-chunk timeout."""
        aiter = response.aiter_lines().__aiter__()
        while True:
            try:
                line = await asyncio.wait_for(
                    aiter.__anext__(),
                    timeout=self.STREAM_CHUNK_TIMEOUT,
                )
                yield line
            except StopAsyncIteration:
                break

    async def chat_stream(self, request: InternalRequest) -> AsyncIterator[StreamChunk]:
        """Stream chat completion via vLLM /v1/chat/completions with stream=true."""
        try:
            client = await self._get_client()
            vllm_request = self._build_chat_request(request)
            vllm_request["stream"] = True
            # Without this, OpenAI-compatible servers never send token usage
            # in the stream and every streamed request audits as 0 tokens
            vllm_request["stream_options"] = {"include_usage": True}

            async with client.stream("POST", "/v1/chat/completions", json=vllm_request) as response:
                response.raise_for_status()
                index = 0
                # The finish chunk is held back: usage arrives AFTER it, in a
                # frame with empty choices. Attach usage, then emit as final.
                pending_final: StreamChunk | None = None

                async for line in self._iter_lines_with_timeout(response):
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line == "[DONE]":
                        break

                    import json

                    chunk_data = json.loads(line)

                    usage = None
                    if chunk_data.get("usage"):
                        usage_data = chunk_data["usage"]
                        usage = UsageStats(
                            prompt_tokens=usage_data.get("prompt_tokens", 0),
                            completion_tokens=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                        )

                    choices = chunk_data.get("choices", [])
                    if not choices:
                        # Usage-only frame (stream_options.include_usage)
                        if usage and pending_final:
                            pending_final = pending_final.model_copy(update={"usage": usage})
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    finish = choice.get("finish_reason")

                    finish_reason = None
                    if finish == "stop":
                        finish_reason = FinishReason.STOP
                    elif finish == "length":
                        finish_reason = FinishReason.LENGTH

                    chunk = StreamChunk(
                        request_id=request.request_id,
                        index=index,
                        delta=content,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                    index += 1

                    if finish_reason is not None:
                        pending_final = chunk
                        continue
                    yield chunk

                if pending_final is not None:
                    yield pending_final

        except Exception:
            yield StreamChunk(
                request_id=request.request_id,
                index=0,
                delta="",
                finish_reason=FinishReason.ERROR,
            )

    # =========================================================================
    # Provider Metadata
    # =========================================================================

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def limitations(self) -> list[str]:
        return [
            "Requires GPU for efficient inference",
            "Model must be loaded at server startup",
        ]

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _build_chat_request(self, request: InternalRequest) -> dict[str, Any]:
        """Build OpenAI-compatible chat request body."""
        import json

        messages = []
        if request.messages:
            for msg in request.messages:
                m: dict[str, Any] = {
                    "role": msg.role.value,
                    "content": msg.content_parts if msg.content_parts else msg.content,
                }
                if msg.tool_calls:
                    m["tool_calls"] = [
                        {
                            "id": tc.id or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.function.get("name", ""),
                                "arguments": json.dumps(tc.function.get("arguments", {}))
                                if isinstance(tc.function.get("arguments"), dict)
                                else str(tc.function.get("arguments", "{}")),
                            },
                        }
                        for i, tc in enumerate(msg.tool_calls)
                    ]
                if msg.tool_call_id:
                    m["tool_call_id"] = msg.tool_call_id
                messages.append(m)

        req: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": False,
        }

        # Only send sampling params the client set — None means "engine default"
        for key, value in (
            ("max_tokens", request.max_tokens),
            ("temperature", request.temperature),
            ("top_p", request.top_p),
        ):
            if value is not None:
                req[key] = value

        if request.tools:
            req["tools"] = request.tools
        if request.tool_choice is not None:
            req["tool_choice"] = request.tool_choice
        if request.response_format:
            req["response_format"] = request.response_format

        return req

    def _parse_chat_response(
        self, request: InternalRequest, data: dict[str, Any], latency_ms: float
    ) -> InternalResponse:
        """Parse OpenAI-compatible chat response."""
        import json

        choices = data.get("choices", [])
        content = ""
        finish_reason = FinishReason.STOP
        tool_calls = None

        if choices:
            choice = choices[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            finish = choice.get("finish_reason", "stop")
            if finish == "length":
                finish_reason = FinishReason.LENGTH
            elif finish == "tool_calls":
                finish_reason = FinishReason.TOOL_CALLS

            # Parse tool calls
            raw_tool_calls = message.get("tool_calls")
            if raw_tool_calls:
                tool_calls = []
                for tc in raw_tool_calls:
                    func = tc.get("function", {})
                    arguments = func.get("arguments", "{}")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}
                    tool_calls.append(
                        ToolCall(
                            id=tc.get("id"),
                            type="function",
                            function={
                                "name": func.get("name", ""),
                                "arguments": arguments,
                            },
                        )
                    )

        usage_data = data.get("usage", {})

        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content=content or None,
            tool_calls=tool_calls,
        )

        return InternalResponse(
            request_id=request.request_id,
            task=request.task,
            provider=self.name,
            model=data.get("model", request.model or "unknown"),
            content=content,
            messages=[assistant_msg],
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=UsageStats(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            latency_ms=latency_ms,
        )

    def _parse_completion_response(
        self, request: InternalRequest, data: dict[str, Any], latency_ms: float
    ) -> InternalResponse:
        """Parse OpenAI-compatible completion response."""
        choices = data.get("choices", [])
        content = ""
        finish_reason = FinishReason.STOP

        if choices:
            choice = choices[0]
            content = choice.get("text", "")
            finish = choice.get("finish_reason", "stop")
            if finish == "length":
                finish_reason = FinishReason.LENGTH

        usage_data = data.get("usage", {})

        return InternalResponse(
            request_id=request.request_id,
            task=request.task,
            provider=self.name,
            model=data.get("model", request.model or "unknown"),
            content=content,
            finish_reason=finish_reason,
            usage=UsageStats(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            latency_ms=latency_ms,
        )

    # _error_response inherited from ProviderAdapter base class (DRY)

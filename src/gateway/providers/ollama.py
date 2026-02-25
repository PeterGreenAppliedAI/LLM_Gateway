"""Ollama provider adapter.

Ollama API reference: https://github.com/ollama/ollama/blob/main/docs/api.md

Per PRD Section 6: Ollama is a required local runtime with full support.
"""

import asyncio
import time
from typing import Any, AsyncIterator

import httpx

from gateway.config import ProviderConfig
from gateway.models.common import (
    FinishReason,
    HealthStatus,
    ModelCapability,
    ModelInfo,
    ProviderType,
    TaskType,
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


class OllamaAdapter(ProviderAdapter):
    """Adapter for Ollama inference runtime.

    Ollama provides a local LLM server with OpenAI-compatible endpoints
    and native Ollama API endpoints.
    """

    def __init__(self, config: ProviderConfig):
        """Initialize Ollama adapter from validated config.

        Args:
            config: Validated provider configuration with base_url, timeout, etc.
        """
        super().__init__(config=config, provider_type=ProviderType.OLLAMA)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
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
        """Check Ollama server health via /api/tags endpoint."""
        try:
            client = await self._get_client()
            response = await client.get("/api/tags")
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
        """List models available in Ollama via /api/tags."""
        try:
            client = await self._get_client()
            response = await client.get("/api/tags")
            response.raise_for_status()
            data = response.json()

            models = []
            for model_data in data.get("models", []):
                model_name = model_data.get("name", "")
                # Determine capabilities based on model name/type
                capabilities = self._infer_capabilities(model_name, model_data)

                models.append(
                    ModelInfo(
                        name=model_name,
                        provider=self.name,
                        capabilities=capabilities,
                        max_context_length=self._infer_context_length(model_data),
                        supports_streaming=True,
                        size_bytes=model_data.get("size"),
                        quantization=self._extract_quantization(model_data),
                    )
                )
            return models
        except Exception:
            return []

    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute chat completion via Ollama /api/chat endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build Ollama chat request
            ollama_request = self._build_chat_request(request)

            response = await client.post("/api/chat", json=ollama_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_chat_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            return self._error_response(
                request, f"HTTP {e.response.status_code}: {e.response.text[:200]}", "http_error"
            )
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    # =========================================================================
    # Optional Methods
    # =========================================================================

    async def generate(self, request: InternalRequest) -> InternalResponse:
        """Execute text generation via Ollama /api/generate endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build Ollama generate request
            ollama_request = self._build_generate_request(request)

            response = await client.post("/api/generate", json=ollama_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_generate_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            return self._error_response(
                request, f"HTTP {e.response.status_code}: {e.response.text[:200]}", "http_error"
            )
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    async def embeddings(self, request: InternalRequest) -> InternalResponse:
        """Generate embeddings via Ollama /api/embed endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Ollama expects 'input' as string or list
            input_texts = request.input_data or [request.get_input_text()]

            ollama_request = {
                "model": request.model or "nomic-embed-text",
                "input": input_texts,
            }

            response = await client.post("/api/embed", json=ollama_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return InternalResponse(
                request_id=request.request_id,
                task=TaskType.EMBEDDINGS,
                provider=self.name,
                model=request.model or "nomic-embed-text",
                embeddings=data.get("embeddings", []),
                finish_reason=FinishReason.STOP,
                usage=UsageStats(
                    prompt_tokens=data.get("prompt_eval_count", 0),
                ),
                latency_ms=latency_ms,
            )

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            return self._error_response(
                request, f"HTTP {e.response.status_code}: {e.response.text[:200]}", "http_error"
            )
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    # =========================================================================
    # Streaming
    # =========================================================================

    # Per-chunk timeout: if no chunk arrives within this window, the stream is dead
    STREAM_CHUNK_TIMEOUT = 120.0  # seconds between chunks

    async def chat_stream(
        self, request: InternalRequest
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion via Ollama /api/chat with stream=true."""
        try:
            client = await self._get_client()
            ollama_request = self._build_chat_request(request)
            ollama_request["stream"] = True

            async with client.stream("POST", "/api/chat", json=ollama_request) as response:
                response.raise_for_status()
                index = 0
                async for line in self._iter_lines_with_timeout(response):
                    if not line:
                        continue
                    import json
                    chunk_data = json.loads(line)

                    message = chunk_data.get("message", {})
                    content = message.get("content", "")
                    # Reasoning models (e.g., Nemotron) stream thinking tokens
                    # in a separate field before the content phase
                    thinking = message.get("thinking", "")
                    done = chunk_data.get("done", False)

                    finish_reason = None
                    usage = None
                    if done:
                        finish_reason = FinishReason.STOP
                        usage = UsageStats(
                            prompt_tokens=chunk_data.get("prompt_eval_count", 0),
                            completion_tokens=chunk_data.get("eval_count", 0),
                            total_tokens=(
                                chunk_data.get("prompt_eval_count", 0)
                                + chunk_data.get("eval_count", 0)
                            ),
                        )

                    yield StreamChunk(
                        request_id=request.request_id,
                        index=index,
                        delta=content,
                        thinking=thinking or None,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                    index += 1

        except asyncio.TimeoutError:
            yield StreamChunk(
                request_id=request.request_id,
                index=0,
                delta="",
                finish_reason=FinishReason.ERROR,
            )
        except Exception as e:
            yield StreamChunk(
                request_id=request.request_id,
                index=0,
                delta="",
                finish_reason=FinishReason.ERROR,
            )

    async def _iter_lines_with_timeout(self, response: httpx.Response) -> AsyncIterator[str]:
        """Iterate response lines with a per-chunk timeout.

        If no data arrives within STREAM_CHUNK_TIMEOUT seconds,
        raises asyncio.TimeoutError to prevent hanging connections.
        """
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

    # =========================================================================
    # Provider Metadata
    # =========================================================================

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def limitations(self) -> list[str]:
        return [
            "Local inference only",
            "Model must be pulled before use",
            "Tool calling support is model-dependent",
        ]

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _build_chat_request(self, request: InternalRequest) -> dict[str, Any]:
        """Build Ollama /api/chat request body."""
        messages = []
        if request.messages:
            for msg in request.messages:
                m: dict[str, Any] = {
                    "role": msg.role.value,
                    "content": msg.content or "",
                }
                # Include images for vision models (native Ollama format)
                if msg.images:
                    m["images"] = msg.images
                # Extract images from OpenAI content_parts (cross-API support)
                elif msg.content_parts:
                    images = []
                    for part in msg.content_parts:
                        if part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            # Strip data URI prefix to get raw base64
                            if url.startswith("data:"):
                                # data:image/png;base64,<data>
                                _, _, b64 = url.partition(",")
                                if b64:
                                    images.append(b64)
                            else:
                                images.append(url)
                    if images:
                        m["images"] = images
                # Include tool_calls for assistant messages
                if msg.tool_calls:
                    m["tool_calls"] = [
                        {"function": tc.function}
                        for tc in msg.tool_calls
                    ]
                messages.append(m)

        result: dict[str, Any] = {
            "model": request.model or "llama3.2",
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": request.max_tokens,
                "num_ctx": 32768,
                "temperature": request.temperature,
                "top_p": request.top_p,
            },
        }

        if request.tools:
            result["tools"] = request.tools

        return result

    def _build_generate_request(self, request: InternalRequest) -> dict[str, Any]:
        """Build Ollama /api/generate request body."""
        return {
            "model": request.model or "llama3.2",
            "prompt": request.prompt or request.get_input_text(),
            "stream": False,
            "options": {
                "num_predict": request.max_tokens,
                "num_ctx": 32768,
                "temperature": request.temperature,
                "top_p": request.top_p,
            },
        }

    def _parse_chat_response(
        self, request: InternalRequest, data: dict[str, Any], latency_ms: float
    ) -> InternalResponse:
        """Parse Ollama /api/chat response."""
        message = data.get("message", {})
        content = message.get("content", "")

        # Parse tool calls from response
        tool_calls = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = [
                ToolCall(
                    type="function",
                    function=tc.get("function", {}),
                )
                for tc in raw_tool_calls
            ]

        # Determine finish reason
        finish_reason = FinishReason.STOP if data.get("done") else FinishReason.LENGTH
        if tool_calls:
            finish_reason = FinishReason.TOOL_CALLS

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
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=(
                    data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                ),
            ),
            latency_ms=latency_ms,
        )

    def _parse_generate_response(
        self, request: InternalRequest, data: dict[str, Any], latency_ms: float
    ) -> InternalResponse:
        """Parse Ollama /api/generate response."""
        return InternalResponse(
            request_id=request.request_id,
            task=request.task,
            provider=self.name,
            model=data.get("model", request.model or "unknown"),
            content=data.get("response", ""),
            finish_reason=FinishReason.STOP if data.get("done") else FinishReason.LENGTH,
            usage=UsageStats(
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=(
                    data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                ),
            ),
            latency_ms=latency_ms,
        )

    # _error_response inherited from ProviderAdapter base class (DRY)

    def _infer_capabilities(
        self, model_name: str, model_data: dict[str, Any]
    ) -> list[ModelCapability]:
        """Infer model capabilities from name and metadata."""
        capabilities = [ModelCapability.CHAT, ModelCapability.COMPLETION, ModelCapability.STREAMING]

        # Embedding models
        lower_name = model_name.lower()
        if "embed" in lower_name or "nomic" in lower_name:
            capabilities = [ModelCapability.EMBEDDINGS]

        # Vision models
        if "vision" in lower_name or "llava" in lower_name:
            capabilities.append(ModelCapability.VISION)

        return capabilities

    def _infer_context_length(self, model_data: dict[str, Any]) -> int:
        """Infer context length from model metadata."""
        # Ollama doesn't always provide this, use sensible default
        details = model_data.get("details", {})
        # Some models expose parameter_size which can hint at context
        return 4096  # Conservative default

    def _extract_quantization(self, model_data: dict[str, Any]) -> str | None:
        """Extract quantization info from model metadata."""
        details = model_data.get("details", {})
        return details.get("quantization_level")

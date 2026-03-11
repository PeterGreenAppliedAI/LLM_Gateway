"""OpenAI-compatible provider adapter.

Supports OpenAI and all OpenAI-compatible APIs:
- OpenAI (api.openai.com)
- Anthropic (via OpenAI-compatible endpoint)
- Groq (api.groq.com)
- Together AI (api.together.xyz)
- Fireworks (api.fireworks.ai)
- Any other OpenAI-compatible endpoint

Per NEXT_STEPS.md Phase 1: Cloud Provider Support
"""

import asyncio
import json
import os
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


class OpenAIAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible APIs.

    Works with any API that follows the OpenAI API specification:
    - POST /v1/chat/completions
    - POST /v1/completions
    - POST /v1/embeddings
    - GET /v1/models

    Supports API key authentication via:
    1. api_key field in ProviderConfig
    2. Environment variable (api_key_env in ProviderConfig)
    """

    def __init__(self, config: ProviderConfig):
        """Initialize OpenAI adapter from validated config.

        Args:
            config: Validated provider configuration with base_url, timeout, etc.
        """
        super().__init__(config=config, provider_type=ProviderType.OPENAI)
        self._client: httpx.AsyncClient | None = None

        # Resolve API key from config or environment
        self._api_key = self._resolve_api_key(config)

        # Custom headers (e.g., anthropic-version for Anthropic)
        self._custom_headers: dict[str, str] = getattr(config, "headers", {}) or {}

    def _resolve_api_key(self, config: ProviderConfig) -> str | None:
        """Resolve API key from config or environment variable."""
        # Direct api_key in config
        api_key = getattr(config, "api_key", None)
        if api_key:
            # Handle ${ENV_VAR} syntax
            if api_key.startswith("${") and api_key.endswith("}"):
                env_var = api_key[2:-1]
                return os.environ.get(env_var)
            return api_key

        # api_key_env specifies which env var to use
        api_key_env = getattr(config, "api_key_env", None)
        if api_key_env:
            return os.environ.get(api_key_env)

        # Default env vars by common provider names
        name_lower = config.name.lower()
        if "openrouter" in name_lower:
            return os.environ.get("OPENROUTER_API_KEY")
        elif "openai" in name_lower:
            return os.environ.get("OPENAI_API_KEY")
        elif "anthropic" in name_lower:
            return os.environ.get("ANTHROPIC_API_KEY")
        elif "groq" in name_lower:
            return os.environ.get("GROQ_API_KEY")
        elif "together" in name_lower:
            return os.environ.get("TOGETHER_API_KEY")
        elif "fireworks" in name_lower:
            return os.environ.get("FIREWORKS_API_KEY")

        return None

    _client_lock: asyncio.Lock | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with authentication headers (thread-safe)."""
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                headers = {"Content-Type": "application/json"}

                if self._api_key:
                    headers["Authorization"] = f"Bearer {self._api_key}"

                # Add custom headers (e.g., anthropic-version)
                headers.update(self._custom_headers)

                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    headers=headers,
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
        """Check provider health via /v1/models endpoint."""
        try:
            client = await self._get_client()
            response = await client.get("/v1/models")
            if response.status_code == 200:
                return HealthStatus.HEALTHY
            elif response.status_code == 401:
                # Unauthorized - API key issue but service is up
                return HealthStatus.DEGRADED
            return HealthStatus.UNHEALTHY
        except httpx.TimeoutException:
            return HealthStatus.UNHEALTHY
        except httpx.ConnectError:
            return HealthStatus.UNHEALTHY
        except Exception:
            return HealthStatus.UNKNOWN

    async def list_models(self) -> list[ModelInfo]:
        """List models available via /v1/models endpoint."""
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
                        capabilities=self._infer_capabilities(model_id),
                        max_context_length=self._infer_context_length(model_id),
                        supports_streaming=True,
                    )
                )
            return models
        except Exception:
            return []

    async def chat(self, request: InternalRequest) -> InternalResponse:
        """Execute chat completion via /v1/chat/completions endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build OpenAI chat request
            openai_request = self._build_chat_request(request)

            response = await client.post("/v1/chat/completions", json=openai_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_chat_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            error_detail = self._parse_error_response(e.response)
            return self._error_response(
                request,
                f"HTTP {e.response.status_code}: {error_detail}",
                "http_error"
            )
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    # =========================================================================
    # Optional Methods
    # =========================================================================

    async def generate(self, request: InternalRequest) -> InternalResponse:
        """Execute text completion via /v1/completions endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Build OpenAI completion request
            openai_request = self._build_completion_request(request)

            response = await client.post("/v1/completions", json=openai_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return self._parse_completion_response(request, data, latency_ms)

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            # If /v1/completions not supported, fall back to chat
            if e.response.status_code == 404:
                return await super().generate(request)
            error_detail = self._parse_error_response(e.response)
            return self._error_response(
                request,
                f"HTTP {e.response.status_code}: {error_detail}",
                "http_error"
            )
        except Exception as e:
            return self._error_response(request, str(e), "unknown_error")

    async def embeddings(self, request: InternalRequest) -> InternalResponse:
        """Generate embeddings via /v1/embeddings endpoint."""
        start_time = time.perf_counter()

        try:
            client = await self._get_client()

            # Get input texts
            input_texts = request.input_data or [request.get_input_text()]

            openai_request = {
                "model": request.model or "text-embedding-ada-002",
                "input": input_texts[0] if len(input_texts) == 1 else input_texts,
            }

            response = await client.post("/v1/embeddings", json=openai_request)
            response.raise_for_status()
            data = response.json()

            latency_ms = (time.perf_counter() - start_time) * 1000

            # Extract embeddings from response
            embeddings = [item["embedding"] for item in data.get("data", [])]

            usage_data = data.get("usage", {})

            return InternalResponse(
                request_id=request.request_id,
                task=TaskType.EMBEDDINGS,
                provider=self.name,
                model=data.get("model", request.model or "unknown"),
                embeddings=embeddings,
                finish_reason=FinishReason.STOP,
                usage=UsageStats(
                    prompt_tokens=usage_data.get("prompt_tokens", 0),
                    total_tokens=usage_data.get("total_tokens", 0),
                ),
                latency_ms=latency_ms,
            )

        except httpx.TimeoutException as e:
            return self._error_response(request, f"Timeout: {e}", "timeout")
        except httpx.HTTPStatusError as e:
            error_detail = self._parse_error_response(e.response)
            return self._error_response(
                request,
                f"HTTP {e.response.status_code}: {error_detail}",
                "http_error"
            )
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

    async def chat_stream(
        self, request: InternalRequest
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion via /v1/chat/completions with stream=true."""
        try:
            client = await self._get_client()
            openai_request = self._build_chat_request(request)
            openai_request["stream"] = True

            async with client.stream(
                "POST", "/v1/chat/completions", json=openai_request
            ) as response:
                response.raise_for_status()
                index = 0

                async for line in self._iter_lines_with_timeout(response):
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]  # Remove "data: " prefix
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk_data.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    finish_reason_str = choice.get("finish_reason")

                    finish_reason = None
                    usage = None

                    if finish_reason_str:
                        finish_reason = self._map_finish_reason(finish_reason_str)
                        # Usage might be in the final chunk
                        usage_data = chunk_data.get("usage")
                        if usage_data:
                            usage = UsageStats(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
                            )

                    yield StreamChunk(
                        request_id=request.request_id,
                        index=index,
                        delta=content,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                    index += 1

        except Exception as e:
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
            "Requires API key",
            "Usage costs may apply",
            "Rate limits enforced by provider",
        ]

    def get_capabilities(self) -> list[str]:
        """Get list of capability strings for this adapter."""
        return ["chat", "completion", "embeddings", "streaming"]

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _build_chat_request(self, request: InternalRequest) -> dict[str, Any]:
        """Build OpenAI /v1/chat/completions request body."""
        messages = []
        if request.messages:
            for msg in request.messages:
                m: dict[str, Any] = {
                    "role": msg.role.value,
                    "content": msg.content_parts if msg.content_parts else msg.content,
                }
                # Include tool_calls for assistant messages (serialize arguments to JSON string)
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
                # Include tool_call_id for tool role messages
                if msg.tool_call_id:
                    m["tool_call_id"] = msg.tool_call_id
                messages.append(m)

        req: dict[str, Any] = {
            "model": request.model or "gpt-3.5-turbo",
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stream": False,
        }

        # Add tool definitions if provided
        if request.tools:
            req["tools"] = request.tools
        if request.tool_choice is not None:
            req["tool_choice"] = request.tool_choice

        # Add stop sequences if provided
        if request.stop:
            req["stop"] = request.stop

        # Add response format if provided
        if request.response_format:
            req["response_format"] = request.response_format

        return req

    def _build_completion_request(self, request: InternalRequest) -> dict[str, Any]:
        """Build OpenAI /v1/completions request body."""
        return {
            "model": request.model or "gpt-3.5-turbo-instruct",
            "prompt": request.prompt or request.get_input_text(),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stream": False,
            "stop": request.stop,
        }

    def _parse_chat_response(
        self, request: InternalRequest, data: dict[str, Any], latency_ms: float
    ) -> InternalResponse:
        """Parse OpenAI /v1/chat/completions response."""
        choices = data.get("choices", [])
        if not choices:
            return self._error_response(request, "No choices in response", "empty_response")

        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content", "")
        finish_reason_str = choice.get("finish_reason", "stop")

        # Parse tool calls from response
        tool_calls = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                # OpenAI returns arguments as JSON string; parse to dict
                arguments = func.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                tool_calls.append(ToolCall(
                    id=tc.get("id"),
                    type="function",
                    function={
                        "name": func.get("name", ""),
                        "arguments": arguments,
                    },
                ))

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
            finish_reason=self._map_finish_reason(finish_reason_str),
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
        """Parse OpenAI /v1/completions response."""
        choices = data.get("choices", [])
        if not choices:
            return self._error_response(request, "No choices in response", "empty_response")

        choice = choices[0]
        content = choice.get("text", "")
        finish_reason_str = choice.get("finish_reason", "stop")

        usage_data = data.get("usage", {})

        return InternalResponse(
            request_id=request.request_id,
            task=request.task,
            provider=self.name,
            model=data.get("model", request.model or "unknown"),
            content=content,
            finish_reason=self._map_finish_reason(finish_reason_str),
            usage=UsageStats(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            ),
            latency_ms=latency_ms,
        )

    def _parse_error_response(self, response: httpx.Response) -> str:
        """Parse error message from API response."""
        try:
            data = response.json()
            error = data.get("error", {})
            if isinstance(error, dict):
                return error.get("message", str(data))
            return str(error)
        except Exception:
            return response.text[:500]

    def _map_finish_reason(self, reason: str | None) -> FinishReason:
        """Map OpenAI finish_reason to internal FinishReason."""
        if not reason:
            return FinishReason.STOP

        mapping = {
            "stop": FinishReason.STOP,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
            "tool_calls": FinishReason.TOOL_CALLS,
            "function_call": FinishReason.TOOL_CALLS,
        }
        return mapping.get(reason.lower(), FinishReason.STOP)

    def _infer_capabilities(self, model_id: str) -> list[ModelCapability]:
        """Infer model capabilities from model ID."""
        model_lower = model_id.lower()
        capabilities = [ModelCapability.CHAT, ModelCapability.STREAMING]

        # Embedding models
        if "embed" in model_lower or "ada-002" in model_lower:
            return [ModelCapability.EMBEDDINGS]

        # Completion-only models
        if "instruct" in model_lower or "davinci" in model_lower:
            capabilities.append(ModelCapability.COMPLETION)

        # Vision models
        if "vision" in model_lower or "gpt-4o" in model_lower or "gpt-4-turbo" in model_lower:
            capabilities.append(ModelCapability.VISION)

        # Function calling
        if "gpt-4" in model_lower or "gpt-3.5-turbo" in model_lower:
            capabilities.append(ModelCapability.FUNCTION_CALLING)

        # JSON mode
        if "gpt-4" in model_lower or "gpt-3.5-turbo" in model_lower:
            capabilities.append(ModelCapability.JSON_MODE)

        return capabilities

    def _infer_context_length(self, model_id: str) -> int:
        """Infer context length from model ID."""
        model_lower = model_id.lower()

        # Known context lengths
        if "gpt-4-turbo" in model_lower or "gpt-4o" in model_lower:
            return 128000
        elif "gpt-4-32k" in model_lower:
            return 32768
        elif "gpt-4" in model_lower:
            return 8192
        elif "gpt-3.5-turbo-16k" in model_lower:
            return 16384
        elif "gpt-3.5" in model_lower:
            return 4096
        elif "claude-3" in model_lower:
            return 200000
        elif "claude-2" in model_lower:
            return 100000
        elif "llama-3" in model_lower and "70b" in model_lower:
            return 8192
        elif "mixtral" in model_lower:
            return 32768

        # Default
        return 4096

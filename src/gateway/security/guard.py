"""Llama Guard 3 shadow security analyzer.

Calls llama-guard3:1b via Ollama API to classify messages as safe/unsafe.
Runs in shadow mode alongside regex — results are logged but do not generate alerts.
"""

import time
from dataclasses import dataclass
from typing import Optional

import httpx

from gateway.observability import get_logger

logger = get_logger(__name__)

# Llama Guard 3 safety category mapping
CATEGORY_MAP: dict[str, str] = {
    "S1": "Violent Crimes",
    "S2": "Non-Violent Crimes",
    "S3": "Sex-Related Crimes",
    "S4": "Child Sexual Exploitation",
    "S5": "Defamation",
    "S6": "Specialized Advice",
    "S7": "Privacy",
    "S8": "Intellectual Property",
    "S9": "Indiscriminate Weapons",
    "S10": "Hate",
    "S11": "Suicide & Self-Harm",
    "S12": "Sexual Content",
    "S13": "Elections",
}


@dataclass
class GuardResult:
    """Result from Llama Guard classification."""

    safe: bool
    raw_response: str = ""
    category_code: Optional[str] = None
    category_name: Optional[str] = None
    inference_time_ms: float = 0.0
    error: Optional[str] = None
    skipped: bool = False

    def to_dict(self) -> dict:
        d: dict = {
            "safe": self.safe,
            "raw_response": self.raw_response,
            "inference_time_ms": self.inference_time_ms,
            "skipped": self.skipped,
        }
        if self.category_code:
            d["category_code"] = self.category_code
            d["category_name"] = self.category_name
        if self.error:
            d["error"] = self.error
        return d


class LlamaGuardClient:
    """Client for Llama Guard 3 safety classification via Ollama."""

    def __init__(
        self,
        base_url: str = "http://10.0.0.15:11434",
        model_name: str = "llama-guard3:1b",
        timeout: float = 10.0,
    ):
        self.base_url = base_url
        self.model_name = model_name
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def classify(self, messages: list[dict]) -> GuardResult:
        """Classify messages as safe or unsafe using Llama Guard 3.

        Combines message content and sends to the guard model for classification.
        Full error isolation — never raises, always returns a GuardResult.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            GuardResult with classification outcome.
        """
        start = time.perf_counter()

        try:
            # Build the conversation for guard model classification
            guard_messages = []
            for msg in messages:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue
                role = msg.get("role", "user")
                # Map system messages to user role for guard model
                if role == "system":
                    role = "user"
                elif role == "assistant":
                    role = "assistant"
                else:
                    role = "user"
                guard_messages.append({"role": role, "content": content})

            if not guard_messages:
                return GuardResult(
                    safe=True,
                    raw_response="",
                    skipped=True,
                    error="no_content",
                    inference_time_ms=_elapsed_ms(start),
                )

            client = await self._get_client()
            response = await client.post(
                "/api/chat",
                json={
                    "model": self.model_name,
                    "messages": guard_messages,
                    "stream": False,
                },
            )
            response.raise_for_status()

            data = response.json()
            raw = data.get("message", {}).get("content", "").strip()

            return self._parse_response(raw, start)

        except httpx.TimeoutException:
            elapsed = _elapsed_ms(start)
            logger.warning("Guard model timeout", timeout_ms=elapsed)
            return GuardResult(
                safe=True, skipped=True, error="timeout",
                inference_time_ms=elapsed,
            )
        except httpx.ConnectError as e:
            elapsed = _elapsed_ms(start)
            logger.warning("Guard model connection error", error=str(e))
            return GuardResult(
                safe=True, skipped=True, error="connection_error",
                inference_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            logger.warning("Guard model error", error=str(e))
            return GuardResult(
                safe=True, skipped=True, error=str(e),
                inference_time_ms=elapsed,
            )

    def _parse_response(self, raw: str, start: float) -> GuardResult:
        """Parse guard model response into GuardResult.

        Llama Guard 3 returns either:
        - "safe" — content is safe
        - "unsafe\\nS1" — content violates category S1
        """
        elapsed = _elapsed_ms(start)
        lower = raw.lower().strip()

        if lower == "safe":
            return GuardResult(safe=True, raw_response=raw, inference_time_ms=elapsed)

        if lower.startswith("unsafe"):
            lines = raw.strip().split("\n")
            category_code = None
            category_name = None
            if len(lines) >= 2:
                code = lines[1].strip().upper()
                if code in CATEGORY_MAP:
                    category_code = code
                    category_name = CATEGORY_MAP[code]

            return GuardResult(
                safe=False,
                raw_response=raw,
                category_code=category_code,
                category_name=category_name,
                inference_time_ms=elapsed,
            )

        # Unexpected format — treat as skipped
        logger.warning("Guard model unexpected response format", raw_response=raw)
        return GuardResult(
            safe=True,
            raw_response=raw,
            skipped=True,
            error="unexpected_format",
            inference_time_ms=elapsed,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


def _elapsed_ms(start: float) -> float:
    """Calculate elapsed time in milliseconds."""
    return round((time.perf_counter() - start) * 1000, 2)

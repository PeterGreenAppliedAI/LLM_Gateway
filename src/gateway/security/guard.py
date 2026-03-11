"""Guard model shadow security analyzer.

Supports multiple guard model backends:
- Llama Guard 3 (1b/8b): Returns "safe" or "unsafe\nS1" with category codes
- Granite Guardian 3.2 (5b): Returns "Yes" (unsafe) or "No" (safe) per category

Runs in shadow mode alongside regex — results are logged but do not generate alerts.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from gateway.observability import get_logger

logger = get_logger(__name__)

# Llama Guard 3 safety category mapping
LLAMA_GUARD_CATEGORIES: dict[str, str] = {
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

# Granite Guardian risk categories
GRANITE_CATEGORIES: dict[str, str] = {
    "jailbreak": "Jailbreaking",
    "harm": "General Harm",
    "social_bias": "Social Bias",
    "violence": "Violence",
    "profanity": "Profanity",
    "sexual_content": "Sexual Content",
    "unethical_behavior": "Unethical Behavior",
}

# Keep CATEGORY_MAP as alias for backward compatibility
CATEGORY_MAP = LLAMA_GUARD_CATEGORIES


@dataclass
class CircuitBreaker:
    """Simple circuit breaker for guard model calls.

    States:
    - closed: normal operation, requests pass through
    - open: too many failures, requests are short-circuited (skipped)
    - half-open: after cooldown, allow one request to test recovery
    """

    failure_threshold: int = 5  # Consecutive failures to open circuit
    cooldown_seconds: float = 60.0  # Time before half-open

    _failure_count: int = field(default=0, init=False)
    _state: str = field(default="closed", init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    def allow_request(self) -> bool:
        """Check if request should be allowed through."""
        if self._state == "closed":
            return True
        if self._state == "open":
            # Check if cooldown has elapsed
            if time.monotonic() - self._last_failure_time >= self.cooldown_seconds:
                self._state = "half-open"
                logger.info("Guard circuit breaker half-open, testing recovery")
                return True
            return False
        # half-open: allow one test request
        return True

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state == "half-open":
            logger.info("Guard circuit breaker closed (recovered)")
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            if self._state != "open":
                logger.warning(
                    "Guard circuit breaker opened",
                    failures=self._failure_count,
                    cooldown_seconds=self.cooldown_seconds,
                )
            self._state = "open"

    @property
    def state(self) -> str:
        return self._state


@dataclass
class GuardResult:
    """Result from guard model classification."""

    safe: bool
    raw_response: str = ""
    category_code: Optional[str] = None
    category_name: Optional[str] = None
    confidence: Optional[str] = None
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
        if self.confidence:
            d["confidence"] = self.confidence
        if self.error:
            d["error"] = self.error
        return d


class LlamaGuardClient:
    """Client for Llama Guard 3 safety classification via Ollama."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model_name: str = "llama-guard3:1b",
        timeout: float = 10.0,
    ):
        self.base_url = base_url
        self.model_name = model_name
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self.circuit_breaker = CircuitBreaker()

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

        if not self.circuit_breaker.allow_request():
            return GuardResult(
                safe=True, skipped=True, error="circuit_breaker_open",
                inference_time_ms=_elapsed_ms(start),
            )

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

            result = self._parse_response(raw, start)
            self.circuit_breaker.record_success()
            return result

        except httpx.TimeoutException:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
            logger.warning("Guard model timeout", timeout_ms=elapsed)
            return GuardResult(
                safe=True, skipped=True, error="timeout",
                inference_time_ms=elapsed,
            )
        except httpx.ConnectError as e:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
            logger.warning("Guard model connection error", error=str(e))
            return GuardResult(
                safe=True, skipped=True, error="connection_error",
                inference_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
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
                if code in LLAMA_GUARD_CATEGORIES:
                    category_code = code
                    category_name = LLAMA_GUARD_CATEGORIES[code]

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


class GraniteGuardianClient:
    """Client for IBM Granite Guardian 3.2 safety classification via Ollama.

    Granite Guardian uses a different API contract than Llama Guard:
    - System message selects the risk category (e.g. "jailbreak", "harm")
    - Returns "Yes" (unsafe) or "No" (safe) instead of "safe"/"unsafe"
    - Each category requires a separate API call

    Primary check is "jailbreak" (prompt injection), with "harm" as secondary.
    """

    # Categories to check, in priority order. First flagged category wins.
    DEFAULT_CATEGORIES = ["jailbreak"]

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model_name: str = "ibm/granite3.2-guardian:5b",
        timeout: float = 10.0,
        categories: Optional[list[str]] = None,
    ):
        self.base_url = base_url
        self.model_name = model_name
        self.timeout = timeout
        self.categories = categories or self.DEFAULT_CATEGORIES
        self._client: Optional[httpx.AsyncClient] = None
        self.circuit_breaker = CircuitBreaker()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def classify(self, messages: list[dict]) -> GuardResult:
        """Classify messages by running each configured category check.

        Runs categories in order. Returns unsafe on first flag.
        Full error isolation — never raises, always returns a GuardResult.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.

        Returns:
            GuardResult with classification outcome.
        """
        start = time.perf_counter()

        if not self.circuit_breaker.allow_request():
            return GuardResult(
                safe=True, skipped=True, error="circuit_breaker_open",
                inference_time_ms=_elapsed_ms(start),
            )

        try:
            # Build user/assistant messages (strip system — we use our own)
            guard_messages = []
            for msg in messages:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue
                role = msg.get("role", "user")
                if role == "system":
                    role = "user"
                elif role not in ("user", "assistant"):
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
            all_raw: list[str] = []

            for category in self.categories:
                # Granite Guardian: system message = category name
                check_messages = [
                    {"role": "system", "content": category},
                    *guard_messages,
                ]

                response = await client.post(
                    "/api/chat",
                    json={
                        "model": self.model_name,
                        "messages": check_messages,
                        "stream": False,
                    },
                )
                response.raise_for_status()

                data = response.json()
                raw = data.get("message", {}).get("content", "").strip()
                all_raw.append(f"{category}={raw}")

                result = self._parse_category_response(raw, category, start)
                if not result.safe:
                    # First flagged category — return immediately
                    result.raw_response = "; ".join(all_raw)
                    return result

            # All categories passed
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_success()
            return GuardResult(
                safe=True,
                raw_response="; ".join(all_raw),
                inference_time_ms=elapsed,
            )

        except httpx.TimeoutException:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
            logger.warning("Granite Guardian timeout", timeout_ms=elapsed)
            return GuardResult(
                safe=True, skipped=True, error="timeout",
                inference_time_ms=elapsed,
            )
        except httpx.ConnectError as e:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
            logger.warning("Granite Guardian connection error", error=str(e))
            return GuardResult(
                safe=True, skipped=True, error="connection_error",
                inference_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = _elapsed_ms(start)
            self.circuit_breaker.record_failure()
            logger.warning("Granite Guardian error", error=str(e))
            return GuardResult(
                safe=True, skipped=True, error=str(e),
                inference_time_ms=elapsed,
            )

    def _parse_category_response(
        self, raw: str, category: str, start: float,
    ) -> GuardResult:
        """Parse Granite Guardian response for a single category.

        Granite Guardian returns:
        - "Yes" or "No" as the verdict
        - Optional "<confidence> High </confidence>" tag
        """
        elapsed = _elapsed_ms(start)

        # Extract confidence if present (e.g. "<confidence> High </confidence>")
        confidence = None
        conf_match = re.search(r"<confidence>\s*(\w+)\s*</confidence>", raw)
        if conf_match:
            confidence = conf_match.group(1).strip()

        # Extract verdict — first word before any tags
        verdict = raw.split("<")[0].strip().lower() if "<" in raw else raw.lower().strip()

        if verdict == "no":
            return GuardResult(
                safe=True, raw_response=raw,
                confidence=confidence, inference_time_ms=elapsed,
            )

        if verdict == "yes":
            category_name = GRANITE_CATEGORIES.get(category, category)
            return GuardResult(
                safe=False,
                raw_response=raw,
                category_code=category,
                category_name=category_name,
                confidence=confidence,
                inference_time_ms=elapsed,
            )

        # Unexpected format — treat as skipped
        logger.warning(
            "Granite Guardian unexpected response",
            raw_response=raw,
            category=category,
        )
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


def create_guard_client(
    base_url: str = "http://localhost:11434",
    model_name: str = "llama-guard3:1b",
    timeout: float = 10.0,
) -> LlamaGuardClient | GraniteGuardianClient:
    """Factory: create the right guard client based on model name.

    Auto-detects Granite Guardian vs Llama Guard from the model name.
    """
    if "granite" in model_name.lower() and "guardian" in model_name.lower():
        logger.info(
            "Using Granite Guardian client",
            model=model_name,
            base_url=base_url,
        )
        return GraniteGuardianClient(
            base_url=base_url,
            model_name=model_name,
            timeout=timeout,
        )

    logger.info(
        "Using Llama Guard client",
        model=model_name,
        base_url=base_url,
    )
    return LlamaGuardClient(
        base_url=base_url,
        model_name=model_name,
        timeout=timeout,
    )


def _elapsed_ms(start: float) -> float:
    """Calculate elapsed time in milliseconds."""
    return round((time.perf_counter() - start) * 1000, 2)

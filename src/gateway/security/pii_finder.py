"""PII extractor: an LLM finds exact values, the gateway verifies them (D-052).

The model (phi4-mini by default) is asked for {category, value} pairs under
a grammar-constrained schema. Every value must appear verbatim in the text;
anything that doesn't is discarded and counted, so a hallucinated "PII"
value can never alter a prompt or enter the dataset.

Calls go straight to the Ollama endpoint, never through the gateway's own
routes: those write audit bodies redacted only by the regex scrubber,
which misses exactly what this pipeline exists to catch.
"""

import json
import time
from dataclasses import dataclass, field

import httpx

from gateway.security.pii_taxonomy import FINDER_SCHEMA, LABELS, finder_system_prompt


@dataclass(frozen=True)
class PIISpan:
    category: str
    value: str
    start: int
    end: int


@dataclass
class FinderResult:
    spans: list[PIISpan] = field(default_factory=list)
    hallucinated: int = 0  # values the model returned that aren't in the text
    error: str | None = None
    latency_ms: float = 0.0

    @property
    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.category] = counts.get(span.category, 0) + 1
        return counts


def locate_spans(text: str, findings: list[dict]) -> tuple[list[PIISpan], int]:
    """Turn model findings into verified spans.

    Every occurrence of a verified value becomes a span (a value repeated in
    the text must be scrubbed everywhere). Unknown categories, empty values
    and values absent from the text are dropped; absences are counted as
    hallucinations.
    """
    spans: list[PIISpan] = []
    seen: set[tuple[str, str]] = set()
    hallucinated = 0
    for item in findings:
        if not isinstance(item, dict):
            continue
        category, value = item.get("category"), item.get("value")
        if category not in LABELS or not isinstance(value, str) or not value.strip():
            continue
        if (category, value) in seen:
            continue
        seen.add((category, value))
        start = text.find(value)
        if start < 0:
            hallucinated += 1
            continue
        while start >= 0:
            spans.append(PIISpan(category, value, start, start + len(value)))
            start = text.find(value, start + len(value))
    spans.sort(key=lambda s: (s.start, s.end))
    return spans, hallucinated


class PIIFinder:
    """Extract PII values from text with an Ollama-served model."""

    def __init__(
        self,
        base_url: str,
        model: str = "phi4-mini",
        timeout: float = 60.0,
        max_output_tokens: int = 1024,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._client = client
        self._system = finder_system_prompt()

    async def find(self, text: str) -> FinderResult:
        started = time.perf_counter()
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": text},
            ],
            "format": FINDER_SCHEMA,
            "stream": False,
            "options": {"temperature": 0, "num_predict": self._max_output_tokens},
        }
        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout)
            try:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
            finally:
                if self._client is None:
                    await client.aclose()
            response.raise_for_status()
            content = response.json().get("message", {}).get("content", "")
            findings = json.loads(content).get("findings", [])
            if not isinstance(findings, list):
                raise ValueError("findings is not a list")
        except Exception as e:
            return FinderResult(
                error=f"{type(e).__name__}: {e}"[:300],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        spans, hallucinated = locate_spans(text, findings)
        return FinderResult(
            spans=spans,
            hallucinated=hallucinated,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

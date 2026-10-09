"""PII gate client: asks the Laya sidecar which categories a text contains (D-052).

Sidecar contract (tools/laya_pii_sidecar):

    POST /classify
    {"texts": ["..."], "questions": {"CREDENTIAL": "Does the text ...?", ...}}
    -> {"results": [{"CREDENTIAL": 0.02, "CONTACT": 0.91, ...}, ...]}

One probability per category per text, from one forward pass per text.
Long texts are split into overlapping chunks client-side and a category's
probability is its maximum over the chunks: PII anywhere in the text
counts.
"""

import time

import httpx

from gateway.security.pii_taxonomy import CATEGORIES

QUESTIONS: dict[str, str] = {c.label: c.question for c in CATEGORIES}


class PIIGateError(Exception):
    """The gate couldn't answer (down, timeout, malformed reply)."""


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split text into overlapping windows so a value on a boundary is seen whole."""
    if len(text) <= max_chars:
        return [text]
    step = max(1, max_chars - overlap)
    return [text[i : i + max_chars] for i in range(0, len(text) - overlap, step)]


class PIIGateClient:
    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        max_chars: int = 24_000,  # ~6k tokens: inside laya-multilingual's 8,192
        overlap: int = 200,
        client: httpx.AsyncClient | None = None,
    ):
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._max_chars = max_chars
        self._overlap = overlap
        self._client = client

    async def classify(self, text: str) -> tuple[dict[str, float], float]:
        """Probability per category for one text, and the call's latency in ms.

        Raises:
            PIIGateError: The sidecar didn't return a usable answer.
        """
        chunks = chunk_text(text, self._max_chars, self._overlap)
        started = time.perf_counter()
        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout)
            try:
                response = await client.post(
                    f"{self._url}/classify", json={"texts": chunks, "questions": QUESTIONS}
                )
            finally:
                if self._client is None:
                    await client.aclose()
            response.raise_for_status()
            results = response.json()["results"]
            if not isinstance(results, list) or len(results) != len(chunks):
                raise ValueError("result count doesn't match the texts sent")
        except Exception as e:
            raise PIIGateError(f"{type(e).__name__}: {e}"[:300]) from e

        merged: dict[str, float] = {label: 0.0 for label in QUESTIONS}
        for result in results:
            for label in QUESTIONS:
                value = result.get(label) if isinstance(result, dict) else None
                if isinstance(value, (int, float)):
                    merged[label] = max(merged[label], float(value))
        return merged, (time.perf_counter() - started) * 1000

"""ML PII detection in shadow mode (D-052).

For each request (after it has been answered, off the request path):

1. The gate (Laya sidecar) scores every taxonomy category.
2. The extractor (phi4-mini) runs when the gate says a category is present,
   when the gate is unavailable (fail toward checking), or on a random
   sample of texts the gate called clean (that sample measures the gate's
   miss rate).
3. Both are compared with the regex scanner, and the outcome is recorded.

Shadow mode changes nothing about the request: it only measures. Recorded
rows hold categories, probabilities, counts and timings, never the text or
the values. Enforcement is a later, separate decision.
"""

import asyncio
import random
from dataclasses import dataclass

from gateway.observability import get_logger
from gateway.security.pii import PIIScrubber
from gateway.security.pii_finder import PIIFinder
from gateway.security.pii_gate import PIIGateClient, PIIGateError

logger = get_logger(__name__)


@dataclass
class ShadowJob:
    request_id: str
    client_id: str
    task: str | None
    model: str | None
    text: str


def messages_to_text(messages: list[dict]) -> str:
    """Everything a user could have put PII into: content, parts, tool arguments."""
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
        for call in message.get("tool_calls") or []:
            args = call.get("function", {}).get("arguments") if isinstance(call, dict) else None
            if isinstance(args, str):
                parts.append(args)
            elif args is not None:
                parts.append(str(args))
    return "\n".join(p for p in parts if p)


class PIIShadowAnalyzer:
    def __init__(
        self,
        gate: PIIGateClient | None,
        finder: PIIFinder,
        store=None,
        threshold: float = 0.3,
        sample_rate: float = 0.05,
        queue_size: int = 500,
        rng: random.Random | None = None,
    ):
        self._gate = gate
        self._finder = finder
        self._store = store
        self._threshold = threshold
        self._sample_rate = sample_rate
        self._queue: asyncio.Queue[ShadowJob] = asyncio.Queue(maxsize=queue_size)
        self._regex = PIIScrubber()
        self._rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self.stats = {
            "queued": 0,
            "dropped": 0,
            "analyzed": 0,
            "gate_positive": 0,
            "gate_errors": 0,
            "finder_runs": 0,
            "finder_errors": 0,
            "gate_missed": 0,
            "hallucinated_values": 0,
        }

    def submit(
        self,
        request_id: str,
        client_id: str,
        messages: list[dict],
        task: str | None = None,
        model: str | None = None,
    ) -> bool:
        text = messages_to_text(messages)
        if not text.strip():
            return False
        try:
            self._queue.put_nowait(ShadowJob(request_id, client_id, task, model, text))
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            return False
        self.stats["queued"] += 1
        return True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self.analyze(job)
            except Exception:
                logger.exception("PII shadow analysis failed", request_id=job.request_id)

    async def analyze(self, job: ShadowJob) -> dict:
        """Run one job through gate -> extractor -> comparison; returns the recorded row."""
        row: dict = {
            "request_id": job.request_id,
            "client_id": job.client_id,
            "task": job.task,
            "model": job.model,
            "text_chars": len(job.text),
        }

        gate_positive = False
        gate_available = self._gate is not None
        if self._gate is not None:
            try:
                probs, gate_ms = await self._gate.classify(job.text)
                row["gate_probs"] = {k: round(v, 4) for k, v in probs.items()}
                row["gate_categories"] = sorted(k for k, v in probs.items() if v >= self._threshold)
                row["gate_ms"] = round(gate_ms, 1)
                gate_positive = bool(row["gate_categories"])
            except PIIGateError as e:
                gate_available = False
                row["gate_error"] = str(e)
                self.stats["gate_errors"] += 1
        if gate_positive:
            self.stats["gate_positive"] += 1

        if gate_positive:
            reason = "gate_positive"
        elif not gate_available:
            reason = "gate_unavailable"  # fail toward checking
        elif self._rng.random() < self._sample_rate:
            reason = "sampled"  # measures what the gate lets through
        else:
            reason = None

        if reason is not None:
            self.stats["finder_runs"] += 1
            result = await self._finder.find(job.text)
            row["finder_reason"] = reason
            row["finder_ms"] = round(result.latency_ms, 1)
            if result.error:
                row["finder_error"] = result.error
                self.stats["finder_errors"] += 1
            else:
                row["finder_categories"] = result.categories
                row["finder_hallucinated"] = result.hallucinated
                self.stats["hallucinated_values"] += result.hallucinated
                if reason == "sampled":
                    row["gate_missed"] = bool(result.spans)
                    if result.spans:
                        self.stats["gate_missed"] += 1

        regex = self._regex.scan(job.text)
        row["regex_types"] = sorted({d.pii_type for d in regex.detections})

        self.stats["analyzed"] += 1
        if self._store is not None:
            try:
                await self._store.record(row)
            except Exception:
                logger.exception("PII shadow result not stored", request_id=job.request_id)
        return row

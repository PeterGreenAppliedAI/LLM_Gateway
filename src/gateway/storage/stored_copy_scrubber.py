"""Replace detected PII values in a request's stored copies (D-052).

Runs off the request path, after the extractor has found values. The audit
row and the security-scan row are written by other background paths and
may land after detection (a long stream is audited when it ends), so a
scrub is retried on a schedule; replacement is idempotent.

Not covered: structured logs already emitted, and the audit fallback
journal (data/audit-journal), which is append-only by design.
"""

import asyncio
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.storage.schema import audit_log, security_scans

logger = get_logger(__name__)

# Seconds after detection to (re)apply: covers rows written later
RETRY_SCHEDULE: tuple[float, ...] = (0, 30, 120, 600)


def scrub_values(obj: Any, replacements: dict[str, str]) -> tuple[Any, int]:
    """Replace every occurrence of each value in all strings of a JSON-like
    structure. Longest values first, so a value containing another is
    replaced whole. Returns (new object, replacements made)."""
    ordered = sorted(replacements.items(), key=lambda kv: len(kv[0]), reverse=True)
    count = 0

    def walk(node: Any) -> Any:
        nonlocal count
        if isinstance(node, str):
            for value, placeholder in ordered:
                if value in node:
                    count += node.count(value)
                    node = node.replace(value, placeholder)
            return node
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node

    return walk(obj), count


class StoredCopyScrubber:
    def __init__(self, engine: AsyncEngine, schedule: tuple[float, ...] = RETRY_SCHEDULE):
        self._engine = engine
        self._schedule = schedule
        self._tasks: set[asyncio.Task] = set()

    async def scrub_now(self, request_id: str, replacements: dict[str, str]) -> int:
        """One pass over the request's stored rows; returns values replaced."""
        total = 0
        targets = (
            (audit_log, ("request_body", "response_body")),
            (security_scans, ("messages",)),
        )
        async with self._engine.begin() as conn:
            for table, columns in targets:
                row = (
                    await conn.execute(
                        select(*(table.c[col] for col in columns)).where(
                            table.c.request_id == request_id
                        )
                    )
                ).first()
                if row is None:
                    continue
                changes = {}
                for col, current in zip(columns, row, strict=True):
                    if current is None:
                        continue
                    scrubbed, n = scrub_values(current, replacements)
                    if n:
                        changes[col] = scrubbed
                        total += n
                if changes:
                    await conn.execute(
                        update(table).where(table.c.request_id == request_id).values(**changes)
                    )
        return total

    def schedule(self, request_id: str, replacements: dict[str, str]) -> None:
        """Apply now and again on the retry schedule, in the background."""
        task = asyncio.create_task(self._run(request_id, dict(replacements)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, request_id: str, replacements: dict[str, str]) -> None:
        waited = 0.0
        for at in self._schedule:
            await asyncio.sleep(max(0.0, at - waited))
            waited = at
            try:
                await self.scrub_now(request_id, replacements)
            except Exception:
                logger.exception("Stored-copy PII scrub failed", request_id=request_id)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

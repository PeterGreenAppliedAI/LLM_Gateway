"""Persistence for PII gate shadow results (D-052). No text, no values."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.storage.schema import pii_gate_shadow


class PIIShadowStore:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def record(self, row: dict[str, Any]) -> None:
        values = {"timestamp": datetime.now(UTC), **row}
        async with self._engine.begin() as conn:
            await conn.execute(pii_gate_shadow.insert().values(**values))

    async def summary(self, hours: int = 24) -> dict[str, Any]:
        """Headline shadow numbers for /health and the dashboard."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        c = pii_gate_shadow.c
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        func.count(),
                        func.count().filter(c.finder_reason.isnot(None)),
                        func.count().filter(c.finder_reason == "sampled"),
                        func.count().filter(c.gate_missed.is_(True)),
                        func.count().filter(c.gate_error.isnot(None)),
                        func.avg(c.gate_ms),
                        func.avg(c.finder_ms),
                    ).where(c.timestamp >= cutoff)
                )
            ).one()
        total, finder_runs, sampled, missed, gate_errors, gate_ms, finder_ms = row
        return {
            "period_hours": hours,
            "requests": total,
            "finder_runs": finder_runs,
            # Share of traffic the gate let skip the extractor: the cost saving
            "skipped_finder_pct": round((1 - finder_runs / total) * 100, 1) if total else None,
            "sampled": sampled,
            "gate_missed": missed,
            # Of sampled "clean" texts, how many the extractor found PII in
            "gate_miss_rate_pct": round(missed / sampled * 100, 2) if sampled else None,
            "gate_errors": gate_errors,
            "avg_gate_ms": round(gate_ms, 1) if gate_ms is not None else None,
            "avg_finder_ms": round(finder_ms, 1) if finder_ms is not None else None,
        }

    async def category_summary(self, hours: int = 24, max_rows: int = 50_000) -> dict[str, dict]:
        """Per category: how often the gate flagged it, the extractor found it,
        and stored copies were scrubbed for it. Counted in Python so it works
        the same on SQLite and PostgreSQL JSON."""
        from gateway.security.pii_taxonomy import LABELS

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        c = pii_gate_shadow.c
        counts = {
            label: {"gate_flagged": 0, "found": 0, "found_in_sampled": 0, "scrubbed": 0}
            for label in LABELS
        }
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(
                    c.gate_categories, c.finder_categories, c.finder_reason, c.scrubbed_categories
                )
                .where(c.timestamp >= cutoff)
                .order_by(c.timestamp.desc())
                .limit(max_rows)
            )
            for gate_cats, found, reason, scrubbed in rows:
                for label in gate_cats or []:
                    if label in counts:
                        counts[label]["gate_flagged"] += 1
                for label in found or {}:
                    if label in counts:
                        counts[label]["found"] += 1
                        if reason == "sampled":
                            counts[label]["found_in_sampled"] += 1
                for label in scrubbed or []:
                    if label in counts:
                        counts[label]["scrubbed"] += 1
        return counts

    async def cleanup(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(pii_gate_shadow).where(pii_gate_shadow.c.timestamp < cutoff)
            )
        return result.rowcount or 0

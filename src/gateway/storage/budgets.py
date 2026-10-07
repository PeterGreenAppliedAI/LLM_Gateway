"""Token budget persistence (D-037).

BudgetStore reads and writes the budget_usage table. BudgetSync connects
it to the in-memory TokenBudgetTracker: every `interval` seconds it writes
the usage recorded since the last write (atomic increments, so several
gateway processes can write the same row) and reads back today's totals,
which then include every process's spend. Checks never wait on the
database; a restart loses at most one interval of usage (none on a clean
shutdown, which flushes).

Tiers and model assignments changed from the dashboard are saved as one
document in runtime_settings. A saved document overrides gateway.yaml's
tiers and assignments, like other dashboard settings.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.policy.token_budget import TokenBudgetTracker, UsageRow
from gateway.storage.runtime_settings import RuntimeSettingsStore
from gateway.storage.schema import budget_usage

logger = get_logger(__name__)

CATALOG_SETTING = "budget.catalog"


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; they were written as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class BudgetStore:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    def _upsert(self, rows: list[UsageRow]):
        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            return None
        stmt = insert(budget_usage).values(
            [
                {
                    "day": r.day,
                    "client_id": r.client_id,
                    "tier": r.tier,
                    "weighted_tokens": r.weighted,
                    "raw_tokens": r.raw,
                    "requests": r.requests,
                }
                for r in rows
            ]
        )
        return stmt.on_conflict_do_update(
            index_elements=["day", "client_id", "tier"],
            set_={
                "weighted_tokens": budget_usage.c.weighted_tokens + stmt.excluded.weighted_tokens,
                "raw_tokens": budget_usage.c.raw_tokens + stmt.excluded.raw_tokens,
                "requests": budget_usage.c.requests + stmt.excluded.requests,
            },
        )

    async def add_usage(self, rows: list[UsageRow]) -> None:
        """Add usage to the stored totals (increments, safe across processes)."""
        if not rows:
            return
        async with self._engine.begin() as conn:
            stmt = self._upsert(rows)
            if stmt is not None:
                await conn.execute(stmt)
                return
            # Other databases: increment, insert when missing (same transaction)
            for r in rows:
                key = (
                    (budget_usage.c.day == r.day)
                    & (budget_usage.c.client_id == r.client_id)
                    & (budget_usage.c.tier == r.tier)
                )
                updated = await conn.execute(
                    budget_usage.update()
                    .where(key)
                    .values(
                        weighted_tokens=budget_usage.c.weighted_tokens + r.weighted,
                        raw_tokens=budget_usage.c.raw_tokens + r.raw,
                        requests=budget_usage.c.requests + r.requests,
                    )
                )
                if updated.rowcount == 0:
                    await conn.execute(
                        budget_usage.insert().values(
                            day=r.day,
                            client_id=r.client_id,
                            tier=r.tier,
                            weighted_tokens=r.weighted,
                            raw_tokens=r.raw,
                            requests=r.requests,
                        )
                    )

    async def day_usage(self, day: str) -> list[UsageRow]:
        async with self._engine.connect() as conn:
            result = await conn.execute(select(budget_usage).where(budget_usage.c.day == day))
            return [
                UsageRow(r.day, r.client_id, r.tier, r.weighted_tokens, r.raw_tokens, r.requests)
                for r in result
            ]

    async def prune(self, keep_days: int) -> int:
        """Delete days older than keep_days. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(budget_usage).where(budget_usage.c.day < cutoff))
            return result.rowcount or 0


class BudgetSync:
    """Keeps a TokenBudgetTracker and the database in step (see module docstring)."""

    def __init__(
        self,
        tracker: TokenBudgetTracker,
        engine: AsyncEngine,
        interval: float = 2.0,
        catalog_check_every: int = 5,
    ):
        self._tracker = tracker
        self._store = BudgetStore(engine)
        self._settings = RuntimeSettingsStore(engine)
        self._interval = interval
        self._catalog_check_every = catalog_check_every
        self._catalog_seen: datetime | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.failures = 0

    async def start(self) -> None:
        """Load saved tiers/assignments and today's totals, then sync in the background."""
        self._tracker.persisted = True
        await self._load_catalog(force=True)
        await self._refresh()
        self._task = asyncio.create_task(self._loop(), name="budget-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()  # a clean shutdown loses nothing

    async def flush(self) -> None:
        """Write pending usage, then read back today's totals (all processes)."""
        async with self._lock:
            rows = self._tracker.take_pending()
            try:
                await self._store.add_usage(rows)
            except Exception as e:
                self._tracker.restore_pending()  # retried with the next batch
                self.failures += 1
                logger.warning("Budget usage write failed; will retry", error=str(e))
                return
            self._tracker.confirm_written()
            await self._refresh()

    async def _refresh(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            self._tracker.set_baseline(day, await self._store.day_usage(day))
        except Exception as e:
            logger.warning("Budget usage read failed", error=str(e))

    async def save_catalog(self, updated_by: str | None) -> None:
        """Persist tiers and assignments after a dashboard change."""
        self._catalog_seen = _utc(
            await self._settings.set(CATALOG_SETTING, self._tracker.catalog_document(), updated_by)
        )

    async def _load_catalog(self, force: bool = False) -> None:
        saved = await self._settings.get(CATALOG_SETTING)
        if saved is None:
            return
        updated_at = _utc(saved["updated_at"])
        if force or self._catalog_seen is None or updated_at > self._catalog_seen:
            self._tracker.load_catalog(saved["value"])
            self._catalog_seen = updated_at

    async def _loop(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()
            ticks += 1
            if ticks % self._catalog_check_every == 0:
                # Another process (or its dashboard) may have changed tiers
                try:
                    await self._load_catalog()
                except Exception as e:
                    logger.debug("Budget catalog check failed", error=str(e))

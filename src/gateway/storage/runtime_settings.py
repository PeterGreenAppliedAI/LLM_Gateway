"""Persisted runtime settings: operator changes made from the dashboard.

Environment variables give each setting its startup default. A value
saved here (by an admin, via the API) overrides that default and
survives restarts. Each row records who changed it and when.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.storage.schema import runtime_settings


class RuntimeSettingsStore:
    """Key/value store over the runtime_settings table."""

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def get(self, key: str) -> dict[str, Any] | None:
        """The saved row for key ({value, updated_at, updated_by}), or None."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(runtime_settings).where(runtime_settings.c.key == key))
            ).fetchone()
        if row is None:
            return None
        return {"value": row.value, "updated_at": row.updated_at, "updated_by": row.updated_by}

    async def set(self, key: str, value: Any, updated_by: str | None) -> datetime:
        """Save value for key (insert or replace). Returns the timestamp written."""
        now = datetime.now(timezone.utc)
        async with self._engine.connect() as conn:
            updated = await conn.execute(
                runtime_settings.update()
                .where(runtime_settings.c.key == key)
                .values(value=value, updated_at=now, updated_by=updated_by)
            )
            if updated.rowcount == 0:
                await conn.execute(
                    runtime_settings.insert().values(
                        key=key, value=value, updated_at=now, updated_by=updated_by
                    )
                )
            await conn.commit()
        return now

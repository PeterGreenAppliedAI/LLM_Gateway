"""Audit logging for request tracking and analytics.

Provides async audit logging that writes request metadata
to the database for compliance, debugging, and analytics.
Uses async SQLAlchemy for non-blocking database I/O.
"""

import asyncio
import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, Table, and_, bindparam, case, cast, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger, get_metrics
from gateway.storage.schema import audit_log, pii_events, usage_daily

logger = get_logger(__name__)

# Backoff between audit write attempts (the first attempt is immediate).
# Short: the caller is usually still holding the client's response.
WRITE_RETRY_DELAYS = (0.05, 0.25)

_SPILLABLE_TABLES: dict[str, Table] = {t.name: t for t in (audit_log, pii_events)}
_DATETIME_MARKER = "__datetime__"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return {_DATETIME_MARKER: value.isoformat()}
    return str(value)


def _restore_row(row: dict) -> dict:
    return {
        key: datetime.fromisoformat(value[_DATETIME_MARKER])
        if isinstance(value, dict) and _DATETIME_MARKER in value
        else value
        for key, value in row.items()
    }


def _record_failure(table: str, outcome: str) -> None:
    try:
        get_metrics().record_audit_write_failure(table, outcome)
    except Exception:
        pass


class AuditLogger:
    """Async audit logger for request tracking.

    Writes request metadata to the audit_log table using async
    SQLAlchemy engine for non-blocking database I/O.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        store_request_body: bool = False,
        store_response_body: bool = False,
        body_redactor: Callable[[Any], Any] | None = None,
        spill_path: str | Path | None = None,
    ):
        self._engine = engine
        self._store_request_body = store_request_body
        self._store_response_body = store_response_body
        # Applied to bodies before they are persisted (PII scrubbing), so
        # stored audit data never holds raw PII when detection is enabled
        self.body_redactor = body_redactor
        # Rows that can't reach the database after retries are appended here
        # as JSON lines and replayed on the next startup, so a DB outage
        # doesn't silently drop the audit trail
        self._spill_path = Path(spill_path) if spill_path else None
        self._spill_lock = asyncio.Lock()

    async def log_request(
        self,
        request_id: str,
        client_id: str,
        task: str,
        model: str,
        endpoint: str,
        status: str,
        *,
        user_id: str | None = None,
        environment: str | None = None,
        provider_type: str | None = None,
        stream: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        latency_ms: float | None = None,
        time_to_first_token_ms: float | None = None,
        tokens_per_second: float | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
        estimated_cost_usd: float | None = None,
        request_body: dict | None = None,
        response_body: dict | None = None,
    ) -> None:
        """Log a request to the audit table."""
        values = {
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc),
            "client_id": client_id,
            "user_id": user_id,
            "environment": environment,
            "task": task,
            "model": model,
            "endpoint": endpoint,
            "provider_type": provider_type,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "status": status,
            "error_code": error_code,
            "error_message": error_message[:1000] if error_message else None,
            "latency_ms": latency_ms,
            "time_to_first_token_ms": time_to_first_token_ms,
            "tokens_per_second": tokens_per_second,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated_cost_usd": estimated_cost_usd,
        }

        if self._store_request_body and request_body:
            values["request_body"] = self._redact(request_body)
        if self._store_response_body and response_body:
            values["response_body"] = self._redact(response_body)

        await self._write_rows(audit_log, [values], request_id)

    async def _write_rows(self, table: Table, rows: list[dict], request_id: str) -> bool:
        """Insert rows, retrying transient failures, spilling to disk if the DB stays down.

        Returns True if the rows reached the database.
        """
        last_error: Exception | None = None
        for delay in (0.0, *WRITE_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._engine.connect() as conn:
                    await conn.execute(insert(table), rows)
                    await conn.commit()
                return True
            except IntegrityError as e:
                # Duplicate/invalid row: retrying or replaying can't fix it
                logger.error(
                    "Audit write rejected by database",
                    table=table.name,
                    request_id=request_id,
                    error=str(e),
                )
                _record_failure(table.name, "rejected")
                return False
            except Exception as e:
                last_error = e

        logger.error(
            "Audit write failed after retries",
            table=table.name,
            request_id=request_id,
            error=str(last_error),
        )
        if await self._spill(table.name, rows):
            _record_failure(table.name, "spilled")
        else:
            logger.critical(
                "Audit rows LOST: database and spill file both unavailable",
                table=table.name,
                request_id=request_id,
            )
            _record_failure(table.name, "lost")
        return False

    async def _spill(self, table_name: str, rows: list[dict]) -> bool:
        """Append rows to the spill file. Returns False if there is none or it failed."""
        if self._spill_path is None:
            return False
        line = json.dumps({"table": table_name, "rows": rows}, default=_json_default) + "\n"

        def _append() -> None:
            self._spill_path.parent.mkdir(parents=True, exist_ok=True)
            with self._spill_path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

        try:
            async with self._spill_lock:
                await asyncio.to_thread(_append)
            return True
        except Exception:
            logger.exception("Failed to write audit spill file", path=str(self._spill_path))
            return False

    async def replay_spill(self) -> int:
        """Insert rows saved to the spill file during a DB outage.

        Call on startup. Rows that still can't be written stay in the spill
        file; rows the database rejects (e.g. already present) are dropped.

        Returns:
            Number of rows written to the database.
        """
        if self._spill_path is None:
            return 0
        replaying = self._spill_path.with_suffix(self._spill_path.suffix + ".replaying")

        def _take_lines() -> list[str]:
            # A .replaying file left by an interrupted replay is read first
            if self._spill_path.exists():
                if replaying.exists():
                    with replaying.open("a", encoding="utf-8") as f:
                        f.write(self._spill_path.read_text("utf-8"))
                    self._spill_path.unlink()
                else:
                    self._spill_path.replace(replaying)
            if not replaying.exists():
                return []
            return replaying.read_text("utf-8").splitlines()

        async with self._spill_lock:
            lines = await asyncio.to_thread(_take_lines)
        if not lines:
            return 0

        written = 0
        failed_lines: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                table = _SPILLABLE_TABLES[record["table"]]
                rows = [_restore_row(row) for row in record["rows"]]
            except Exception:
                logger.exception("Unreadable audit spill line; skipped")
                continue
            try:
                async with self._engine.connect() as conn:
                    await conn.execute(insert(table), rows)
                    await conn.commit()
                written += len(rows)
            except IntegrityError:
                logger.warning("Spilled audit rows already present; dropped", table=table.name)
            except Exception:
                failed_lines.append(line)

        async with self._spill_lock:
            if failed_lines:

                def _restore() -> None:
                    with self._spill_path.open("a", encoding="utf-8") as f:
                        f.write("\n".join(failed_lines) + "\n")

                await asyncio.to_thread(_restore)
            await asyncio.to_thread(replaying.unlink)

        logger.info(
            "Replayed audit spill file",
            rows_written=written,
            records_remaining=len(failed_lines),
        )
        return written

    def _redact(self, body: Any) -> Any:
        if self.body_redactor is None:
            return body
        try:
            return self.body_redactor(body)
        except Exception:
            # Never fall back to storing the unredacted body
            logger.exception("Body redaction failed; body not stored")
            return {"redaction_failed": True}

    async def get_recent_requests(
        self,
        limit: int = 100,
        client_id: str | None = None,
        environment: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Get recent requests from the audit log."""
        stmt = select(audit_log).order_by(audit_log.c.timestamp.desc()).limit(limit)

        conditions = []
        if client_id:
            conditions.append(audit_log.c.client_id == client_id)
        if environment:
            conditions.append(audit_log.c.environment == environment)
        if status:
            conditions.append(audit_log.c.status == status)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [dict(row._mapping) for row in result.fetchall()]

    async def get_stats(
        self,
        hours: int = 24,
        client_id: str | None = None,
    ) -> dict:
        """Get usage statistics for the specified time period."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._engine.connect() as conn:
            conditions = [audit_log.c.timestamp >= cutoff]
            if client_id:
                conditions.append(audit_log.c.client_id == client_id)

            base_filter = and_(*conditions)

            # Total requests
            total_stmt = select(func.count()).select_from(audit_log).where(base_filter)
            total_requests = (await conn.execute(total_stmt)).scalar() or 0

            # Success count
            success_stmt = (
                select(func.count())
                .select_from(audit_log)
                .where(and_(base_filter, audit_log.c.status == "success"))
            )
            success_count = (await conn.execute(success_stmt)).scalar() or 0

            error_count = total_requests - success_count

            # Token totals
            tokens_stmt = select(
                func.sum(audit_log.c.prompt_tokens),
                func.sum(audit_log.c.completion_tokens),
                func.sum(audit_log.c.total_tokens),
            ).where(base_filter)
            token_result = (await conn.execute(tokens_stmt)).fetchone()
            prompt_tokens = token_result[0] or 0
            completion_tokens = token_result[1] or 0
            total_tokens = token_result[2] or 0

            # Latency stats
            latency_stmt = select(
                func.avg(audit_log.c.latency_ms),
                func.min(audit_log.c.latency_ms),
                func.max(audit_log.c.latency_ms),
            ).where(and_(base_filter, audit_log.c.latency_ms.isnot(None)))
            latency_result = (await conn.execute(latency_stmt)).fetchone()

            # Cost total
            cost_stmt = select(func.sum(audit_log.c.estimated_cost_usd)).where(
                and_(base_filter, audit_log.c.estimated_cost_usd.isnot(None))
            )
            total_cost = (await conn.execute(cost_stmt)).scalar() or 0.0

            # Requests by endpoint
            endpoint_stmt = (
                select(audit_log.c.endpoint, func.count())
                .where(base_filter)
                .group_by(audit_log.c.endpoint)
            )
            endpoints = {row[0]: row[1] for row in (await conn.execute(endpoint_stmt)).fetchall()}

            # Requests by model
            model_stmt = (
                select(audit_log.c.model, func.count())
                .where(base_filter)
                .group_by(audit_log.c.model)
                .order_by(func.count().desc())
                .limit(10)
            )
            top_models = {row[0]: row[1] for row in (await conn.execute(model_stmt)).fetchall()}

            return {
                "period_hours": hours,
                "total_requests": total_requests,
                "success_count": success_count,
                "error_count": error_count,
                "success_rate": (
                    round(success_count / total_requests * 100, 2) if total_requests > 0 else 0
                ),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "avg_latency_ms": round(latency_result[0], 2) if latency_result[0] else None,
                "min_latency_ms": round(latency_result[1], 2) if latency_result[1] else None,
                "max_latency_ms": round(latency_result[2], 2) if latency_result[2] else None,
                "total_cost_usd": round(total_cost, 4),
                "requests_by_endpoint": endpoints,
                "top_models": top_models,
            }

    async def get_request_by_id(self, request_id: str) -> dict | None:
        """Get a specific request by its ID."""
        stmt = select(audit_log).where(audit_log.c.request_id == request_id)

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            if row:
                return dict(row._mapping)
            return None

    async def get_models_usage(self, hours: int = 24) -> list[dict]:
        """Get usage statistics grouped by model."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._engine.connect() as conn:
            stmt = (
                select(
                    audit_log.c.model,
                    func.count().label("request_count"),
                    func.sum(cast(audit_log.c.status == "success", Integer)).label("success_count"),
                    func.sum(audit_log.c.total_tokens).label("total_tokens"),
                    func.avg(audit_log.c.latency_ms).label("avg_latency_ms"),
                )
                .where(audit_log.c.timestamp >= cutoff)
                .group_by(audit_log.c.model)
                .order_by(func.count().desc())
            )

            results = []
            for row in (await conn.execute(stmt)).fetchall():
                request_count = row.request_count or 0
                success_count = row.success_count or 0
                results.append(
                    {
                        "model": row.model,
                        "request_count": request_count,
                        "success_count": success_count,
                        "error_count": request_count - success_count,
                        "total_tokens": row.total_tokens or 0,
                        "avg_latency_ms": round(row.avg_latency_ms, 2)
                        if row.avg_latency_ms
                        else None,
                    }
                )

            return results

    async def get_endpoints_usage(self, hours: int = 24) -> list[dict]:
        """Get usage statistics grouped by endpoint."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._engine.connect() as conn:
            stmt = (
                select(
                    audit_log.c.endpoint,
                    func.count().label("request_count"),
                    func.sum(cast(audit_log.c.status == "success", Integer)).label("success_count"),
                    func.sum(audit_log.c.total_tokens).label("total_tokens"),
                    func.avg(audit_log.c.latency_ms).label("avg_latency_ms"),
                )
                .where(audit_log.c.timestamp >= cutoff)
                .group_by(audit_log.c.endpoint)
                .order_by(func.count().desc())
            )

            results = []
            for row in (await conn.execute(stmt)).fetchall():
                request_count = row.request_count or 0
                success_count = row.success_count or 0
                results.append(
                    {
                        "endpoint": row.endpoint,
                        "request_count": request_count,
                        "success_count": success_count,
                        "error_count": request_count - success_count,
                        "total_tokens": row.total_tokens or 0,
                        "avg_latency_ms": round(row.avg_latency_ms, 2)
                        if row.avg_latency_ms
                        else None,
                    }
                )

            return results

    async def aggregate_daily_usage(self, date: datetime | None = None) -> dict:
        """Aggregate usage from audit_log into usage_daily table.

        Computes daily rollups by client, endpoint, and model for faster
        dashboard queries over long time periods.
        """
        if date is None:
            date = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)

        start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        async with self._engine.connect() as conn:
            # Delete existing aggregates for this date (idempotent)
            delete_stmt = delete(usage_daily).where(
                and_(
                    usage_daily.c.date >= start_of_day,
                    usage_daily.c.date < end_of_day,
                )
            )
            await conn.execute(delete_stmt)

            # Aggregate from audit_log
            agg_stmt = (
                select(
                    bindparam("agg_date", value=start_of_day).label("date"),
                    audit_log.c.client_id,
                    audit_log.c.endpoint,
                    audit_log.c.model,
                    func.count().label("request_count"),
                    func.sum(case((audit_log.c.status == "success", 1), else_=0)).label(
                        "success_count"
                    ),
                    func.sum(audit_log.c.total_tokens).label("total_tokens"),
                    func.sum(audit_log.c.estimated_cost_usd).label("total_cost_usd"),
                    func.avg(audit_log.c.latency_ms).label("avg_latency_ms"),
                )
                .where(
                    and_(
                        audit_log.c.timestamp >= start_of_day,
                        audit_log.c.timestamp < end_of_day,
                    )
                )
                .group_by(
                    audit_log.c.client_id,
                    audit_log.c.endpoint,
                    audit_log.c.model,
                )
            )

            rows = (await conn.execute(agg_stmt)).fetchall()

            # Bulk insert all rows at once
            if rows:
                bulk_values = [
                    {
                        "date": start_of_day,
                        "client_id": row.client_id,
                        "endpoint": row.endpoint,
                        "model": row.model,
                        "request_count": row.request_count or 0,
                        "success_count": row.success_count or 0,
                        "total_tokens": row.total_tokens or 0,
                        "total_cost_usd": row.total_cost_usd or 0.0,
                        "avg_latency_ms": row.avg_latency_ms,
                    }
                    for row in rows
                ]
                await conn.execute(insert(usage_daily), bulk_values)

            await conn.commit()

            return {
                "date": start_of_day.isoformat(),
                "rows_aggregated": len(rows),
            }

    async def get_daily_usage(
        self,
        days: int = 30,
        client_id: str | None = None,
    ) -> list[dict]:
        """Get daily usage from the aggregated table."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stmt = (
            select(
                usage_daily.c.date,
                func.sum(usage_daily.c.request_count).label("request_count"),
                func.sum(usage_daily.c.success_count).label("success_count"),
                func.sum(usage_daily.c.total_tokens).label("total_tokens"),
                func.sum(usage_daily.c.total_cost_usd).label("total_cost_usd"),
            )
            .where(usage_daily.c.date >= cutoff)
            .group_by(usage_daily.c.date)
            .order_by(usage_daily.c.date)
        )

        if client_id:
            stmt = stmt.where(usage_daily.c.client_id == client_id)

        async with self._engine.connect() as conn:
            results = []
            for row in (await conn.execute(stmt)).fetchall():
                results.append(
                    {
                        "date": row.date.isoformat() if row.date else None,
                        "request_count": row.request_count or 0,
                        "success_count": row.success_count or 0,
                        "total_tokens": row.total_tokens or 0,
                        "total_cost_usd": float(row.total_cost_usd or 0),
                    }
                )
            return results

    async def cleanup_old_records(self, retention_days: int) -> int:
        """Delete audit log records older than retention_days.

        Returns the number of records deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(delete(audit_log).where(audit_log.c.timestamp < cutoff))
                await conn.commit()
                deleted = result.rowcount
                if deleted > 0:
                    logger.info(
                        "Audit log cleanup completed",
                        deleted=deleted,
                        retention_days=retention_days,
                    )
                return deleted
        except Exception as e:
            logger.error("Audit log cleanup failed", error=str(e))
            return 0

    async def log_pii_events(
        self,
        request_id: str,
        client_id: str,
        task: str | None,
        model: str | None,
        messages: list[dict],
        pii_results: list,
        was_scrubbed: bool,
    ) -> int:
        """Log PII detection events with hashed values — never stores raw PII.

        Args:
            messages: Original messages (before scrubbing) to extract raw values for hashing
            pii_results: List of PIIScanResult from the scrubber
            was_scrubbed: Whether scrubbing was applied

        Returns:
            Number of PII events logged
        """
        import hashlib

        now = datetime.now(timezone.utc)
        rows = []

        # Map pii_results back to messages — one result per message content
        result_idx = 0
        for msg_idx, msg in enumerate(messages):
            content = msg.get("content", "")
            if not isinstance(content, str) or not content:
                continue
            if result_idx >= len(pii_results):
                break

            scan_result = pii_results[result_idx]
            result_idx += 1

            for detection in scan_result.detections:
                # Extract the raw value from the original text and hash it
                raw_value = content[detection.start : detection.end]
                value_hash = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()

                rows.append(
                    {
                        "request_id": request_id,
                        "timestamp": now,
                        "client_id": client_id,
                        "model": model,
                        "task": task,
                        "pii_type": detection.pii_type,
                        "message_index": msg_idx,
                        "message_role": msg.get("role"),
                        "position_start": detection.start,
                        "position_end": detection.end,
                        "value_hash": value_hash,
                        "was_scrubbed": was_scrubbed,
                        "scan_time_ms": scan_result.scan_time_ms,
                    }
                )

        if not rows:
            return 0

        if await self._write_rows(pii_events, rows, request_id):
            logger.info(
                "PII events logged",
                request_id=request_id,
                event_count=len(rows),
                pii_types=list(set(r["pii_type"] for r in rows)),
                scrubbed=was_scrubbed,
            )

        return len(rows)

    async def get_pii_stats(self, hours: int = 24) -> dict:
        """Get PII detection statistics."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with self._engine.connect() as conn:
            base = pii_events.c.timestamp >= cutoff

            # Total events
            total = (
                await conn.execute(select(func.count()).select_from(pii_events).where(base))
            ).scalar() or 0

            # By type
            type_stmt = (
                select(pii_events.c.pii_type, func.count())
                .where(base)
                .group_by(pii_events.c.pii_type)
            )
            by_type = {row[0]: row[1] for row in (await conn.execute(type_stmt)).fetchall()}

            # Scrubbed vs flagged-only
            scrubbed = (
                await conn.execute(
                    select(func.count())
                    .select_from(pii_events)
                    .where(and_(base, pii_events.c.was_scrubbed == True))  # noqa: E712
                )
            ).scalar() or 0

            # Unique requests with PII
            unique_requests = (
                await conn.execute(
                    select(func.count(func.distinct(pii_events.c.request_id)))
                    .select_from(pii_events)
                    .where(base)
                )
            ).scalar() or 0

            # Unique value hashes (distinct PII values seen)
            unique_values = (
                await conn.execute(
                    select(func.count(func.distinct(pii_events.c.value_hash)))
                    .select_from(pii_events)
                    .where(base)
                )
            ).scalar() or 0

            return {
                "period_hours": hours,
                "total_detections": total,
                "by_type": by_type,
                "scrubbed_count": scrubbed,
                "flagged_only_count": total - scrubbed,
                "unique_requests": unique_requests,
                "unique_values": unique_values,
            }

    async def get_pii_events(
        self,
        limit: int = 50,
        pii_type: str | None = None,
        client_id: str | None = None,
    ) -> list[dict]:
        """Get recent PII events (no raw PII — only hashes and metadata)."""
        stmt = select(pii_events).order_by(pii_events.c.timestamp.desc()).limit(limit)

        conditions = []
        if pii_type:
            conditions.append(pii_events.c.pii_type == pii_type)
        if client_id:
            conditions.append(pii_events.c.client_id == client_id)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [dict(row._mapping) for row in result.fetchall()]

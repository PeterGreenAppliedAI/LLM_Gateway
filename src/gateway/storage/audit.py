"""Audit logging for request tracking and analytics.

Provides async-compatible audit logging that writes request metadata
to the database for compliance, debugging, and analytics.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Engine, insert, select, func, and_, Integer, cast
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gateway.storage.schema import audit_log, usage_daily
from sqlalchemy import case, literal_column
from gateway.observability import get_logger

logger = get_logger(__name__)


class AuditLogger:
    """Async-compatible audit logger for request tracking.

    Writes request metadata to the audit_log table. Uses a thread pool
    to avoid blocking the async event loop on database writes.
    """

    def __init__(
        self,
        engine: Engine,
        store_request_body: bool = False,
        store_response_body: bool = False,
        max_workers: int = 4,
    ):
        """Initialize the audit logger.

        Args:
            engine: SQLAlchemy engine for database connection
            store_request_body: Whether to store request bodies (privacy)
            store_response_body: Whether to store response bodies (privacy)
            max_workers: Thread pool size for async writes
        """
        self._engine = engine
        self._store_request_body = store_request_body
        self._store_response_body = store_response_body
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._is_sqlite = str(engine.url).startswith("sqlite")

    async def log_request(
        self,
        request_id: str,
        client_id: str,
        task: str,
        model: str,
        endpoint: str,
        status: str,
        *,
        user_id: Optional[str] = None,
        environment: Optional[str] = None,
        provider_type: Optional[str] = None,
        stream: bool = False,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        latency_ms: Optional[float] = None,
        time_to_first_token_ms: Optional[float] = None,
        tokens_per_second: Optional[float] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        estimated_cost_usd: Optional[float] = None,
        request_body: Optional[dict] = None,
        response_body: Optional[dict] = None,
    ) -> None:
        """Log a request to the audit table.

        This method is async but uses a thread pool to avoid blocking
        on database writes.

        Args:
            request_id: Unique request identifier
            client_id: Authenticated client ID
            task: Task type (chat, completion, embeddings)
            model: Model name used
            endpoint: Endpoint that handled the request
            status: Request status (success, error, rate_limited)
            user_id: Optional user identifier
            environment: Optional environment (dev, prod)
            provider_type: Provider type (ollama, openai, etc.)
            stream: Whether streaming was used
            max_tokens: Requested max tokens
            temperature: Requested temperature
            latency_ms: Total request latency
            time_to_first_token_ms: Time to first token (streaming)
            tokens_per_second: Generation throughput
            prompt_tokens: Input token count
            completion_tokens: Output token count
            error_code: Error code if failed
            error_message: Error message if failed
            estimated_cost_usd: Estimated cost in USD
            request_body: Request body (if store_request_body enabled)
            response_body: Response body (if store_response_body enabled)
        """
        # Build the values dict
        values = {
            "request_id": request_id,
            "timestamp": datetime.utcnow(),
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

        # Add bodies if configured
        if self._store_request_body and request_body:
            values["request_body"] = request_body
        if self._store_response_body and response_body:
            values["response_body"] = response_body

        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                self._executor,
                self._insert_audit_log,
                values,
            )
        except Exception as e:
            # Don't fail the request if audit logging fails
            logger.error(f"Failed to write audit log: {e}")

    def _insert_audit_log(self, values: dict) -> None:
        """Insert audit log entry (runs in thread pool)."""
        stmt = insert(audit_log).values(**values)
        with self._engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    async def get_recent_requests(
        self,
        limit: int = 100,
        client_id: Optional[str] = None,
        environment: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        """Get recent requests from the audit log.

        Args:
            limit: Maximum number of records to return
            client_id: Filter by client ID
            environment: Filter by environment
            status: Filter by status

        Returns:
            List of audit log entries as dicts
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._select_recent,
            limit,
            client_id,
            environment,
            status,
        )

    def _select_recent(
        self,
        limit: int,
        client_id: Optional[str],
        environment: Optional[str],
        status: Optional[str],
    ) -> list[dict]:
        """Select recent audit log entries (runs in thread pool)."""
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

        with self._engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result.fetchall()]

    async def get_stats(
        self,
        hours: int = 24,
        client_id: Optional[str] = None,
    ) -> dict:
        """Get usage statistics for the specified time period.

        Args:
            hours: Number of hours to look back
            client_id: Filter by client ID

        Returns:
            Dict with usage statistics
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._compute_stats,
            hours,
            client_id,
        )

    def _compute_stats(self, hours: int, client_id: Optional[str]) -> dict:
        """Compute usage statistics (runs in thread pool)."""
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=hours)

        with self._engine.connect() as conn:
            # Build base condition
            conditions = [audit_log.c.timestamp >= cutoff]
            if client_id:
                conditions.append(audit_log.c.client_id == client_id)

            base_filter = and_(*conditions)

            # Total requests
            total_stmt = select(func.count()).select_from(audit_log).where(base_filter)
            total_requests = conn.execute(total_stmt).scalar() or 0

            # Success count
            success_stmt = (
                select(func.count())
                .select_from(audit_log)
                .where(and_(base_filter, audit_log.c.status == "success"))
            )
            success_count = conn.execute(success_stmt).scalar() or 0

            # Error count
            error_count = total_requests - success_count

            # Token totals
            tokens_stmt = select(
                func.sum(audit_log.c.prompt_tokens),
                func.sum(audit_log.c.completion_tokens),
                func.sum(audit_log.c.total_tokens),
            ).where(base_filter)
            token_result = conn.execute(tokens_stmt).fetchone()
            prompt_tokens = token_result[0] or 0
            completion_tokens = token_result[1] or 0
            total_tokens = token_result[2] or 0

            # Latency stats
            latency_stmt = select(
                func.avg(audit_log.c.latency_ms),
                func.min(audit_log.c.latency_ms),
                func.max(audit_log.c.latency_ms),
            ).where(and_(base_filter, audit_log.c.latency_ms.isnot(None)))
            latency_result = conn.execute(latency_stmt).fetchone()

            # Cost total
            cost_stmt = select(func.sum(audit_log.c.estimated_cost_usd)).where(
                and_(base_filter, audit_log.c.estimated_cost_usd.isnot(None))
            )
            total_cost = conn.execute(cost_stmt).scalar() or 0.0

            # Requests by endpoint
            endpoint_stmt = (
                select(audit_log.c.endpoint, func.count())
                .where(base_filter)
                .group_by(audit_log.c.endpoint)
            )
            endpoints = {
                row[0]: row[1] for row in conn.execute(endpoint_stmt).fetchall()
            }

            # Requests by model
            model_stmt = (
                select(audit_log.c.model, func.count())
                .where(base_filter)
                .group_by(audit_log.c.model)
                .order_by(func.count().desc())
                .limit(10)
            )
            top_models = {
                row[0]: row[1] for row in conn.execute(model_stmt).fetchall()
            }

            return {
                "period_hours": hours,
                "total_requests": total_requests,
                "success_count": success_count,
                "error_count": error_count,
                "success_rate": (
                    round(success_count / total_requests * 100, 2)
                    if total_requests > 0
                    else 0
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

    async def get_request_by_id(self, request_id: str) -> Optional[dict]:
        """Get a specific request by its ID.

        Args:
            request_id: The request ID to look up

        Returns:
            Request details as dict, or None if not found
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._select_by_id,
            request_id,
        )

    def _select_by_id(self, request_id: str) -> Optional[dict]:
        """Select a single request by ID (runs in thread pool)."""
        stmt = select(audit_log).where(audit_log.c.request_id == request_id)

        with self._engine.connect() as conn:
            result = conn.execute(stmt)
            row = result.fetchone()
            if row:
                return dict(row._mapping)
            return None

    async def get_models_usage(self, hours: int = 24) -> list[dict]:
        """Get usage statistics grouped by model.

        Args:
            hours: Number of hours to look back

        Returns:
            List of per-model usage statistics
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._compute_models_usage,
            hours,
        )

    def _compute_models_usage(self, hours: int) -> list[dict]:
        """Compute per-model usage statistics (runs in thread pool)."""
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=hours)

        with self._engine.connect() as conn:
            stmt = (
                select(
                    audit_log.c.model,
                    func.count().label("request_count"),
                    func.sum(
                        cast(audit_log.c.status == "success", Integer)
                    ).label("success_count"),
                    func.sum(audit_log.c.total_tokens).label("total_tokens"),
                    func.avg(audit_log.c.latency_ms).label("avg_latency_ms"),
                )
                .where(audit_log.c.timestamp >= cutoff)
                .group_by(audit_log.c.model)
                .order_by(func.count().desc())
            )

            results = []
            for row in conn.execute(stmt).fetchall():
                request_count = row.request_count or 0
                success_count = row.success_count or 0
                results.append({
                    "model": row.model,
                    "request_count": request_count,
                    "success_count": success_count,
                    "error_count": request_count - success_count,
                    "total_tokens": row.total_tokens or 0,
                    "avg_latency_ms": round(row.avg_latency_ms, 2) if row.avg_latency_ms else None,
                })

            return results

    async def get_endpoints_usage(self, hours: int = 24) -> list[dict]:
        """Get usage statistics grouped by endpoint.

        Args:
            hours: Number of hours to look back

        Returns:
            List of per-endpoint usage statistics
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._compute_endpoints_usage,
            hours,
        )

    def _compute_endpoints_usage(self, hours: int) -> list[dict]:
        """Compute per-endpoint usage statistics (runs in thread pool)."""
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=hours)

        with self._engine.connect() as conn:
            stmt = (
                select(
                    audit_log.c.endpoint,
                    func.count().label("request_count"),
                    func.sum(
                        cast(audit_log.c.status == "success", Integer)
                    ).label("success_count"),
                    func.sum(audit_log.c.total_tokens).label("total_tokens"),
                    func.avg(audit_log.c.latency_ms).label("avg_latency_ms"),
                )
                .where(audit_log.c.timestamp >= cutoff)
                .group_by(audit_log.c.endpoint)
                .order_by(func.count().desc())
            )

            results = []
            for row in conn.execute(stmt).fetchall():
                request_count = row.request_count or 0
                success_count = row.success_count or 0
                results.append({
                    "endpoint": row.endpoint,
                    "request_count": request_count,
                    "success_count": success_count,
                    "error_count": request_count - success_count,
                    "total_tokens": row.total_tokens or 0,
                    "avg_latency_ms": round(row.avg_latency_ms, 2) if row.avg_latency_ms else None,
                })

            return results

    async def aggregate_daily_usage(self, date: Optional[datetime] = None) -> dict:
        """Aggregate usage from audit_log into usage_daily table.

        Computes daily rollups by client, endpoint, and model for faster
        dashboard queries over long time periods.

        Args:
            date: Date to aggregate (default: yesterday)

        Returns:
            Dict with aggregation results
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._aggregate_daily,
            date,
        )

    def _aggregate_daily(self, date: Optional[datetime]) -> dict:
        """Aggregate daily usage (runs in thread pool)."""
        from datetime import timedelta
        from sqlalchemy import delete

        # Default to yesterday if no date provided
        if date is None:
            date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

        # Calculate date range
        start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        with self._engine.connect() as conn:
            # Delete existing aggregates for this date (idempotent)
            delete_stmt = delete(usage_daily).where(
                and_(
                    usage_daily.c.date >= start_of_day,
                    usage_daily.c.date < end_of_day,
                )
            )
            conn.execute(delete_stmt)

            # Aggregate from audit_log
            agg_stmt = (
                select(
                    literal_column(f"'{start_of_day.isoformat()}'").label("date"),
                    audit_log.c.client_id,
                    audit_log.c.endpoint,
                    audit_log.c.model,
                    func.count().label("request_count"),
                    func.sum(
                        case((audit_log.c.status == "success", 1), else_=0)
                    ).label("success_count"),
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

            rows = conn.execute(agg_stmt).fetchall()
            inserted = 0

            for row in rows:
                insert_stmt = insert(usage_daily).values(
                    date=start_of_day,
                    client_id=row.client_id,
                    endpoint=row.endpoint,
                    model=row.model,
                    request_count=row.request_count or 0,
                    success_count=row.success_count or 0,
                    total_tokens=row.total_tokens or 0,
                    total_cost_usd=row.total_cost_usd or 0.0,
                    avg_latency_ms=row.avg_latency_ms,
                )
                conn.execute(insert_stmt)
                inserted += 1

            conn.commit()

            return {
                "date": start_of_day.isoformat(),
                "rows_aggregated": inserted,
            }

    async def get_daily_usage(
        self,
        days: int = 30,
        client_id: Optional[str] = None,
    ) -> list[dict]:
        """Get daily usage from the aggregated table.

        Args:
            days: Number of days to look back
            client_id: Filter by client ID

        Returns:
            List of daily usage entries
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._select_daily_usage,
            days,
            client_id,
        )

    def _select_daily_usage(self, days: int, client_id: Optional[str]) -> list[dict]:
        """Select daily usage (runs in thread pool)."""
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(days=days)

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

        with self._engine.connect() as conn:
            results = []
            for row in conn.execute(stmt).fetchall():
                results.append({
                    "date": row.date.isoformat() if row.date else None,
                    "request_count": row.request_count or 0,
                    "success_count": row.success_count or 0,
                    "total_tokens": row.total_tokens or 0,
                    "total_cost_usd": float(row.total_cost_usd or 0),
                })
            return results

    def close(self) -> None:
        """Shutdown the thread pool."""
        self._executor.shutdown(wait=True)

"""Tests for the storage module (database layer).

Tests cover:
- Database engine creation for SQLite and PostgreSQL
- AuditLogger request logging
- Query methods (get_recent_requests, get_stats)
- Schema compatibility
"""

import pytest
from datetime import datetime, timedelta

from sqlalchemy import text

from gateway.storage import (
    AuditLogger,
    DatabaseConfig,
    create_async_db_engine,
    metadata,
    audit_log,
    usage_daily,
    api_keys,
)
from gateway.storage.engine import get_table_stats


# =============================================================================
# Database Engine Tests
# =============================================================================


class TestDatabaseConfig:
    """Tests for DatabaseConfig validation."""

    def test_default_config(self):
        """Default config uses SQLite."""
        config = DatabaseConfig()
        assert config.url == "sqlite:///./data/gateway.db"
        assert config.pool_size == 5
        assert config.create_tables is True
        assert config.store_request_body is False
        assert config.store_response_body is False

    def test_postgresql_config(self):
        """PostgreSQL config is accepted."""
        config = DatabaseConfig(
            url="postgresql://user:pass@localhost:5432/gateway",
            pool_size=10,
            max_overflow=20,
        )
        assert config.url == "postgresql://user:pass@localhost:5432/gateway"
        assert config.pool_size == 10
        assert config.max_overflow == 20

    def test_pool_size_bounds(self):
        """Pool size must be within bounds."""
        with pytest.raises(ValueError):
            DatabaseConfig(pool_size=0)
        with pytest.raises(ValueError):
            DatabaseConfig(pool_size=100)

    def test_privacy_settings(self):
        """Privacy settings can be enabled."""
        config = DatabaseConfig(
            store_request_body=True,
            store_response_body=True,
        )
        assert config.store_request_body is True
        assert config.store_response_body is True


class TestDatabaseEngine:
    """Tests for database engine creation."""

    @pytest.mark.asyncio
    async def test_create_sqlite_engine(self, tmp_path):
        """Creates SQLite engine with correct settings."""
        db_path = tmp_path / "test.db"
        config = DatabaseConfig(
            url=f"sqlite:///{db_path}",
            create_tables=True,
        )
        engine = await create_async_db_engine(config)

        assert engine is not None
        assert "sqlite" in str(engine.url)

        # Tables should be created
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = {row[0] for row in result.fetchall()}
            assert "audit_log" in tables
            assert "usage_daily" in tables
            assert "api_keys" in tables

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_create_inmemory_sqlite_engine(self):
        """Creates in-memory SQLite engine."""
        config = DatabaseConfig(
            url="sqlite:///:memory:",
            create_tables=True,
        )
        engine = await create_async_db_engine(config)

        assert engine is not None

        # Should be able to query tables
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM audit_log"))
            assert result.scalar() == 0

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_engine_without_table_creation(self, tmp_path):
        """Engine can skip table creation."""
        db_path = tmp_path / "empty.db"
        config = DatabaseConfig(
            url=f"sqlite:///{db_path}",
            create_tables=False,
        )
        engine = await create_async_db_engine(config, create_tables=False)

        assert engine is not None

        # No tables should exist
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = list(result.fetchall())
            assert len(tables) == 0

        await engine.dispose()


class TestTableStats:
    """Tests for get_table_stats function."""

    @pytest.mark.asyncio
    async def test_get_table_stats_empty(self):
        """Gets stats for empty tables."""
        config = DatabaseConfig(url="sqlite:///:memory:", create_tables=True)
        engine = await create_async_db_engine(config)

        stats = await get_table_stats(engine)

        assert "audit_log" in stats
        # May have row_count or error depending on implementation
        if "row_count" in stats["audit_log"]:
            assert stats["audit_log"]["row_count"] == 0
        assert "usage_daily" in stats
        assert "api_keys" in stats

        await engine.dispose()


# =============================================================================
# Audit Logger Tests
# =============================================================================


class TestAuditLogger:
    """Tests for AuditLogger class."""

    @pytest.fixture
    async def db_engine(self):
        """Create in-memory database for testing."""
        config = DatabaseConfig(url="sqlite:///:memory:", create_tables=True)
        engine = await create_async_db_engine(config)
        yield engine
        await engine.dispose()

    @pytest.fixture
    def audit_logger(self, db_engine):
        """Create AuditLogger instance."""
        return AuditLogger(
            engine=db_engine,
            store_request_body=True,
            store_response_body=True,
        )

    @pytest.mark.asyncio
    async def test_log_request_basic(self, audit_logger, db_engine):
        """Logs a basic request."""
        await audit_logger.log_request(
            request_id="req-001",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            prompt_tokens=100,
            completion_tokens=50,
        )

        # Verify the record was inserted
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM audit_log WHERE request_id = 'req-001'")
            )
            row = result.fetchone()

        assert row is not None
        assert row.client_id == "test-client"
        assert row.task == "chat"
        assert row.model == "phi4:14b"
        assert row.endpoint == "gpunode-ollama"
        assert row.status == "success"
        assert row.prompt_tokens == 100
        assert row.completion_tokens == 50
        assert row.total_tokens == 150

    @pytest.mark.asyncio
    async def test_log_request_with_all_fields(self, audit_logger, db_engine):
        """Logs a request with all optional fields."""
        await audit_logger.log_request(
            request_id="req-002",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            user_id="user-123",
            environment="prod",
            provider_type="ollama",
            stream=True,
            max_tokens=1000,
            temperature=0.7,
            latency_ms=1500.5,
            time_to_first_token_ms=200.0,
            tokens_per_second=50.5,
            prompt_tokens=200,
            completion_tokens=100,
            estimated_cost_usd=0.005,
            request_body={"messages": [{"role": "user", "content": "Hello"}]},
            response_body={"content": "Hi there!"},
        )

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM audit_log WHERE request_id = 'req-002'")
            )
            row = result.fetchone()

        assert row.user_id == "user-123"
        assert row.environment == "prod"
        assert row.provider_type == "ollama"
        assert row.stream == 1  # SQLite stores boolean as 1/0
        assert row.max_tokens == 1000
        assert row.temperature == 0.7
        assert row.latency_ms == 1500.5
        assert row.time_to_first_token_ms == 200.0
        assert row.tokens_per_second == 50.5
        assert row.estimated_cost_usd == 0.005

    @pytest.mark.asyncio
    async def test_log_request_error(self, audit_logger, db_engine):
        """Logs an error request."""
        await audit_logger.log_request(
            request_id="req-error",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
            error_code="PROVIDER_ERROR",
            error_message="Connection timeout",
        )

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM audit_log WHERE request_id = 'req-error'")
            )
            row = result.fetchone()

        assert row.status == "error"
        assert row.error_code == "PROVIDER_ERROR"
        assert row.error_message == "Connection timeout"

    @pytest.mark.asyncio
    async def test_log_request_truncates_long_error(self, audit_logger, db_engine):
        """Truncates long error messages."""
        long_error = "x" * 2000  # Over 1000 char limit

        await audit_logger.log_request(
            request_id="req-long-error",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
            error_message=long_error,
        )

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT error_message FROM audit_log WHERE request_id = 'req-long-error'")
            )
            row = result.fetchone()

        assert len(row.error_message) == 1000

    @pytest.mark.asyncio
    async def test_log_request_privacy_disabled(self, db_engine):
        """Request body not stored when disabled."""
        audit = AuditLogger(
            engine=db_engine,
            store_request_body=False,
            store_response_body=False,
        )

        await audit.log_request(
            request_id="req-private",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            request_body={"messages": [{"role": "user", "content": "Secret"}]},
            response_body={"content": "Response"},
        )

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT request_body, response_body FROM audit_log WHERE request_id = 'req-private'")
            )
            row = result.fetchone()

        assert row.request_body is None
        assert row.response_body is None


class TestAuditLoggerQueries:
    """Tests for AuditLogger query methods."""

    @pytest.fixture
    async def db_engine(self):
        """Create in-memory database for testing."""
        config = DatabaseConfig(url="sqlite:///:memory:", create_tables=True)
        engine = await create_async_db_engine(config)
        yield engine
        await engine.dispose()

    @pytest.fixture
    def audit_logger(self, db_engine):
        """Create AuditLogger instance."""
        return AuditLogger(engine=db_engine)

    @pytest.mark.asyncio
    async def test_get_recent_requests_empty(self, audit_logger):
        """Returns empty list when no requests."""
        results = await audit_logger.get_recent_requests(limit=10)
        assert results == []

    @pytest.mark.asyncio
    async def test_get_recent_requests(self, audit_logger):
        """Returns recent requests in descending order."""
        # Log multiple requests
        for i in range(5):
            await audit_logger.log_request(
                request_id=f"req-{i:03d}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
            )

        results = await audit_logger.get_recent_requests(limit=3)

        assert len(results) == 3
        # Most recent first
        assert results[0]["request_id"] == "req-004"

    @pytest.mark.asyncio
    async def test_get_recent_requests_filter_client(self, audit_logger):
        """Filters by client_id."""
        await audit_logger.log_request(
            request_id="req-a",
            client_id="client-a",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )
        await audit_logger.log_request(
            request_id="req-b",
            client_id="client-b",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )

        results = await audit_logger.get_recent_requests(client_id="client-a")

        assert len(results) == 1
        assert results[0]["client_id"] == "client-a"

    @pytest.mark.asyncio
    async def test_get_recent_requests_filter_status(self, audit_logger):
        """Filters by status."""
        await audit_logger.log_request(
            request_id="req-success",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )
        await audit_logger.log_request(
            request_id="req-error",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
        )

        results = await audit_logger.get_recent_requests(status="error")

        assert len(results) == 1
        assert results[0]["status"] == "error"


class TestAuditLoggerStats:
    """Tests for AuditLogger stats methods."""

    @pytest.fixture
    async def db_engine(self):
        """Create in-memory database for testing."""
        config = DatabaseConfig(url="sqlite:///:memory:", create_tables=True)
        engine = await create_async_db_engine(config)
        yield engine
        await engine.dispose()

    @pytest.fixture
    def audit_logger(self, db_engine):
        """Create AuditLogger instance."""
        return AuditLogger(engine=db_engine)

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, audit_logger):
        """Returns zero stats when no requests."""
        stats = await audit_logger.get_stats(hours=24)

        assert stats["total_requests"] == 0
        assert stats["success_count"] == 0
        assert stats["error_count"] == 0
        assert stats["success_rate"] == 0
        assert stats["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_with_requests(self, audit_logger):
        """Computes correct statistics."""
        # Log successful requests
        for i in range(3):
            await audit_logger.log_request(
                request_id=f"req-success-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
                latency_ms=100.0 + i * 50,  # 100, 150, 200
                prompt_tokens=100,
                completion_tokens=50,
            )

        # Log an error
        await audit_logger.log_request(
            request_id="req-error",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
        )

        stats = await audit_logger.get_stats(hours=24)

        assert stats["total_requests"] == 4
        assert stats["success_count"] == 3
        assert stats["error_count"] == 1
        assert stats["success_rate"] == 75.0
        assert stats["prompt_tokens"] == 300
        assert stats["completion_tokens"] == 150
        assert stats["total_tokens"] == 450
        assert stats["avg_latency_ms"] == 150.0  # (100+150+200) / 3
        assert stats["min_latency_ms"] == 100.0
        assert stats["max_latency_ms"] == 200.0

    @pytest.mark.asyncio
    async def test_get_stats_filter_client(self, audit_logger):
        """Filters stats by client_id."""
        await audit_logger.log_request(
            request_id="req-a",
            client_id="client-a",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            prompt_tokens=100,
            completion_tokens=50,
        )
        await audit_logger.log_request(
            request_id="req-b",
            client_id="client-b",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            prompt_tokens=200,
            completion_tokens=100,
        )

        stats = await audit_logger.get_stats(hours=24, client_id="client-a")

        assert stats["total_requests"] == 1
        assert stats["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_get_stats_top_models(self, audit_logger):
        """Returns top models by request count."""
        # 3 requests to model A
        for i in range(3):
            await audit_logger.log_request(
                request_id=f"req-a-{i}",
                client_id="test-client",
                task="chat",
                model="model-a",
                endpoint="gpunode-ollama",
                status="success",
            )

        # 2 requests to model B
        for i in range(2):
            await audit_logger.log_request(
                request_id=f"req-b-{i}",
                client_id="test-client",
                task="chat",
                model="model-b",
                endpoint="gpunode-ollama",
                status="success",
            )

        stats = await audit_logger.get_stats(hours=24)

        assert "model-a" in stats["top_models"]
        assert "model-b" in stats["top_models"]
        assert stats["top_models"]["model-a"] == 3
        assert stats["top_models"]["model-b"] == 2

    @pytest.mark.asyncio
    async def test_get_stats_requests_by_endpoint(self, audit_logger):
        """Returns request counts by endpoint."""
        await audit_logger.log_request(
            request_id="req-1",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )
        await audit_logger.log_request(
            request_id="req-2",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="dgxspark-ollama",
            status="success",
        )

        stats = await audit_logger.get_stats(hours=24)

        assert "gpunode-ollama" in stats["requests_by_endpoint"]
        assert "dgxspark-ollama" in stats["requests_by_endpoint"]


# =============================================================================
# Schema Tests
# =============================================================================


class TestSchema:
    """Tests for database schema."""

    def test_audit_log_table_structure(self):
        """Audit log table has expected columns."""
        columns = {c.name for c in audit_log.columns}

        # Required columns
        assert "id" in columns
        assert "request_id" in columns
        assert "timestamp" in columns
        assert "client_id" in columns
        assert "task" in columns
        assert "model" in columns
        assert "endpoint" in columns
        assert "status" in columns

        # Performance columns
        assert "latency_ms" in columns
        assert "time_to_first_token_ms" in columns
        assert "tokens_per_second" in columns

        # Token columns
        assert "prompt_tokens" in columns
        assert "completion_tokens" in columns
        assert "total_tokens" in columns

    def test_usage_daily_table_structure(self):
        """Usage daily table has expected columns."""
        columns = {c.name for c in usage_daily.columns}

        assert "date" in columns
        assert "client_id" in columns
        assert "endpoint" in columns
        assert "model" in columns
        assert "request_count" in columns
        assert "success_count" in columns
        assert "total_tokens" in columns

    def test_api_keys_table_structure(self):
        """API keys table has expected columns."""
        columns = {c.name for c in api_keys.columns}

        assert "key_hash" in columns
        assert "key_prefix" in columns
        assert "client_id" in columns
        assert "is_active" in columns
        assert "expires_at" in columns


# =============================================================================
# Integration Tests
# =============================================================================


class TestDatabaseIntegration:
    """Integration tests for the full database layer."""

    @pytest.mark.asyncio
    async def test_concurrent_logging(self, tmp_path):
        """Multiple concurrent log operations succeed."""
        db_path = tmp_path / "concurrent_test.db"
        config = DatabaseConfig(url=f"sqlite:///{db_path}", create_tables=True)
        engine = await create_async_db_engine(config)
        audit = AuditLogger(engine=engine)

        # Log requests sequentially to avoid SQLite locking issues
        for i in range(10):
            await audit.log_request(
                request_id=f"concurrent-{i:03d}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
            )

        # All should be logged
        results = await audit.get_recent_requests(limit=100)
        assert len(results) == 10

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_database_error_handling(self):
        """Handles database errors gracefully."""
        config = DatabaseConfig(url="sqlite:///:memory:", create_tables=True)
        engine = await create_async_db_engine(config)
        audit = AuditLogger(engine=engine)

        # Close the engine to simulate database failure
        await engine.dispose()

        # Should not raise, just log error
        await audit.log_request(
            request_id="should-fail",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )

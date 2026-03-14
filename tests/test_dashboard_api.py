"""Tests for Dashboard API endpoints.

Tests cover:
- GET /api/stats - Usage statistics
- GET /api/requests - Request listing
- GET /api/requests/{id} - Request details
- GET /api/models/usage - Model usage breakdown
- GET /api/endpoints/usage - Endpoint usage breakdown
- GET /api/usage/daily - Daily aggregated usage
- POST /api/usage/aggregate - Manual aggregation trigger
"""

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.main import create_app
from gateway.settings import Settings
from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine


@pytest.fixture
async def db_engine(tmp_path):
    """Create file-based database for testing."""
    db_path = tmp_path / "test_dashboard.db"
    config = DatabaseConfig(url=f"sqlite:///{db_path}", create_tables=True)
    engine = await create_async_db_engine(config)
    yield engine
    await engine.dispose()


@pytest.fixture
def audit_logger(db_engine):
    """Create AuditLogger instance."""
    return AuditLogger(engine=db_engine)


@pytest.fixture
async def test_client(db_engine, audit_logger):
    """Create async test client with configured audit logger."""
    settings = Settings(debug=True)
    app = create_app(settings)

    # Inject audit logger into app state
    app.state.db_engine = db_engine
    app.state.audit_logger = audit_logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestStatsEndpoint:
    """Tests for GET /api/stats."""

    @pytest.mark.asyncio
    async def test_stats_empty(self, test_client):
        """Returns zero stats when no requests logged."""
        response = await test_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["total_requests"] == 0
        assert data["success_count"] == 0
        assert data["error_count"] == 0
        assert data["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_stats_with_data(self, test_client, audit_logger):
        """Returns correct stats with logged requests."""
        # Log some requests
        for i in range(5):
            await audit_logger.log_request(
                request_id=f"stats-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
                prompt_tokens=100,
                completion_tokens=50,
                latency_ms=100.0 + i * 10,
            )

        # Log an error
        await audit_logger.log_request(
            request_id="stats-error",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
        )

        response = await test_client.get("/api/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["total_requests"] == 6
        assert data["success_count"] == 5
        assert data["error_count"] == 1
        assert data["total_tokens"] == 750  # 5 * 150

    @pytest.mark.asyncio
    async def test_stats_with_hours_filter(self, test_client):
        """Respects hours parameter."""
        response = await test_client.get("/api/stats?hours=1")

        assert response.status_code == 200
        data = response.json()
        assert data["period_hours"] == 1


class TestRequestsListEndpoint:
    """Tests for GET /api/requests."""

    @pytest.mark.asyncio
    async def test_requests_empty(self, test_client):
        """Returns empty list when no requests."""
        response = await test_client.get("/api/requests")

        assert response.status_code == 200
        data = response.json()
        assert data["requests"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_requests_list(self, test_client, audit_logger):
        """Returns list of recent requests."""
        # Log requests
        for i in range(3):
            await audit_logger.log_request(
                request_id=f"list-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
            )

        response = await test_client.get("/api/requests")

        assert response.status_code == 200
        data = response.json()
        assert len(data["requests"]) == 3

    @pytest.mark.asyncio
    async def test_requests_limit(self, test_client, audit_logger):
        """Respects limit parameter."""
        for i in range(10):
            await audit_logger.log_request(
                request_id=f"limit-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success",
            )

        response = await test_client.get("/api/requests?limit=5")

        assert response.status_code == 200
        data = response.json()
        assert len(data["requests"]) == 5
        assert data["limit"] == 5

    @pytest.mark.asyncio
    async def test_requests_filter_status(self, test_client, audit_logger):
        """Filters by status."""
        await audit_logger.log_request(
            request_id="success-1",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
        )
        await audit_logger.log_request(
            request_id="error-1",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="error",
        )

        response = await test_client.get("/api/requests?filter_status=error")

        assert response.status_code == 200
        data = response.json()
        assert len(data["requests"]) == 1
        assert data["requests"][0]["status"] == "error"


class TestRequestDetailEndpoint:
    """Tests for GET /api/requests/{request_id}."""

    @pytest.mark.asyncio
    async def test_request_detail(self, test_client, audit_logger):
        """Returns full request details."""
        await audit_logger.log_request(
            request_id="detail-test",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="gpunode-ollama",
            status="success",
            latency_ms=123.45,
            prompt_tokens=100,
            completion_tokens=50,
        )

        response = await test_client.get("/api/requests/detail-test")

        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "detail-test"
        assert data["client_id"] == "test-client"
        assert data["task"] == "chat"
        assert data["model"] == "phi4:14b"
        assert data["latency_ms"] == 123.45

    @pytest.mark.asyncio
    async def test_request_not_found(self, test_client):
        """Returns 404 for non-existent request."""
        response = await test_client.get("/api/requests/nonexistent")

        assert response.status_code == 404


class TestModelsUsageEndpoint:
    """Tests for GET /api/models/usage."""

    @pytest.mark.asyncio
    async def test_models_usage_empty(self, test_client):
        """Returns empty list when no requests."""
        response = await test_client.get("/api/models/usage")

        assert response.status_code == 200
        data = response.json()
        assert data["models"] == []

    @pytest.mark.asyncio
    async def test_models_usage(self, test_client, audit_logger):
        """Returns per-model usage."""
        # Log requests for different models
        for i in range(3):
            await audit_logger.log_request(
                request_id=f"model-a-{i}",
                client_id="test-client",
                task="chat",
                model="model-a",
                endpoint="gpunode-ollama",
                status="success",
                prompt_tokens=50,
                completion_tokens=50,
            )

        for i in range(2):
            await audit_logger.log_request(
                request_id=f"model-b-{i}",
                client_id="test-client",
                task="chat",
                model="model-b",
                endpoint="gpunode-ollama",
                status="success",
                prompt_tokens=25,
                completion_tokens=25,
            )

        response = await test_client.get("/api/models/usage")

        assert response.status_code == 200
        data = response.json()
        assert len(data["models"]) == 2

        # Should be sorted by request count
        assert data["models"][0]["model"] == "model-a"
        assert data["models"][0]["request_count"] == 3


class TestEndpointsUsageEndpoint:
    """Tests for GET /api/endpoints/usage."""

    @pytest.mark.asyncio
    async def test_endpoints_usage_empty(self, test_client):
        """Returns empty list when no requests."""
        response = await test_client.get("/api/endpoints/usage")

        assert response.status_code == 200
        data = response.json()
        assert data["endpoints"] == []

    @pytest.mark.asyncio
    async def test_endpoints_usage(self, test_client, audit_logger):
        """Returns per-endpoint usage."""
        # Log requests for different endpoints
        for i in range(3):
            await audit_logger.log_request(
                request_id=f"endpoint-a-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="endpoint-a",
                status="success",
            )

        await audit_logger.log_request(
            request_id="endpoint-b-0",
            client_id="test-client",
            task="chat",
            model="phi4:14b",
            endpoint="endpoint-b",
            status="success",
        )

        response = await test_client.get("/api/endpoints/usage")

        assert response.status_code == 200
        data = response.json()
        assert len(data["endpoints"]) == 2


class TestDailyUsageEndpoint:
    """Tests for GET /api/usage/daily."""

    @pytest.mark.asyncio
    async def test_daily_usage_empty(self, test_client):
        """Returns empty list when no aggregated data."""
        response = await test_client.get("/api/usage/daily")

        assert response.status_code == 200
        data = response.json()
        assert data["usage"] == []


class TestAggregationEndpoint:
    """Tests for POST /api/usage/aggregate."""

    @pytest.mark.asyncio
    async def test_aggregate_yesterday(self, test_client, audit_logger):
        """Aggregates yesterday's data."""
        response = await test_client.post("/api/usage/aggregate")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "date" in data

    @pytest.mark.asyncio
    async def test_aggregate_invalid_date(self, test_client):
        """Returns error for invalid date format."""
        response = await test_client.post("/api/usage/aggregate?date=invalid")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "Invalid date format" in data["message"]


class TestDashboardIntegration:
    """Integration tests for dashboard workflow."""

    @pytest.mark.asyncio
    async def test_full_dashboard_workflow(self, test_client, audit_logger):
        """Tests complete dashboard data flow."""
        # 1. Log some requests
        for i in range(5):
            await audit_logger.log_request(
                request_id=f"workflow-{i}",
                client_id="test-client",
                task="chat",
                model="phi4:14b",
                endpoint="gpunode-ollama",
                status="success" if i < 4 else "error",
                latency_ms=100.0 + i * 20,
                prompt_tokens=100,
                completion_tokens=50,
            )

        # 2. Check stats
        stats = (await test_client.get("/api/stats")).json()
        assert stats["total_requests"] == 5
        assert stats["success_rate"] == 80.0  # 4/5

        # 3. List requests
        requests = (await test_client.get("/api/requests")).json()
        assert len(requests["requests"]) == 5

        # 4. Get request detail
        detail = (await test_client.get("/api/requests/workflow-0")).json()
        assert detail["status"] == "success"

        # 5. Check model usage
        models = (await test_client.get("/api/models/usage")).json()
        assert len(models["models"]) == 1
        assert models["models"][0]["model"] == "phi4:14b"

        # 6. Check endpoint usage
        endpoints = (await test_client.get("/api/endpoints/usage")).json()
        assert len(endpoints["endpoints"]) == 1
        assert endpoints["endpoints"][0]["endpoint"] == "gpunode-ollama"

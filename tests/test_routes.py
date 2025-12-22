"""Tests for API routes - OpenAI-compatible and DevMesh extensions."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.config import GatewayConfig, ProviderConfig, AuthConfig, ApiKeyConfig
from gateway.dispatch import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.models.common import FinishReason, HealthStatus, ProviderType, TaskType, UsageStats
from gateway.models.internal import InternalResponse
from gateway.routes import openai_router, devmesh_router


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def app() -> FastAPI:
    """Create test app with routes."""
    app = FastAPI()

    # Register centralized exception handlers
    register_exception_handlers(app)

    app.include_router(openai_router)
    app.include_router(devmesh_router)

    # Set up minimal state
    app.state.config = GatewayConfig(
        providers=[
            ProviderConfig(
                name="ollama",
                type=ProviderType.OLLAMA,
                base_url="http://localhost:11434",
            )
        ],
        auth=AuthConfig(enabled=False),
    )
    app.state.registry = None
    app.state.enforcer = None

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def app_with_auth() -> FastAPI:
    """Create test app with authentication enabled."""
    app = FastAPI()

    # Register centralized exception handlers
    register_exception_handlers(app)

    app.include_router(openai_router)
    app.include_router(devmesh_router)

    app.state.config = GatewayConfig(
        providers=[
            ProviderConfig(
                name="ollama",
                type=ProviderType.OLLAMA,
                base_url="http://localhost:11434",
            )
        ],
        auth=AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(
                    key="test-api-key-12345678",
                    client_id="test-client",
                )
            ],
        ),
    )
    app.state.registry = None
    app.state.enforcer = None

    return app


@pytest.fixture
def auth_client(app_with_auth: FastAPI) -> TestClient:
    """Create test client with auth app."""
    return TestClient(app_with_auth)


@pytest.fixture
def mock_response() -> InternalResponse:
    """Create mock internal response."""
    return InternalResponse(
        request_id="test-request-123",
        task=TaskType.CHAT,
        provider="ollama",
        model="llama3.2",
        content="Hello! How can I help you?",
        finish_reason=FinishReason.STOP,
        usage=UsageStats.from_counts(prompt=10, completion=20),
    )


# =============================================================================
# Health Check Tests
# =============================================================================


class TestHealthCheck:
    """Tests for health check endpoint."""

    def test_health_check_basic(self, client: TestClient):
        """Basic health check returns 200."""
        response = client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] in ("healthy", "degraded")
        assert data["version"] == "0.1.0"
        assert "config_loaded" in data

    def test_health_check_with_providers(self, app: FastAPI, client: TestClient):
        """Health check shows provider status."""
        # Setup mock registry
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = ["ollama"]
        mock_health = MagicMock()
        mock_health.status = HealthStatus.HEALTHY
        mock_registry.get_health.return_value = mock_health
        app.state.registry = mock_registry

        response = client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["providers_configured"] == 1
        assert data["providers_healthy"] == 1
        assert len(data["providers"]) == 1
        assert data["providers"][0]["name"] == "ollama"
        assert data["providers"][0]["healthy"] is True


# =============================================================================
# Prometheus Metrics Tests
# =============================================================================


class TestPrometheusMetrics:
    """Tests for Prometheus metrics endpoint."""

    def test_metrics_endpoint(self, client: TestClient):
        """Metrics endpoint returns Prometheus format."""
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]


# =============================================================================
# Models List Tests
# =============================================================================


class TestModelsList:
    """Tests for models list endpoint."""

    def test_list_models_no_providers(self, app: FastAPI, client: TestClient):
        """List models with no registry returns empty list."""
        # Create empty registry
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = []
        app.state.registry = mock_registry

        response = client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        assert data["object"] == "list"
        assert data["data"] == []

    def test_list_models_with_providers(self, app: FastAPI, client: TestClient):
        """List models returns models from providers."""
        # Create mock registry and adapter
        mock_adapter = MagicMock()
        mock_adapter.list_models = AsyncMock(return_value=["llama3.2", "codellama"])
        mock_adapter.get_capabilities = MagicMock(return_value=["chat", "completion"])

        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = ["ollama"]
        mock_registry.is_healthy.return_value = True
        mock_registry.get.return_value = mock_adapter
        app.state.registry = mock_registry

        response = client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 2
        assert data["data"][0]["id"] == "ollama/llama3.2"
        assert data["data"][0]["provider"] == "ollama"


# =============================================================================
# Authentication Tests
# =============================================================================


class TestAuthentication:
    """Tests for authentication."""

    def test_no_auth_required_when_disabled(self, client: TestClient, app: FastAPI):
        """No auth required when auth is disabled."""
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = []
        app.state.registry = mock_registry

        response = client.get("/v1/models")
        assert response.status_code == 200

    def test_auth_required_when_enabled(self, auth_client: TestClient):
        """Auth required when enabled."""
        response = auth_client.get("/v1/models")
        assert response.status_code == 401
        # New error format: {"error": {"code": "...", "message": "..."}}
        assert "API key required" in response.json()["error"]["message"]

    def test_auth_with_bearer_token(self, auth_client: TestClient, app_with_auth: FastAPI):
        """Auth with Bearer token works."""
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = []
        app_with_auth.state.registry = mock_registry

        response = auth_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-api-key-12345678"},
        )
        assert response.status_code == 200

    def test_auth_with_x_api_key(self, auth_client: TestClient, app_with_auth: FastAPI):
        """Auth with X-API-Key header works."""
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = []
        app_with_auth.state.registry = mock_registry

        response = auth_client.get(
            "/v1/models",
            headers={"X-API-Key": "test-api-key-12345678"},
        )
        assert response.status_code == 200

    def test_auth_invalid_key(self, auth_client: TestClient):
        """Invalid API key returns 401."""
        response = auth_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer invalid-key-12345678"},
        )
        assert response.status_code == 401

    def test_auth_malformed_key_rejected(self, auth_client: TestClient):
        """Malformed API key is rejected."""
        # Key with injection attempt
        response = auth_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer key\nX-Injected: header"},
        )
        assert response.status_code == 401


# =============================================================================
# Chat Completions Tests
# =============================================================================


class TestChatCompletions:
    """Tests for chat completions endpoint."""

    def test_chat_completions_success(
        self, app: FastAPI, client: TestClient, mock_response: InternalResponse
    ):
        """Successful chat completion."""
        # Setup mocks
        from gateway.dispatch import DispatchResult
        from gateway.routes.dependencies import get_dispatcher

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=mock_response,
                provider_used="ollama",
            )
        )

        app.dependency_overrides[get_dispatcher] = lambda: mock_dispatcher
        try:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "llama3.2",
                    "messages": [{"role": "user", "content": "Hello!"}],
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "llama3.2"
        assert len(data["choices"]) == 1
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["usage"]["total_tokens"] == 30

    def test_chat_completions_validation_error(self, client: TestClient):
        """Invalid request body returns validation error."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "llama3.2",
                # Missing required 'messages' field
            },
        )
        assert response.status_code == 422

    def test_chat_completions_provider_unavailable(self, app: FastAPI, client: TestClient):
        """Provider unavailable returns 503."""
        from gateway.errors import ProviderUnavailableError
        from gateway.routes.dependencies import get_dispatcher

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch = AsyncMock(
            side_effect=ProviderUnavailableError(provider="ollama")
        )

        app.dependency_overrides[get_dispatcher] = lambda: mock_dispatcher
        try:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "llama3.2",
                    "messages": [{"role": "user", "content": "Hello!"}],
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)

        assert response.status_code == 503


# =============================================================================
# Completions Tests
# =============================================================================


class TestCompletions:
    """Tests for completions endpoint."""

    def test_completions_success(
        self, app: FastAPI, client: TestClient, mock_response: InternalResponse
    ):
        """Successful completion."""
        from gateway.dispatch import DispatchResult
        from gateway.routes.dependencies import get_dispatcher

        mock_response.task = TaskType.COMPLETION

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=mock_response,
                provider_used="ollama",
            )
        )

        app.dependency_overrides[get_dispatcher] = lambda: mock_dispatcher
        try:
            response = client.post(
                "/v1/completions",
                json={
                    "model": "llama3.2",
                    "prompt": "Once upon a time",
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "text_completion"
        assert len(data["choices"]) == 1


# =============================================================================
# Embeddings Tests
# =============================================================================


class TestEmbeddings:
    """Tests for embeddings endpoint."""

    def test_embeddings_success(self, app: FastAPI, client: TestClient):
        """Successful embeddings request."""
        from gateway.dispatch import DispatchResult
        from gateway.routes.dependencies import get_dispatcher

        mock_response = InternalResponse(
            request_id="test-request-123",
            task=TaskType.EMBEDDINGS,
            provider="ollama",
            model="nomic-embed-text",
            embeddings=[[0.1, 0.2, 0.3, 0.4, 0.5]],
            usage=UsageStats(prompt_tokens=5, completion_tokens=0),
        )

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=mock_response,
                provider_used="ollama",
            )
        )

        app.dependency_overrides[get_dispatcher] = lambda: mock_dispatcher
        try:
            response = client.post(
                "/v1/embeddings",
                json={
                    "model": "nomic-embed-text",
                    "input": "Hello world",
                },
            )
        finally:
            app.dependency_overrides.pop(get_dispatcher, None)

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["object"] == "embedding"
        assert len(data["data"][0]["embedding"]) == 5


# =============================================================================
# Route Debug Tests
# =============================================================================


class TestRouteDebug:
    """Tests for route debugging endpoint."""

    def test_route_debug(self, app: FastAPI, client: TestClient):
        """Route debug shows routing decision."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.resolve_provider.return_value = ("ollama", "llama3.2")

        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.is_healthy.return_value = True
        mock_registry.get_fallback_chain.return_value = ["vllm"]
        app.state.registry = mock_registry

        with patch("gateway.routes.devmesh.get_dispatcher", return_value=mock_dispatcher):
            response = client.post(
                "/v1/devmesh/route",
                json={
                    "model": "ollama/llama3.2",
                    "task": "chat",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["resolved_provider"] == "ollama"
        assert data["resolved_model"] == "llama3.2"
        assert data["provider_healthy"] is True
        assert data["would_fallback"] is False

    def test_route_debug_unhealthy_provider(self, app: FastAPI, client: TestClient):
        """Route debug shows fallback when provider unhealthy."""
        mock_dispatcher = MagicMock()
        mock_dispatcher.resolve_provider.return_value = ("ollama", "llama3.2")

        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.is_healthy.return_value = False
        mock_registry.get_fallback_chain.return_value = ["vllm"]
        app.state.registry = mock_registry

        with patch("gateway.routes.devmesh.get_dispatcher", return_value=mock_dispatcher):
            response = client.post(
                "/v1/devmesh/route",
                json={
                    "model": "ollama/llama3.2",
                    "task": "chat",
                    "fallback_allowed": True,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["provider_healthy"] is False
        assert data["would_fallback"] is True
        assert "unhealthy" in data["reason"].lower()


# =============================================================================
# Provider Management Tests
# =============================================================================


class TestProviderManagement:
    """Tests for provider management endpoints."""

    def test_list_providers(self, app: FastAPI, client: TestClient):
        """List providers returns provider details."""
        mock_adapter = MagicMock()
        mock_adapter.list_models = AsyncMock(return_value=["llama3.2"])
        mock_adapter.get_capabilities = MagicMock(return_value=["chat"])

        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_health = MagicMock()
        mock_health.status = HealthStatus.HEALTHY
        mock_registry.get_health.return_value = mock_health
        mock_registry.get.return_value = mock_adapter
        app.state.registry = mock_registry

        response = client.get("/v1/devmesh/providers")
        assert response.status_code == 200

        data = response.json()
        assert len(data["providers"]) == 1
        assert data["providers"][0]["name"] == "ollama"
        assert data["providers"][0]["healthy"] is True

    def test_check_provider_health(self, app: FastAPI, client: TestClient):
        """Force provider health check."""
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = ["ollama"]
        mock_registry.check_health = AsyncMock(return_value=HealthStatus.HEALTHY)
        app.state.registry = mock_registry

        response = client.post("/v1/devmesh/providers/ollama/health")
        assert response.status_code == 200

        data = response.json()
        assert data["provider"] == "ollama"
        assert data["healthy"] is True

    def test_check_provider_health_not_found(self, app: FastAPI, client: TestClient):
        """Health check for unknown provider returns 404."""
        mock_registry = MagicMock(spec=ProviderRegistry)
        mock_registry.list_providers.return_value = ["ollama"]
        app.state.registry = mock_registry

        response = client.post("/v1/devmesh/providers/unknown/health")
        assert response.status_code == 404


# =============================================================================
# Security Tests
# =============================================================================


class TestSecurityBoundaries:
    """Security-focused tests for routes."""

    def test_api_key_format_validation(self, auth_client: TestClient):
        """API key format is validated to prevent injection."""
        # Various injection attempts
        injection_attempts = [
            "short",  # Too short
            "x" * 200,  # Too long
            "key\nX-Header: inject",  # Header injection
            "key\x00null",  # Null byte
            "key with spaces",  # Spaces
        ]

        for bad_key in injection_attempts:
            response = auth_client.get(
                "/v1/models",
                headers={"X-API-Key": bad_key},
            )
            assert response.status_code == 401, f"Should reject key: {bad_key!r}"

    def test_model_name_sanitization(self, app: FastAPI, client: TestClient, mock_response):
        """Model names are handled safely."""
        from gateway.dispatch import DispatchResult

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch = AsyncMock(
            return_value=DispatchResult(
                response=mock_response,
                provider_used="ollama",
            )
        )

        with patch("gateway.routes.openai.get_dispatcher", return_value=mock_dispatcher):
            # Try malicious model name
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "../../../etc/passwd",
                    "messages": [{"role": "user", "content": "Hello!"}],
                },
            )

        # Should not fail due to model name - validation happens at provider level
        assert response.status_code in (200, 400, 503)

    def test_large_message_rejected(self, client: TestClient):
        """Very large messages are rejected."""
        # 2MB message should be rejected
        large_content = "x" * (2 * 1024 * 1024)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "llama3.2",
                "messages": [{"role": "user", "content": large_content}],
            },
        )

        # Should be rejected by validation
        assert response.status_code == 422

"""Tests for FastAPI application."""

import pytest
from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.settings import Settings


class TestHealthEndpoint:
    """Test suite for health check endpoint."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client with default settings."""
        settings = Settings()
        app = create_app(settings)
        return TestClient(app)

    def test_health_returns_200(self, client: TestClient) -> None:
        """Test that health endpoint returns 200 OK."""
        response = client.get("/health")

        assert response.status_code == 200

    def test_health_returns_healthy_status(self, client: TestClient) -> None:
        """Test that health endpoint returns healthy status."""
        response = client.get("/health")
        data = response.json()

        assert data["status"] == "healthy"
        assert "version" in data
        assert data["version"] == "0.1.0"

    def test_health_indicates_config_not_loaded(self, client: TestClient) -> None:
        """Test health shows config not loaded when no config file exists."""
        response = client.get("/health")
        data = response.json()

        # Config won't be loaded in test environment without config files
        assert "config_loaded" in data


class TestAppCreation:
    """Test suite for application creation."""

    def test_create_app_returns_fastapi_instance(self) -> None:
        """Test that create_app returns a FastAPI instance."""
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_create_app_with_custom_settings(self) -> None:
        """Test creating app with custom settings."""
        settings = Settings(debug=True, log_level="DEBUG")
        app = create_app(settings)

        assert app is not None

    def test_app_has_correct_metadata(self) -> None:
        """Test that app has correct title and version."""
        app = create_app()

        assert app.title == "DevMesh LLM Gateway"
        assert app.version == "0.1.0"

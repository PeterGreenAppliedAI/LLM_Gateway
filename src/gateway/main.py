"""FastAPI application entry point.

Per API Error Handling Architecture:
- Exception handlers are registered for centralized error handling
- Domain errors (GatewayError subclasses) are translated to HTTP responses

Per Endpoints/Environments Architecture:
- Starts model discovery service on startup
- Integrates catalog with registry
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI

from gateway.catalog import ModelCatalog, ModelDiscoveryService
from gateway.config import GatewayConfig, load_config
from gateway.dispatch import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.settings import Settings, get_settings
from gateway.routes import openai_router, devmesh_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    settings = get_settings()
    config_path = Path(settings.config_path)
    providers_path = Path(settings.providers_config_path)

    # Load config if files exist, otherwise use defaults for testing
    if config_path.exists():
        config = load_config(config_path, providers_path if providers_path.exists() else None)
        app.state.config = config
    else:
        app.state.config = GatewayConfig()

    app.state.settings = settings

    # Initialize registry and discovery service if endpoints are configured
    if app.state.config and app.state.config.endpoints:
        registry = ProviderRegistry(app.state.config)
        await registry.initialize()
        app.state.registry = registry

        # Start model discovery service
        discovery = ModelDiscoveryService(
            endpoints=app.state.config.get_enabled_endpoints(),
            catalog=registry.catalog,
            discovery_interval=60.0,  # Discover every minute
        )
        await discovery.start()
        app.state.discovery_service = discovery

    yield

    # Shutdown
    if hasattr(app.state, "discovery_service"):
        await app.state.discovery_service.stop()

    if hasattr(app.state, "registry"):
        await app.state.registry.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="DevMesh LLM Gateway",
        description="AI control plane for inference runtimes",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Register centralized exception handlers
    # Per API Error Handling Architecture: single choke point for error translation
    register_exception_handlers(app)

    # Include routers
    app.include_router(openai_router)
    app.include_router(devmesh_router)

    return app


# Application instance for uvicorn
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )

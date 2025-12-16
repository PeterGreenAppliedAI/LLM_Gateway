"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from gateway.config import GatewayConfig, load_config
from gateway.settings import Settings, get_settings


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
        app.state.config = None

    app.state.settings = settings

    yield

    # Shutdown
    pass


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

    @app.get("/health", status_code=status.HTTP_200_OK)
    async def health_check() -> JSONResponse:
        """Health check endpoint."""
        config: GatewayConfig | None = getattr(app.state, "config", None)

        health_status = {
            "status": "healthy",
            "version": "0.1.0",
            "config_loaded": config is not None,
        }

        if config:
            health_status["providers_configured"] = len(config.providers)
            health_status["providers_enabled"] = len(config.get_enabled_providers())

        return JSONResponse(content=health_status)

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

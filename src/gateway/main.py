"""FastAPI application entry point.

Per API Error Handling Architecture:
- Exception handlers are registered for centralized error handling
- Domain errors (GatewayError subclasses) are translated to HTTP responses

Per Endpoints/Environments Architecture:
- Starts model discovery service on startup
- Integrates catalog with registry

Per Database Architecture:
- Initializes database engine and AuditLogger on startup
- SQLite default, PostgreSQL production-ready
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.catalog import ModelCatalog, ModelDiscoveryService
from gateway.config import GatewayConfig, load_config
from gateway.dispatch import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.observability import get_logger
from gateway.security import AsyncSecurityAnalyzer
from gateway.security.guard import create_guard_client
from gateway.settings import Settings, get_settings
from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine
from gateway.routes import openai_router, devmesh_router, ollama_router

logger = get_logger(__name__)


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

    # Initialize database and audit logger
    db_config = DatabaseConfig(
        url=settings.db.url,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.max_overflow,
        pool_timeout=settings.db.pool_timeout,
        pool_recycle=settings.db.pool_recycle,
        store_request_body=settings.db.store_request_body,
        store_response_body=settings.db.store_response_body,
        create_tables=settings.db.create_tables,
        echo=settings.db.echo,
    )

    try:
        db_engine = await create_async_db_engine(db_config)
        app.state.db_engine = db_engine

        audit_logger = AuditLogger(
            engine=db_engine,
            store_request_body=settings.db.store_request_body,
            store_response_body=settings.db.store_response_body,
        )
        app.state.audit_logger = audit_logger
        logger.info(f"Database initialized: {settings.db.url.split('://')[0]}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        # Continue without database - audit logging will be disabled
        app.state.db_engine = None
        app.state.audit_logger = None

    # Initialize registry and discovery service if endpoints are configured
    if app.state.config and app.state.config.endpoints:
        registry = ProviderRegistry(app.state.config)
        await registry.initialize()
        await registry.start_health_monitoring()
        app.state.registry = registry

        # Start model discovery service
        discovery = ModelDiscoveryService(
            endpoints=app.state.config.get_enabled_endpoints(),
            catalog=registry.catalog,
            discovery_interval=60.0,  # Discover every minute
        )
        await discovery.start()
        app.state.discovery_service = discovery

    # Start security analyzer (async background analysis)
    guard_client = None
    if settings.guard.enabled:
        guard_client = create_guard_client(
            base_url=settings.guard.base_url,
            model_name=settings.guard.model_name,
            timeout=settings.guard.timeout,
        )
        logger.info(
            "Guard model enabled (shadow mode)",
            model=settings.guard.model_name,
            base_url=settings.guard.base_url,
        )

    scan_allowlist_ips = settings.security.scan_allowlist_ips
    security_analyzer = AsyncSecurityAnalyzer(
        guard_client=guard_client,
        scan_allowlist_ips=scan_allowlist_ips,
    )
    await security_analyzer.start()
    app.state.security_analyzer = security_analyzer
    if scan_allowlist_ips:
        logger.info("Security scan allowlist", allowlisted_ips=scan_allowlist_ips)
    logger.info("Security analyzer started")

    # Initialize PII scrubber
    if settings.pii.enabled:
        from gateway.security.pii import PIIScrubber
        pii_scrubber = PIIScrubber()
        app.state.pii_scrubber = pii_scrubber
        app.state.pii_settings = settings.pii
        logger.info(
            "PII detection enabled",
            scrub_enabled=settings.pii.scrub_enabled,
            scrub_routes=settings.pii.scrub_routes or ["all"],
        )

    # Start periodic audit log cleanup
    retention_days = settings.db.retention_days
    if app.state.audit_logger and retention_days > 0:
        async def _cleanup_loop() -> None:
            while True:
                await asyncio.sleep(86400)  # Run daily
                await app.state.audit_logger.cleanup_old_records(retention_days)

        app.state._cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("Audit log retention policy", retention_days=retention_days)

    yield

    # Shutdown
    if hasattr(app.state, "_cleanup_task"):
        app.state._cleanup_task.cancel()
        try:
            await app.state._cleanup_task
        except asyncio.CancelledError:
            pass

    if hasattr(app.state, "security_analyzer"):
        await app.state.security_analyzer.stop()
        logger.info("Security analyzer stopped")

    if hasattr(app.state, "discovery_service"):
        await app.state.discovery_service.stop()

    if hasattr(app.state, "registry"):
        await app.state.registry.close()

    # Dispose async database engine
    if hasattr(app.state, "db_engine") and app.state.db_engine:
        await app.state.db_engine.dispose()
        logger.info("Database engine disposed")


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

    # Add CORS middleware for dashboard
    # Configure via GATEWAY_CORS_ORIGINS env var (JSON list)
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register centralized exception handlers
    # Per API Error Handling Architecture: single choke point for error translation
    register_exception_handlers(app)

    # Include routers
    app.include_router(openai_router)
    app.include_router(devmesh_router)
    app.include_router(ollama_router)

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

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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.catalog import ModelDiscoveryService
from gateway.config import GatewayConfig, load_config
from gateway.dispatch import ProviderRegistry
from gateway.exception_handlers import register_exception_handlers
from gateway.observability import get_logger
from gateway.routes import audio_router, devmesh_router, ollama_router, openai_router
from gateway.security import AsyncSecurityAnalyzer
from gateway.security.guard import create_guard_client
from gateway.settings import Settings, get_settings
from gateway.storage import AuditLogger, DatabaseConfig, SecurityScanStore, create_async_db_engine

logger = get_logger(__name__)


async def _load_saved_pii_scrub(app: FastAPI) -> None:
    """Apply a dashboard-saved PII scrubbing policy, if one exists."""
    from gateway.security.pii_config import SETTING_KEY, PIIScrubConfig

    try:
        saved = await app.state.runtime_settings.get(SETTING_KEY)
        if saved is None:
            return
        app.state.pii_settings = PIIScrubConfig.from_saved(saved)
        logger.info(
            "PII scrubbing policy loaded from dashboard setting",
            scrub_enabled=app.state.pii_settings.scrub_enabled,
            scrub_routes=app.state.pii_settings.scrub_routes or ["all"],
            updated_by=app.state.pii_settings.updated_by,
        )
    except Exception:
        # Keep the environment default rather than failing startup
        logger.exception("Saved PII scrubbing policy unreadable; using environment default")


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

    auth = app.state.config.auth
    if auth.enabled and auth.anonymous.enabled and auth.anonymous.unrestricted:
        logger.warning(
            "Keyless inference is unrestricted: any client can omit its key to bypass "
            "per-key model/endpoint allowlists and rate limits. Set auth.anonymous.enabled: "
            "false or restrict auth.anonymous in gateway.yaml."
        )
    if auth.enabled and not settings.admin_api_key:
        logger.warning(
            "GATEWAY_ADMIN_API_KEY is not set: any valid client key can manage keys, "
            "budgets, and security labels."
        )

    # Initialize PII scrubber first: stores below redact with it, so raw PII
    # is never persisted even in flag-only mode (scrub_enabled: false)
    pii_scrubber = None
    if settings.pii.enabled:
        from gateway.security.pii import PIIScrubber
        from gateway.security.pii_config import PIIScrubConfig

        pii_scrubber = PIIScrubber()
        app.state.pii_scrubber = pii_scrubber
        # Environment default; a dashboard-saved value replaces it below
        app.state.pii_settings = PIIScrubConfig(
            scrub_enabled=settings.pii.scrub_enabled,
            scrub_routes=settings.pii.scrub_routes,
        )
        logger.info(
            "PII detection enabled",
            scrub_enabled=settings.pii.scrub_enabled,
            scrub_routes=settings.pii.scrub_routes or ["all"],
        )
    elif settings.db.store_request_body or settings.db.store_response_body:
        logger.warning(
            "Request/response bodies are stored but PII detection is off: stored bodies "
            "may contain raw PII. Set GATEWAY_PII_ENABLED=true to redact them."
        )
    body_redactor = pii_scrubber.redact if pii_scrubber else None

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
            body_redactor=body_redactor,
            spill_path=settings.db.audit_spill_path or None,
        )
        app.state.audit_logger = audit_logger
        logger.info(f"Database initialized: {settings.db.url.split('://')[0]}")
    except Exception as e:
        if settings.db.required:
            # No database means no audit trail, no DB-backed keys and no
            # security scans; serving traffic anyway would be silent
            raise RuntimeError(
                f"Database initialization failed: {e}. Set GATEWAY_DB_REQUIRED=false to "
                "run without an audit trail."
            ) from e
        logger.error(f"Failed to initialize database, continuing WITHOUT audit logging: {e}")
        app.state.db_engine = None
        app.state.audit_logger = None

    # Audit intent log (D-038): requests append to a local log; a background
    # task writes the rows to the database. sync mode keeps direct writes.
    app.state.intent_log = None
    durability = settings.db.audit_durability
    if durability == "auto" and app.state.db_engine is not None:
        # SQLite: one writer at a time, so funnel writes through the log.
        # PostgreSQL handles concurrent writers, and often runs where local
        # disk is ephemeral (containers), so write directly.
        durability = "process" if app.state.db_engine.dialect.name == "sqlite" else "sync"
    if app.state.audit_logger and durability != "sync":
        from gateway.storage.intent_log import IntentLog

        intent_log = IntentLog(
            settings.db.audit_journal_path,
            app.state.db_engine,
            durability=durability,
            max_bytes=settings.db.audit_journal_max_mb * 1024 * 1024,
        )
        await intent_log.start()
        app.state.intent_log = intent_log
        app.state.audit_logger.intent_log = intent_log

    if app.state.audit_logger:
        try:
            replayed = await app.state.audit_logger.replay_spill()
            if replayed:
                logger.warning("Recovered audit rows from spill file", rows=replayed)
        except Exception:
            logger.exception("Audit spill replay failed; rows remain in the spill file")

    # Operator settings saved from the dashboard override env defaults
    if app.state.db_engine is not None:
        from gateway.storage import RuntimeSettingsStore

        app.state.runtime_settings = RuntimeSettingsStore(app.state.db_engine)
        if pii_scrubber is not None:
            await _load_saved_pii_scrub(app)

    # Shared state (D-035): in-memory unless GATEWAY_REDIS_URL is set
    from gateway.state import create_shared_state

    redis_url = settings.redis_url.get_secret_value() if settings.redis_url else None
    admission_cfg = app.state.config.admission if app.state.config else None
    app.state.shared_state = create_shared_state(
        redis_url,
        prefix=settings.redis_prefix,
        batch_max_share=admission_cfg.batch_max_share if admission_cfg else 1.0,
    )

    # Validated-key cache and batched last_used_at (D-040)
    app.state.key_cache = None
    if app.state.db_engine is not None and settings.db.key_cache_seconds > 0:
        from gateway.storage.key_cache import KeyCache

        app.state.key_cache = KeyCache(app.state.db_engine, ttl=settings.db.key_cache_seconds)
        await app.state.key_cache.start()

    # Policy enforcer, built now (not on the first request) so token budgets
    # can load saved tiers and today's usage before traffic arrives (D-037)
    from gateway.routes.dependencies import build_enforcer

    enforcer = build_enforcer(app)
    app.state.budget_sync = None
    if app.state.db_engine is not None:
        from gateway.storage.budgets import BudgetStore, BudgetSync

        app.state.budget_sync = BudgetSync(enforcer.token_budget, app.state.db_engine)
        await app.state.budget_sync.start()
        if settings.db.retention_days > 0:
            await BudgetStore(app.state.db_engine).prune(settings.db.retention_days)

    # Initialize registry and discovery service if endpoints are configured
    if app.state.config and app.state.config.endpoints:
        registry = ProviderRegistry(
            app.state.config, endpoint_slots=app.state.shared_state.endpoint_slots
        )
        await registry.initialize()
        await registry.start_health_monitoring()
        app.state.registry = registry

        # Media catalog: voices/models per media endpoint, enriched by engine
        # profiles (D-020/D-028). A profile name that doesn't exist is a
        # config error, so fail startup rather than serve without it.
        from gateway.media.catalog import MediaCatalog
        from gateway.media.profiles import load_profiles

        profiles = load_profiles(settings.profiles_path)
        missing = sorted(
            {ep.profile for ep in app.state.config.endpoints if ep.profile} - set(profiles)
        )
        if missing:
            raise RuntimeError(
                f"Endpoint profiles not found in {settings.profiles_path}: {missing}"
            )
        if any(ep.capabilities for ep in app.state.config.endpoints):
            media_catalog = MediaCatalog(registry, profiles)
            await media_catalog.start()
            app.state.media_catalog = media_catalog

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

    # Create security scan store for training data collection
    scan_store = None
    if getattr(app.state, "db_engine", None):
        scan_store = SecurityScanStore(
            app.state.db_engine,
            redactor=body_redactor,
            store_messages=settings.security.store_messages,
        )
        app.state.scan_store = scan_store
        logger.info(
            "Security scan store enabled",
            stores_messages=settings.security.store_messages,
            retention_days=settings.security.retention_days,
        )

    scan_allowlist_ips = settings.security.scan_allowlist_ips
    security_analyzer = AsyncSecurityAnalyzer(
        guard_client=guard_client,
        scan_store=scan_store,
        scan_allowlist_ips=scan_allowlist_ips,
    )
    await security_analyzer.start()
    app.state.security_analyzer = security_analyzer
    if scan_allowlist_ips:
        logger.info("Security scan allowlist", allowlisted_ips=scan_allowlist_ips)
    logger.info("Security analyzer started")

    # Retention (D-041): audit rows, security scans, PII events and budget
    # usage each expire; at startup, then daily
    retention_days = settings.db.retention_days
    security_retention = settings.security.retention_days
    if app.state.audit_logger and (retention_days > 0 or security_retention > 0):
        from gateway.storage.budgets import BudgetStore

        async def _cleanup_once() -> None:
            try:
                if retention_days > 0:
                    await app.state.audit_logger.cleanup_old_records(retention_days)
                    await BudgetStore(app.state.db_engine).prune(retention_days)
                if security_retention > 0:
                    await app.state.audit_logger.cleanup_old_pii_events(security_retention)
                    if scan_store is not None:
                        await scan_store.cleanup_old_scans(security_retention)
            except Exception as e:
                logger.error("Retention cleanup failed; will retry tomorrow", error=str(e))

        async def _cleanup_loop() -> None:
            while True:
                await _cleanup_once()
                await asyncio.sleep(86400)  # daily

        app.state._cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info(
            "Retention policy",
            audit_days=retention_days,
            security_scan_and_pii_days=security_retention,
        )

    yield

    # Shutdown
    if hasattr(app.state, "_cleanup_task"):
        app.state._cleanup_task.cancel()
        try:
            await app.state._cleanup_task
        except asyncio.CancelledError:
            pass

    if getattr(app.state, "budget_sync", None) is not None:
        await app.state.budget_sync.stop()  # writes the last batch of usage

    if getattr(app.state, "key_cache", None) is not None:
        await app.state.key_cache.stop()  # writes pending last_used_at

    if hasattr(app.state, "security_analyzer"):
        await app.state.security_analyzer.stop()
        logger.info("Security analyzer stopped")

    if hasattr(app.state, "discovery_service"):
        await app.state.discovery_service.stop()

    if hasattr(app.state, "media_catalog"):
        await app.state.media_catalog.stop()

    if hasattr(app.state, "registry"):
        await app.state.registry.close()

    if getattr(app.state, "shared_state", None) is not None:
        await app.state.shared_state.close()

    # Last writer to stop: everything above may still have logged audit rows
    if getattr(app.state, "intent_log", None) is not None:
        await app.state.intent_log.close()

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
    app.include_router(audio_router)
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

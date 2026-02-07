"""DevMesh extension endpoints.

Per PRD Section 7:
- GET /health (health check)
- GET /metrics (Prometheus)
- GET /v1/models (list available models)
- POST /v1/devmesh/route (debug routing decisions)

These endpoints provide gateway-specific functionality beyond OpenAI compatibility.

Per API Error Handling Architecture:
- Routes raise domain errors (GatewayError subclasses)
- Exception handler middleware translates to HTTP responses
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from gateway.config import GatewayConfig
from gateway.dispatch import Dispatcher, ProviderRegistry
from gateway.errors import DispatchError, ProviderNotFoundError
from gateway.models.common import HealthStatus, TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.observability import get_logger, get_metrics
from gateway.routes.dependencies import (
    authenticate,
    get_audit_logger,
    get_config,
    get_dispatcher,
    get_registry,
    get_security_analyzer,
)
from gateway.security import AsyncSecurityAnalyzer
from gateway.storage import AuditLogger, KeyManager

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = get_logger(__name__)
metrics = get_metrics()

router = APIRouter(tags=["devmesh"])


# =============================================================================
# Health Check
# =============================================================================


class ProviderHealth(BaseModel):
    """Health status for a single provider."""
    name: str
    status: str
    healthy: bool
    last_check: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    config_loaded: bool
    providers_configured: int = 0
    providers_healthy: int = 0
    providers: list[ProviderHealth] = Field(default_factory=list)


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Health check endpoint with detailed provider status.

    Returns:
        Health status of the gateway and all configured providers.
    """
    config: GatewayConfig | None = getattr(request.app.state, "config", None)
    registry: ProviderRegistry | None = getattr(request.app.state, "registry", None)

    providers = []
    providers_healthy = 0

    if registry:
        for name in registry.list_providers():
            health = registry.get_health(name)
            status_enum = health.status if health else HealthStatus.UNKNOWN
            is_healthy = status_enum == HealthStatus.HEALTHY
            if is_healthy:
                providers_healthy += 1

            providers.append(ProviderHealth(
                name=name,
                status=status_enum.value if status_enum else "unknown",
                healthy=is_healthy,
            ))

    return HealthResponse(
        status="healthy" if providers_healthy > 0 or not providers else "degraded",
        version="0.1.0",
        config_loaded=config is not None,
        providers_configured=len(providers),
        providers_healthy=providers_healthy,
        providers=providers,
    )


# =============================================================================
# Prometheus Metrics
# =============================================================================


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint.

    Returns metrics in Prometheus text format for scraping.
    """
    from gateway.errors import ProviderError, ErrorCode

    if not PROMETHEUS_AVAILABLE:
        raise ProviderError(
            message="Prometheus client not installed",
            provider="prometheus",
            code=ErrorCode.PROVIDER_UNAVAILABLE,
        )

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# =============================================================================
# Models List
# =============================================================================


class ModelInfo(BaseModel):
    """Information about a model."""
    id: str
    object: str = "model"
    owned_by: str
    provider: str
    capabilities: list[str] = Field(default_factory=list)


class ModelsResponse(BaseModel):
    """Response for list models endpoint."""
    object: str = "list"
    data: list[ModelInfo]


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> ModelsResponse:
    """List available models across all providers.

    Returns all models from all healthy providers.
    """
    models = []

    for provider_name in registry.list_providers():
        # Skip unhealthy providers
        if not registry.is_healthy(provider_name):
            continue

        adapter = registry.get(provider_name)
        if adapter is None:
            continue

        # Get models from adapter
        try:
            provider_models = await adapter.list_models()
            capabilities = adapter.get_capabilities()

            for model_name in provider_models:
                models.append(ModelInfo(
                    id=f"{provider_name}/{model_name}",
                    owned_by=provider_name,
                    provider=provider_name,
                    capabilities=capabilities,
                ))
        except Exception as e:
            logger.warning(
                f"Failed to list models from provider {provider_name}: {e}"
            )
            continue

    return ModelsResponse(data=models)


# =============================================================================
# Routing Debug
# =============================================================================


class RouteRequest(BaseModel):
    """Request for route debugging."""
    model: str
    task: str = "chat"
    preferred_provider: Optional[str] = None
    fallback_allowed: bool = True


class RouteResponse(BaseModel):
    """Response for route debugging."""
    resolved_provider: str
    resolved_model: str
    fallback_chain: list[str]
    provider_healthy: bool
    would_fallback: bool
    reason: Optional[str] = None


@router.post("/v1/devmesh/route", response_model=RouteResponse)
async def debug_route(
    request: Request,
    body: RouteRequest,
    client_id: Annotated[str, Depends(authenticate)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> RouteResponse:
    """Debug routing decisions.

    Shows which provider and model would be selected for a given request
    without actually executing it. DispatchError propagates to exception handler.
    """
    # Create minimal internal request
    internal_request = InternalRequest(
        task=TaskType(body.task) if body.task else TaskType.CHAT,
        model=body.model,
        messages=[Message(role=MessageRole.USER, content="routing test")],
        client_id=client_id,
        preferred_provider=body.preferred_provider,
        fallback_allowed=body.fallback_allowed,
    )

    # Resolve provider - DispatchError propagates to exception handler
    provider_name, model_name = dispatcher.resolve_provider(internal_request)

    # Check health
    is_healthy = registry.is_healthy(provider_name)

    # Get fallback chain
    fallback_chain = []
    if body.fallback_allowed:
        fallback_chain = registry.get_fallback_chain(exclude=provider_name)

    # Would we fallback?
    would_fallback = not is_healthy and body.fallback_allowed and len(fallback_chain) > 0

    reason = None
    if not is_healthy:
        if would_fallback:
            reason = f"Primary provider '{provider_name}' unhealthy, would fallback"
        else:
            reason = f"Primary provider '{provider_name}' unhealthy, no fallback available"

    return RouteResponse(
        resolved_provider=provider_name,
        resolved_model=model_name or body.model,
        fallback_chain=fallback_chain,
        provider_healthy=is_healthy,
        would_fallback=would_fallback,
        reason=reason,
    )


# =============================================================================
# Provider Management
# =============================================================================


class ProviderStatus(BaseModel):
    """Detailed provider status."""
    name: str
    type: str
    enabled: bool
    healthy: bool
    status: str
    base_url: str
    models: list[str]
    capabilities: list[str]


class ProvidersResponse(BaseModel):
    """Response for providers list."""
    providers: list[ProviderStatus]


@router.get("/v1/devmesh/providers", response_model=ProvidersResponse)
async def list_providers(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    config: Annotated[GatewayConfig, Depends(get_config)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> ProvidersResponse:
    """List all configured providers with their status.

    Returns detailed information about each provider including health status.
    """
    providers = []

    for provider_config in config.providers:
        health = registry.get_health(provider_config.name)
        status_enum = health.status if health else HealthStatus.UNKNOWN
        adapter = registry.get(provider_config.name)

        models = []
        capabilities = []

        if adapter:
            try:
                models = await adapter.list_models()
                capabilities = adapter.get_capabilities()
            except Exception:
                pass

        providers.append(ProviderStatus(
            name=provider_config.name,
            type=provider_config.type.value,
            enabled=provider_config.enabled,
            healthy=status_enum == HealthStatus.HEALTHY if status_enum else False,
            status=status_enum.value if status_enum else "unknown",
            base_url=provider_config.base_url,
            models=models,
            capabilities=capabilities,
        ))

    return ProvidersResponse(providers=providers)


@router.post("/v1/devmesh/providers/{provider_name}/health")
async def check_provider_health(
    provider_name: str,
    client_id: Annotated[str, Depends(authenticate)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
) -> dict[str, Any]:
    """Force a health check on a specific provider.

    Triggers an immediate health check regardless of cache.

    Args:
        provider_name: Name of the provider to check

    Returns:
        Health status and details
    """
    if provider_name not in registry.list_providers():
        raise ProviderNotFoundError(provider=provider_name)

    status_enum = await registry.check_health(provider_name)

    return {
        "provider": provider_name,
        "status": status_enum.value,
        "healthy": status_enum == HealthStatus.HEALTHY,
    }


# =============================================================================
# Model Catalog
# =============================================================================


class CatalogModel(BaseModel):
    """Model discovered from an endpoint."""
    name: str
    endpoint: str
    discovered_at: str
    size_bytes: Optional[int] = None
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization: Optional[str] = None


class CatalogEndpoint(BaseModel):
    """Endpoint with its discovered models."""
    name: str
    type: str
    url: str
    enabled: bool
    healthy: bool
    labels: dict[str, str] = Field(default_factory=dict)
    models: list[str] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    """Response for catalog endpoint."""
    last_discovery: Optional[str] = None
    endpoints: list[CatalogEndpoint]
    models: list[CatalogModel]
    total_models: int
    total_endpoints: int


@router.get("/v1/devmesh/catalog", response_model=CatalogResponse)
async def get_catalog(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> CatalogResponse:
    """Get the model catalog with discovered models per endpoint.

    Returns discovered models and their availability across endpoints.
    Useful for debugging routing and checking model availability.
    """
    catalog = registry.catalog

    # Build endpoint info
    endpoints = []
    for endpoint_config in config.get_enabled_endpoints():
        health = registry.get_health(endpoint_config.name)
        status_enum = health.status if health else HealthStatus.UNKNOWN

        models = catalog.get_models_for_endpoint(endpoint_config.name)

        endpoints.append(CatalogEndpoint(
            name=endpoint_config.name,
            type=endpoint_config.type.value,
            url=endpoint_config.url,
            enabled=endpoint_config.enabled,
            healthy=status_enum == HealthStatus.HEALTHY,
            labels=endpoint_config.labels,
            models=models,
        ))

    # Build model info
    models = []
    for discovered in catalog.discovered:
        models.append(CatalogModel(
            name=discovered.name,
            endpoint=discovered.endpoint,
            discovered_at=discovered.discovered_at.isoformat(),
            size_bytes=discovered.size_bytes,
            family=discovered.family,
            parameter_size=discovered.parameter_size,
            quantization=discovered.quantization,
        ))

    return CatalogResponse(
        last_discovery=catalog.last_discovery.isoformat() if catalog.last_discovery else None,
        endpoints=endpoints,
        models=models,
        total_models=len(models),
        total_endpoints=len(endpoints),
    )


@router.post("/v1/devmesh/catalog/refresh")
async def refresh_catalog(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
) -> dict[str, Any]:
    """Trigger immediate model discovery.

    Forces a refresh of the model catalog by querying all endpoints.

    Returns:
        Discovery results with models found per endpoint
    """
    discovery = getattr(request.app.state, "discovery_service", None)
    if discovery is None:
        return {
            "status": "error",
            "message": "Discovery service not configured",
        }

    results = await discovery.discover_all()

    return {
        "status": "success",
        "discovered": results,
        "total_models": sum(len(models) for models in results.values()),
    }


# =============================================================================
# Dashboard APIs
# =============================================================================


class StatsResponse(BaseModel):
    """Usage statistics response."""
    period_hours: int
    total_requests: int
    success_count: int
    error_count: int
    success_rate: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: Optional[float] = None
    min_latency_ms: Optional[float] = None
    max_latency_ms: Optional[float] = None
    total_cost_usd: float = 0.0
    requests_by_endpoint: dict[str, int] = Field(default_factory=dict)
    top_models: dict[str, int] = Field(default_factory=dict)


@router.get("/api/stats", response_model=StatsResponse)
async def get_stats(
    request: Request,
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    hours: int = 24,
    filter_client: Optional[str] = None,
) -> StatsResponse:
    """Get usage statistics for the dashboard.

    Args:
        hours: Number of hours to look back (default 24)
        filter_client: Optional client ID to filter by

    Returns:
        Aggregated usage statistics
    """
    if audit_logger is None:
        return StatsResponse(
            period_hours=hours,
            total_requests=0,
            success_count=0,
            error_count=0,
            success_rate=0.0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )

    stats = await audit_logger.get_stats(hours=hours, client_id=filter_client)
    return StatsResponse(**stats)


class AuditRequestSummary(BaseModel):
    """Summary of an audit log entry."""
    id: int
    request_id: str
    timestamp: str
    client_id: str
    user_id: Optional[str] = None
    environment: Optional[str] = None
    task: str
    model: str
    endpoint: str
    status: str
    latency_ms: Optional[float] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error_code: Optional[str] = None


class RequestsListResponse(BaseModel):
    """Response for listing requests."""
    requests: list[AuditRequestSummary]
    total: int
    limit: int
    offset: int


@router.get("/api/requests", response_model=RequestsListResponse)
async def list_requests(
    request: Request,
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    limit: int = 50,
    offset: int = 0,
    filter_client: Optional[str] = None,
    filter_status: Optional[str] = None,
    filter_environment: Optional[str] = None,
) -> RequestsListResponse:
    """Get recent requests from the audit log.

    Args:
        limit: Maximum number of records (default 50, max 500)
        offset: Number of records to skip (for pagination)
        filter_client: Filter by client ID
        filter_status: Filter by status (success, error)
        filter_environment: Filter by environment

    Returns:
        List of recent requests with metadata
    """
    if audit_logger is None:
        return RequestsListResponse(requests=[], total=0, limit=limit, offset=offset)

    # Clamp limit
    limit = min(limit, 500)

    # Get requests (offset handling done in query)
    requests = await audit_logger.get_recent_requests(
        limit=limit + offset,  # Fetch enough to skip offset
        client_id=filter_client,
        environment=filter_environment,
        status=filter_status,
    )

    # Apply offset
    requests = requests[offset:offset + limit]

    # Convert to response format
    summaries = []
    for req in requests:
        summaries.append(AuditRequestSummary(
            id=req.get("id", 0),
            request_id=req["request_id"],
            timestamp=req["timestamp"].isoformat() if req.get("timestamp") else "",
            client_id=req["client_id"],
            user_id=req.get("user_id"),
            environment=req.get("environment"),
            task=req["task"],
            model=req["model"],
            endpoint=req["endpoint"],
            status=req["status"],
            latency_ms=req.get("latency_ms"),
            prompt_tokens=req.get("prompt_tokens", 0),
            completion_tokens=req.get("completion_tokens", 0),
            total_tokens=req.get("total_tokens", 0),
            error_code=req.get("error_code"),
        ))

    return RequestsListResponse(
        requests=summaries,
        total=len(requests),
        limit=limit,
        offset=offset,
    )


class RequestDetailResponse(BaseModel):
    """Detailed information about a single request."""
    id: int
    request_id: str
    timestamp: str
    client_id: str
    user_id: Optional[str] = None
    environment: Optional[str] = None
    task: str
    model: str
    endpoint: str
    provider_type: Optional[str] = None
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    status: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    latency_ms: Optional[float] = None
    time_to_first_token_ms: Optional[float] = None
    tokens_per_second: Optional[float] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    request_body: Optional[dict] = None
    response_body: Optional[dict] = None


@router.get("/api/requests/{request_id}", response_model=RequestDetailResponse)
async def get_request_detail(
    request: Request,
    request_id: str,
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
) -> RequestDetailResponse:
    """Get detailed information about a specific request.

    Args:
        request_id: The request ID to look up

    Returns:
        Full request details including bodies if stored

    Raises:
        404 if request not found
    """
    from gateway.errors import GatewayError, ErrorCode, ErrorCategory

    if audit_logger is None:
        raise GatewayError(
            message="Audit logging not configured",
            code=ErrorCode.CONFIGURATION_ERROR,
            category=ErrorCategory.INTERNAL,
        )

    result = await audit_logger.get_request_by_id(request_id)

    if result is None:
        raise GatewayError(
            message=f"Request not found: {request_id}",
            code=ErrorCode.NOT_FOUND,
            category=ErrorCategory.VALIDATION,
        )

    return RequestDetailResponse(
        id=result.get("id", 0),
        request_id=result["request_id"],
        timestamp=result["timestamp"].isoformat() if result.get("timestamp") else "",
        client_id=result["client_id"],
        user_id=result.get("user_id"),
        environment=result.get("environment"),
        task=result["task"],
        model=result["model"],
        endpoint=result["endpoint"],
        provider_type=result.get("provider_type"),
        stream=bool(result.get("stream", False)),
        max_tokens=result.get("max_tokens"),
        temperature=result.get("temperature"),
        status=result["status"],
        error_code=result.get("error_code"),
        error_message=result.get("error_message"),
        latency_ms=result.get("latency_ms"),
        time_to_first_token_ms=result.get("time_to_first_token_ms"),
        tokens_per_second=result.get("tokens_per_second"),
        prompt_tokens=result.get("prompt_tokens", 0),
        completion_tokens=result.get("completion_tokens", 0),
        total_tokens=result.get("total_tokens", 0),
        estimated_cost_usd=result.get("estimated_cost_usd"),
        request_body=result.get("request_body"),
        response_body=result.get("response_body"),
    )


class ModelUsageItem(BaseModel):
    """Usage statistics for a single model."""
    model: str
    request_count: int
    success_count: int
    error_count: int
    total_tokens: int
    avg_latency_ms: Optional[float] = None


class ModelsUsageResponse(BaseModel):
    """Response for model usage breakdown."""
    period_hours: int
    models: list[ModelUsageItem]


@router.get("/api/models/usage", response_model=ModelsUsageResponse)
async def get_models_usage(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    hours: int = 24,
) -> ModelsUsageResponse:
    """Get usage breakdown by model.

    Args:
        hours: Number of hours to look back (default 24)

    Returns:
        Per-model usage statistics
    """
    if audit_logger is None:
        return ModelsUsageResponse(period_hours=hours, models=[])

    models = await audit_logger.get_models_usage(hours=hours)

    return ModelsUsageResponse(
        period_hours=hours,
        models=[ModelUsageItem(**m) for m in models],
    )


class EndpointUsageItem(BaseModel):
    """Usage statistics for a single endpoint."""
    endpoint: str
    request_count: int
    success_count: int
    error_count: int
    total_tokens: int
    avg_latency_ms: Optional[float] = None


class EndpointsUsageResponse(BaseModel):
    """Response for endpoint usage breakdown."""
    period_hours: int
    endpoints: list[EndpointUsageItem]


@router.get("/api/endpoints/usage", response_model=EndpointsUsageResponse)
async def get_endpoints_usage(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    hours: int = 24,
) -> EndpointsUsageResponse:
    """Get usage breakdown by endpoint.

    Args:
        hours: Number of hours to look back (default 24)

    Returns:
        Per-endpoint usage statistics
    """
    if audit_logger is None:
        return EndpointsUsageResponse(period_hours=hours, endpoints=[])

    endpoints = await audit_logger.get_endpoints_usage(hours=hours)

    return EndpointsUsageResponse(
        period_hours=hours,
        endpoints=[EndpointUsageItem(**e) for e in endpoints],
    )


class DailyUsageItem(BaseModel):
    """Usage statistics for a single day."""
    date: str
    request_count: int
    success_count: int
    total_tokens: int
    total_cost_usd: float


class DailyUsageResponse(BaseModel):
    """Response for daily usage breakdown."""
    days: int
    usage: list[DailyUsageItem]


@router.get("/api/usage/daily", response_model=DailyUsageResponse)
async def get_daily_usage(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    days: int = 30,
    filter_client: Optional[str] = None,
) -> DailyUsageResponse:
    """Get daily usage from aggregated data.

    Note: Requires aggregation to have been run. Use /api/usage/aggregate
    to manually trigger aggregation.

    Args:
        days: Number of days to look back (default 30)
        filter_client: Filter by client ID

    Returns:
        Daily usage statistics
    """
    if audit_logger is None:
        return DailyUsageResponse(days=days, usage=[])

    usage = await audit_logger.get_daily_usage(days=days, client_id=filter_client)

    return DailyUsageResponse(
        days=days,
        usage=[DailyUsageItem(**u) for u in usage],
    )


@router.post("/api/usage/aggregate")
async def trigger_aggregation(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    date: Optional[str] = None,
) -> dict[str, Any]:
    """Manually trigger usage aggregation for a specific date.

    Aggregates audit_log data into usage_daily table for faster queries.
    Default is to aggregate yesterday's data.

    Args:
        date: Date to aggregate (YYYY-MM-DD format, default: yesterday)

    Returns:
        Aggregation results
    """
    from datetime import datetime as dt

    if audit_logger is None:
        return {"status": "error", "message": "Audit logging not configured"}

    # Parse date if provided
    target_date = None
    if date:
        try:
            target_date = dt.fromisoformat(date)
        except ValueError:
            return {"status": "error", "message": f"Invalid date format: {date}"}

    result = await audit_logger.aggregate_daily_usage(date=target_date)

    return {
        "status": "success",
        **result,
    }


# =============================================================================
# Security APIs
# =============================================================================


class SecurityAlertResponse(BaseModel):
    """A security alert."""
    timestamp: str
    request_id: str
    client_id: str
    severity: str
    alert_type: str
    description: str
    details: dict = Field(default_factory=dict)


class SecurityAlertsResponse(BaseModel):
    """Response for security alerts."""
    alerts: list[SecurityAlertResponse]
    total: int


@router.get("/api/security/alerts", response_model=SecurityAlertsResponse)
async def get_security_alerts(
    request: Request,
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
    limit: int = 100,
) -> SecurityAlertsResponse:
    """Get recent security alerts.

    Args:
        limit: Maximum number of alerts to return (default 100)

    Returns:
        List of recent security alerts
    """
    if security_analyzer is None:
        return SecurityAlertsResponse(alerts=[], total=0)

    alerts = security_analyzer.get_recent_alerts(limit=limit)

    return SecurityAlertsResponse(
        alerts=[
            SecurityAlertResponse(
                timestamp=a.timestamp,
                request_id=a.request_id,
                client_id=a.client_id,
                severity=a.severity.value,
                alert_type=a.alert_type,
                description=a.description,
                details=a.details,
            )
            for a in alerts
        ],
        total=len(alerts),
    )


class SecurityStatsResponse(BaseModel):
    """Security analyzer statistics."""
    requests_analyzed: int
    alerts_generated: int
    requests_dropped: int
    queue_size: int
    alerts_in_memory: int


@router.get("/api/security/stats", response_model=SecurityStatsResponse)
async def get_security_stats(
    request: Request,
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
) -> SecurityStatsResponse:
    """Get security analyzer statistics.

    Returns:
        Statistics about security analysis activity
    """
    if security_analyzer is None:
        return SecurityStatsResponse(
            requests_analyzed=0,
            alerts_generated=0,
            requests_dropped=0,
            queue_size=0,
            alerts_in_memory=0,
        )

    stats = security_analyzer.get_stats()

    return SecurityStatsResponse(**stats)


@router.delete("/api/security/alerts")
async def clear_security_alerts(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
) -> dict[str, Any]:
    """Clear all security alerts from memory.

    Requires authentication.

    Returns:
        Number of alerts cleared
    """
    if security_analyzer is None:
        return {"status": "error", "message": "Security analyzer not configured"}

    count = security_analyzer.clear_alerts()

    return {
        "status": "success",
        "alerts_cleared": count,
    }


# =============================================================================
# API Key Management
# =============================================================================


def get_key_manager(request: Request) -> KeyManager | None:
    """Get KeyManager from app state, or None if DB not configured."""
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return None
    return KeyManager(engine)


class CreateKeyRequest(BaseModel):
    """Request to create a new API key."""
    name: str = Field(..., min_length=1, max_length=128)
    client_id: str = Field(..., min_length=1, max_length=128)
    environment: Optional[str] = None
    description: Optional[str] = None
    allowed_endpoints: Optional[list[str]] = None
    allowed_models: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = Field(default=None, ge=1)


class CreateKeyResponse(BaseModel):
    """Response after creating an API key. Contains plaintext key shown once."""
    key: str
    key_id: int
    prefix: str
    name: str
    client_id: str
    created_at: str


class KeyInfo(BaseModel):
    """API key info (no secret data)."""
    id: int
    prefix: str
    name: str
    client_id: str
    environment: Optional[str] = None
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    is_active: bool
    allowed_endpoints: Optional[list[str]] = None
    allowed_models: Optional[list[str]] = None
    rate_limit_rpm: Optional[int] = None
    description: Optional[str] = None


class KeyListResponse(BaseModel):
    """Response for listing API keys."""
    keys: list[KeyInfo]
    total: int


@router.post("/api/keys", response_model=CreateKeyResponse)
async def create_api_key(
    request: Request,
    body: CreateKeyRequest,
    client_id: Annotated[str, Depends(authenticate)],
) -> CreateKeyResponse:
    """Create a new database-backed API key.

    The plaintext key is returned exactly once in the response.
    Store it securely - it cannot be retrieved again.

    Requires authentication.
    """
    from gateway.errors import GatewayError, ErrorCode, ErrorCategory

    km = get_key_manager(request)
    if km is None:
        raise GatewayError(
            message="Database not configured - cannot manage keys",
            code=ErrorCode.CONFIGURATION_ERROR,
            category=ErrorCategory.INTERNAL,
        )

    result = await km.create_key(
        name=body.name,
        client_id=body.client_id,
        environment=body.environment,
        description=body.description,
        allowed_endpoints=body.allowed_endpoints,
        allowed_models=body.allowed_models,
        rate_limit_rpm=body.rate_limit_rpm,
    )

    return CreateKeyResponse(**result)


@router.get("/api/keys", response_model=KeyListResponse)
async def list_api_keys(
    request: Request,
    client_id: Annotated[str, Depends(authenticate)],
) -> KeyListResponse:
    """List all API keys (masked - no secret data).

    Requires authentication.
    """
    from gateway.errors import GatewayError, ErrorCode, ErrorCategory

    km = get_key_manager(request)
    if km is None:
        raise GatewayError(
            message="Database not configured - cannot manage keys",
            code=ErrorCode.CONFIGURATION_ERROR,
            category=ErrorCategory.INTERNAL,
        )

    keys = await km.list_keys()

    return KeyListResponse(
        keys=[KeyInfo(**k) for k in keys],
        total=len(keys),
    )


@router.delete("/api/keys/{key_id}")
async def revoke_api_key(
    request: Request,
    key_id: int,
    client_id: Annotated[str, Depends(authenticate)],
) -> dict[str, Any]:
    """Revoke an API key by ID.

    Sets the key as inactive. It can no longer be used for authentication.

    Requires authentication.
    """
    from gateway.errors import GatewayError, ErrorCode, ErrorCategory

    km = get_key_manager(request)
    if km is None:
        raise GatewayError(
            message="Database not configured - cannot manage keys",
            code=ErrorCode.CONFIGURATION_ERROR,
            category=ErrorCategory.INTERNAL,
        )

    revoked = await km.revoke_key(key_id)

    if not revoked:
        raise GatewayError(
            message=f"Key not found: {key_id}",
            code=ErrorCode.NOT_FOUND,
            category=ErrorCategory.VALIDATION,
        )

    return {"status": "success", "key_id": key_id, "revoked": True}

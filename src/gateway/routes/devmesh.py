"""DevMesh extension endpoints.

Per PRD Section 7:
- GET /health (health check)
- GET /metrics (Prometheus)
- GET /v1/models (list available models)
- POST /v1/devmesh/route (debug routing decisions)

These endpoints provide gateway-specific functionality beyond OpenAI compatibility.
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from gateway.config import GatewayConfig
from gateway.dispatch import Dispatcher, DispatchError, ProviderRegistry
from gateway.models.common import HealthStatus, TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.observability import get_logger, get_metrics
from gateway.routes.dependencies import (
    authenticate,
    get_config,
    get_dispatcher,
    get_registry,
)

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
    if not PROMETHEUS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prometheus client not installed",
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
    without actually executing it.
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

    try:
        # Resolve provider
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

    except DispatchError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": e.code, "message": str(e)},
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{provider_name}' not found",
        )

    status_enum = await registry.check_health(provider_name)

    return {
        "provider": provider_name,
        "status": status_enum.value,
        "healthy": status_enum == HealthStatus.HEALTHY,
    }

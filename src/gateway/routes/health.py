"""Health and metrics endpoints.

Per PRD Section 7:
- GET /health (health check)
- GET /metrics (Prometheus)
"""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from gateway.config import GatewayConfig
from gateway.dispatch import ProviderRegistry
from gateway.models.common import HealthStatus

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

router = APIRouter(tags=["health"])


class ProviderHealth(BaseModel):
    """Health status for a single provider."""

    name: str
    status: str
    healthy: bool
    last_check: str | None = None


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
    """Health check endpoint with detailed provider status."""
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

            providers.append(
                ProviderHealth(
                    name=name,
                    status=status_enum.value if status_enum else "unknown",
                    healthy=is_healthy,
                )
            )

    return HealthResponse(
        status="healthy" if providers_healthy > 0 or not providers else "degraded",
        version="0.1.0",
        config_loaded=config is not None,
        providers_configured=len(providers),
        providers_healthy=providers_healthy,
        providers=providers,
    )


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus metrics endpoint."""
    from gateway.errors import ErrorCode, ProviderError

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

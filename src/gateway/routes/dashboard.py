"""Dashboard API endpoints for usage stats and audit log queries.

Endpoints:
- GET /api/stats
- GET /api/requests
- GET /api/requests/{request_id}
- GET /api/models/usage
- GET /api/endpoints/usage
- GET /api/usage/daily
- POST /api/usage/aggregate
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.policy import PolicyEnforcer
from gateway.routes.dependencies import (
    authenticate,
    get_audit_logger,
    get_config,
    get_enforcer,
)
from gateway.storage import AuditLogger

router = APIRouter(tags=["dashboard"])


# =============================================================================
# Stats
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
    avg_latency_ms: float | None = None
    min_latency_ms: float | None = None
    max_latency_ms: float | None = None
    total_cost_usd: float = 0.0
    requests_by_endpoint: dict[str, int] = Field(default_factory=dict)
    top_models: dict[str, int] = Field(default_factory=dict)


@router.get("/api/stats", response_model=StatsResponse)
async def get_stats(
    request: Request,
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    hours: int = 24,
    filter_client: str | None = None,
) -> StatsResponse:
    """Get usage statistics for the dashboard."""
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


# =============================================================================
# Requests
# =============================================================================


class AuditRequestSummary(BaseModel):
    """Summary of an audit log entry."""

    id: int
    request_id: str
    timestamp: str
    client_id: str
    user_id: str | None = None
    environment: str | None = None
    task: str
    model: str
    endpoint: str
    status: str
    latency_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error_code: str | None = None


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
    filter_client: str | None = None,
    filter_status: str | None = None,
    filter_environment: str | None = None,
) -> RequestsListResponse:
    """Get recent requests from the audit log."""
    if audit_logger is None:
        return RequestsListResponse(requests=[], total=0, limit=limit, offset=offset)

    limit = min(limit, 500)

    requests = await audit_logger.get_recent_requests(
        limit=limit + offset,
        client_id=filter_client,
        environment=filter_environment,
        status=filter_status,
    )

    requests = requests[offset : offset + limit]

    summaries = []
    for req in requests:
        summaries.append(
            AuditRequestSummary(
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
            )
        )

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
    user_id: str | None = None
    environment: str | None = None
    task: str
    model: str
    endpoint: str
    provider_type: str | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: float | None = None
    time_to_first_token_ms: float | None = None
    tokens_per_second: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None
    request_body: dict | None = None
    response_body: dict | None = None


@router.get("/api/requests/{request_id}", response_model=RequestDetailResponse)
async def get_request_detail(
    request: Request,
    request_id: str,
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
) -> RequestDetailResponse:
    """Get detailed information about a specific request."""
    from gateway.errors import ErrorCategory, ErrorCode, GatewayError

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


# =============================================================================
# Usage Breakdowns
# =============================================================================


class ModelUsageItem(BaseModel):
    """Usage statistics for a single model."""

    model: str
    request_count: int
    success_count: int
    error_count: int
    total_tokens: int
    avg_latency_ms: float | None = None


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
    """Get usage breakdown by model."""
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
    avg_latency_ms: float | None = None


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
    """Get usage breakdown by endpoint."""
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
    filter_client: str | None = None,
) -> DailyUsageResponse:
    """Get daily usage from aggregated data."""
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
    date: str | None = None,
) -> dict[str, Any]:
    """Manually trigger usage aggregation for a specific date."""
    from datetime import datetime as dt

    if audit_logger is None:
        return {"status": "error", "message": "Audit logging not configured"}

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
# Token Budget
# =============================================================================


@router.get("/api/budget/config")
async def budget_config(
    request: Request,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Get token budget configuration, including tier assignments and unclassified models."""
    config = get_config(request)
    budget = config.token_budgets
    tracker = enforcer.token_budget

    # Get all discovered models from catalog (on registry)
    registry = getattr(request.app.state, "registry", None)
    catalog = registry.catalog if registry else None
    discovered_models = catalog.get_all_models() if catalog else []

    # Classify each discovered model
    model_classifications = []
    for model_name in sorted(set(discovered_models)):
        tier = tracker.resolve_tier(model_name)
        model_classifications.append(
            {
                "model": model_name,
                "tier": tier.name if tier else None,
                "cost_multiplier": tier.cost_multiplier if tier else budget.default_cost_multiplier,
                "classified": tier is not None,
            }
        )

    return {
        "enabled": budget.enabled,
        "default_daily_limit": budget.default_daily_limit,
        "default_cost_multiplier": budget.default_cost_multiplier,
        "enforce_pre_request": budget.enforce_pre_request,
        "tiers": [
            {
                "name": t.name,
                "cost_multiplier": t.cost_multiplier,
                "daily_limit": t.daily_limit,
            }
            for t in tracker.tiers.values()
        ],
        "model_assignments": tracker.model_assignments,
        "model_classifications": model_classifications,
    }


@router.get("/api/budget/usage")
async def budget_usage(
    request: Request,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
    key: str | None = None,
) -> dict:
    """Get token budget usage for a key (or all tracked keys)."""
    tracker = enforcer.token_budget

    if not tracker.enabled:
        return {"enabled": False, "keys": []}

    if key:
        state = tracker.get_budget_state(key)
        return {
            "enabled": True,
            "keys": [
                {
                    "key": key,
                    "daily_limit": state.daily_limit,
                    "tokens_used": state.tokens_used,
                    "tokens_remaining": state.tokens_remaining,
                    "tier_usage": state.tier_usage,
                    "resets_at": state.resets_at,
                }
            ],
        }

    # Return all tracked keys
    keys = []
    for k, usage in tracker._usage.items():
        state = tracker.get_budget_state(k)
        keys.append(
            {
                "key": k,
                "daily_limit": state.daily_limit,
                "tokens_used": state.tokens_used,
                "tokens_remaining": state.tokens_remaining,
                "tier_usage": state.tier_usage,
                "request_count": usage.request_count,
                "resets_at": state.resets_at,
            }
        )

    return {
        "enabled": True,
        "keys": sorted(keys, key=lambda x: x["tokens_used"], reverse=True),
    }


class TierCreateRequest(BaseModel):
    """Request to create or update a cost tier."""

    name: str = Field(description="Tier name (e.g., frontier, standard, embedding)")
    cost_multiplier: float = Field(description="Cost multiplier (1.0 = baseline)", ge=0.0, le=1000.0)
    daily_limit: int | None = Field(default=None, description="Optional daily token cap for this tier", ge=0)


@router.post("/api/budget/tiers")
async def create_tier(
    body: TierCreateRequest,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Create or update a cost tier at runtime (no restart needed)."""
    tracker = enforcer.token_budget
    is_new = tracker.add_tier(body.name, body.cost_multiplier, body.daily_limit)

    return {
        "status": "success",
        "tier": body.name,
        "cost_multiplier": body.cost_multiplier,
        "daily_limit": body.daily_limit,
        "created": is_new,
    }


@router.delete("/api/budget/tiers/{tier_name}")
async def delete_tier(
    tier_name: str,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Remove a cost tier. Fails if models are still assigned to it."""
    tracker = enforcer.token_budget
    removed = tracker.remove_tier(tier_name)

    if not removed:
        if tier_name not in tracker.tiers:
            return {"status": "error", "message": f"Tier '{tier_name}' not found"}
        return {"status": "error", "message": f"Tier '{tier_name}' still has models assigned — unassign them first"}

    return {"status": "success", "tier": tier_name}


class ModelAssignmentRequest(BaseModel):
    """Request to assign a model to a tier."""

    model: str = Field(description="Model name or glob pattern")
    tier: str = Field(description="Tier name to assign to")


@router.post("/api/budget/assignments")
async def assign_model_tier(
    request: Request,
    body: ModelAssignmentRequest,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Assign a model to a cost tier at runtime (no restart needed)."""
    tracker = enforcer.token_budget
    success = tracker.assign_model(body.model, body.tier)

    if not success:
        available = list(tracker.tiers.keys())
        return {
            "status": "error",
            "message": f"Tier '{body.tier}' not found. Available: {available}",
        }

    return {
        "status": "success",
        "model": body.model,
        "tier": body.tier,
        "cost_multiplier": tracker.get_cost_multiplier(body.model),
    }


@router.delete("/api/budget/assignments/{model_name:path}")
async def unassign_model_tier(
    model_name: str,
    _client_id: Annotated[str, Depends(authenticate)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Remove a model's tier assignment (reverts to default cost multiplier)."""
    tracker = enforcer.token_budget
    existed = tracker.unassign_model(model_name)

    return {
        "status": "success" if existed else "not_found",
        "model": model_name,
        "now_using": "default_cost_multiplier",
    }

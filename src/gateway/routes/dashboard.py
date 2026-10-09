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

from gateway.models.common import TaskType
from gateway.observability import get_logger
from gateway.policy import PolicyEnforcer
from gateway.routes.dependencies import (
    get_audit_logger,
    get_config,
    get_enforcer,
    require_admin,
)
from gateway.routing_config import RoutingUpdate
from gateway.security.pii_config import PIIScrubUpdate
from gateway.security.pii_policy import PIIMLPolicyUpdate
from gateway.storage import AuditLogger

router = APIRouter(tags=["dashboard"])
logger = get_logger(__name__)


# =============================================================================
# Stats
# =============================================================================


class StatsResponse(BaseModel):
    """Usage statistics response."""

    period_hours: int
    total_requests: int
    success_count: int
    error_count: int
    # Auth/policy/rate-limit denials: audited, but excluded from the
    # inference figures above (D-057)
    denied_count: int = 0
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
    _client_id: Annotated[str, Depends(require_admin)],
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
    total: int  # rows in this page
    limit: int
    offset: int
    has_more: bool = False  # another page exists past this one (D-057)


@router.get("/api/requests", response_model=RequestsListResponse)
async def list_requests(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    limit: int = 50,
    offset: int = 0,
    filter_client: str | None = None,
    filter_status: str | None = None,
    filter_environment: str | None = None,
    hours: float | None = None,
) -> RequestsListResponse:
    """Get recent requests from the audit log, newest first.

    Paged with offset/limit; `has_more` says whether another page exists.
    `hours` limits to the last N hours.
    """
    from datetime import UTC, datetime, timedelta

    if audit_logger is None:
        return RequestsListResponse(requests=[], total=0, limit=limit, offset=offset)

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    since = datetime.now(UTC) - timedelta(hours=hours) if hours and hours > 0 else None

    # One extra row tells us whether another page exists, without a COUNT
    # over the whole audit log
    requests = await audit_logger.get_recent_requests(
        limit=limit + 1,
        offset=offset,
        client_id=filter_client or None,
        environment=filter_environment or None,
        status=filter_status or None,
        since=since,
    )
    has_more = len(requests) > limit
    requests = requests[:limit]

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
        has_more=has_more,
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
    _client_id: Annotated[str, Depends(require_admin)],
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
    client_id: Annotated[str, Depends(require_admin)],
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
    client_id: Annotated[str, Depends(require_admin)],
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
    client_id: Annotated[str, Depends(require_admin)],
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
    client_id: Annotated[str, Depends(require_admin)],
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
    _client_id: Annotated[str, Depends(require_admin)],
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
    _client_id: Annotated[str, Depends(require_admin)],
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

    # Every key with usage today (this process and, via the database, others)
    keys = []
    for k in tracker.keys_today():
        state = tracker.get_budget_state(k)
        keys.append(
            {
                "key": k,
                "daily_limit": state.daily_limit,
                "tokens_used": state.tokens_used,
                "tokens_remaining": state.tokens_remaining,
                "tier_usage": state.tier_usage,
                "request_count": state.request_count,
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
    cost_multiplier: float = Field(
        description="Cost multiplier (1.0 = baseline)", ge=0.0, le=1000.0
    )
    daily_limit: int | None = Field(
        default=None, description="Optional daily token cap for this tier", ge=0
    )


async def _save_budget_catalog(request: Request, admin_id: str) -> None:
    """Persist tiers/assignments so the change survives restarts (D-037)."""
    sync = getattr(request.app.state, "budget_sync", None)
    if sync is not None:
        await sync.save_catalog(admin_id)


@router.post("/api/budget/tiers")
async def create_tier(
    request: Request,
    body: TierCreateRequest,
    _client_id: Annotated[str, Depends(require_admin)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Create or update a cost tier at runtime (no restart needed)."""
    tracker = enforcer.token_budget
    is_new = tracker.add_tier(body.name, body.cost_multiplier, body.daily_limit)
    await _save_budget_catalog(request, _client_id)

    return {
        "status": "success",
        "tier": body.name,
        "cost_multiplier": body.cost_multiplier,
        "daily_limit": body.daily_limit,
        "created": is_new,
    }


@router.delete("/api/budget/tiers/{tier_name}")
async def delete_tier(
    request: Request,
    tier_name: str,
    _client_id: Annotated[str, Depends(require_admin)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Remove a cost tier. Fails if models are still assigned to it."""
    tracker = enforcer.token_budget
    removed = tracker.remove_tier(tier_name)
    if removed:
        await _save_budget_catalog(request, _client_id)

    if not removed:
        if tier_name not in tracker.tiers:
            return {"status": "error", "message": f"Tier '{tier_name}' not found"}
        return {
            "status": "error",
            "message": f"Tier '{tier_name}' still has models assigned — unassign them first",
        }

    return {"status": "success", "tier": tier_name}


class ModelAssignmentRequest(BaseModel):
    """Request to assign a model to a tier."""

    model: str = Field(description="Model name or glob pattern")
    tier: str = Field(description="Tier name to assign to")


@router.post("/api/budget/assignments")
async def assign_model_tier(
    request: Request,
    body: ModelAssignmentRequest,
    _client_id: Annotated[str, Depends(require_admin)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Assign a model to a cost tier at runtime (no restart needed)."""
    tracker = enforcer.token_budget
    success = tracker.assign_model(body.model, body.tier)
    if success:
        await _save_budget_catalog(request, _client_id)

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
    request: Request,
    model_name: str,
    _client_id: Annotated[str, Depends(require_admin)],
    enforcer: Annotated[PolicyEnforcer, Depends(get_enforcer)],
) -> dict:
    """Remove a model's tier assignment (reverts to default cost multiplier)."""
    tracker = enforcer.token_budget
    existed = tracker.unassign_model(model_name)
    if existed:
        await _save_budget_catalog(request, _client_id)

    return {
        "status": "success" if existed else "not_found",
        "model": model_name,
        "now_using": "default_cost_multiplier",
    }


# =============================================================================
# PII Detection Audit
# =============================================================================


@router.get("/api/pii/stats")
async def pii_stats(
    _client_id: Annotated[str, Depends(require_admin)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    hours: int = 24,
) -> dict:
    """Get PII detection statistics — no raw PII exposed."""
    if not audit_logger:
        return {"enabled": False}

    stats = await audit_logger.get_pii_stats(hours=hours)
    return {"enabled": True, **stats}


def _pii_config_view(request: Request) -> dict:
    """Current scrubbing policy as the dashboard shows it."""
    from gateway.security.pii_config import PII_SCAN_ROUTES

    detection_enabled = getattr(request.app.state, "pii_scrubber", None) is not None
    config = getattr(request.app.state, "pii_settings", None)
    return {
        "detection_enabled": detection_enabled,
        "scrub_enabled": bool(config and config.scrub_enabled),
        "scrub_routes": list(config.scrub_routes) if config else [],
        "available_routes": list(PII_SCAN_ROUTES),
        "source": getattr(config, "source", "environment"),
        "updated_at": config.updated_at.isoformat()
        if getattr(config, "updated_at", None)
        else None,
        "updated_by": getattr(config, "updated_by", None),
        "persisted": getattr(request.app.state, "runtime_settings", None) is not None,
    }


@router.get("/api/pii/config")
async def get_pii_config(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """PII scrubbing policy in effect. scrub_routes empty = all routes."""
    return _pii_config_view(request)


@router.put("/api/pii/config")
async def update_pii_config(
    request: Request,
    body: PIIScrubUpdate,
    admin_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Change PII scrubbing at runtime (admin only).

    Takes effect on the next request and is saved, so it survives restarts
    and overrides the GATEWAY_PII_SCRUB_* environment defaults. Detection
    itself stays an environment setting.
    """
    from gateway.errors import ValidationError
    from gateway.security.pii_config import SETTING_KEY, PIIScrubConfig

    if getattr(request.app.state, "pii_scrubber", None) is None:
        raise ValidationError(
            message="PII detection is disabled (GATEWAY_PII_ENABLED=false); scrubbing needs "
            "detection, which is set by environment variable, not the dashboard"
        )
    update = body
    previous = request.app.state.pii_settings
    store = getattr(request.app.state, "runtime_settings", None)
    updated_at = None
    if store is not None:
        # Save first: if this fails, the running policy stays unchanged
        updated_at = await store.set(SETTING_KEY, update.model_dump(), updated_by=admin_id)

    request.app.state.pii_settings = PIIScrubConfig(
        scrub_enabled=update.scrub_enabled,
        scrub_routes=update.scrub_routes,
        source="dashboard",
        updated_at=updated_at,
        updated_by=admin_id,
    )
    logger.warning(
        "PII scrubbing policy changed",
        changed_by=admin_id,
        scrub_enabled_before=previous.scrub_enabled,
        scrub_enabled=update.scrub_enabled,
        scrub_routes_before=list(previous.scrub_routes) or ["all"],
        scrub_routes=update.scrub_routes or ["all"],
        persisted=store is not None,
    )
    return _pii_config_view(request)


async def _pii_ml_view(request: Request, hours: int) -> dict:
    """ML PII detection (D-052): gate health, measured numbers, per-category policy."""
    from gateway.security.pii_policy import ACTIONS, PIIMLPolicy
    from gateway.security.pii_taxonomy import CATEGORIES

    settings = request.app.state.settings
    shadow = getattr(request.app.state, "pii_shadow", None)
    policy = getattr(request.app.state, "pii_ml_policy", None) or PIIMLPolicy()
    engine = getattr(request.app.state, "db_engine", None)
    summary = per_category = None
    if engine is not None:
        from gateway.storage.pii_shadow_store import PIIShadowStore

        store = PIIShadowStore(engine)
        summary = await store.summary(hours=hours)
        per_category = await store.category_summary(hours=hours)
    return {
        "enabled": shadow is not None,
        "gate_url": settings.pii_gate.url if shadow is not None else None,
        "finder_model": settings.pii_gate.finder_model if shadow is not None else None,
        "threshold": settings.pii_gate.threshold,
        "sample_rate": settings.pii_gate.sample_rate,
        "live": shadow.stats if shadow is not None else None,
        "summary": summary,
        "actions": list(ACTIONS),
        "categories": [
            {
                "label": c.label,
                "name": c.name,
                "includes": c.includes,
                "action": policy.categories.get(c.label, "detect"),
                **((per_category or {}).get(c.label) or {}),
            }
            for c in CATEGORIES
        ],
        "source": policy.source,
        "updated_at": policy.updated_at.isoformat()
        if getattr(policy.updated_at, "isoformat", None)
        else policy.updated_at,
        "updated_by": policy.updated_by,
        "persisted": getattr(request.app.state, "runtime_settings", None) is not None,
    }


@router.get("/api/pii/ml")
async def get_pii_ml(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
    hours: int = 24,
) -> dict:
    """ML PII detection status, measured miss rate and per-category policy."""
    return await _pii_ml_view(request, hours)


@router.put("/api/pii/ml")
async def update_pii_ml(
    request: Request,
    body: PIIMLPolicyUpdate,
    admin_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Set categories to detect or scrub_stored (admin only).

    Takes effect on the next analysed request and is saved. Only the
    categories sent change. Never changes what models receive.
    """
    from gateway.security.pii_policy import SETTING_KEY, PIIMLPolicy

    previous = getattr(request.app.state, "pii_ml_policy", None) or PIIMLPolicy()
    merged = previous.merged(body)
    store = getattr(request.app.state, "runtime_settings", None)
    updated_at = None
    if store is not None:
        # Save first: if this fails, the running policy stays unchanged
        updated_at = await store.set(SETTING_KEY, {"categories": merged}, updated_by=admin_id)
    request.app.state.pii_ml_policy = PIIMLPolicy(
        categories=merged, source="dashboard", updated_at=updated_at, updated_by=admin_id
    )
    logger.warning(
        "ML PII policy changed",
        changed_by=admin_id,
        scrub_stored_before=sorted(previous.scrub_labels()),
        scrub_stored=sorted(request.app.state.pii_ml_policy.scrub_labels()),
        gate_enabled=getattr(request.app.state, "pii_shadow", None) is not None,
        persisted=store is not None,
    )
    return await _pii_ml_view(request, 24)


def _routing_config_view(request: Request) -> dict:
    """Routing policy in effect, plus what the dashboard can choose from."""
    from gateway.routing_config import RoutingState, routing_from_config

    config = request.app.state.config
    state = getattr(request.app.state, "routing_state", None) or RoutingState()
    effective = routing_from_config(request.app)
    return {
        "strategy": effective.strategy,
        "task_endpoints": [p.model_dump() for p in effective.task_endpoints],
        "model_defaults": [m.model_dump() for m in effective.model_defaults],
        "available_endpoints": [e.name for e in config.endpoints if e.enabled]
        or [p.name for p in config.get_enabled_providers()],
        "available_tasks": [t.value for t in TaskType],
        # yaml-only, shown for context
        "endpoint_priority": list(config.resolution.endpoint_priority),
        "source": state.source,
        "updated_at": state.updated_at.isoformat()
        if getattr(state.updated_at, "isoformat", None)
        else state.updated_at,
        "updated_by": state.updated_by,
        "persisted": getattr(request.app.state, "runtime_settings", None) is not None,
    }


@router.get("/api/routing/config")
async def get_routing_config(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Routing policy in effect: strategy, task pins, model homes."""
    return _routing_config_view(request)


@router.put("/api/routing/config")
async def update_routing_config(
    request: Request,
    body: RoutingUpdate,
    admin_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Change routing policy at runtime (admin only).

    Takes effect on the next request and is saved, so it survives
    restarts and overrides the gateway.yaml defaults. endpoint_priority
    and ambiguous_behavior stay yaml-only.
    """
    from gateway.errors import ValidationError
    from gateway.routing_config import SETTING_KEY, RoutingState, apply_routing

    config = request.app.state.config
    known = {e.name for e in config.endpoints if e.enabled} | {
        p.name for p in config.get_enabled_providers()
    }
    unknown = body.referenced_endpoints() - known
    if unknown:
        raise ValidationError(
            message=f"Unknown or disabled endpoints: {', '.join(sorted(unknown))}"
        )

    store = getattr(request.app.state, "runtime_settings", None)
    updated_at = None
    if store is not None:
        # Save first: if this fails, the running policy stays unchanged
        updated_at = await store.set(SETTING_KEY, body.model_dump(mode="json"), updated_by=admin_id)

    apply_routing(
        request.app,
        body,
        RoutingState(source="dashboard", updated_at=updated_at, updated_by=admin_id),
    )
    logger.warning(
        "Routing policy changed",
        changed_by=admin_id,
        strategy=body.strategy,
        task_pins={
            p.task.value: p.allowed_endpoints or p.denied_endpoints for p in body.task_endpoints
        },
        model_defaults={m.model: m.endpoint for m in body.model_defaults},
        persisted=store is not None,
    )
    return _routing_config_view(request)


@router.get("/api/media/catalog")
async def media_catalog(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Voice/media endpoints with their discovered models, voices, profile
    and setting ranges: everything the dashboard's media controls need."""
    catalog = getattr(request.app.state, "media_catalog", None)
    return {"endpoints": catalog.snapshot() if catalog else []}


@router.post("/api/media/catalog/refresh")
async def refresh_media_catalog(
    request: Request,
    _client_id: Annotated[str, Depends(require_admin)],
) -> dict:
    """Re-discover voices and models now instead of waiting for the next poll."""
    catalog = getattr(request.app.state, "media_catalog", None)
    if catalog is None:
        return {"endpoints": []}
    await catalog.refresh()
    return {"endpoints": catalog.snapshot()}


@router.get("/api/pii/events")
async def pii_events(
    _auth_client_id: Annotated[str, Depends(require_admin)],
    audit_logger: Annotated[AuditLogger | None, Depends(get_audit_logger)],
    limit: int = 50,
    pii_type: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Get recent PII detection events — hashed values only, never raw PII."""
    if not audit_logger:
        return {"events": []}

    events = await audit_logger.get_pii_events(limit=limit, pii_type=pii_type, client_id=client_id)

    # Serialize datetimes
    for e in events:
        if e.get("timestamp"):
            e["timestamp"] = e["timestamp"].isoformat()

    return {"events": events, "total": len(events)}

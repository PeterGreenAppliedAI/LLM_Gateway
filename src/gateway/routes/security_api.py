"""Security analysis API endpoints.

Endpoints:
- GET /api/security/alerts
- GET /api/security/stats
- DELETE /api/security/alerts
- GET /api/security/results
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.routes.dependencies import (
    authenticate,
    get_security_analyzer,
)
from gateway.security import AsyncSecurityAnalyzer

router = APIRouter(tags=["security"])


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
    """Get recent security alerts."""
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
    requests_allowlisted: int = 0
    queue_size: int
    alerts_in_memory: int
    guard_scans: int = 0
    guard_skipped: int = 0
    guard_unsafe: int = 0


@router.get("/api/security/stats", response_model=SecurityStatsResponse)
async def get_security_stats(
    request: Request,
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
) -> SecurityStatsResponse:
    """Get security analyzer statistics."""
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
    """Clear all security alerts from memory."""
    if security_analyzer is None:
        return {"status": "error", "message": "Security analyzer not configured"}

    count = security_analyzer.clear_alerts()

    return {
        "status": "success",
        "alerts_cleared": count,
    }


class SecurityResultResponse(BaseModel):
    """A single analysis result with both regex and guard verdicts."""
    request_id: str
    analyzed_at: str
    regex_threat_level: str
    regex_match_count: int
    guard_safe: Optional[bool] = None
    guard_skipped: Optional[bool] = None
    guard_category_code: Optional[str] = None
    guard_category_name: Optional[str] = None
    guard_confidence: Optional[str] = None
    guard_inference_ms: Optional[float] = None
    guard_error: Optional[str] = None
    alert_count: int = 0


class SecurityResultsResponse(BaseModel):
    """Response for security analysis results."""
    results: list[SecurityResultResponse]
    total: int
    filter: str = "all"


@router.get("/api/security/results", response_model=SecurityResultsResponse)
async def get_security_results(
    request: Request,
    security_analyzer: Annotated[AsyncSecurityAnalyzer | None, Depends(get_security_analyzer)],
    limit: int = 50,
    guard_only: bool = False,
    disagreements_only: bool = False,
) -> SecurityResultsResponse:
    """Get recent analysis results with both regex and guard verdicts."""
    if security_analyzer is None:
        return SecurityResultsResponse(results=[], total=0)

    raw_results = security_analyzer.get_recent_results(limit=limit * 3)

    results = []
    filter_name = "all"

    for r in raw_results:
        if len(results) >= limit:
            break

        guard_scan = r.guard_scan
        injection_scan = r.injection_scan

        regex_threat = injection_scan.threat_level.value if injection_scan else "none"
        regex_matches = injection_scan.match_count if injection_scan else 0
        regex_flagged = regex_threat not in ("none",)

        has_guard = guard_scan is not None and not (guard_scan.skipped if guard_scan else True)

        if guard_only and not has_guard:
            continue

        if disagreements_only:
            filter_name = "disagreements"
            if not has_guard:
                continue
            guard_safe = guard_scan.safe if guard_scan else True
            if regex_flagged == (not guard_safe):
                continue  # They agree

        item = SecurityResultResponse(
            request_id=r.request_id,
            analyzed_at=r.analyzed_at,
            regex_threat_level=regex_threat,
            regex_match_count=regex_matches,
            alert_count=len(r.alerts),
        )

        if guard_scan:
            item.guard_safe = guard_scan.safe
            item.guard_skipped = guard_scan.skipped
            item.guard_category_code = guard_scan.category_code
            item.guard_category_name = guard_scan.category_name
            item.guard_confidence = guard_scan.confidence
            item.guard_inference_ms = guard_scan.inference_time_ms
            item.guard_error = guard_scan.error

        results.append(item)

    if guard_only:
        filter_name = "guard_only"
    if disagreements_only:
        filter_name = "disagreements"

    return SecurityResultsResponse(
        results=results,
        total=len(results),
        filter=filter_name,
    )

"""Security analysis API endpoints.

Endpoints:
- GET /api/security/alerts
- GET /api/security/stats
- DELETE /api/security/alerts
- GET /api/security/results
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.routes.dependencies import (
    authenticate,
    get_security_analyzer,
)
from gateway.security import AsyncSecurityAnalyzer
from gateway.storage.security_store import SecurityScanStore

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
    guard_safe: bool | None = None
    guard_skipped: bool | None = None
    guard_category_code: str | None = None
    guard_category_name: str | None = None
    guard_confidence: str | None = None
    guard_inference_ms: float | None = None
    guard_error: str | None = None
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


# =============================================================================
# Training Data Collection — Labeling & Export
# =============================================================================


def _get_scan_store(request: Request) -> SecurityScanStore | None:
    """Get the security scan store from app state."""
    return getattr(request.app.state, "scan_store", None)


class ScanSummary(BaseModel):
    """Summary of a persisted security scan."""

    request_id: str
    timestamp: str
    client_id: str
    model: str | None = None
    task: str | None = None
    messages: list[dict]
    regex_threat_level: str
    regex_match_count: int
    guard_safe: bool | None = None
    guard_skipped: bool | None = None
    guard_category_code: str | None = None
    is_disagreement: bool = False
    label: str | None = None
    label_category: str | None = None
    labeled_by: str | None = None
    label_notes: str | None = None


class ScansListResponse(BaseModel):
    """Response for listing security scans."""

    scans: list[ScanSummary]
    total: int
    limit: int
    offset: int


@router.get("/api/security/scans", response_model=ScansListResponse)
async def list_security_scans(
    request: Request,
    _client_id: Annotated[str, Depends(authenticate)],
    limit: int = 50,
    offset: int = 0,
    label: str | None = None,
    disagreements_only: bool = False,
    unlabeled_only: bool = False,
    min_threat_level: str | None = None,
) -> ScansListResponse:
    """List persisted security scans for review and labeling."""
    scan_store = _get_scan_store(request)
    if scan_store is None:
        return ScansListResponse(scans=[], total=0, limit=limit, offset=offset)

    limit = min(limit, 500)
    rows = await scan_store.get_scans(
        limit=limit,
        offset=offset,
        label_filter=label,
        disagreements_only=disagreements_only,
        unlabeled_only=unlabeled_only,
        min_threat_level=min_threat_level,
    )

    scans = []
    for row in rows:
        scans.append(
            ScanSummary(
                request_id=row["request_id"],
                timestamp=row["timestamp"].isoformat() if row.get("timestamp") else "",
                client_id=row["client_id"],
                model=row.get("model"),
                task=row.get("task"),
                messages=row.get("messages", []),
                regex_threat_level=row["regex_threat_level"],
                regex_match_count=row.get("regex_match_count", 0),
                guard_safe=row.get("guard_safe"),
                guard_skipped=row.get("guard_skipped"),
                guard_category_code=row.get("guard_category_code"),
                is_disagreement=row.get("is_disagreement", False),
                label=row.get("label"),
                label_category=row.get("label_category"),
                labeled_by=row.get("labeled_by"),
                label_notes=row.get("label_notes"),
            )
        )

    return ScansListResponse(
        scans=scans,
        total=len(scans),
        limit=limit,
        offset=offset,
    )


class LabelRequest(BaseModel):
    """Request to label a security scan."""

    label: str = Field(description="'safe' or 'unsafe'")
    label_category: str | None = Field(
        default=None,
        description="Category code if unsafe (e.g. S1, jailbreak, injection)",
    )
    notes: str | None = Field(default=None, description="Review notes")


@router.post("/api/security/scans/{request_id}/label")
async def label_security_scan(
    request: Request,
    request_id: str,
    body: LabelRequest,
    client_id: Annotated[str, Depends(authenticate)],
) -> dict[str, Any]:
    """Apply a human label to a security scan for training data."""
    scan_store = _get_scan_store(request)
    if scan_store is None:
        return {"status": "error", "message": "Scan store not configured"}

    if body.label not in ("safe", "unsafe"):
        return {"status": "error", "message": "Label must be 'safe' or 'unsafe'"}

    success = await scan_store.label_scan(
        request_id=request_id,
        label=body.label,
        label_category=body.label_category,
        labeled_by=client_id,
        label_notes=body.notes,
    )

    if not success:
        return {"status": "error", "message": f"Scan not found: {request_id}"}

    return {
        "status": "success",
        "request_id": request_id,
        "label": body.label,
        "label_category": body.label_category,
    }


class BulkLabelRequest(BaseModel):
    """Bulk label multiple scans at once."""

    request_ids: list[str]
    label: str = Field(description="'safe' or 'unsafe'")
    label_category: str | None = None
    notes: str | None = None


@router.post("/api/security/scans/bulk-label")
async def bulk_label_scans(
    request: Request,
    body: BulkLabelRequest,
    client_id: Annotated[str, Depends(authenticate)],
) -> dict[str, Any]:
    """Bulk label multiple security scans."""
    scan_store = _get_scan_store(request)
    if scan_store is None:
        return {"status": "error", "message": "Scan store not configured"}

    if body.label not in ("safe", "unsafe"):
        return {"status": "error", "message": "Label must be 'safe' or 'unsafe'"}

    labeled = 0
    failed = []
    for rid in body.request_ids:
        success = await scan_store.label_scan(
            request_id=rid,
            label=body.label,
            label_category=body.label_category,
            labeled_by=client_id,
            label_notes=body.notes,
        )
        if success:
            labeled += 1
        else:
            failed.append(rid)

    return {
        "status": "success",
        "labeled": labeled,
        "failed": failed,
        "total_requested": len(body.request_ids),
    }


@router.get("/api/security/scans/stats")
async def get_scan_label_stats(
    request: Request,
    _client_id: Annotated[str, Depends(authenticate)],
) -> dict[str, Any]:
    """Get labeling progress statistics."""
    scan_store = _get_scan_store(request)
    if scan_store is None:
        return {"status": "error", "message": "Scan store not configured"}

    return await scan_store.get_label_stats()


@router.get("/api/security/training-data")
async def export_training_data(
    request: Request,
    _client_id: Annotated[str, Depends(authenticate)],
    format: str = "llama_guard",
    labeled_only: bool = True,
    limit: int = 10000,
) -> dict[str, Any]:
    """Export labeled security scans as training data.

    Formats:
    - llama_guard: Messages + "safe"/"unsafe\\nS1" target (for finetuning)
    - raw: All fields for custom processing
    """
    scan_store = _get_scan_store(request)
    if scan_store is None:
        return {"status": "error", "message": "Scan store not configured"}

    if format not in ("llama_guard", "raw"):
        return {"status": "error", "message": "Format must be 'llama_guard' or 'raw'"}

    examples = await scan_store.export_training_data(
        format=format,
        labeled_only=labeled_only,
        limit=min(limit, 50000),
    )

    return {
        "format": format,
        "labeled_only": labeled_only,
        "count": len(examples),
        "examples": examples,
    }

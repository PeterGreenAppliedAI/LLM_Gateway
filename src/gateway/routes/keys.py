"""API key management endpoints.

Endpoints:
- POST /api/keys
- GET /api/keys
- DELETE /api/keys/{key_id}
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from gateway.routes.dependencies import require_admin
from gateway.storage import KeyManager

router = APIRouter(tags=["keys"])


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
    environment: str | None = None
    description: str | None = None
    allowed_endpoints: list[str] | None = None
    allowed_models: list[str] | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1)


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
    environment: str | None = None
    created_at: str | None = None
    last_used_at: str | None = None
    is_active: bool
    allowed_endpoints: list[str] | None = None
    allowed_models: list[str] | None = None
    rate_limit_rpm: int | None = None
    description: str | None = None


class KeyListResponse(BaseModel):
    """Response for listing API keys."""

    keys: list[KeyInfo]
    total: int


@router.post("/api/keys", response_model=CreateKeyResponse)
async def create_api_key(
    request: Request,
    body: CreateKeyRequest,
    client_id: Annotated[str, Depends(require_admin)],
) -> CreateKeyResponse:
    """Create a new database-backed API key."""
    from gateway.errors import ErrorCategory, ErrorCode, GatewayError

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
    client_id: Annotated[str, Depends(require_admin)],
) -> KeyListResponse:
    """List all API keys (masked - no secret data)."""
    from gateway.errors import ErrorCategory, ErrorCode, GatewayError

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
    client_id: Annotated[str, Depends(require_admin)],
) -> dict[str, Any]:
    """Revoke an API key by ID."""
    from gateway.errors import ErrorCategory, ErrorCode, GatewayError

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

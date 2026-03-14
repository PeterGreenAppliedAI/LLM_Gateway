"""DevMesh extension endpoints.

Per PRD Section 7:
- GET /health (health check)
- GET /metrics (Prometheus)
- GET /v1/models (list available models)
- POST /v1/devmesh/route (debug routing decisions)

These endpoints provide gateway-specific functionality beyond OpenAI compatibility.

Split into sub-routers for maintainability:
- health.py: Health and Prometheus metrics
- catalog.py: Model catalog, providers, routing debug
- dashboard.py: Usage stats and audit log queries
- security_api.py: Security analysis results and alerts
- keys.py: API key management
"""

from fastapi import APIRouter

from gateway.routes.catalog import router as catalog_router
from gateway.routes.dashboard import router as dashboard_router
from gateway.routes.health import router as health_router
from gateway.routes.keys import router as keys_router
from gateway.routes.security_api import router as security_router

router = APIRouter()

router.include_router(health_router)
router.include_router(catalog_router)
router.include_router(dashboard_router)
router.include_router(security_router)
router.include_router(keys_router)

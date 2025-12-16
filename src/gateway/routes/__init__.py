"""API Routes - OpenAI-compatible endpoints and DevMesh extensions.

Per PRD Section 7:
- POST /v1/chat/completions (OpenAI-compatible)
- POST /v1/completions (OpenAI-compatible)
- POST /v1/embeddings (OpenAI-compatible)
- GET /health (DevMesh)
- GET /metrics (DevMesh - Prometheus)
- GET /v1/models (DevMesh)
- POST /v1/devmesh/route (DevMesh - debug routing)
"""

from gateway.routes.openai import router as openai_router
from gateway.routes.devmesh import router as devmesh_router

__all__ = [
    "openai_router",
    "devmesh_router",
]

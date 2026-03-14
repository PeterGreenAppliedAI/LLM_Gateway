"""API Routes - OpenAI-compatible endpoints and DevMesh extensions.

Per PRD Section 7:
- POST /v1/chat/completions (OpenAI-compatible)
- POST /v1/completions (OpenAI-compatible)
- POST /v1/embeddings (OpenAI-compatible)
- GET /health (DevMesh)
- GET /metrics (DevMesh - Prometheus)
- GET /v1/models (DevMesh)
- POST /v1/devmesh/route (DevMesh - debug routing)

Ollama-compatible endpoints:
- POST /api/chat
- POST /api/generate
- GET /api/tags
- POST /api/embeddings
"""

from gateway.routes.devmesh import router as devmesh_router
from gateway.routes.ollama import router as ollama_router
from gateway.routes.openai import router as openai_router

__all__ = [
    "openai_router",
    "devmesh_router",
    "ollama_router",
]

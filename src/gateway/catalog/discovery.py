"""Model discovery service for querying endpoints.

Provides background discovery of models from various endpoint types
(Ollama, vLLM, TRT-LLM, SGLang).
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from gateway.catalog.models import DiscoveredModel, ModelCatalog
from gateway.config import EndpointConfig
from gateway.models.common import ProviderType
from gateway.observability import get_logger

logger = get_logger(__name__)


class ModelDiscoveryService:
    """Background service that discovers models from endpoints.

    Periodically queries each endpoint's model list API and updates
    the central ModelCatalog with discovered models.
    """

    def __init__(
        self,
        endpoints: list[EndpointConfig],
        catalog: ModelCatalog,
        discovery_interval: float = 60.0,
        timeout: float = 10.0,
    ):
        """Initialize the discovery service.

        Args:
            endpoints: List of endpoint configurations to discover from
            catalog: ModelCatalog to update with discoveries
            discovery_interval: Seconds between discovery runs
            timeout: HTTP request timeout in seconds
        """
        self._endpoints = endpoints
        self._catalog = catalog
        self._discovery_interval = discovery_interval
        self._timeout = timeout
        self._task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def catalog(self) -> ModelCatalog:
        """Get the model catalog."""
        return self._catalog

    async def start(self) -> None:
        """Start the background discovery task."""
        if self._task is not None:
            return

        self._shutdown = False
        self._client = httpx.AsyncClient(timeout=self._timeout)

        # Run initial discovery immediately
        await self.discover_all()

        # Start background loop
        self._task = asyncio.create_task(self._discovery_loop())
        logger.info("Model discovery service started")

    async def stop(self) -> None:
        """Stop the background discovery task."""
        self._shutdown = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info("Model discovery service stopped")

    async def _discovery_loop(self) -> None:
        """Background loop that periodically discovers models."""
        while not self._shutdown:
            await asyncio.sleep(self._discovery_interval)
            if not self._shutdown:
                await self.discover_all()

    async def discover_all(self) -> dict[str, list[str]]:
        """Discover models from all endpoints.

        Returns:
            Dict mapping endpoint name to list of discovered model names
        """
        results: dict[str, list[str]] = {}
        tasks = []

        for endpoint in self._endpoints:
            if not endpoint.enabled:
                continue
            tasks.append(self._discover_endpoint(endpoint))

        if tasks:
            endpoint_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, endpoint in enumerate(e for e in self._endpoints if e.enabled):
                result = endpoint_results[i]
                if isinstance(result, Exception):
                    logger.warning(
                        f"Discovery failed for {endpoint.name}: {result}"
                    )
                    results[endpoint.name] = []
                else:
                    results[endpoint.name] = result

        self._catalog.last_discovery = datetime.now(timezone.utc)
        return results

    async def _discover_endpoint(self, endpoint: EndpointConfig) -> list[str]:
        """Discover models from a single endpoint.

        Args:
            endpoint: Endpoint configuration

        Returns:
            List of model names discovered
        """
        if endpoint.type == ProviderType.OLLAMA:
            return await self._discover_ollama(endpoint)
        elif endpoint.type == ProviderType.VLLM:
            return await self._discover_vllm(endpoint)
        elif endpoint.type == ProviderType.TRTLLM:
            return await self._discover_trtllm(endpoint)
        elif endpoint.type == ProviderType.SGLANG:
            return await self._discover_sglang(endpoint)
        else:
            logger.warning(f"Unknown endpoint type: {endpoint.type}")
            return []

    async def _discover_ollama(self, endpoint: EndpointConfig) -> list[str]:
        """Discover models from Ollama endpoint.

        Ollama exposes GET /api/tags which returns:
        {
            "models": [
                {
                    "name": "phi4:14b",
                    "modified_at": "...",
                    "size": 123456,
                    "digest": "sha256:...",
                    "details": {
                        "family": "phi",
                        "parameter_size": "14B",
                        "quantization_level": "Q4_0"
                    }
                }
            ]
        }
        """
        if self._client is None:
            return []

        try:
            url = f"{endpoint.url}/api/tags"
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()

            # Clear existing models for this endpoint
            self._catalog.remove_endpoint_models(endpoint.name)

            model_names = []
            for model_data in data.get("models", []):
                name = model_data.get("name")
                if not name:
                    continue

                details = model_data.get("details", {})
                model = DiscoveredModel(
                    name=name,
                    endpoint=endpoint.name,
                    size_bytes=model_data.get("size"),
                    digest=model_data.get("digest"),
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                )

                # Parse modified_at if present
                if modified_at := model_data.get("modified_at"):
                    try:
                        model.modified_at = datetime.fromisoformat(
                            modified_at.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        pass

                self._catalog.add_model(model)
                model_names.append(name)

            logger.debug(
                f"Discovered {len(model_names)} models from {endpoint.name}"
            )
            return model_names

        except httpx.HTTPError as e:
            logger.warning(f"HTTP error discovering from {endpoint.name}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Error discovering from {endpoint.name}: {e}")
            return []

    async def _discover_vllm(self, endpoint: EndpointConfig) -> list[str]:
        """Discover models from vLLM endpoint.

        vLLM exposes GET /v1/models which returns OpenAI-compatible format:
        {
            "data": [
                {
                    "id": "meta-llama/Llama-3.1-8B-Instruct",
                    "object": "model",
                    "owned_by": "vllm"
                }
            ]
        }
        """
        if self._client is None:
            return []

        try:
            url = f"{endpoint.url}/v1/models"
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()

            # Clear existing models for this endpoint
            self._catalog.remove_endpoint_models(endpoint.name)

            model_names = []
            for model_data in data.get("data", []):
                name = model_data.get("id")
                if not name:
                    continue

                model = DiscoveredModel(
                    name=name,
                    endpoint=endpoint.name,
                )
                self._catalog.add_model(model)
                model_names.append(name)

            logger.debug(
                f"Discovered {len(model_names)} models from {endpoint.name}"
            )
            return model_names

        except httpx.HTTPError as e:
            logger.warning(f"HTTP error discovering from {endpoint.name}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Error discovering from {endpoint.name}: {e}")
            return []

    async def _discover_trtllm(self, endpoint: EndpointConfig) -> list[str]:
        """Discover models from TensorRT-LLM endpoint.

        TRT-LLM typically uses Triton Inference Server which exposes:
        GET /v2/models
        {
            "models": [
                {"name": "model_name", "version": "1", "state": "READY"}
            ]
        }
        """
        if self._client is None:
            return []

        try:
            url = f"{endpoint.url}/v2/models"
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()

            # Clear existing models for this endpoint
            self._catalog.remove_endpoint_models(endpoint.name)

            model_names = []
            for model_data in data.get("models", []):
                name = model_data.get("name")
                state = model_data.get("state")
                if not name or state != "READY":
                    continue

                model = DiscoveredModel(
                    name=name,
                    endpoint=endpoint.name,
                )
                self._catalog.add_model(model)
                model_names.append(name)

            logger.debug(
                f"Discovered {len(model_names)} models from {endpoint.name}"
            )
            return model_names

        except httpx.HTTPError as e:
            logger.warning(f"HTTP error discovering from {endpoint.name}: {e}")
            return []
        except Exception as e:
            logger.warning(f"Error discovering from {endpoint.name}: {e}")
            return []

    async def _discover_sglang(self, endpoint: EndpointConfig) -> list[str]:
        """Discover models from SGLang endpoint.

        SGLang exposes OpenAI-compatible /v1/models endpoint.
        """
        # SGLang uses same API as vLLM
        return await self._discover_vllm(endpoint)

    async def discover_endpoint(self, endpoint_name: str) -> list[str]:
        """Discover models from a specific endpoint by name.

        Args:
            endpoint_name: Name of endpoint to discover from

        Returns:
            List of discovered model names, or empty if endpoint not found
        """
        for endpoint in self._endpoints:
            if endpoint.name == endpoint_name and endpoint.enabled:
                return await self._discover_endpoint(endpoint)
        return []

    def update_endpoints(self, endpoints: list[EndpointConfig]) -> None:
        """Update the list of endpoints to discover from.

        Args:
            endpoints: New list of endpoint configurations
        """
        self._endpoints = endpoints

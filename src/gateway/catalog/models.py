"""Model catalog data models.

Provides the core data structures for tracking discovered models
and their availability across endpoints.
"""

import fnmatch
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from gateway.config import EnvironmentConfig


class DiscoveredModel(BaseModel):
    """A model discovered from an endpoint.

    Represents a model that was found during endpoint discovery,
    including when it was discovered and basic metadata.
    """

    name: str  # Model identifier (e.g., phi4:14b)
    endpoint: str  # Endpoint name where discovered (e.g., gpunode-ollama)
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Optional metadata from discovery
    size_bytes: int | None = None
    modified_at: datetime | None = None
    digest: str | None = None  # Model hash/digest if available
    family: str | None = None  # Model family (e.g., llama, phi)
    parameter_size: str | None = None  # e.g., "14b", "70b"
    quantization: str | None = None  # e.g., "Q4_0", "fp16"

    def matches_pattern(self, pattern: str) -> bool:
        """Check if model name matches a glob pattern.

        Supports patterns like:
        - "phi4:14b" - exact match
        - "phi4:*" - all phi4 variants
        - "nemotron*" - all nemotron models
        - "*:70b" - all 70b models
        """
        return fnmatch.fnmatch(self.name, pattern)


class ModelCatalog(BaseModel):
    """Central catalog of all discovered models.

    Maintains a list of models discovered from all endpoints,
    with methods to query available models by various criteria.
    """

    discovered: list[DiscoveredModel] = Field(default_factory=list)
    last_discovery: datetime | None = None

    def add_model(self, model: DiscoveredModel) -> None:
        """Add or update a discovered model.

        If a model with the same name and endpoint exists, updates it.
        """
        # Remove existing entry if present
        self.discovered = [
            m
            for m in self.discovered
            if not (m.name == model.name and m.endpoint == model.endpoint)
        ]
        self.discovered.append(model)

    def remove_endpoint_models(self, endpoint: str) -> None:
        """Remove all models from a specific endpoint."""
        self.discovered = [m for m in self.discovered if m.endpoint != endpoint]

    def get_endpoints_for_model(self, model: str) -> list[str]:
        """Get all endpoints that have a specific model.

        Args:
            model: Model name to search for

        Returns:
            List of endpoint names that have this model
        """
        return list({m.endpoint for m in self.discovered if m.name == model})

    def get_models_for_endpoint(self, endpoint: str) -> list[str]:
        """Get all models available on a specific endpoint.

        Args:
            endpoint: Endpoint name

        Returns:
            List of model names available on this endpoint
        """
        return [m.name for m in self.discovered if m.endpoint == endpoint]

    def get_all_models(self) -> list[str]:
        """Get all unique model names."""
        return list({m.name for m in self.discovered})

    def get_all_endpoints(self) -> list[str]:
        """Get all endpoints with discovered models."""
        return list({m.endpoint for m in self.discovered})

    def has_model(self, model: str, endpoint: str | None = None) -> bool:
        """Check if a model exists, optionally on a specific endpoint."""
        for m in self.discovered:
            if m.name == model:
                if endpoint is None or m.endpoint == endpoint:
                    return True
        return False

    def effective_models(
        self,
        environment: EnvironmentConfig | None,
        endpoint_labels: dict[str, dict[str, str]] | None = None,
    ) -> list[DiscoveredModel]:
        """Get models available in a given environment.

        Filters models based on:
        1. Environment's allowed_endpoints (if specified)
        2. Environment's endpoint_filter labels (if specified)
        3. Environment's approved_models (if not allow_all_discovered)

        Args:
            environment: Environment configuration to filter by
            endpoint_labels: Dict mapping endpoint name to its labels

        Returns:
            List of DiscoveredModel objects available in the environment
        """
        if environment is None:
            return list(self.discovered)

        endpoint_labels = endpoint_labels or {}
        effective = []

        for model in self.discovered:
            # Check if endpoint is allowed
            if not self._endpoint_allowed(model.endpoint, environment, endpoint_labels):
                continue

            # Check if model is approved
            if not self._model_approved(model.name, environment):
                continue

            effective.append(model)

        return effective

    def _endpoint_allowed(
        self,
        endpoint: str,
        environment: EnvironmentConfig,
        endpoint_labels: dict[str, dict[str, str]],
    ) -> bool:
        """Check if endpoint is allowed in environment."""
        # Check explicit allowed_endpoints
        if environment.allowed_endpoints:
            if endpoint not in environment.allowed_endpoints:
                return False

        # Check label filters
        if environment.endpoint_filter:
            labels = endpoint_labels.get(endpoint, {})
            for key, value in environment.endpoint_filter.items():
                if labels.get(key) != value:
                    return False

        return True

    def _model_approved(self, model: str, environment: EnvironmentConfig) -> bool:
        """Check if model is approved in environment."""
        if environment.allow_all_discovered:
            return True

        if not environment.approved_models:
            # No approved models list = allow all
            return True

        # Check if model matches any approved pattern
        for pattern in environment.approved_models:
            if fnmatch.fnmatch(model, pattern):
                return True

        return False

    def find_model(self, model_pattern: str) -> list[DiscoveredModel]:
        """Find all models matching a pattern.

        Args:
            model_pattern: Glob pattern to match

        Returns:
            List of matching DiscoveredModel objects
        """
        return [m for m in self.discovered if m.matches_pattern(model_pattern)]

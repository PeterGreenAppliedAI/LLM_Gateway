"""Provider registry - manages live adapter instances and health state.

Per rule.md:
- Single Responsibility: Registry only manages provider lifecycle and health
- Contracts: Clear interface for provider lookup and health queries

Supports both legacy ProviderConfig and new EndpointConfig for
backward compatibility during the migration period.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from gateway.catalog.models import ModelCatalog
from gateway.config import GatewayConfig, ProviderConfig, EndpointConfig
from gateway.models.common import HealthStatus
from gateway.providers import ProviderAdapter, create_adapter


class ProviderHealth:
    """Health state for a single provider."""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self.status: HealthStatus = HealthStatus.UNKNOWN
        self.last_check: Optional[datetime] = None
        self.last_healthy: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.error_message: Optional[str] = None

    def record_healthy(self) -> None:
        """Record a successful health check."""
        self.status = HealthStatus.HEALTHY
        self.last_check = datetime.now(timezone.utc)
        self.last_healthy = self.last_check
        self.consecutive_failures = 0
        self.error_message = None

    def record_unhealthy(self, status: HealthStatus, error: Optional[str] = None) -> None:
        """Record a failed health check."""
        self.status = status
        self.last_check = datetime.now(timezone.utc)
        self.consecutive_failures += 1
        self.error_message = error

    def is_available(self) -> bool:
        """Check if provider is available for requests."""
        return self.status == HealthStatus.HEALTHY

    def time_since_healthy(self) -> Optional[timedelta]:
        """Get time since last healthy state."""
        if self.last_healthy is None:
            return None
        return datetime.now(timezone.utc) - self.last_healthy


class ProviderRegistry:
    """Registry of provider adapters with health tracking.

    Manages:
    - Creating adapters from config
    - Tracking health state per provider
    - Background health monitoring
    - Provider lookup and listing
    - Model catalog integration
    """

    def __init__(self, config: GatewayConfig):
        """Initialize registry from gateway config.

        Args:
            config: Validated gateway configuration
        """
        self._config = config
        self._adapters: Dict[str, ProviderAdapter] = {}
        self._health: Dict[str, ProviderHealth] = {}
        self._endpoint_configs: Dict[str, EndpointConfig] = {}
        self._health_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._catalog: ModelCatalog = ModelCatalog()

        # Health check settings
        self._health_interval_seconds: float = 30.0
        self._health_timeout_seconds: float = 10.0

    @property
    def catalog(self) -> ModelCatalog:
        """Get the model catalog."""
        return self._catalog

    async def initialize(self) -> None:
        """Initialize all configured providers.

        Creates adapter instances for all enabled providers/endpoints.
        Does NOT start health monitoring - call start_health_monitoring() for that.
        """
        # Initialize from endpoints if available, otherwise from providers
        if self._config.endpoints:
            for endpoint_config in self._config.get_enabled_endpoints():
                await self._register_endpoint(endpoint_config)
        else:
            for provider_config in self._config.get_enabled_providers():
                await self._register_provider(provider_config)

    async def _register_provider(self, config: ProviderConfig) -> None:
        """Create and register a single provider adapter."""
        adapter = create_adapter(config)
        self._adapters[config.name] = adapter
        self._health[config.name] = ProviderHealth(config.name)

    async def _register_endpoint(self, config: EndpointConfig) -> None:
        """Create and register an adapter from endpoint config."""
        # Convert EndpointConfig to ProviderConfig for adapter creation
        provider_config = ProviderConfig(
            name=config.name,
            type=config.type,
            base_url=config.url,
            enabled=config.enabled,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
        adapter = create_adapter(provider_config)
        self._adapters[config.name] = adapter
        self._health[config.name] = ProviderHealth(config.name)
        self._endpoint_configs[config.name] = config

    def get(self, name: str) -> Optional[ProviderAdapter]:
        """Get provider adapter by name.

        Args:
            name: Provider name as configured

        Returns:
            ProviderAdapter if found and enabled, None otherwise
        """
        return self._adapters.get(name)

    def get_health(self, name: str) -> Optional[ProviderHealth]:
        """Get health state for a provider.

        Args:
            name: Provider name

        Returns:
            ProviderHealth if provider exists, None otherwise
        """
        return self._health.get(name)

    def is_healthy(self, name: str) -> bool:
        """Check if a provider is healthy.

        Args:
            name: Provider name

        Returns:
            True if provider exists and is healthy
        """
        health = self._health.get(name)
        return health is not None and health.is_available()

    def list_providers(self) -> List[str]:
        """List all registered provider names."""
        return list(self._adapters.keys())

    def list_healthy_providers(self) -> List[str]:
        """List all healthy provider names."""
        return [name for name in self._adapters if self.is_healthy(name)]

    def get_default_provider(self) -> Optional[str]:
        """Get the default provider name from config.

        Returns first enabled provider if no routing config specified.
        """
        if self._config.routing and self._config.routing.default_provider:
            return self._config.routing.default_provider

        # Fall back to first enabled provider
        enabled = self._config.get_enabled_providers()
        if enabled:
            return enabled[0].name
        return None

    def get_fallback_chain(self, exclude: Optional[str] = None) -> List[str]:
        """Get ordered list of providers for fallback.

        Args:
            exclude: Provider name to exclude (usually the failed primary)

        Returns:
            List of provider names in config order, excluding the specified one
        """
        chain = []
        for provider_config in self._config.get_enabled_providers():
            if provider_config.name != exclude:
                chain.append(provider_config.name)
        return chain

    def get_endpoint_config(self, name: str) -> Optional[EndpointConfig]:
        """Get endpoint configuration by name.

        Args:
            name: Endpoint name

        Returns:
            EndpointConfig if found, None otherwise
        """
        return self._endpoint_configs.get(name)

    def get_endpoints_with_model(
        self,
        model: str,
        environment_filter: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Get endpoints that have a specific model.

        Uses the catalog to find which endpoints have the model.

        Args:
            model: Model name to search for
            environment_filter: Optional label filter for endpoints

        Returns:
            List of endpoint names that have the model
        """
        endpoints = self._catalog.get_endpoints_for_model(model)

        if environment_filter:
            filtered = []
            for ep_name in endpoints:
                config = self._endpoint_configs.get(ep_name)
                if config and self._matches_filter(config.labels, environment_filter):
                    filtered.append(ep_name)
            return filtered

        return endpoints

    def _matches_filter(
        self,
        labels: Dict[str, str],
        required: Dict[str, str],
    ) -> bool:
        """Check if labels match required filter."""
        for key, value in required.items():
            if labels.get(key) != value:
                return False
        return True

    def get_endpoint_labels(self) -> Dict[str, Dict[str, str]]:
        """Get all endpoint labels as a dict.

        Returns:
            Dict mapping endpoint name to its labels dict
        """
        return {
            name: config.labels
            for name, config in self._endpoint_configs.items()
        }

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def start_health_monitoring(self) -> None:
        """Start background health monitoring task."""
        if self._health_task is not None:
            return  # Already running

        self._shutdown = False
        self._health_task = asyncio.create_task(self._health_monitor_loop())

    async def stop_health_monitoring(self) -> None:
        """Stop background health monitoring task."""
        self._shutdown = True
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

    async def _health_monitor_loop(self) -> None:
        """Background loop that periodically checks all provider health."""
        while not self._shutdown:
            await self.check_all_health()
            await asyncio.sleep(self._health_interval_seconds)

    async def check_all_health(self) -> Dict[str, HealthStatus]:
        """Check health of all registered providers.

        Returns:
            Dict mapping provider name to health status
        """
        results = {}
        tasks = []

        for name, adapter in self._adapters.items():
            tasks.append(self._check_provider_health(name, adapter))

        await asyncio.gather(*tasks, return_exceptions=True)

        for name in self._adapters:
            results[name] = self._health[name].status

        return results

    async def _check_provider_health(
        self, name: str, adapter: ProviderAdapter
    ) -> None:
        """Check health of a single provider and update state."""
        try:
            status = await asyncio.wait_for(
                adapter.health(),
                timeout=self._health_timeout_seconds
            )

            if status == HealthStatus.HEALTHY:
                self._health[name].record_healthy()
            else:
                self._health[name].record_unhealthy(status)

        except asyncio.TimeoutError:
            self._health[name].record_unhealthy(
                HealthStatus.UNHEALTHY,
                error="Health check timed out"
            )
        except Exception as e:
            self._health[name].record_unhealthy(
                HealthStatus.UNHEALTHY,
                error=str(e)
            )

    async def check_health(self, name: str) -> HealthStatus:
        """Check health of a specific provider (on-demand).

        Args:
            name: Provider name

        Returns:
            Current health status
        """
        adapter = self._adapters.get(name)
        if adapter is None:
            return HealthStatus.UNKNOWN

        await self._check_provider_health(name, adapter)
        return self._health[name].status

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def close(self) -> None:
        """Shutdown registry and all providers."""
        await self.stop_health_monitoring()

        # Close all adapter connections
        for adapter in self._adapters.values():
            await adapter.close()

        self._adapters.clear()
        self._health.clear()

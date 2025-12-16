"""Provider dispatch - registry, health monitoring, and request routing.

This module handles:
- Provider registry: holds live adapter instances
- Health monitoring: background polling of provider health
- Dispatcher: routes requests to providers with fallback support

NOT smart routing - just explicit dispatch with health-aware fallback.
"""

from gateway.dispatch.registry import ProviderRegistry
from gateway.dispatch.dispatcher import Dispatcher, DispatchResult, DispatchError

__all__ = [
    "ProviderRegistry",
    "Dispatcher",
    "DispatchResult",
    "DispatchError",
]

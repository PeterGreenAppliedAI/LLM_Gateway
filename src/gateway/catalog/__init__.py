"""Model catalog module for discovered models and endpoints.

This module provides:
- DiscoveredModel: Model information discovered from endpoints
- ModelCatalog: Central registry of all discovered models
- ModelDiscoveryService: Background service that queries endpoints for available models
"""

from gateway.catalog.models import DiscoveredModel, ModelCatalog
from gateway.catalog.discovery import ModelDiscoveryService

__all__ = ["DiscoveredModel", "ModelCatalog", "ModelDiscoveryService"]

"""Security module for prompt injection defense.

Provides layered defense against prompt injection attacks:
- Unicode sanitization (zero latency)
- Content wrapping with trust markers (zero latency)
- Pattern detection and logging (minimal latency)
- Async background analysis (zero request latency)
"""

from gateway.security.sanitizer import Sanitizer, SanitizationResult
from gateway.security.injection import (
    InjectionDetector,
    DetectionResult,
    ThreatLevel,
    ContentWrapper,
)
from gateway.security.analyzer import AsyncSecurityAnalyzer, SecurityAlert
from gateway.security.guard import (
    GuardResult,
    LlamaGuardClient,
    GraniteGuardianClient,
    create_guard_client,
)

__all__ = [
    "Sanitizer",
    "SanitizationResult",
    "InjectionDetector",
    "DetectionResult",
    "ThreatLevel",
    "ContentWrapper",
    "AsyncSecurityAnalyzer",
    "SecurityAlert",
    "GuardResult",
    "LlamaGuardClient",
    "GraniteGuardianClient",
    "create_guard_client",
]

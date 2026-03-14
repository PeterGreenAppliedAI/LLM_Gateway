"""Security module for prompt injection defense.

Provides layered defense against prompt injection attacks:
- Unicode sanitization (zero latency)
- Content wrapping with trust markers (zero latency)
- Pattern detection and logging (minimal latency)
- Async background analysis (zero request latency)
"""

from gateway.security.analyzer import AsyncSecurityAnalyzer, SecurityAlert
from gateway.security.guard import (
    CircuitBreaker,
    GraniteGuardianClient,
    GuardResult,
    LlamaGuardClient,
    create_guard_client,
)
from gateway.security.injection import (
    ContentWrapper,
    DetectionResult,
    InjectionDetector,
    ThreatLevel,
)
from gateway.security.pii import PIIMatch, PIIScanResult, PIIScrubber
from gateway.security.sanitizer import SanitizationResult, Sanitizer

__all__ = [
    "Sanitizer",
    "SanitizationResult",
    "InjectionDetector",
    "DetectionResult",
    "ThreatLevel",
    "ContentWrapper",
    "AsyncSecurityAnalyzer",
    "SecurityAlert",
    "CircuitBreaker",
    "GuardResult",
    "LlamaGuardClient",
    "GraniteGuardianClient",
    "create_guard_client",
    "PIIScrubber",
    "PIIScanResult",
    "PIIMatch",
]

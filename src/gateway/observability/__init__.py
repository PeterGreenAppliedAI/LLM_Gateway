"""Observability - structured logging and Prometheus metrics.

This module handles:
- Structured JSON logging with request context
- Prometheus metrics for monitoring
- Request tracing and latency tracking

Per rule.md: Single Responsibility, Auditability.
Per PRD Section 11: No raw prompts stored.

Metrics tracked:
- request_latency_ms: Total request time (histogram)
- time_to_first_token_ms: TTFT for streaming (histogram)
- tokens_per_second: Generation throughput (histogram)
- tokens_prompt_total: Input tokens (counter)
- tokens_completion_total: Output tokens (counter)
- requests_total: Request count by labels (counter)
- provider_errors_total: Error count (counter)
- active_requests: Concurrent requests (gauge)
"""

from gateway.observability.logging import (
    get_logger,
    configure_logging,
    RequestContext,
    LogConfig,
)
from gateway.observability.metrics import (
    MetricsCollector,
    get_metrics,
    MetricsConfig,
)

__all__ = [
    "get_logger",
    "configure_logging",
    "RequestContext",
    "LogConfig",
    "MetricsCollector",
    "get_metrics",
    "MetricsConfig",
]

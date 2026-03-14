"""Prometheus metrics for gateway observability.

Per rule.md:
- Auditability: Metrics enable monitoring and alerting
- Explicit Boundaries: Clear metric definitions

Per PRD Section 11:
- requests_total{provider,model,task,status}
- request_latency_ms (histogram)
- tokens_prompt_total
- tokens_completion_total
- provider_errors_total
- active_requests{provider}

Additional performance metrics:
- time_to_first_token_ms (histogram) - critical for streaming UX
- tokens_per_second (histogram) - generation throughput
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional

from pydantic import BaseModel, Field

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class MetricsConfig(BaseModel):
    """Configuration for metrics collection."""

    enabled: bool = Field(default=True, description="Whether metrics collection is enabled")
    prefix: str = Field(default="gateway", description="Metric name prefix")


# Histogram buckets for latency metrics (in milliseconds)
LATENCY_BUCKETS = (
    5,
    10,
    25,
    50,
    75,
    100,
    150,
    200,
    300,
    400,
    500,
    750,
    1000,
    1500,
    2000,
    3000,
    5000,
    7500,
    10000,
    15000,
    30000,
    60000,
)

# Histogram buckets for TTFT (typically faster)
TTFT_BUCKETS = (5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000)

# Histogram buckets for tokens per second
TPS_BUCKETS = (1, 5, 10, 20, 30, 40, 50, 75, 100, 150, 200, 300, 500)


class MetricsCollector:
    """Collects and exposes Prometheus metrics.

    Metrics:
    - requests_total: Counter of requests by provider, model, task, status
    - request_latency_ms: Histogram of total request latency
    - time_to_first_token_ms: Histogram of TTFT for streaming
    - tokens_per_second: Histogram of generation throughput
    - tokens_prompt_total: Counter of input tokens
    - tokens_completion_total: Counter of output tokens
    - provider_errors_total: Counter of provider errors
    - active_requests: Gauge of concurrent requests per provider
    """

    def __init__(
        self, config: MetricsConfig | None = None, registry: Optional["CollectorRegistry"] = None
    ):
        """Initialize metrics collector.

        Args:
            config: Metrics configuration
            registry: Optional custom Prometheus registry (for testing)
        """
        self._config = config or MetricsConfig()
        self._registry = registry

        if not PROMETHEUS_AVAILABLE:
            self._enabled = False
            return

        self._enabled = self._config.enabled
        if not self._enabled:
            return

        prefix = self._config.prefix
        reg_kwargs = {"registry": registry} if registry else {}

        # Request counter
        self._requests_total = Counter(
            f"{prefix}_requests_total",
            "Total number of requests",
            ["provider", "model", "task", "status"],
            **reg_kwargs,
        )

        # Latency histogram (milliseconds)
        self._request_latency = Histogram(
            f"{prefix}_request_latency_ms",
            "Request latency in milliseconds",
            ["provider", "model", "task"],
            buckets=LATENCY_BUCKETS,
            **reg_kwargs,
        )

        # Time to first token histogram (milliseconds)
        self._ttft = Histogram(
            f"{prefix}_time_to_first_token_ms",
            "Time to first token in milliseconds",
            ["provider", "model"],
            buckets=TTFT_BUCKETS,
            **reg_kwargs,
        )

        # Tokens per second histogram
        self._tokens_per_second = Histogram(
            f"{prefix}_tokens_per_second",
            "Token generation throughput (tokens/second)",
            ["provider", "model"],
            buckets=TPS_BUCKETS,
            **reg_kwargs,
        )

        # Token counters
        self._tokens_prompt = Counter(
            f"{prefix}_tokens_prompt_total",
            "Total prompt/input tokens",
            ["provider", "model"],
            **reg_kwargs,
        )

        self._tokens_completion = Counter(
            f"{prefix}_tokens_completion_total",
            "Total completion/output tokens",
            ["provider", "model"],
            **reg_kwargs,
        )

        # Error counter
        self._provider_errors = Counter(
            f"{prefix}_provider_errors_total",
            "Total provider errors",
            ["provider", "error_type"],
            **reg_kwargs,
        )

        # Active requests gauge
        self._active_requests = Gauge(
            f"{prefix}_active_requests", "Number of active requests", ["provider"], **reg_kwargs
        )

    @property
    def enabled(self) -> bool:
        """Check if metrics collection is enabled."""
        return self._enabled

    def record_request(
        self,
        provider: str,
        model: str,
        task: str,
        status: str,
        latency_ms: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        time_to_first_token_ms: float | None = None,
        tokens_per_second: float | None = None,
    ) -> None:
        """Record a completed request with all metrics.

        Args:
            provider: Provider name
            model: Model name
            task: Task type
            status: Request status (success, error, etc.)
            latency_ms: Total request latency in milliseconds
            prompt_tokens: Number of input tokens
            completion_tokens: Number of output tokens
            time_to_first_token_ms: Time to first token (for streaming)
            tokens_per_second: Generation throughput
        """
        if not self._enabled:
            return

        # Sanitize labels to prevent injection
        provider = self._sanitize_label(provider)
        model = self._sanitize_label(model)
        task = self._sanitize_label(task)
        status = self._sanitize_label(status)

        # Request counter
        self._requests_total.labels(provider=provider, model=model, task=task, status=status).inc()

        # Latency histogram
        self._request_latency.labels(provider=provider, model=model, task=task).observe(latency_ms)

        # Token metrics
        if prompt_tokens is not None:
            self._tokens_prompt.labels(provider=provider, model=model).inc(prompt_tokens)

        if completion_tokens is not None:
            self._tokens_completion.labels(provider=provider, model=model).inc(completion_tokens)

        # TTFT
        if time_to_first_token_ms is not None:
            self._ttft.labels(provider=provider, model=model).observe(time_to_first_token_ms)

        # Tokens per second
        if tokens_per_second is not None:
            self._tokens_per_second.labels(provider=provider, model=model).observe(
                tokens_per_second
            )

    def record_error(self, provider: str, error_type: str) -> None:
        """Record a provider error.

        Args:
            provider: Provider name
            error_type: Type of error
        """
        if not self._enabled:
            return

        provider = self._sanitize_label(provider)
        error_type = self._sanitize_label(error_type)

        self._provider_errors.labels(provider=provider, error_type=error_type).inc()

    @contextmanager
    def track_request(self, provider: str) -> Generator[None, None, None]:
        """Context manager to track active requests.

        Args:
            provider: Provider name

        Usage:
            with metrics.track_request("ollama"):
                # process request
        """
        if not self._enabled:
            yield
            return

        provider = self._sanitize_label(provider)

        self._active_requests.labels(provider=provider).inc()
        try:
            yield
        finally:
            self._active_requests.labels(provider=provider).dec()

    def get_active_requests(self, provider: str) -> float:
        """Get current active request count for a provider.

        Args:
            provider: Provider name

        Returns:
            Number of active requests
        """
        if not self._enabled:
            return 0

        provider = self._sanitize_label(provider)
        return self._active_requests.labels(provider=provider)._value.get()

    def _sanitize_label(self, value: str) -> str:
        """Sanitize a label value for Prometheus.

        Security: Prevents label injection attacks.
        """
        if not value:
            return "unknown"
        # Replace unsafe characters
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in value)
        # Truncate to reasonable length
        return safe[:64]


# Global metrics instance
_metrics: MetricsCollector | None = None


def get_metrics(config: MetricsConfig | None = None) -> MetricsCollector:
    """Get the global metrics collector.

    Args:
        config: Metrics configuration (only used on first call)

    Returns:
        MetricsCollector instance
    """
    global _metrics

    if _metrics is None:
        _metrics = MetricsCollector(config)

    return _metrics


def reset_metrics() -> None:
    """Reset the global metrics instance (for testing)."""
    global _metrics
    _metrics = None

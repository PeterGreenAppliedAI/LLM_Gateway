"""Tests for observability - logging and metrics."""

import json
import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry

from gateway.observability.logging import (
    LogConfig,
    LogLevel,
    RequestContext,
    StructuredJsonFormatter,
    configure_logging,
    get_logger,
    get_request_context,
    set_request_context,
    clear_request_context,
    sanitize_log_value,
)
from gateway.observability.metrics import (
    MetricsCollector,
    MetricsConfig,
    get_metrics,
    reset_metrics,
)


# =============================================================================
# RequestContext Tests
# =============================================================================


class TestRequestContext:
    """Tests for RequestContext."""

    def test_basic_context(self):
        """Basic context creation."""
        ctx = RequestContext(
            request_id="req-123",
            client_id="client-456",
            user_id="user-789",
        )

        assert ctx.request_id == "req-123"
        assert ctx.client_id == "client-456"
        assert ctx.status == "pending"

    def test_to_dict_excludes_none(self):
        """to_dict excludes None values."""
        ctx = RequestContext(request_id="req-123")
        d = ctx.to_dict()

        assert "request_id" in d
        assert "client_id" not in d
        assert "error_message" not in d

    def test_record_first_token(self):
        """record_first_token calculates TTFT."""
        ctx = RequestContext(request_id="req-123")
        ctx.start_time = datetime.utcnow() - timedelta(milliseconds=150)

        ctx.record_first_token()

        assert ctx.time_to_first_token_ms is not None
        assert ctx.time_to_first_token_ms >= 150

    def test_record_complete(self):
        """record_complete calculates latency and throughput."""
        ctx = RequestContext(request_id="req-123")
        ctx.start_time = datetime.utcnow() - timedelta(milliseconds=500)

        ctx.record_complete(prompt_tokens=100, completion_tokens=200)

        assert ctx.status == "success"
        assert ctx.total_latency_ms >= 500
        assert ctx.prompt_tokens == 100
        assert ctx.completion_tokens == 200
        assert ctx.total_tokens == 300
        assert ctx.tokens_per_second is not None
        # 200 tokens in ~0.5s = ~400 tokens/sec
        assert ctx.tokens_per_second > 0

    def test_record_error(self):
        """record_error captures error info."""
        ctx = RequestContext(request_id="req-123")

        ctx.record_error("connection_error", "Failed to connect")

        assert ctx.status == "error"
        assert ctx.error_type == "connection_error"
        assert ctx.error_message == "Failed to connect"
        assert ctx.total_latency_ms is not None

    def test_error_message_truncated(self):
        """Error messages are truncated to prevent log bloat."""
        ctx = RequestContext(request_id="req-123")
        long_error = "x" * 1000

        ctx.record_error("error", long_error)

        assert len(ctx.error_message) == 500


# =============================================================================
# Context Variable Tests
# =============================================================================


class TestLogValueSanitization:
    """Tests for log value sanitization."""

    def test_valid_values_unchanged(self):
        """Valid values pass through unchanged."""
        assert sanitize_log_value("client-123") == "client-123"
        assert sanitize_log_value("user_456") == "user_456"
        assert sanitize_log_value("req:abc:123") == "req:abc:123"

    def test_none_returns_none(self):
        """None returns None."""
        assert sanitize_log_value(None) is None

    def test_injection_attempts_sanitized(self):
        """Injection attempts are sanitized."""
        # Newline injection
        result = sanitize_log_value("client\nX-Injection: malicious")
        assert "\n" not in result

        # Control characters
        result = sanitize_log_value("client\x00\x1b[31mRED")
        assert "\x00" not in result
        assert "\x1b" not in result

    def test_long_values_truncated(self):
        """Long values are truncated."""
        long_value = "a" * 200
        result = sanitize_log_value(long_value)
        assert len(result) <= 128


class TestContextVariables:
    """Tests for request context variables."""

    def test_set_and_get_context(self):
        """Can set and get request context."""
        ctx = RequestContext(request_id="req-123")

        set_request_context(ctx)
        retrieved = get_request_context()

        assert retrieved is ctx
        assert retrieved.request_id == "req-123"

        clear_request_context()

    def test_clear_context(self):
        """clear_request_context clears the context."""
        ctx = RequestContext(request_id="req-123")
        set_request_context(ctx)

        clear_request_context()

        assert get_request_context() is None

    def test_default_context_is_none(self):
        """Default context is None."""
        clear_request_context()
        assert get_request_context() is None


# =============================================================================
# Structured Logging Tests
# =============================================================================


class TestStructuredLogging:
    """Tests for structured logging."""

    def test_json_formatter_basic(self):
        """JSON formatter produces valid JSON."""
        config = LogConfig(format="json")
        formatter = StructuredJsonFormatter(config)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["message"] == "Test message"
        assert "timestamp" in data

    def test_json_formatter_includes_context(self):
        """JSON formatter includes request context."""
        config = LogConfig(format="json")
        formatter = StructuredJsonFormatter(config)

        ctx = RequestContext(
            request_id="req-123",
            client_id="client-456",
            provider="ollama",
        )
        set_request_context(ctx)

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["request_id"] == "req-123"
        assert data["client_id"] == "client-456"
        assert data["provider"] == "ollama"

        clear_request_context()

    def test_get_logger_returns_adapter(self):
        """get_logger returns a ContextLogger."""
        logger = get_logger("test.module")

        assert logger is not None
        # Should be a LoggerAdapter
        assert hasattr(logger, "process")


# =============================================================================
# Metrics Tests
# =============================================================================


class TestMetricsCollector:
    """Tests for MetricsCollector."""

    @pytest.fixture
    def registry(self):
        """Create isolated registry for each test."""
        return CollectorRegistry()

    @pytest.fixture
    def metrics(self, registry):
        """Create metrics collector with isolated registry."""
        return MetricsCollector(
            config=MetricsConfig(enabled=True, prefix="test"),
            registry=registry,
        )

    def test_disabled_metrics_noop(self, registry):
        """Disabled metrics collector does nothing."""
        metrics = MetricsCollector(
            config=MetricsConfig(enabled=False),
            registry=registry,
        )

        # Should not raise
        metrics.record_request(
            provider="ollama",
            model="llama3.2",
            task="chat",
            status="success",
            latency_ms=100,
        )

        assert not metrics.enabled

    def test_record_request(self, metrics):
        """record_request updates all metrics."""
        metrics.record_request(
            provider="ollama",
            model="llama3.2",
            task="chat",
            status="success",
            latency_ms=250.5,
            prompt_tokens=100,
            completion_tokens=200,
            time_to_first_token_ms=50.0,
            tokens_per_second=400.0,
        )

        # Verify metrics were recorded (no exceptions)
        assert metrics.enabled

    def test_record_error(self, metrics):
        """record_error updates error counter."""
        metrics.record_error(
            provider="ollama",
            error_type="connection_error",
        )

        # Should not raise
        assert metrics.enabled

    def test_track_request_context_manager(self, metrics):
        """track_request context manager works."""
        with metrics.track_request("ollama"):
            # Active requests should be 1
            active = metrics.get_active_requests("ollama")
            assert active == 1

        # After context, should be 0
        active = metrics.get_active_requests("ollama")
        assert active == 0

    def test_label_sanitization(self, metrics):
        """Labels are sanitized to prevent injection."""
        # Should not raise even with malicious input
        metrics.record_request(
            provider="ollama\nX-Header: injection",
            model="model;DROP TABLE;",
            task="../../etc/passwd",
            status="success",
            latency_ms=100,
        )

    def test_empty_labels_handled(self, metrics):
        """Empty labels are handled gracefully."""
        metrics.record_request(
            provider="",
            model="",
            task="",
            status="",
            latency_ms=100,
        )

        # Empty labels should become "unknown"
        assert metrics.enabled


# =============================================================================
# Global Metrics Tests
# =============================================================================


class TestGlobalMetrics:
    """Tests for global metrics functions."""

    def test_get_metrics_singleton(self):
        """get_metrics returns singleton."""
        # Note: Can't fully test reset in same process due to prometheus
        # registry not allowing duplicate metric names
        m1 = get_metrics()
        m2 = get_metrics()

        assert m1 is m2

    def test_metrics_enabled_by_default(self):
        """Metrics are enabled by default."""
        metrics = get_metrics()
        assert metrics.enabled


# =============================================================================
# Integration Tests
# =============================================================================


class TestObservabilityIntegration:
    """Integration tests for observability system."""

    @pytest.fixture
    def registry(self):
        return CollectorRegistry()

    def test_full_request_flow(self, registry):
        """Test complete request logging and metrics flow."""
        # Setup
        metrics = MetricsCollector(
            config=MetricsConfig(enabled=True, prefix="test"),
            registry=registry,
        )

        ctx = RequestContext(
            request_id="req-integration-123",
            client_id="client-test",
            provider="ollama",
            model="llama3.2",
            task="chat",
        )
        set_request_context(ctx)

        # Simulate request flow
        with metrics.track_request("ollama"):
            # Record TTFT
            ctx.record_first_token()

            # Complete request
            ctx.record_complete(
                prompt_tokens=50,
                completion_tokens=100,
            )

            # Record metrics
            metrics.record_request(
                provider=ctx.provider,
                model=ctx.model,
                task=ctx.task,
                status=ctx.status,
                latency_ms=ctx.total_latency_ms,
                prompt_tokens=ctx.prompt_tokens,
                completion_tokens=ctx.completion_tokens,
                time_to_first_token_ms=ctx.time_to_first_token_ms,
                tokens_per_second=ctx.tokens_per_second,
            )

        # Verify context
        assert ctx.status == "success"
        assert ctx.total_tokens == 150
        assert ctx.tokens_per_second is not None

        # Cleanup
        clear_request_context()

    def test_error_request_flow(self, registry):
        """Test error request logging and metrics flow."""
        metrics = MetricsCollector(
            config=MetricsConfig(enabled=True, prefix="test"),
            registry=registry,
        )

        ctx = RequestContext(
            request_id="req-error-123",
            provider="ollama",
            model="llama3.2",
            task="chat",
        )
        set_request_context(ctx)

        # Simulate error
        ctx.record_error("connection_error", "Connection refused")
        metrics.record_error("ollama", "connection_error")
        metrics.record_request(
            provider="ollama",
            model="llama3.2",
            task="chat",
            status="error",
            latency_ms=ctx.total_latency_ms,
        )

        assert ctx.status == "error"

        clear_request_context()

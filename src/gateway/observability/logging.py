"""Structured logging with request context.

Per rule.md:
- Auditability: Log tool usage, errors, and state changes
- No Implicit Trust: Redact sensitive data, sanitize logged fields

Per PRD Section 11:
- Structured JSON logging
- No raw prompts stored (configurable redaction)
"""

import logging
import json
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


# Safe pattern for logged identifiers (prevents log injection)
SAFE_LOG_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-.:]{0,127}$")


def sanitize_log_value(value: Optional[str]) -> Optional[str]:
    """Sanitize a value for safe logging.

    Security: Prevents log injection attacks by ensuring logged values
    don't contain newlines or other control characters.
    """
    if value is None:
        return None
    if SAFE_LOG_VALUE_PATTERN.match(value):
        return value
    # Replace unsafe characters
    safe = "".join(c if c.isalnum() or c in "-_.:@" else "_" for c in value)
    return safe[:128]


# Context variable for request-scoped logging context
_request_context: ContextVar[Optional["RequestContext"]] = ContextVar(
    "request_context", default=None
)


class LogLevel(str, Enum):
    """Valid log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogConfig(BaseModel):
    """Configuration for logging."""

    level: LogLevel = Field(default=LogLevel.INFO, description="Log level")
    format: Literal["json", "text"] = Field(
        default="json",
        description="Log format: 'json' or 'text'",
    )
    redact_prompts: bool = Field(
        default=True,
        description="Whether to redact prompt content from logs",
    )
    include_timestamp: bool = Field(default=True)
    include_request_id: bool = Field(default=True)


@dataclass
class RequestContext:
    """Context for a single request, used in structured logging.

    Security: Prompt content is NOT stored here to prevent accidental logging.
    """

    request_id: str
    client_id: Optional[str] = None
    user_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    task: Optional[str] = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Timing metrics (populated as request progresses)
    time_to_first_token_ms: Optional[float] = None
    total_latency_ms: Optional[float] = None

    # Token counts (populated from response)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    # Throughput (calculated)
    tokens_per_second: Optional[float] = None

    # Status
    status: str = "pending"
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging, excluding None values."""
        result = {}
        for k, v in asdict(self).items():
            if v is not None:
                if isinstance(v, datetime):
                    result[k] = v.isoformat()
                else:
                    result[k] = v
        return result

    def record_first_token(self) -> None:
        """Record time to first token."""
        elapsed = datetime.now(timezone.utc) - self.start_time
        self.time_to_first_token_ms = elapsed.total_seconds() * 1000

    def record_complete(
        self,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
    ) -> None:
        """Record request completion with token counts."""
        elapsed = datetime.now(timezone.utc) - self.start_time
        self.total_latency_ms = elapsed.total_seconds() * 1000
        self.status = "success"

        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = completion_tokens

        if self.prompt_tokens and self.completion_tokens:
            self.total_tokens = self.prompt_tokens + self.completion_tokens

        # Calculate tokens per second (completion tokens / total time)
        if completion_tokens and self.total_latency_ms > 0:
            self.tokens_per_second = completion_tokens / (self.total_latency_ms / 1000)

    def record_error(self, error_type: str, error_message: str) -> None:
        """Record request error."""
        elapsed = datetime.now(timezone.utc) - self.start_time
        self.total_latency_ms = elapsed.total_seconds() * 1000
        self.status = "error"
        self.error_type = error_type
        # Truncate error message to prevent log bloat
        self.error_message = error_message[:500] if error_message else None


def set_request_context(ctx: RequestContext) -> None:
    """Set the current request context."""
    _request_context.set(ctx)


def get_request_context() -> Optional[RequestContext]:
    """Get the current request context."""
    return _request_context.get()


def clear_request_context() -> None:
    """Clear the current request context."""
    _request_context.set(None)


class StructuredJsonFormatter(logging.Formatter):
    """JSON formatter that includes request context."""

    def __init__(self, config: LogConfig):
        super().__init__()
        self._config = config

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON with request context.

        Security: All user-provided values are sanitized to prevent log injection.
        """
        log_data: Dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if self._config.include_timestamp:
            log_data["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Add request context if available
        # Security: Sanitize all user-provided values
        ctx = get_request_context()
        if ctx and self._config.include_request_id:
            log_data["request_id"] = sanitize_log_value(ctx.request_id)
            if ctx.client_id:
                log_data["client_id"] = sanitize_log_value(ctx.client_id)
            if ctx.user_id:
                log_data["user_id"] = sanitize_log_value(ctx.user_id)
            if ctx.provider:
                log_data["provider"] = sanitize_log_value(ctx.provider)
            if ctx.model:
                log_data["model"] = sanitize_log_value(ctx.model)
            if ctx.task:
                log_data["task"] = sanitize_log_value(ctx.task)

        # Add extra fields from record
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


class StructuredTextFormatter(logging.Formatter):
    """Text formatter that includes request context."""

    def __init__(self, config: LogConfig):
        super().__init__()
        self._config = config

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as structured text."""
        parts = []

        if self._config.include_timestamp:
            parts.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

        parts.append(f"[{record.levelname}]")
        parts.append(f"{record.name}:")

        # Add request context if available
        ctx = get_request_context()
        if ctx and self._config.include_request_id:
            parts.append(f"[{ctx.request_id[:8]}]")

        parts.append(record.getMessage())

        result = " ".join(parts)

        if record.exc_info:
            result += "\n" + self.formatException(record.exc_info)

        return result


class ContextLogger(logging.LoggerAdapter):
    """Logger adapter that automatically includes request context."""

    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        """Add request context to log records."""
        extra = kwargs.get("extra", {})

        # Merge any extra fields
        if "extra_fields" not in extra:
            extra["extra_fields"] = {}

        # Add any kwargs that aren't standard logging kwargs
        standard_kwargs = {"exc_info", "stack_info", "stacklevel", "extra"}
        for key, value in list(kwargs.items()):
            if key not in standard_kwargs:
                extra["extra_fields"][key] = value
                del kwargs[key]

        kwargs["extra"] = extra
        return msg, kwargs


# Global logger cache
_loggers: Dict[str, ContextLogger] = {}
_configured = False
_config: LogConfig = LogConfig()


def configure_logging(config: Optional[LogConfig] = None) -> None:
    """Configure the logging system.

    Args:
        config: Logging configuration. Uses defaults if not provided.
    """
    global _configured, _config

    _config = config or LogConfig()

    # Get root logger
    root_logger = logging.getLogger()
    # LogLevel enum value is already uppercase
    log_level = getattr(logging, _config.level.value)
    root_logger.setLevel(log_level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    # Set formatter based on config
    if _config.format == "json":
        formatter = StructuredJsonFormatter(_config)
    else:
        formatter = StructuredTextFormatter(_config)

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    _configured = True


def get_logger(name: str) -> ContextLogger:
    """Get a logger with request context support.

    Args:
        name: Logger name (typically __name__)

    Returns:
        ContextLogger that includes request context
    """
    global _configured

    if not _configured:
        configure_logging()

    if name not in _loggers:
        base_logger = logging.getLogger(name)
        _loggers[name] = ContextLogger(base_logger, {})

    return _loggers[name]

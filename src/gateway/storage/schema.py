"""Database schema for audit logging and persistence.

Uses SQLAlchemy Core for maximum portability between SQLite and PostgreSQL.
All types are chosen for compatibility with both databases.

Tables:
- audit_log: Every request with full details
- usage_daily: Aggregated daily usage per client/model
- api_keys: Database-managed API keys (optional, alternative to config)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

# Naming convention for constraints (helps with migrations)
convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


# =============================================================================
# Audit Log Table
# =============================================================================

audit_log = Table(
    "audit_log",
    metadata,
    # Primary key
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(64), unique=True, nullable=False),
    Column("timestamp", DateTime, default=lambda: datetime.now(timezone.utc), nullable=False),
    # Who made the request
    Column("client_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=True),
    Column("environment", String(64), nullable=True),  # dev, prod, etc.
    # What was requested
    Column("task", String(32), nullable=False),  # chat, completion, embeddings
    Column("model", String(128), nullable=False),
    Column("endpoint", String(64), nullable=False),  # Which endpoint handled it
    Column("provider_type", String(32), nullable=True),  # ollama, openai, etc.
    # Request details
    Column("stream", Boolean, default=False),
    Column("max_tokens", Integer, nullable=True),
    Column("temperature", Float, nullable=True),
    # How it went
    Column("status", String(16), nullable=False),  # success, error, rate_limited
    Column("error_code", String(64), nullable=True),
    Column("error_message", Text, nullable=True),
    # Performance metrics
    Column("latency_ms", Float, nullable=True),
    Column("time_to_first_token_ms", Float, nullable=True),
    Column("tokens_per_second", Float, nullable=True),
    # Token usage
    Column("prompt_tokens", Integer, default=0),
    Column("completion_tokens", Integer, default=0),
    Column("total_tokens", Integer, default=0),
    # Cost tracking (if configured)
    Column("estimated_cost_usd", Float, nullable=True),
    # Optional: store request/response (configurable, off by default for privacy)
    # Use JSON for SQLite compatibility, JSONB preferred for PostgreSQL
    Column("request_body", JSON, nullable=True),
    Column("response_body", JSON, nullable=True),
    # Indexes for common queries
    Index("ix_audit_log_timestamp", "timestamp"),
    Index("ix_audit_log_client_id", "client_id"),
    Index("ix_audit_log_user_id", "user_id"),
    Index("ix_audit_log_model", "model"),
    Index("ix_audit_log_endpoint", "endpoint"),
    Index("ix_audit_log_status", "status"),
    Index("ix_audit_log_environment", "environment"),
    # Composite index for common dashboard queries
    Index("ix_audit_log_client_timestamp", "client_id", "timestamp"),
)


# =============================================================================
# Usage Daily Aggregates Table
# =============================================================================

usage_daily = Table(
    "usage_daily",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", DateTime, nullable=False),  # Date (time component = 00:00:00)
    # Grouping dimensions
    Column("client_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=True),
    Column("environment", String(64), nullable=True),
    Column("endpoint", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    # Counts
    Column("request_count", Integer, default=0),
    Column("success_count", Integer, default=0),
    Column("error_count", Integer, default=0),
    Column("stream_count", Integer, default=0),
    # Token totals
    Column("total_prompt_tokens", Integer, default=0),
    Column("total_completion_tokens", Integer, default=0),
    Column("total_tokens", Integer, default=0),
    # Cost
    Column("total_cost_usd", Float, default=0.0),
    # Performance aggregates
    Column("avg_latency_ms", Float, nullable=True),
    Column("min_latency_ms", Float, nullable=True),
    Column("max_latency_ms", Float, nullable=True),
    Column("p50_latency_ms", Float, nullable=True),
    Column("p95_latency_ms", Float, nullable=True),
    Column("p99_latency_ms", Float, nullable=True),
    Column("avg_ttft_ms", Float, nullable=True),  # Avg time to first token
    Column("avg_tokens_per_second", Float, nullable=True),
    # Indexes
    Index("ix_usage_daily_date", "date"),
    Index("ix_usage_daily_client_date", "client_id", "date"),
    Index("ix_usage_daily_endpoint_date", "endpoint", "date"),
    # Unique constraint for upserts
    Index(
        "ix_usage_daily_unique",
        "date",
        "client_id",
        "user_id",
        "environment",
        "endpoint",
        "model",
        unique=True,
    ),
)


# =============================================================================
# API Keys Table (optional, alternative to config-based keys)
# =============================================================================

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Key identification (never store plaintext!)
    Column("key_hash", String(128), unique=True, nullable=False),  # SHA256 hash
    Column("key_prefix", String(16), nullable=False),  # First 8 chars for identification
    # Ownership
    Column("name", String(128), nullable=False),  # Human-readable name
    Column("client_id", String(128), nullable=False),
    Column("environment", String(64), nullable=True),  # Which environment this key accesses
    # Lifecycle
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc), nullable=False),
    Column("expires_at", DateTime, nullable=True),
    Column("last_used_at", DateTime, nullable=True),
    Column("is_active", Boolean, default=True),
    # Permissions and limits
    Column("rate_limit_rpm", Integer, nullable=True),  # Requests per minute
    Column("allowed_models", JSON, nullable=True),  # ["ollama/*", "openai/gpt-4"]
    Column("allowed_endpoints", JSON, nullable=True),  # ["gpunode-ollama"]
    # Metadata
    Column("description", Text, nullable=True),
    Column("created_by", String(128), nullable=True),
    # Indexes
    Index("ix_api_keys_client_id", "client_id"),
    Index("ix_api_keys_key_prefix", "key_prefix"),
    Index("ix_api_keys_is_active", "is_active"),
)


# =============================================================================
# Security Scans Table (for guard model training data collection)
# =============================================================================

security_scans = Table(
    "security_scans",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(64), unique=True, nullable=False),
    Column("timestamp", DateTime, default=lambda: datetime.now(timezone.utc), nullable=False),
    # Context
    Column("client_id", String(128), nullable=False),
    Column("model", String(128), nullable=True),
    Column("task", String(32), nullable=True),  # chat, completion, embeddings
    # The actual messages (training input)
    Column("messages", JSON, nullable=False),
    # Regex verdict
    Column("regex_threat_level", String(16), nullable=False),  # none, low, medium, high, critical
    Column("regex_match_count", Integer, default=0),
    Column("regex_matches", JSON, nullable=True),  # Pattern match details
    # Guard model verdict
    Column("guard_safe", Boolean, nullable=True),  # null = guard didn't run
    Column("guard_skipped", Boolean, nullable=True),
    Column("guard_category_code", String(8), nullable=True),
    Column("guard_category_name", String(64), nullable=True),
    Column("guard_confidence", String(16), nullable=True),
    Column("guard_inference_ms", Float, nullable=True),
    Column("guard_raw_response", Text, nullable=True),
    Column("guard_error", String(128), nullable=True),
    # Human label (for training data)
    Column("label", String(16), nullable=True),  # null=unlabeled, safe, unsafe
    Column("label_category", String(64), nullable=True),  # Why it's unsafe (optional)
    Column("labeled_by", String(128), nullable=True),  # Who labeled it
    Column("labeled_at", DateTime, nullable=True),
    Column("label_notes", Text, nullable=True),  # Free-form notes from reviewer
    # Derived flags
    Column("is_disagreement", Boolean, default=False),  # regex and guard disagree
    # Indexes
    Index("ix_security_scans_timestamp", "timestamp"),
    Index("ix_security_scans_label", "label"),
    Index("ix_security_scans_disagreement", "is_disagreement"),
    Index("ix_security_scans_client_id", "client_id"),
    Index("ix_security_scans_regex_threat", "regex_threat_level"),
)

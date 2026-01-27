"""Database storage module for audit logging and persistence.

Provides:
- Database schema (SQLAlchemy Core, SQLite/PostgreSQL compatible)
- AuditLogger for request logging
- Usage aggregation queries

Default: SQLite (zero config)
Production: PostgreSQL recommended
"""

from gateway.storage.schema import metadata, audit_log, usage_daily, api_keys
from gateway.storage.engine import create_db_engine, DatabaseConfig
from gateway.storage.audit import AuditLogger

__all__ = [
    # Schema
    "metadata",
    "audit_log",
    "usage_daily",
    "api_keys",
    # Engine
    "create_db_engine",
    "DatabaseConfig",
    # Audit
    "AuditLogger",
]

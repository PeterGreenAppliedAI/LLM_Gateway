"""Database storage module for audit logging and persistence.

Provides:
- Database schema (SQLAlchemy Core, SQLite/PostgreSQL compatible)
- AuditLogger for request logging
- Usage aggregation queries

Default: SQLite (zero config)
Production: PostgreSQL recommended
"""

from gateway.storage.audit import AuditLogger
from gateway.storage.engine import DatabaseConfig, create_async_db_engine
from gateway.storage.keys import KeyManager
from gateway.storage.schema import api_keys, audit_log, metadata, security_scans, usage_daily
from gateway.storage.security_store import SecurityScanStore

__all__ = [
    # Schema
    "metadata",
    "audit_log",
    "usage_daily",
    "api_keys",
    "security_scans",
    # Engine
    "create_async_db_engine",
    "DatabaseConfig",
    # Audit
    "AuditLogger",
    # Keys
    "KeyManager",
    # Security scans
    "SecurityScanStore",
]

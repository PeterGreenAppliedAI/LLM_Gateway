"""API key management with secure hash storage.

Keys are generated with a 'gw-' prefix and stored as SHA256 hashes.
The plaintext key is only returned once at creation time.
Uses async SQLAlchemy for non-blocking database I/O.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.storage.schema import api_keys

logger = get_logger(__name__)


def _hash_key(plaintext: str) -> str:
    """SHA256 hash of a plaintext key."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _generate_key(prefix: str = "gw") -> str:
    """Generate a cryptographically random API key with prefix."""
    token = secrets.token_urlsafe(32)
    return f"{prefix}-{token}"


class KeyManager:
    """Manage database-backed API keys.

    Keys are stored as SHA256 hashes. The plaintext is returned exactly
    once at creation time and never stored or logged.
    """

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def create_key(
        self,
        name: str,
        client_id: str,
        environment: Optional[str] = None,
        description: Optional[str] = None,
        allowed_endpoints: Optional[list[str]] = None,
        allowed_models: Optional[list[str]] = None,
        rate_limit_rpm: Optional[int] = None,
    ) -> dict:
        """Create a new API key.

        Returns a dict containing the plaintext key (shown once),
        key_id, prefix, and metadata.
        """
        plaintext = _generate_key()
        key_hash = _hash_key(plaintext)
        key_prefix = plaintext[:12]  # "gw-" + first 9 chars of token

        now = datetime.now(timezone.utc)

        async with self._engine.connect() as conn:
            result = await conn.execute(
                api_keys.insert().values(
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    name=name,
                    client_id=client_id,
                    environment=environment,
                    description=description,
                    allowed_endpoints=allowed_endpoints,
                    allowed_models=allowed_models,
                    rate_limit_rpm=rate_limit_rpm,
                    created_at=now,
                    is_active=True,
                )
            )
            await conn.commit()
            key_id = result.inserted_primary_key[0]

        logger.info("API key created", key_prefix=key_prefix, client_id=client_id, name=name)

        return {
            "key": plaintext,
            "key_id": key_id,
            "prefix": key_prefix,
            "name": name,
            "client_id": client_id,
            "created_at": now.isoformat(),
        }

    async def list_keys(self) -> list[dict]:
        """List all API keys (without hashes).

        Returns prefix, metadata, and status for each key.
        """
        async with self._engine.connect() as conn:
            rows = (await conn.execute(
                select(
                    api_keys.c.id,
                    api_keys.c.key_prefix,
                    api_keys.c.name,
                    api_keys.c.client_id,
                    api_keys.c.environment,
                    api_keys.c.created_at,
                    api_keys.c.last_used_at,
                    api_keys.c.is_active,
                    api_keys.c.allowed_endpoints,
                    api_keys.c.allowed_models,
                    api_keys.c.rate_limit_rpm,
                    api_keys.c.description,
                ).order_by(api_keys.c.created_at.desc())
            )).fetchall()

        return [
            {
                "id": row.id,
                "prefix": row.key_prefix,
                "name": row.name,
                "client_id": row.client_id,
                "environment": row.environment,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                "is_active": row.is_active,
                "allowed_endpoints": row.allowed_endpoints,
                "allowed_models": row.allowed_models,
                "rate_limit_rpm": row.rate_limit_rpm,
                "description": row.description,
            }
            for row in rows
        ]

    async def revoke_key(self, key_id: int) -> bool:
        """Revoke a key by setting is_active=False.

        Returns True if the key was found and revoked.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                update(api_keys)
                .where(api_keys.c.id == key_id)
                .values(is_active=False)
            )
            await conn.commit()
            revoked = result.rowcount > 0
            if revoked:
                logger.info("API key revoked", key_id=key_id)
            else:
                logger.warning("API key revoke failed: not found", key_id=key_id)
            return revoked

    async def get_key_by_hash(self, key_hash: str) -> Optional[dict]:
        """Look up an active key by its hash.

        Returns key metadata if found and active, None otherwise.
        """
        async with self._engine.connect() as conn:
            now = datetime.now(timezone.utc)
            row = (await conn.execute(
                select(
                    api_keys.c.id,
                    api_keys.c.client_id,
                    api_keys.c.environment,
                    api_keys.c.is_active,
                    api_keys.c.allowed_endpoints,
                    api_keys.c.allowed_models,
                    api_keys.c.rate_limit_rpm,
                ).where(
                    api_keys.c.key_hash == key_hash,
                    api_keys.c.is_active == True,  # noqa: E712
                    or_(
                        api_keys.c.expires_at.is_(None),
                        api_keys.c.expires_at > now,
                    ),
                )
            )).fetchone()

            if row is None:
                logger.debug("API key validation failed: key not found or expired")
                return None

            # Update last_used_at
            await conn.execute(
                update(api_keys)
                .where(api_keys.c.id == row.id)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            await conn.commit()

            return {
                "id": row.id,
                "client_id": row.client_id,
                "environment": row.environment,
                "allowed_endpoints": row.allowed_endpoints,
                "allowed_models": row.allowed_models,
                "rate_limit_rpm": row.rate_limit_rpm,
            }

    async def validate_plaintext_key(self, plaintext: str) -> Optional[dict]:
        """Validate a plaintext API key by hashing and looking up.

        Returns key metadata if valid, None otherwise.
        """
        key_hash = _hash_key(plaintext)
        return await self.get_key_by_hash(key_hash)

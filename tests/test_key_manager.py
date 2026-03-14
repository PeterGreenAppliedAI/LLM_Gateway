"""Tests for KeyManager (database-backed API key management)."""

import pytest

from gateway.storage import (
    DatabaseConfig,
    create_async_db_engine,
)
from gateway.storage.keys import KeyManager, _generate_key, _hash_key

# =============================================================================
# Helper Functions Tests
# =============================================================================


class TestKeyHelpers:
    """Tests for key generation and hashing helpers."""

    def test_generate_key_has_prefix(self):
        key = _generate_key()
        assert key.startswith("gw-")

    def test_generate_key_custom_prefix(self):
        key = _generate_key(prefix="test")
        assert key.startswith("test-")

    def test_generate_key_unique(self):
        keys = {_generate_key() for _ in range(100)}
        assert len(keys) == 100

    def test_generate_key_length(self):
        key = _generate_key()
        # "gw-" + 43 chars from token_urlsafe(32) = ~46 chars
        assert len(key) > 20

    def test_hash_key_deterministic(self):
        assert _hash_key("test-key") == _hash_key("test-key")

    def test_hash_key_different_for_different_inputs(self):
        assert _hash_key("key-a") != _hash_key("key-b")

    def test_hash_key_is_hex(self):
        h = _hash_key("test")
        assert len(h) == 64  # SHA256 hex digest
        int(h, 16)  # Should not raise


# =============================================================================
# KeyManager Tests
# =============================================================================


@pytest.fixture
async def key_manager():
    """Create a KeyManager with an in-memory SQLite database."""
    config = DatabaseConfig(url="sqlite:///:memory:")
    engine = await create_async_db_engine(config, create_tables=True)
    km = KeyManager(engine)
    yield km
    await engine.dispose()


class TestKeyManager:
    """Tests for KeyManager CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_key(self, key_manager):
        result = await key_manager.create_key(
            name="test-key",
            client_id="test-client",
        )
        assert "key" in result
        assert result["key"].startswith("gw-")
        assert "key_id" in result
        assert result["name"] == "test-key"
        assert result["client_id"] == "test-client"

    @pytest.mark.asyncio
    async def test_create_key_with_metadata(self, key_manager):
        result = await key_manager.create_key(
            name="prod-key",
            client_id="app-1",
            environment="prod",
            description="Production API key",
            allowed_endpoints=["gpunode-1"],
            allowed_models=["llama3.2:latest"],
            rate_limit_rpm=100,
        )
        assert result["client_id"] == "app-1"

    @pytest.mark.asyncio
    async def test_list_keys(self, key_manager):
        await key_manager.create_key(name="key-1", client_id="c1")
        await key_manager.create_key(name="key-2", client_id="c2")

        keys = await key_manager.list_keys()
        assert len(keys) == 2
        # Should not contain plaintext key
        for k in keys:
            assert "key_hash" not in k
            assert "prefix" in k
            assert k["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_keys_empty(self, key_manager):
        keys = await key_manager.list_keys()
        assert keys == []

    @pytest.mark.asyncio
    async def test_revoke_key(self, key_manager):
        result = await key_manager.create_key(name="revoke-me", client_id="c1")
        key_id = result["key_id"]

        revoked = await key_manager.revoke_key(key_id)
        assert revoked is True

        # Should no longer be active
        keys = await key_manager.list_keys()
        assert any(k["id"] == key_id and k["is_active"] is False for k in keys)

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_key(self, key_manager):
        revoked = await key_manager.revoke_key(9999)
        assert revoked is False

    @pytest.mark.asyncio
    async def test_validate_plaintext_key(self, key_manager):
        result = await key_manager.create_key(name="auth-key", client_id="auth-client")
        plaintext = result["key"]

        validated = await key_manager.validate_plaintext_key(plaintext)
        assert validated is not None
        assert validated["client_id"] == "auth-client"

    @pytest.mark.asyncio
    async def test_validate_invalid_key(self, key_manager):
        validated = await key_manager.validate_plaintext_key("gw-nonexistent-key")
        assert validated is None

    @pytest.mark.asyncio
    async def test_validate_revoked_key_fails(self, key_manager):
        result = await key_manager.create_key(name="to-revoke", client_id="c1")
        plaintext = result["key"]
        key_id = result["key_id"]

        await key_manager.revoke_key(key_id)

        validated = await key_manager.validate_plaintext_key(plaintext)
        assert validated is None

    @pytest.mark.asyncio
    async def test_get_key_by_hash(self, key_manager):
        result = await key_manager.create_key(name="hash-test", client_id="c1")
        plaintext = result["key"]
        key_hash = _hash_key(plaintext)

        found = await key_manager.get_key_by_hash(key_hash)
        assert found is not None
        assert found["client_id"] == "c1"

    @pytest.mark.asyncio
    async def test_get_key_by_hash_updates_last_used(self, key_manager):
        result = await key_manager.create_key(name="usage-test", client_id="c1")
        plaintext = result["key"]
        key_hash = _hash_key(plaintext)

        # First lookup
        await key_manager.get_key_by_hash(key_hash)

        keys = await key_manager.list_keys()
        matching = [k for k in keys if k["id"] == result["key_id"]]
        assert len(matching) == 1
        assert matching[0]["last_used_at"] is not None

    @pytest.mark.asyncio
    async def test_multiple_keys_same_client(self, key_manager):
        await key_manager.create_key(name="key-a", client_id="same-client")
        await key_manager.create_key(name="key-b", client_id="same-client")

        keys = await key_manager.list_keys()
        assert len(keys) == 2
        assert all(k["client_id"] == "same-client" for k in keys)

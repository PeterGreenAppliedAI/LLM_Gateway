"""Validated-key cache and batched last_used_at (D-040)."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from gateway.config import AuthConfig, GatewayConfig
from gateway.errors import InvalidApiKeyError
from gateway.routes.dependencies import validate_api_key
from gateway.storage.key_cache import KeyCache
from gateway.storage.keys import KeyManager, _hash_key
from gateway.storage.schema import api_keys


@pytest.fixture
def engine(db_engine):
    """SQLite and PostgreSQL (see conftest.py)."""
    return db_engine


def _count_lookups(cache: KeyCache, monkeypatch) -> list:
    calls = []
    real = cache._keys.lookup_by_hash

    async def counted(key_hash):
        calls.append(key_hash)
        return await real(key_hash)

    monkeypatch.setattr(cache._keys, "lookup_by_hash", counted)
    return calls


async def _last_used(engine, key_id):
    async with engine.connect() as conn:
        return (
            await conn.execute(select(api_keys.c.last_used_at).where(api_keys.c.id == key_id))
        ).scalar()


class TestCaching:
    @pytest.mark.asyncio
    async def test_repeat_requests_do_no_db_work(self, engine, monkeypatch):
        key = await KeyManager(engine).create_key(name="app", client_id="app")
        cache = KeyCache(engine)
        lookups = _count_lookups(cache, monkeypatch)
        for _ in range(50):
            assert (await cache.validate(key["key"]))["client_id"] == "app"
        assert len(lookups) == 1
        assert await _last_used(engine, key["key_id"]) is None  # not written per request

    @pytest.mark.asyncio
    async def test_entry_expires_after_ttl(self, engine, monkeypatch):
        key = await KeyManager(engine).create_key(name="app", client_id="app")
        cache = KeyCache(engine, ttl=0.05)
        lookups = _count_lookups(cache, monkeypatch)
        await cache.validate(key["key"])
        await asyncio.sleep(0.08)
        await cache.validate(key["key"])
        assert len(lookups) == 2

    @pytest.mark.asyncio
    async def test_key_expiring_while_cached_is_refused(self, engine):
        km = KeyManager(engine)
        key = await km.create_key(name="app", client_id="app")
        soon = datetime.now(timezone.utc) + timedelta(milliseconds=100)
        async with engine.begin() as conn:
            await conn.execute(
                update(api_keys).where(api_keys.c.id == key["key_id"]).values(expires_at=soon)
            )
        cache = KeyCache(engine, ttl=60)
        assert await cache.validate(key["key"]) is not None
        await asyncio.sleep(0.15)
        assert await cache.validate(key["key"]) is None

    @pytest.mark.asyncio
    async def test_size_bounded(self, engine):
        km = KeyManager(engine)
        keys = [await km.create_key(name=f"k{i}", client_id=f"c{i}") for i in range(5)]
        cache = KeyCache(engine, max_entries=3)
        for k in keys:
            await cache.validate(k["key"])
        assert len(cache._valid) == 3


class TestRevocation:
    @pytest.mark.asyncio
    async def test_revoke_is_immediate_in_this_process(self, engine):
        km = KeyManager(engine)
        key = await km.create_key(name="app", client_id="app")
        cache = KeyCache(engine, ttl=60)
        assert await cache.validate(key["key"]) is not None
        await km.revoke_key(key["key_id"])
        cache.invalidate_key(key["key_id"])
        assert await cache.validate(key["key"]) is None

    @pytest.mark.asyncio
    async def test_other_process_follows_within_ttl(self, engine):
        km = KeyManager(engine)
        key = await km.create_key(name="app", client_id="app")
        other = KeyCache(engine, ttl=0.1)  # another gateway process
        assert await other.validate(key["key"]) is not None
        await km.revoke_key(key["key_id"])  # revoked elsewhere
        assert await other.validate(key["key"]) is not None  # still cached
        await asyncio.sleep(0.15)
        assert await other.validate(key["key"]) is None


class TestUnknownKeys:
    @pytest.mark.asyncio
    async def test_guesses_dont_each_hit_the_db(self, engine, monkeypatch):
        cache = KeyCache(engine)
        lookups = _count_lookups(cache, monkeypatch)
        for _ in range(20):
            assert await cache.validate("gw-not-a-real-key-000000000000") is None
        assert len(lookups) == 1

    @pytest.mark.asyncio
    async def test_new_key_accepted_despite_earlier_miss(self, engine):
        cache = KeyCache(engine, negative_ttl=60)
        km = KeyManager(engine)
        key = await km.create_key(name="app", client_id="app")
        cache._remember(
            cache._invalid, _hash_key(key["key"]), time.monotonic()
        )  # tried before it existed
        cache.forget_plaintext(key["key"])  # what POST /api/keys does
        assert await cache.validate(key["key"]) is not None


class TestLastUsed:
    @pytest.mark.asyncio
    async def test_coalesced_and_written_on_flush(self, engine):
        key = await KeyManager(engine).create_key(name="app", client_id="app")
        cache = KeyCache(engine)
        for _ in range(10):
            await cache.validate(key["key"])
        await cache.flush()
        used = await _last_used(engine, key["key_id"])
        assert used is not None
        assert datetime.now(timezone.utc) - used < timedelta(seconds=5)

    @pytest.mark.asyncio
    async def test_never_moves_backwards(self, engine):
        km = KeyManager(engine)
        key = await km.create_key(name="app", client_id="app")
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await km.touch({key["key_id"]: later})
        await km.touch({key["key_id"]: datetime.now(timezone.utc)})  # an older flush arrives late
        assert abs((await _last_used(engine, key["key_id"])) - later) < timedelta(seconds=1)

    @pytest.mark.asyncio
    async def test_failed_flush_kept_for_retry(self, engine, monkeypatch):
        key = await KeyManager(engine).create_key(name="app", client_id="app")
        cache = KeyCache(engine)
        await cache.validate(key["key"])
        real = cache._keys.touch

        async def down(pending):
            raise ConnectionError("database locked")

        monkeypatch.setattr(cache._keys, "touch", down)
        await cache.flush()
        assert key["key_id"] in cache._last_used
        monkeypatch.setattr(cache._keys, "touch", real)
        await cache.flush()
        assert await _last_used(engine, key["key_id"]) is not None

    @pytest.mark.asyncio
    async def test_stop_writes_pending(self, engine):
        key = await KeyManager(engine).create_key(name="app", client_id="app")
        cache = KeyCache(engine, flush_interval=3600)
        await cache.start()
        await cache.validate(key["key"])
        await cache.stop()
        assert await _last_used(engine, key["key_id"]) is not None


@pytest.mark.asyncio
async def test_auth_path_uses_the_cache(engine, monkeypatch):
    key = await KeyManager(engine).create_key(name="app", client_id="tenant", rate_limit_rpm=600)
    config = GatewayConfig(auth=AuthConfig(enabled=True))
    cache = KeyCache(engine)
    lookups = _count_lookups(cache, monkeypatch)
    for _ in range(5):
        info = await validate_api_key(key["key"], config, engine, cache)
        assert (info["client_id"], info["rate_limit_rpm"]) == ("tenant", 600)
    assert len(lookups) == 1
    with pytest.raises(InvalidApiKeyError):
        await validate_api_key("gw-unknown-key-0000000000000000", config, engine, cache)

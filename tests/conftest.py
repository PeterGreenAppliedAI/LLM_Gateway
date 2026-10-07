"""Shared fixtures.

`db_engine` runs a test on SQLite and on PostgreSQL. The PostgreSQL run is
skipped unless one is reachable at GATEWAY_TEST_PG_URL (default
postgresql://postgres@127.0.0.1:5439/gateway); each test gets fresh tables.
With GATEWAY_TEST_REQUIRE_SERVICES=1 (CI) an unreachable server is an error,
not a skip, so the PostgreSQL claims are tested rather than assumed.
"""

import asyncio
import os

import pytest

PG_URL = os.environ.get("GATEWAY_TEST_PG_URL", "postgresql://postgres@127.0.0.1:5439/gateway")


def _pg_reachable() -> bool:
    try:
        import asyncpg
    except ImportError:
        return False

    async def ping() -> bool:
        try:
            conn = await asyncpg.connect(PG_URL, timeout=1)
        except Exception:
            return False
        await conn.close()
        return True

    return asyncio.run(ping())


_PG = _pg_reachable()
REQUIRE_SERVICES = os.environ.get("GATEWAY_TEST_REQUIRE_SERVICES") == "1"
if REQUIRE_SERVICES and not _PG:
    raise RuntimeError(f"GATEWAY_TEST_REQUIRE_SERVICES=1 but no PostgreSQL at {PG_URL}")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres", marks=pytest.mark.skipif(not _PG, reason=f"no PostgreSQL at {PG_URL}")
        ),
    ]
)
async def db_engine(request, tmp_path):
    from gateway.storage import DatabaseConfig, create_async_db_engine
    from gateway.storage.schema import metadata

    url = f"sqlite:///{tmp_path}/test.db" if request.param == "sqlite" else PG_URL
    engine = await create_async_db_engine(DatabaseConfig(url=url), create_tables=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield engine
    if request.param == "postgres":
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
    await engine.dispose()

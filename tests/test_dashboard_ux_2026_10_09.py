"""Backend regressions for the 2026-10-09 operator-experience review (D-057).

U3: inference stats exclude audited denials, which are reported separately.
U4: the request listing pages in SQL, says whether more exists, and filters
    by time range.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from gateway.main import create_app
from gateway.settings import Settings
from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine
from gateway.storage.schema import audit_log


@pytest.fixture
async def engine(tmp_path):
    engine = await create_async_db_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path}/ux.db", create_tables=True)
    )
    yield engine
    await engine.dispose()


@pytest.fixture
def audit(engine):
    return AuditLogger(engine=engine)


async def _log(audit, request_id, status, client="app", task="chat", model="m"):
    await audit.log_request(
        request_id=request_id,
        client_id=client,
        task=task,
        model=model,
        endpoint="ep",
        status=status,
        prompt_tokens=10 if status == "success" else 0,
        completion_tokens=5 if status == "success" else 0,
    )


class TestStatsExcludeDenials:
    @pytest.mark.asyncio
    async def test_denials_not_in_inference_figures(self, audit):
        """The review's scenario: dashboard polling with a bad key produced
        36 denials and a headline of '0% success' before any inference."""
        for i in range(36):
            await _log(audit, f"d{i}", "denied", client="unknown", task="unknown", model="unknown")
        await _log(audit, "s1", "success")
        await _log(audit, "s2", "success")
        await _log(audit, "e1", "error")

        stats = await audit.get_stats(hours=24)
        assert stats["total_requests"] == 3
        assert stats["success_count"] == 2
        assert stats["error_count"] == 1
        assert stats["success_rate"] == pytest.approx(66.67, abs=0.01)
        assert stats["denied_count"] == 36
        assert "unknown" not in stats["top_models"]

    @pytest.mark.asyncio
    async def test_only_denials_reads_as_no_traffic(self, audit):
        await _log(audit, "d1", "denied", client="unknown", task="unknown", model="unknown")
        stats = await audit.get_stats(hours=24)
        assert stats["total_requests"] == 0
        assert stats["denied_count"] == 1


class TestRequestListing:
    @pytest.mark.asyncio
    async def test_sql_offset_paging(self, audit):
        for i in range(5):
            await _log(audit, f"r{i}", "success")
        first = await audit.get_recent_requests(limit=2, offset=0)
        second = await audit.get_recent_requests(limit=2, offset=2)
        last = await audit.get_recent_requests(limit=2, offset=4)
        ids = [r["request_id"] for r in first + second + last]
        assert len(ids) == 5 and len(set(ids)) == 5  # no overlap, nothing skipped

    @pytest.mark.asyncio
    async def test_since_filter(self, audit, engine):
        await _log(audit, "old", "success")
        await _log(audit, "new", "success")
        async with engine.begin() as conn:
            await conn.execute(
                update(audit_log)
                .where(audit_log.c.request_id == "old")
                .values(timestamp=datetime.now(UTC) - timedelta(days=3))
            )
        recent = await audit.get_recent_requests(since=datetime.now(UTC) - timedelta(hours=24))
        assert [r["request_id"] for r in recent] == ["new"]

    @pytest.mark.asyncio
    async def test_route_has_more_and_status_filter(self, engine, audit):
        for i in range(3):
            await _log(audit, f"ok{i}", "success")
        await _log(audit, "deny1", "denied", client="unknown", task="unknown", model="unknown")

        app = create_app(Settings(debug=True))
        app.state.db_engine = engine
        app.state.audit_logger = audit
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page1 = (await client.get("/api/requests?limit=2&offset=0")).json()
            page2 = (await client.get("/api/requests?limit=2&offset=2")).json()
            denied = (await client.get("/api/requests?filter_status=denied")).json()

        assert len(page1["requests"]) == 2 and page1["has_more"] is True
        assert len(page2["requests"]) == 2 and page2["has_more"] is False
        assert [r["request_id"] for r in denied["requests"]] == ["deny1"]


class TestScanLabelIndex:
    def test_migration_creates_label_timestamp_index(self, tmp_path):
        """The labeling view's 'newest unlabeled' query sorted every
        unlabeled row (7 s on 714k rows) without this index."""
        import sqlalchemy as sa

        from gateway.storage.migrate import head_revision, upgrade

        engine = sa.create_engine(f"sqlite:///{tmp_path}/mig.db")
        with engine.begin() as conn:
            upgrade(conn)
            assert head_revision() == "c2f8a4d6e913"
            names = {ix["name"] for ix in sa.inspect(conn).get_indexes("security_scans")}
            plan = " ".join(
                str(row[3])
                for row in conn.exec_driver_sql(
                    "EXPLAIN QUERY PLAN SELECT * FROM security_scans "
                    "WHERE label IS NULL ORDER BY timestamp DESC LIMIT 50"
                )
            )
        engine.dispose()
        assert "ix_security_scans_label_timestamp" in names
        assert "TEMP B-TREE" not in plan  # no full sort of matching rows

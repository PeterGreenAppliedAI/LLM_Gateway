"""Audit intent log: local log + background drain to the database (D-038)."""

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from gateway.storage.audit import AuditLogger
from gateway.storage.intent_log import IntentLog, _segments
from gateway.storage.schema import audit_journal, audit_log, pii_events


@pytest.fixture
def engine(db_engine):
    """SQLite and PostgreSQL (see conftest.py)."""
    return db_engine


def _audit_row(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "timestamp": datetime.now(UTC),
        "client_id": "tenant",
        "task": "chat",
        "model": "m",
        "endpoint": "e",
        "status": "success",
    }


def _pii_row(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "timestamp": datetime.now(UTC),
        "client_id": "tenant",
        "pii_type": "email",
        "message_index": 0,
        "position_start": 0,
        "position_end": 5,
        "value_hash": "x" * 64,
        "was_scrubbed": True,
    }


async def _count(engine, table, column=None) -> int:
    async with engine.connect() as conn:
        target = func.count(func.distinct(column)) if column is not None else func.count()
        return (await conn.execute(select(target).select_from(table))).scalar()


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def _crash(log: IntentLog) -> None:
    """Simulate kill -9: background tasks gone, files closed, nothing drained."""
    for task in log._tasks:
        task.cancel()
    for task in log._tasks:  # a dead process runs nothing: wait until they're gone
        with contextlib.suppress(BaseException):
            await task
    # A dead process's open transaction is rolled back by the database; here,
    # let the batch in flight finish (D-046: an abandoned SQLite connection
    # would hold the write lock, which a real crash releases)
    await log._drain_work.finish()
    await log._orphan_work.finish()
    log._file.close()
    log._file = None
    log._lock_file.close()  # the OS releases the lock when a process dies


class TestDrain:
    @pytest.mark.asyncio
    async def test_append_returns_then_rows_reach_db(self, engine, tmp_path):
        log = IntentLog(tmp_path / "journal", engine)
        await log.start()
        await log.append("audit_log", [_audit_row("r1")])
        await log.append("pii_events", [_pii_row("r1"), _pii_row("r1")])
        await _until(lambda: _has(engine, 1, 2))
        assert log.status()["backlog_records"] == 0
        await log.close()

    @pytest.mark.asyncio
    async def test_many_rows_across_segments_in_order(self, engine, tmp_path):
        log = IntentLog(tmp_path / "journal", engine, segment_bytes=4096, batch_records=50)
        await log.start()
        for i in range(600):
            await log.append("audit_log", [_audit_row(f"r{i}")])
        await _until(lambda: _counted(engine, audit_log, 600))
        await _until(lambda: _segment_count(log, 1))  # closed segments deleted
        async with engine.connect() as conn:
            ids = [
                r[0]
                for r in await conn.execute(select(audit_log.c.request_id).order_by(audit_log.c.id))
            ]
        assert ids == [f"r{i}" for i in range(600)]
        await log.close()

    @pytest.mark.asyncio
    async def test_clean_close_drains_and_cleans_up(self, engine, tmp_path):
        log = IntentLog(tmp_path / "journal", engine, idle_interval=60)
        await log.start()
        log._wake.wait = _never  # drainer asleep: everything still pending at close
        for i in range(20):
            await log.append("audit_log", [_audit_row(f"r{i}")])
        await log.close()
        assert await _count(engine, audit_log) == 20
        assert not (tmp_path / "journal" / log.instance).exists()
        assert await _count(engine, audit_journal) == 0


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_kill_9_loses_nothing_and_repeats_nothing(self, engine, tmp_path):
        root = tmp_path / "journal"
        crashed = IntentLog(root, engine, batch_records=10)
        await crashed.start()
        for i in range(25):
            await crashed.append("audit_log", [_audit_row(f"r{i}")])
            await crashed.append("pii_events", [_pii_row(f"r{i}")])
        # Let it write part of the log, then die mid-way
        await _until(lambda: _counted(engine, audit_log, 1, at_least=True))
        await _crash(crashed)
        partial = await _count(engine, pii_events)
        assert partial < 25 or await _count(engine, audit_log) <= 25

        survivor = IntentLog(root, engine)
        await survivor.start()  # drains the dead process's log first
        assert await _count(engine, audit_log) == 25
        # pii_events has no unique key: a duplicate would show up here
        assert await _count(engine, pii_events) == 25
        assert not (root / crashed.instance).exists()
        await survivor.close()

    @pytest.mark.asyncio
    async def test_live_process_log_not_taken(self, engine, tmp_path):
        root = tmp_path / "journal"
        a = IntentLog(root, engine, idle_interval=60)
        await a.start()
        a._wake.wait = _never
        await a.append("audit_log", [_audit_row("from-a")])
        b = IntentLog(root, engine)
        await b.start()
        await b._recover_orphans()
        assert await _count(engine, audit_log) == 0  # a is alive: its log is a's to drain
        await a.close()
        assert await _count(engine, audit_log) == 1
        await b.close()

    @pytest.mark.asyncio
    async def test_torn_last_record_is_left_alone(self, engine, tmp_path):
        root = tmp_path / "journal"
        crashed = IntentLog(root, engine, idle_interval=60)
        await crashed.start()
        crashed._wake.wait = _never
        await crashed.append("audit_log", [_audit_row("whole")])
        crashed._file.write(b'{"t": 1, "table": "audit_log", "rows": [{"request_')  # died mid-write
        crashed._file.flush()
        await _crash(crashed)
        survivor = IntentLog(root, engine)
        await survivor.start()
        assert await _count(engine, audit_log) == 1
        await survivor.close()


class TestConcurrentDrainers:
    @pytest.mark.asyncio
    async def test_two_drainers_on_one_log_never_double_apply(self, engine, tmp_path):
        """Exactly-once is enforced by the database (compare-and-set on the
        position), not only by the lock file: even two drainers racing over
        the same log apply each record once."""
        root = tmp_path / "journal"
        source = IntentLog(root, engine, batch_records=7, idle_interval=60)
        await source.start()
        source._wake.wait = _never
        for i in range(60):
            await source.append("pii_events", [_pii_row(f"r{i}")])  # no unique key
        await _crash(source)

        a, b = IntentLog(root, engine, batch_records=7), IntentLog(root, engine, batch_records=5)
        instance_dir = root / source.instance
        await asyncio.gather(
            a._drain(source.instance, instance_dir, live=False),
            b._drain(source.instance, instance_dir, live=False),
            a._drain(source.instance, instance_dir, live=False),
        )
        assert await _count(engine, pii_events) == 60


class TestDatabaseOutage:
    @pytest.mark.asyncio
    async def test_rows_wait_in_the_log_then_drain(self, engine, tmp_path, monkeypatch):
        log = IntentLog(tmp_path / "journal", engine, idle_interval=0.02)
        await log.start()
        real_commit = log._commit

        async def down(*args):
            raise ConnectionError("database is down")

        monkeypatch.setattr(log, "_commit", down)
        for i in range(5):
            await log.append("audit_log", [_audit_row(f"r{i}")])
        await asyncio.sleep(0.3)
        status = log.status()
        assert status["backlog_records"] == 5
        assert status["database_reachable"] is False
        assert status["oldest_pending_seconds"] > 0

        monkeypatch.setattr(log, "_commit", real_commit)
        log._wake.set()
        await _until(lambda: _counted(engine, audit_log, 5), timeout=40)
        assert log.status()["backlog_records"] == 0
        await log.close()

    @pytest.mark.asyncio
    async def test_size_cap_drops_oldest_keeps_serving(self, engine, tmp_path, monkeypatch):
        log = IntentLog(
            tmp_path / "journal", engine, segment_bytes=2048, max_bytes=6144, idle_interval=60
        )
        await log.start()
        log._wake.wait = _never

        for i in range(100):
            await log.append("audit_log", [_audit_row(f"r{i}")])  # never raises
        assert log.rows_lost > 0
        assert log.status()["log_bytes"] <= 6144 + 2048
        await log.close()
        async with engine.connect() as conn:
            ids = {r[0] for r in await conn.execute(select(audit_log.c.request_id))}
        assert "r99" in ids and "r0" not in ids  # newest kept, oldest dropped


class TestDurabilityModes:
    @pytest.mark.asyncio
    async def test_grouped_mode_shares_fsyncs(self, engine, tmp_path, monkeypatch):
        calls = []
        real_fsync = os.fsync

        def slow_fsync(fd):
            calls.append(fd)
            time.sleep(0.02)  # a real disk flush takes time; others pile up behind it
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", slow_fsync)
        log = IntentLog(tmp_path / "journal", engine, durability="grouped")
        await log.start()
        await asyncio.gather(*(log.append("audit_log", [_audit_row(f"r{i}")]) for i in range(20)))
        assert 1 <= len(calls) <= 3  # 20 appends share a couple of fsyncs
        assert log._synced == 20
        await log.close()

    @pytest.mark.asyncio
    async def test_grouped_mode_lone_request_does_not_wait_a_window(
        self, engine, tmp_path, monkeypatch
    ):
        # fsync stubbed out: how long a real disk flush takes (tens of ms on
        # Windows runners) is not what this measures; a batching window is
        calls = []
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
        log = IntentLog(tmp_path / "journal", engine, durability="grouped")
        await log.start()
        started = time.monotonic()
        await log.append("audit_log", [_audit_row("alone")])
        assert time.monotonic() - started < 0.04  # no batching delay
        assert len(calls) == 1
        assert log._synced == 1
        await log.close()

    @pytest.mark.asyncio
    async def test_process_mode_never_fsyncs_on_append(self, engine, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
        log = IntentLog(tmp_path / "journal", engine)
        await log.start()
        for i in range(20):
            await log.append("audit_log", [_audit_row(f"r{i}")])
        assert calls == []
        await log.close()


class TestRejectedRows:
    @pytest.mark.asyncio
    async def test_bad_row_skipped_rest_of_batch_written(self, engine, tmp_path):
        log = IntentLog(tmp_path / "journal", engine, idle_interval=60)
        await log.start()
        log._wake.wait = _never
        await log.append("audit_log", [_audit_row("dup")])
        await log.append("audit_log", [_audit_row("dup")])  # unique request_id: rejected
        await log.append("audit_log", [_audit_row("ok")])
        await log.close()
        assert await _count(engine, audit_log) == 2
        assert log.rows_rejected == 1


class TestAuditLoggerIntegration:
    @pytest.mark.asyncio
    async def test_log_request_goes_through_the_log(self, engine, tmp_path):
        log = IntentLog(tmp_path / "journal", engine)
        await log.start()
        audit = AuditLogger(engine, intent_log=log)
        await audit.log_request("r1", "tenant", "chat", "m", "e", "success")
        await _until(lambda: _counted(engine, audit_log, 1))
        await log.close()

    @pytest.mark.asyncio
    async def test_append_failure_falls_back_to_direct_write(self, engine, tmp_path, monkeypatch):
        log = IntentLog(tmp_path / "journal", engine)
        await log.start()

        async def disk_full(*args):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(log, "append", disk_full)
        audit = AuditLogger(engine, intent_log=log)
        await audit.log_request("r1", "tenant", "chat", "m", "e", "success")
        assert await _count(engine, audit_log) == 1  # written synchronously instead
        await log.close()


# -- helpers -------------------------------------------------------------------


async def _never():
    await asyncio.Event().wait()


async def _has(engine, audits: int, piis: int) -> bool:
    return await _count(engine, audit_log) == audits and await _count(engine, pii_events) == piis


async def _counted(engine, table, n: int, at_least: bool = False) -> bool:
    count = await _count(engine, table)
    return count >= n if at_least else count == n


async def _segment_count(log: IntentLog, n: int) -> bool:
    return len(_segments(log._dir)) == n

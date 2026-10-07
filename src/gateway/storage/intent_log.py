"""Audit intent log: write audit rows to a local log, drain them to the database (D-038).

The idea is ZFS's SLOG (separate intent log). A request appends its audit
rows to a local, append-only log file and responds; it never waits on the
database. A background drainer reads the log in order and inserts the rows
in batches. Each batch commits together with the drainer's position in the
log (the audit_journal table), so the rows and "how far we got" can't
disagree: after any crash the drainer resumes exactly where the last
commit left off. No row is lost, none is inserted twice.

Durability of the append (GATEWAY_DB_AUDIT_DURABILITY):
- process: written to the OS, not forced to disk. Survives any crash of
  the gateway (including kill -9). A power cut or kernel crash can lose
  what the OS hadn't written back yet: up to ~30 s with Linux defaults
  (vm.dirty_expire_centisecs). Microseconds per request.
- grouped: a request waits until its record is forced to disk (fsync).
  Requests arriving together share one fsync (group commit), so it costs
  about one disk flush per burst, not per request. Nothing acknowledged is
  ever lost, even on power loss.
- sync: no log; the request waits for the database commit (the old path).
- auto (the default): process on SQLite, sync on PostgreSQL.

Layout: <directory>/<instance>/ holds one gateway process's log segments
(numbered files) and a lock file the process holds while alive. A process
that finds another instance's lock free knows that process is gone, and
drains its leftover segments. Several workers each get their own instance.
"""

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Table, delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.storage.schema import audit_journal, audit_log, pii_events

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

logger = get_logger(__name__)

Durability = Literal["process", "grouped", "sync"]

TABLES: dict[str, Table] = {t.name: t for t in (audit_log, pii_events)}
_DATETIME = "__datetime__"
_SEGMENT_SUFFIX = ".log"


def _encode(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, datetime):
        return {_DATETIME: value.isoformat()}
    return str(value)


def _decode_row(row: dict) -> dict:
    from datetime import datetime

    return {
        k: datetime.fromisoformat(v[_DATETIME]) if isinstance(v, dict) and _DATETIME in v else v
        for k, v in row.items()
    }


def _try_lock(handle: Any) -> bool:
    """Take an exclusive lock on an open lock file without waiting.

    flock on POSIX, msvcrt byte-range locking on Windows. Either way the OS
    drops the lock when the process dies, which is what orphan recovery
    relies on to tell a dead process's log from a live one.
    """
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _segment_name(number: int) -> str:
    return f"{number:012d}{_SEGMENT_SUFFIX}"


def _segments(instance_dir: Path) -> list[Path]:
    return sorted(instance_dir.glob(f"*{_SEGMENT_SUFFIX}"))


class IntentLog:
    def __init__(
        self,
        directory: str | Path,
        engine: AsyncEngine,
        durability: Durability = "process",
        segment_bytes: int = 64 * 1024 * 1024,
        max_bytes: int = 1024 * 1024 * 1024,
        batch_records: int = 500,
        idle_interval: float = 0.1,
        orphan_scan_interval: float = 30.0,
    ):
        self._root = Path(directory)
        self._engine = engine
        self.durability = durability
        self._segment_bytes = segment_bytes
        self._max_bytes = max_bytes
        self._batch = batch_records
        self._idle = idle_interval
        self._orphan_scan_interval = orphan_scan_interval

        self.instance = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._dir = self._root / self.instance
        self._lock_file: Any = None
        self._file: Any = None
        self._segment_no = 0
        self._segment_size = 0
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._sync_task: asyncio.Task | None = None
        self._synced = 0  # records known to be on disk (grouped mode)

        # Backlog bookkeeping (this process's log)
        self._appended = 0
        self._drained = 0
        self._oldest_pending: float | None = None
        self._bytes_on_disk = 0
        self.rows_lost = 0
        self.rows_rejected = 0
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self._dir / "lock").open("w")
        if not _try_lock(self._lock_file):
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError(f"Audit intent log {self._dir} is in use by another process")
        self._open_segment(1)
        # Leftovers from previous runs first, so they reach the DB in order
        await self._recover_orphans()
        self._tasks = [
            asyncio.create_task(self._drain_loop(), name="audit-log-drain"),
            asyncio.create_task(self._orphan_loop(), name="audit-log-orphans"),
        ]
        logger.info("Audit intent log started", path=str(self._dir), durability=self.durability)

    async def close(self, drain_timeout: float = 5.0) -> None:
        """Stop, after writing what's pending (anything left stays in the log for next start)."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        # Close the active segment first: the final drain deletes segments it
        # has written, and Windows can't delete a file that is still open
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None
        try:
            await asyncio.wait_for(self._drain(self.instance, self._dir, live=False), drain_timeout)
        except Exception as e:
            logger.warning(
                "Audit log not fully drained at shutdown; kept for next start", error=str(e)
            )
        drained = not _segments(self._dir)
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        if drained:
            await self._forget_instance(self.instance, self._dir)

    # -- append (request path) ---------------------------------------------

    @property
    def accepting(self) -> bool:
        return self._file is not None

    async def append(self, table: str, rows: list[dict]) -> None:
        """Record rows; returns once they're durable at the configured level."""
        if not rows:
            return
        line = (
            json.dumps({"t": time.time(), "table": table, "rows": rows}, default=_encode) + "\n"
        ).encode("utf-8")
        if self._segment_size + len(line) > self._segment_bytes and self._segment_size:
            self._rotate()
        self._file.write(line)
        self._file.flush()  # to the OS: survives a gateway crash
        self._segment_size += len(line)
        self._bytes_on_disk += len(line)
        self._appended += 1
        if self._oldest_pending is None:
            self._oldest_pending = time.time()
        if self._bytes_on_disk > self._max_bytes:
            self._enforce_cap()
        self._wake.set()
        if self.durability == "grouped":
            await self._group_commit()

    async def _group_commit(self) -> None:
        """Wait until this record is on disk (leader-based group commit).

        If no fsync is running, start one now: a lone request pays one fsync,
        no artificial wait. Requests arriving while it runs share the next
        one, which starts as soon as it finishes, so under load one fsync
        covers many requests.
        """
        target = self._appended
        while self._synced < target:
            if self._sync_task is None or self._sync_task.done():
                self._sync_task = asyncio.ensure_future(self._fsync_now())
            await asyncio.shield(self._sync_task)

    async def _fsync_now(self) -> None:
        upto = self._appended  # everything written before this fsync starts
        file = self._file
        try:
            await asyncio.to_thread(os.fsync, file.fileno())
        except (OSError, ValueError):
            if not file.closed:
                raise
            # Rotated meanwhile: _rotate() fsynced the segment before closing it
        self._synced = max(self._synced, upto)

    def _open_segment(self, number: int) -> None:
        self._segment_no = number
        self._file = (self._dir / _segment_name(number)).open("ab")
        self._segment_size = self._file.tell()

    def _rotate(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())  # rare; keeps closed segments complete on disk
        self._synced = self._appended
        self._file.close()
        self._open_segment(self._segment_no + 1)

    def _enforce_cap(self) -> None:
        """Over the size cap (DB down a long time): drop the oldest closed segment."""
        closed = [p for p in _segments(self._dir) if p.name != _segment_name(self._segment_no)]
        if not closed:
            return
        oldest = closed[0]
        size = oldest.stat().st_size
        records = oldest.read_bytes().count(b"\n")
        oldest.unlink()
        self._bytes_on_disk -= size
        self.rows_lost += records
        self._drained += records
        _record_failure("audit_log", "lost", records)
        logger.critical(
            "Audit intent log over its size cap; oldest records DROPPED",
            dropped_records=records,
            cap_bytes=self._max_bytes,
            last_error=self.last_error,
        )

    # -- drain (background) ------------------------------------------------

    async def _drain_loop(self) -> None:
        backoff = self._idle
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), backoff)
            self._wake.clear()
            try:
                await self._drain(self.instance, self._dir, live=True)
                backoff = self._idle
                if self.last_error:
                    logger.info("Audit database reachable again; backlog draining")
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.last_error is None:
                    logger.error(
                        "Audit rows can't reach the database; they're safe in the intent log "
                        "and will be written when it's back",
                        error=str(e),
                    )
                self.last_error = str(e)
                backoff = min(30.0, max(0.5, backoff * 2))

    async def _drain(self, instance: str, instance_dir: Path, live: bool) -> None:
        """Write everything in an instance's log after its committed position."""
        # stored: the position as the database holds it (each commit is a
        # compare-and-set against it); position: where to read next
        stored = position = await self._position(instance)
        while True:
            segments = _segments(instance_dir)
            if position is not None:
                segments = [s for s in segments if s.name >= position[0]]
            if not segments:
                return
            segment = segments[0]
            offset = position[1] if position and position[0] == segment.name else 0
            active = live and segment.name == _segment_name(self._segment_no)

            batch, next_offset, oldest = self._read(segment, offset)
            if batch:
                if live and oldest:
                    self._oldest_pending = oldest  # the oldest record not yet in the DB
                try:
                    await self._commit(instance, stored, segment.name, next_offset, batch)
                except PositionConflict:
                    # Another drainer advanced this log first: nothing of ours was
                    # applied. Re-read the stored position and carry on from it.
                    logger.warning("Audit log position moved by another drainer", instance=instance)
                    stored = position = await self._position(instance)
                    continue
                stored = position = (segment.name, next_offset)
                if live:
                    self._drained += len(batch)
                    if self._drained >= self._appended:
                        self._oldest_pending = None
                continue
            if active:
                if self._drained >= self._appended:
                    self._oldest_pending = None
                return  # caught up with the writer
            # A closed segment fully written to the DB: delete it
            size = segment.stat().st_size
            segment.unlink()
            if live:
                self._bytes_on_disk -= size
            position = None

    def _read(self, segment: Path, offset: int) -> tuple[list[dict], int, float | None]:
        """Up to batch_records complete records from offset; a torn last line is left."""
        records: list[dict] = []
        oldest = None
        with segment.open("rb") as f:
            f.seek(offset)
            while len(records) < self._batch:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break  # end, or a record still being written
                offset += len(line)
                try:
                    record = json.loads(line)
                except ValueError:
                    logger.error("Unreadable audit intent log record skipped", segment=segment.name)
                    continue
                oldest = oldest or record.get("t")
                records.append(record)
        return records, offset, oldest

    async def _commit(
        self,
        instance: str,
        expected: tuple[str, int] | None,
        segment: str,
        offset: int,
        records: list[dict],
    ) -> None:
        """Insert a batch and advance the position in one transaction (exactly once).

        The position update is a compare-and-set against `expected`: if any
        other drainer has moved it, the whole transaction rolls back, so a
        batch can never be applied twice, not even by two drainers at once.
        """
        by_table: OrderedDict[str, list[dict]] = OrderedDict()
        for record in records:
            table = record.get("table")
            if table in TABLES:
                by_table.setdefault(table, []).extend(_decode_row(r) for r in record["rows"])
        async with self._engine.begin() as conn:
            for name, rows in by_table.items():
                try:
                    async with conn.begin_nested():
                        await conn.execute(insert(TABLES[name]), rows)
                except IntegrityError:
                    # One bad row mustn't block the log: insert one by one, skip rejects
                    for row in rows:
                        try:
                            async with conn.begin_nested():
                                await conn.execute(insert(TABLES[name]), [row])
                        except IntegrityError as e:
                            self.rows_rejected += 1
                            _record_failure(name, "rejected", 1)
                            logger.error(
                                "Audit row rejected by database",
                                table=name,
                                request_id=row.get("request_id"),
                                error=str(e)[:200],
                            )
            await _save_position(conn, instance, expected, segment, offset)

    async def _position(self, instance: str) -> tuple[str, int] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(audit_journal.c.segment, audit_journal.c.byte_offset).where(
                        audit_journal.c.instance == instance
                    )
                )
            ).fetchone()
        return (row.segment, row.byte_offset) if row else None

    async def _forget_instance(self, instance: str, instance_dir: Path) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(audit_journal).where(audit_journal.c.instance == instance))
        for leftover in instance_dir.iterdir():
            with contextlib.suppress(OSError):
                leftover.unlink()
        with contextlib.suppress(OSError):
            instance_dir.rmdir()

    # -- other instances' leftovers ----------------------------------------

    async def _recover_orphans(self) -> None:
        if not self._root.exists():
            return
        for instance_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            if instance_dir.name == self.instance:
                continue
            lock_path = instance_dir / "lock"
            try:
                handle = lock_path.open("a")
            except OSError:
                continue
            try:
                if not _try_lock(handle):
                    continue  # its process is alive and draining it
                records = sum(p.read_bytes().count(b"\n") for p in _segments(instance_dir))
                await self._drain(instance_dir.name, instance_dir, live=False)
                if not _segments(instance_dir):
                    # Unlock before removing the directory (Windows can't delete
                    # an open file). Nothing is left to drain, so a recoverer
                    # that takes the lock in between finds nothing to do.
                    handle.close()
                    await self._forget_instance(instance_dir.name, instance_dir)
                    if records:
                        logger.warning(
                            "Recovered audit rows from a previous gateway process",
                            instance=instance_dir.name,
                            records=records,
                        )
            except Exception as e:
                logger.warning(
                    "Couldn't drain a previous process's audit log yet; will retry",
                    instance=instance_dir.name,
                    error=str(e),
                )
            finally:
                handle.close()

    async def _orphan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._orphan_scan_interval)
            await self._recover_orphans()

    # -- status --------------------------------------------------------------

    def status(self) -> dict:
        backlog = max(0, self._appended - self._drained)
        oldest = (
            round(time.time() - self._oldest_pending, 1)
            if backlog and self._oldest_pending
            else 0.0
        )
        return {
            "mode": self.durability,
            "backlog_records": backlog,
            "oldest_pending_seconds": oldest,
            "log_bytes": self._bytes_on_disk,
            "database_reachable": self.last_error is None,
            "records_dropped": self.rows_lost,
        }


class PositionConflict(Exception):
    """The stored log position isn't the one this batch was read from."""


async def _save_position(
    conn, instance: str, expected: tuple[str, int] | None, segment: str, offset: int
) -> None:
    if expected is None:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    audit_journal.insert().values(
                        instance=instance, segment=segment, byte_offset=offset
                    )
                )
        except IntegrityError as e:
            raise PositionConflict() from e  # someone else wrote the first position
        return
    updated = await conn.execute(
        audit_journal.update()
        .where(
            audit_journal.c.instance == instance,
            audit_journal.c.segment == expected[0],
            audit_journal.c.byte_offset == expected[1],
        )
        .values(segment=segment, byte_offset=offset)
    )
    if updated.rowcount != 1:
        raise PositionConflict()


def _record_failure(table: str, outcome: str, count: int) -> None:
    try:
        from gateway.observability import get_metrics

        for _ in range(count):
            get_metrics().record_audit_write_failure(table, outcome)
    except Exception:
        pass

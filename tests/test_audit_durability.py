"""Audit writes survive a database outage instead of being dropped.

Regressions covered:
- a failed audit insert was logged and the row discarded
- the gateway served traffic with no database (and so no audit trail)
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

import gateway.storage.audit as audit_module
from gateway.storage import AuditLogger, DatabaseConfig, create_async_db_engine


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(audit_module, "WRITE_RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
async def engine(tmp_path):
    engine = await create_async_db_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path}/gw.db", create_tables=True)
    )
    yield engine
    await engine.dispose()


def _down_engine() -> MagicMock:
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("INSERT", {}, Exception("database is locked"))
    return engine


async def _log(logger: AuditLogger, request_id: str = "req-1") -> None:
    await logger.log_request(
        request_id=request_id,
        client_id="app",
        task="chat",
        model="phi4:14b",
        endpoint="gpu-1",
        status="success",
        prompt_tokens=3,
        completion_tokens=4,
    )


@pytest.mark.asyncio
async def test_outage_spills_then_replays(tmp_path, engine):
    spill = tmp_path / "spill.jsonl"

    down = AuditLogger(_down_engine(), spill_path=spill)
    await _log(down)
    assert down._engine.connect.call_count == 3  # first try + 2 retries
    assert spill.exists()

    up = AuditLogger(engine, spill_path=spill)
    assert await up.replay_spill() == 1
    assert not spill.exists()
    row = await up.get_request_by_id("req-1")
    assert row["total_tokens"] == 7
    assert row["timestamp"] is not None


@pytest.mark.asyncio
async def test_duplicate_row_is_not_spilled(tmp_path, engine):
    spill = tmp_path / "spill.jsonl"
    logger = AuditLogger(engine, spill_path=spill)
    await _log(logger)
    await _log(logger)  # unique request_id: rejected, not retried or spilled
    assert not spill.exists()


@pytest.mark.asyncio
async def test_failed_replay_keeps_rows(tmp_path):
    spill = tmp_path / "spill.jsonl"
    await _log(AuditLogger(_down_engine(), spill_path=spill))

    still_down = AuditLogger(_down_engine(), spill_path=spill)
    assert await still_down.replay_spill() == 0
    assert spill.exists()
    assert len(spill.read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_pii_events_spill_too(tmp_path):
    from gateway.security.pii import PIIScrubber

    spill = tmp_path / "spill.jsonl"
    messages = [{"role": "user", "content": "mail a.b@example.com"}]
    _, results = PIIScrubber().scan_messages(messages)
    logger = AuditLogger(_down_engine(), spill_path=spill)
    await logger.log_pii_events(
        request_id="req-1",
        client_id="app",
        task="chat",
        model="phi4:14b",
        findings=results,
        was_scrubbed=False,
    )
    assert '"table": "pii_events"' in spill.read_text()
    assert "example.com" not in spill.read_text()  # only hashes are stored


def test_startup_refuses_without_database(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import gateway.main
    from gateway.settings import DatabaseSettings, Settings

    settings = Settings(
        config_path=str(tmp_path / "missing.yaml"),
        db=DatabaseSettings(url="notadb://nowhere"),
    )
    monkeypatch.setattr(gateway.main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="GATEWAY_DB_REQUIRED"):
        with TestClient(gateway.main.create_app(settings)):
            pass


def test_startup_without_database_when_optional(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import gateway.main
    from gateway.settings import DatabaseSettings, Settings

    settings = Settings(
        config_path=str(tmp_path / "missing.yaml"),
        db=DatabaseSettings(url="notadb://nowhere", required=False),
    )
    monkeypatch.setattr(gateway.main, "get_settings", lambda: settings)
    with TestClient(gateway.main.create_app(settings)) as client:
        assert client.app.state.audit_logger is None

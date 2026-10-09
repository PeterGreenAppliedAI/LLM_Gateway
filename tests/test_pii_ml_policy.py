"""Per-category ML PII policy (D-052): dashboard switch, stored-copy scrubbing."""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import gateway.settings
from gateway.exception_handlers import register_exception_handlers
from gateway.main import _load_saved_pii_ml_policy
from gateway.routes.dashboard import router as dashboard_router
from gateway.security.pii_finder import FinderResult, locate_spans
from gateway.security.pii_policy import PIIMLPolicy
from gateway.security.pii_shadow import PIIShadowAnalyzer, ShadowJob
from gateway.security.pii_taxonomy import LABELS
from gateway.settings import Settings
from gateway.storage import DatabaseConfig, RuntimeSettingsStore, create_async_db_engine
from gateway.storage.schema import audit_log, security_scans
from gateway.storage.stored_copy_scrubber import StoredCopyScrubber, scrub_values

ADMIN = "admin-key-1234567890"
EMAIL = "jane.doe@acme.com"
KEY = "sk-ant-api03-AbCdEf1234567890"


@pytest.fixture
async def engine(tmp_path):
    engine = await create_async_db_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path}/gw.db", create_tables=True)
    )
    yield engine
    await engine.dispose()


def _required(table, i=0) -> dict:
    """Filler for a table's NOT NULL columns without defaults."""
    values = {}
    for c in table.columns:
        if c.nullable or c.primary_key or c.default is not None or c.server_default is not None:
            continue
        if isinstance(c.type, sa.String):
            values[c.name] = f"x{i}"
        elif isinstance(c.type, sa.DateTime):
            from datetime import UTC, datetime

            values[c.name] = datetime.now(UTC)
        elif isinstance(c.type, sa.JSON):
            values[c.name] = []
        elif isinstance(c.type, sa.Boolean):
            values[c.name] = False
        else:
            values[c.name] = 0
    return values


async def _seed(engine, request_id="r1"):
    body = {"messages": [{"role": "user", "content": f"mail {EMAIL}, key {KEY}"}]}
    response = {"content": f"I'll email {EMAIL}."}
    async with engine.begin() as conn:
        await conn.execute(
            audit_log.insert().values(
                **{
                    **_required(audit_log),
                    "request_id": request_id,
                    "request_body": body,
                    "response_body": response,
                }
            )
        )
        await conn.execute(
            security_scans.insert().values(
                **{
                    **_required(security_scans),
                    "request_id": request_id,
                    "messages": body["messages"],
                }
            )
        )


async def _stored(engine, request_id="r1") -> str:
    async with engine.connect() as conn:
        a = (
            await conn.execute(
                sa.select(audit_log.c.request_body, audit_log.c.response_body).where(
                    audit_log.c.request_id == request_id
                )
            )
        ).one()
        s = (
            await conn.execute(
                sa.select(security_scans.c.messages).where(
                    security_scans.c.request_id == request_id
                )
            )
        ).one()
    return repr((a, s))


class TestScrubValues:
    def test_nested_and_longest_first(self):
        obj = {"a": [f"x {EMAIL} y", {"b": f"{EMAIL}{EMAIL}"}], "n": 3}
        out, n = scrub_values(obj, {EMAIL: "[CONTACT]", "jane": "[PERSON_NAME]"})
        assert out == {"a": ["x [CONTACT] y", {"b": "[CONTACT][CONTACT]"}], "n": 3}
        assert n == 3


class TestStoredCopyScrubber:
    @pytest.mark.asyncio
    async def test_audit_bodies_and_scan_messages_scrubbed(self, engine):
        await _seed(engine)
        n = await StoredCopyScrubber(engine).scrub_now("r1", {EMAIL: "[CONTACT]"})
        stored = await _stored(engine)
        assert n == 3  # request body, response body, scan messages
        assert EMAIL not in stored and "[CONTACT]" in stored
        assert KEY in stored  # only the categories asked for

    @pytest.mark.asyncio
    async def test_row_written_after_detection_is_caught_by_retry(self, engine):
        scrubber = StoredCopyScrubber(engine, schedule=(0, 0.05))
        scrubber.schedule("late", {EMAIL: "[CONTACT]"})
        await _seed(engine, "late")  # lands after the first pass
        import asyncio

        for _ in range(100):  # the retry fires within ~0.05s; allow for a loaded runner
            await asyncio.sleep(0.05)
            if EMAIL not in await _stored(engine, "late"):
                break
        assert EMAIL not in await _stored(engine, "late")
        await scrubber.stop()


class FakeGate:
    async def classify(self, text):
        return {
            label: (0.9 if label in ("CONTACT", "CREDENTIAL") else 0.0) for label in LABELS
        }, 5.0


class FakeFinder:
    model = "fake"

    async def find(self, text):
        spans, h, r = locate_spans(
            text,
            [{"category": "CONTACT", "value": EMAIL}, {"category": "CREDENTIAL", "value": KEY}],
        )
        return FinderResult(spans=spans, hallucinated=h, rejected=r)


class RecordingScrubber:
    def __init__(self):
        self.calls = []

    def schedule(self, request_id, replacements):
        self.calls.append((request_id, replacements))

    async def stop(self):
        pass


class TestShadowEnforcement:
    @pytest.mark.asyncio
    async def test_detect_only_by_default(self):
        scrubber = RecordingScrubber()
        shadow = PIIShadowAnalyzer(
            FakeGate(), FakeFinder(), policy=lambda: PIIMLPolicy(), scrubber=scrubber
        )
        row = await shadow.analyze(ShadowJob("r1", "c", None, None, f"{EMAIL} {KEY}"))
        assert scrubber.calls == [] and "scrubbed_categories" not in row

    @pytest.mark.asyncio
    async def test_scrub_stored_category_only(self):
        scrubber = RecordingScrubber()
        policy = PIIMLPolicy(
            categories={**dict.fromkeys(LABELS, "detect"), "CREDENTIAL": "scrub_stored"}
        )
        shadow = PIIShadowAnalyzer(
            FakeGate(), FakeFinder(), policy=lambda: policy, scrubber=scrubber
        )
        row = await shadow.analyze(ShadowJob("r1", "c", None, None, f"{EMAIL} {KEY}"))
        assert scrubber.calls == [("r1", {KEY: "[CREDENTIAL]"})]
        assert row["scrubbed_categories"] == ["CREDENTIAL"]
        assert KEY not in repr({k: v for k, v in row.items()})  # row still holds no values
        assert shadow.stats["stored_scrubbed"] == 1


@pytest.fixture
def app(engine, monkeypatch):
    settings = Settings(admin_api_key=SecretStr(ADMIN))
    monkeypatch.setattr(gateway.settings, "get_settings", lambda: settings)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(dashboard_router)
    app.state.settings = settings
    app.state.db_engine = engine
    app.state.runtime_settings = RuntimeSettingsStore(engine)
    app.state.pii_ml_policy = PIIMLPolicy()
    return app


def _h(key=ADMIN):
    return {"X-API-Key": key}


class TestPolicyRoutes:
    def test_admin_only_and_defaults_to_detect(self, app):
        client = TestClient(app)
        assert client.get("/api/pii/ml", headers=_h("nope-key-123456")).status_code == 401
        view = client.get("/api/pii/ml", headers=_h()).json()
        assert view["enabled"] is False  # gate not configured
        assert [c["label"] for c in view["categories"]] == list(LABELS)
        assert {c["action"] for c in view["categories"]} == {"detect"}
        assert view["actions"] == ["detect", "scrub_stored"]

    def test_put_changes_only_sent_categories_and_saves(self, app):
        client = TestClient(app)
        resp = client.put(
            "/api/pii/ml", json={"categories": {"CREDENTIAL": "scrub_stored"}}, headers=_h()
        )
        assert resp.status_code == 200
        actions = {c["label"]: c["action"] for c in resp.json()["categories"]}
        assert actions["CREDENTIAL"] == "scrub_stored" and actions["CONTACT"] == "detect"
        assert resp.json()["updated_by"] == "admin"
        assert app.state.pii_ml_policy.scrub_labels() == {"CREDENTIAL"}

    @pytest.mark.parametrize(
        "body", [{"categories": {"EMAIL": "scrub_stored"}}, {"categories": {"CONTACT": "block"}}]
    )
    def test_unknown_category_or_action_rejected_loudly(self, app, body):
        resp = TestClient(app).put("/api/pii/ml", json=body, headers=_h())
        assert resp.status_code == 422
        assert app.state.pii_ml_policy.scrub_labels() == set()

    @pytest.mark.asyncio
    async def test_saved_policy_survives_restart(self, engine):
        store = RuntimeSettingsStore(engine)
        await store.set("pii.ml_policy", {"categories": {"GOV_ID": "scrub_stored"}}, "admin")
        restarted = FastAPI()
        restarted.state.runtime_settings = store
        await _load_saved_pii_ml_policy(restarted)
        assert restarted.state.pii_ml_policy.scrub_labels() == {"GOV_ID"}
        assert restarted.state.pii_ml_policy.categories["CONTACT"] == "detect"

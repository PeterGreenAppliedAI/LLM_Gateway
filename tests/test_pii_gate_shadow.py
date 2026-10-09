"""ML PII detection, shadow mode (D-052): taxonomy, extractor, gate, pipeline, store."""

import json
import random

import httpx
import pytest

from gateway.security.pii_finder import PIIFinder, locate_spans
from gateway.security.pii_gate import QUESTIONS, PIIGateClient, PIIGateError, chunk_text
from gateway.security.pii_shadow import PIIShadowAnalyzer, ShadowJob, messages_to_text
from gateway.security.pii_taxonomy import FINDER_SCHEMA, LABELS, finder_system_prompt

EMAIL = "jane.doe@acme.com"
KEY = "sk-ant-api03-AbCdEf1234567890"
TEXT = f"Email {EMAIL} the report, and rotate {KEY}. Thanks, {EMAIL}"


# =============================================================================
# Taxonomy
# =============================================================================


class TestTaxonomy:
    def test_labels_unique_and_match_decision_log(self):
        assert len(LABELS) == len(set(LABELS)) == 9
        assert LABELS[0] == "CREDENTIAL"

    def test_schema_and_questions_derive_from_labels(self):
        enum = FINDER_SCHEMA["properties"]["findings"]["items"]["properties"]["category"]["enum"]
        assert enum == list(LABELS)
        assert set(QUESTIONS) == set(LABELS)

    def test_prompt_covers_every_category_and_its_negatives(self):
        prompt = finder_system_prompt()
        for label in LABELS:
            assert label in prompt
        assert "Do NOT report" in prompt and "EXACTLY" in prompt


# =============================================================================
# Extractor
# =============================================================================


class TestLocateSpans:
    def test_every_occurrence_found(self):
        spans, hallucinated = locate_spans(TEXT, [{"category": "CONTACT", "value": EMAIL}])
        assert hallucinated == 0
        assert [TEXT[s.start : s.end] for s in spans] == [EMAIL, EMAIL]

    def test_hallucinated_value_discarded_and_counted(self):
        spans, hallucinated = locate_spans(TEXT, [{"category": "GOV_ID", "value": "123-45-6789"}])
        assert spans == [] and hallucinated == 1

    def test_unknown_category_and_empty_value_dropped(self):
        spans, hallucinated = locate_spans(
            TEXT, [{"category": "MADE_UP", "value": EMAIL}, {"category": "CONTACT", "value": " "}]
        )
        assert spans == [] and hallucinated == 0


def _ollama(content: str, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["format"] == FINDER_SCHEMA  # grammar-constrained
        assert body["options"]["temperature"] == 0
        return httpx.Response(status, json={"message": {"content": content}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFinder:
    @pytest.mark.asyncio
    async def test_find_verifies_spans(self):
        content = json.dumps(
            {
                "findings": [
                    {"category": "CONTACT", "value": EMAIL},
                    {"category": "CREDENTIAL", "value": KEY},
                    {"category": "GOV_ID", "value": "not-in-text"},
                ]
            }
        )
        finder = PIIFinder("http://ollama", client=_ollama(content))
        result = await finder.find(TEXT)
        assert result.error is None
        assert result.categories == {"CONTACT": 2, "CREDENTIAL": 1}
        assert result.hallucinated == 1

    @pytest.mark.asyncio
    async def test_bad_output_is_an_error_not_a_crash(self):
        finder = PIIFinder("http://ollama", client=_ollama("not json"))
        result = await finder.find(TEXT)
        assert result.error and result.spans == []


# =============================================================================
# Gate
# =============================================================================


def _sidecar(reply_for):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert set(body["questions"]) == set(LABELS)
        return httpx.Response(200, json={"results": [reply_for(t) for t in body["texts"]]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestGate:
    def test_chunks_overlap_and_cover_the_text(self):
        text = "a" * 1000 + EMAIL + "b" * 1000
        chunks = chunk_text(text, max_chars=600, overlap=50)
        assert any(EMAIL in c for c in chunks)
        assert chunks[0][-50:] == chunks[1][:50]

    @pytest.mark.asyncio
    async def test_category_probability_is_max_over_chunks(self):
        def reply(text):
            return {
                label: (0.9 if label == "CONTACT" and EMAIL in text else 0.01) for label in LABELS
            }

        gate = PIIGateClient("http://laya", max_chars=600, overlap=50, client=_sidecar(reply))
        probs, _ = await gate.classify("x" * 1500 + EMAIL + "y" * 1500)
        assert probs["CONTACT"] == 0.9
        assert probs["CREDENTIAL"] == 0.01

    @pytest.mark.asyncio
    async def test_down_sidecar_raises_gate_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        gate = PIIGateClient(
            "http://laya", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(PIIGateError):
            await gate.classify(TEXT)


# =============================================================================
# Shadow pipeline
# =============================================================================


class FakeGate:
    def __init__(self, probs=None, fail=False):
        self.probs, self.fail = probs or {}, fail

    async def classify(self, text):
        if self.fail:
            raise PIIGateError("ConnectError: refused")
        return {label: self.probs.get(label, 0.0) for label in LABELS}, 12.0


class FakeFinder:
    def __init__(self, findings):
        self.findings, self.calls = findings, 0

    async def find(self, text):
        from gateway.security.pii_finder import FinderResult

        self.calls += 1
        spans, hallucinated = locate_spans(text, self.findings)
        return FinderResult(spans=spans, hallucinated=hallucinated, latency_ms=300.0)


class RecordingStore:
    def __init__(self):
        self.rows = []

    async def record(self, row):
        self.rows.append(row)


def _job(text=TEXT):
    return ShadowJob("r1", "app", "chat", "m", text)


CONTACT_FINDING = [{"category": "CONTACT", "value": EMAIL}]


class TestShadowPipeline:
    @pytest.mark.asyncio
    async def test_gate_positive_runs_finder(self):
        finder = FakeFinder(CONTACT_FINDING)
        shadow = PIIShadowAnalyzer(FakeGate({"CONTACT": 0.9}), finder, sample_rate=0.0)
        row = await shadow.analyze(_job())
        assert finder.calls == 1
        assert row["finder_reason"] == "gate_positive"
        assert row["gate_categories"] == ["CONTACT"]
        assert row["finder_categories"] == {"CONTACT": 2}

    @pytest.mark.asyncio
    async def test_gate_negative_skips_finder_unless_sampled(self):
        finder = FakeFinder(CONTACT_FINDING)
        shadow = PIIShadowAnalyzer(FakeGate(), finder, sample_rate=0.0)
        row = await shadow.analyze(_job())
        assert finder.calls == 0 and row.get("finder_reason") is None

    @pytest.mark.asyncio
    async def test_sampled_negative_measures_a_miss(self):
        finder = FakeFinder(CONTACT_FINDING)
        shadow = PIIShadowAnalyzer(FakeGate(), finder, sample_rate=1.0, rng=random.Random(0))
        row = await shadow.analyze(_job())
        assert row["finder_reason"] == "sampled"
        assert row["gate_missed"] is True
        assert shadow.stats["gate_missed"] == 1

    @pytest.mark.asyncio
    async def test_gate_down_fails_toward_checking(self):
        finder = FakeFinder(CONTACT_FINDING)
        shadow = PIIShadowAnalyzer(FakeGate(fail=True), finder, sample_rate=0.0)
        row = await shadow.analyze(_job())
        assert row["finder_reason"] == "gate_unavailable"
        assert "refused" in row["gate_error"]

    @pytest.mark.asyncio
    async def test_regex_comparison_recorded(self):
        shadow = PIIShadowAnalyzer(FakeGate(), FakeFinder([]), sample_rate=0.0)
        row = await shadow.analyze(_job())
        assert row["regex_types"] == ["EMAIL"]  # regex sees the email, not the API key

    @pytest.mark.asyncio
    async def test_recorded_row_never_contains_text_or_values(self):
        store = RecordingStore()
        shadow = PIIShadowAnalyzer(
            FakeGate({"CONTACT": 0.9, "CREDENTIAL": 0.8}),
            FakeFinder(CONTACT_FINDING + [{"category": "CREDENTIAL", "value": KEY}]),
            store=store,
        )
        await shadow.analyze(_job())
        serialized = json.dumps(store.rows)
        assert EMAIL not in serialized
        assert KEY not in serialized
        assert "rotate" not in serialized  # no text either

    def test_messages_to_text_covers_parts_and_tool_args(self):
        text = messages_to_text(
            [
                {"role": "user", "content": [{"type": "text", "text": "part text"}]},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"arguments": f'{{"to":"{EMAIL}"}}'}}],
                },
            ]
        )
        assert "part text" in text and EMAIL in text

    @pytest.mark.asyncio
    async def test_full_queue_drops_and_counts(self):
        shadow = PIIShadowAnalyzer(FakeGate(), FakeFinder([]), queue_size=1)
        assert shadow.submit("r1", "app", [{"role": "user", "content": "a"}])
        assert not shadow.submit("r2", "app", [{"role": "user", "content": "b"}])
        assert shadow.stats["dropped"] == 1


# =============================================================================
# Store
# =============================================================================


class TestShadowStore:
    @pytest.mark.asyncio
    async def test_record_and_summary(self, tmp_path):
        from gateway.storage import DatabaseConfig, create_async_db_engine
        from gateway.storage.pii_shadow_store import PIIShadowStore

        engine = await create_async_db_engine(
            DatabaseConfig(url=f"sqlite:///{tmp_path}/s.db", create_tables=True)
        )
        try:
            store = PIIShadowStore(engine)
            await store.record(
                {"request_id": "a", "client_id": "c", "text_chars": 10, "gate_ms": 10.0}
            )
            await store.record(
                {
                    "request_id": "b",
                    "client_id": "c",
                    "text_chars": 10,
                    "gate_ms": 20.0,
                    "finder_reason": "sampled",
                    "gate_missed": True,
                    "finder_ms": 400.0,
                }
            )
            summary = await store.summary(hours=24)
            assert summary["requests"] == 2
            assert summary["finder_runs"] == 1
            assert summary["skipped_finder_pct"] == 50.0
            assert summary["gate_miss_rate_pct"] == 100.0
            assert summary["avg_gate_ms"] == 15.0
        finally:
            await engine.dispose()


# =============================================================================
# Dataset (synthetic + backlog labelling)
# =============================================================================


class TestSyntheticDataset:
    def test_spans_are_exact_and_categories_known(self):
        from gateway.security.pii_dataset import SyntheticGenerator

        records = list(SyntheticGenerator(seed=1).generate(500))
        for r in records:
            for s in r["spans"]:
                assert r["text"][s["start"] : s["end"]] == s["value"]
                assert s["category"] in LABELS

    def test_every_category_and_hard_negatives_present(self):
        from gateway.security.pii_dataset import SyntheticGenerator

        records = list(SyntheticGenerator(seed=2).generate(2000))
        seen = {s["category"] for r in records for s in r["spans"]}
        assert seen == set(LABELS)
        assert any(not r["spans"] for r in records)

    def test_seeded_and_reproducible(self):
        from gateway.security.pii_dataset import SyntheticGenerator

        a = list(SyntheticGenerator(seed=7).generate(50))
        b = list(SyntheticGenerator(seed=7).generate(50))
        assert a == b

    def test_generated_cards_pass_luhn(self):
        from gateway.security.pii_dataset import SyntheticGenerator

        gen = SyntheticGenerator(seed=3)
        for _ in range(100):
            digits = [int(c) for c in gen.card() if c.isdigit()]
            total = sum(
                d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2)
                for i, d in enumerate(reversed(digits))
            )
            assert total % 10 == 0

    def test_files_are_owner_only(self, tmp_path):
        import stat

        from gateway.security.pii_dataset import write_synthetic

        out = tmp_path / "ds" / "synthetic.jsonl"
        assert write_synthetic(out, 10, seed=0, negative_share=0.3) == 10
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


class TestBacklogLabelling:
    def test_redacted_and_empty_rows_skipped(self):
        from gateway.security.pii_dataset import usable_backlog_text

        assert usable_backlog_text([{"role": "user", "content": "mail [EMAIL] now"}]) is None
        assert usable_backlog_text([{"role": "user", "content": "  "}]) is None
        assert usable_backlog_text("not json") is None
        assert usable_backlog_text(json.dumps([{"role": "user", "content": "hi"}])) == "hi"

    @pytest.mark.asyncio
    async def test_labels_dedupes_and_resumes(self, tmp_path):
        import sqlalchemy as sa

        from gateway.security.pii_dataset import label_backlog
        from gateway.storage import DatabaseConfig, create_async_db_engine
        from gateway.storage.schema import security_scans

        db = f"sqlite:///{tmp_path}/g.db"
        engine = await create_async_db_engine(DatabaseConfig(url=db))
        cols = {c.name for c in security_scans.columns}
        base = {"label": None} if "label" in cols else {}
        rows = [TEXT, TEXT, "nothing here", "redacted [EMAIL]"]
        async with engine.begin() as conn:
            for i, t in enumerate(rows):
                values = {**base, "messages": [{"role": "user", "content": t}]}
                for c in security_scans.columns:
                    if (
                        c.name not in values
                        and not c.nullable
                        and c.default is None
                        and not c.primary_key
                    ):
                        values[c.name] = f"x{i}" if isinstance(c.type, sa.String) else 0
                await conn.execute(security_scans.insert().values(**values))
        await engine.dispose()

        out = tmp_path / "backlog.jsonl"
        finder = FakeFinder(CONTACT_FINDING)
        assert await label_backlog(db, out, finder, limit=10, log=lambda *_: None) == 2
        records = [json.loads(line) for line in out.read_text().splitlines()]
        labelled = {r["text"]: r for r in records}
        assert labelled[TEXT]["spans"][0]["value"] == EMAIL
        assert labelled["nothing here"]["spans"] == []

        # resumable: a second run finds nothing new
        assert await label_backlog(db, out, finder, limit=10, log=lambda *_: None) == 0
        assert finder.calls == 2

"""Token budgets persisted to the database (D-037)."""

import pytest

from gateway.policy.token_budget import (
    ModelAssignment,
    ModelTierConfig,
    TokenBudgetConfig,
    TokenBudgetExceeded,
    TokenBudgetTracker,
)
from gateway.storage.budgets import BudgetStore, BudgetSync


def _config(**overrides) -> TokenBudgetConfig:
    return TokenBudgetConfig(
        enabled=True,
        default_daily_limit=10_000,
        default_cost_multiplier=5.0,
        model_tiers=[
            ModelTierConfig(name="standard", cost_multiplier=1.0),
            ModelTierConfig(name="frontier", cost_multiplier=10.0, daily_limit=3_000),
        ],
        model_assignments=[
            ModelAssignment(model="llama3.1:8b", tier="standard"),
            ModelAssignment(model="big:70b", tier="frontier"),
        ],
        **overrides,
    )


@pytest.fixture
def engine(db_engine):
    """SQLite and PostgreSQL (see conftest.py)."""
    return db_engine


async def _gateway(engine) -> tuple[TokenBudgetTracker, BudgetSync]:
    """One gateway process: a tracker and its sync (not started as a loop)."""
    tracker = TokenBudgetTracker(_config())
    sync = BudgetSync(tracker, engine, interval=3600)
    await sync.start()
    return tracker, sync


class TestSurvivesRestart:
    @pytest.mark.asyncio
    async def test_usage_survives_restart(self, engine):
        tracker, sync = await _gateway(engine)
        tracker.record_usage("tenant", "llama3.1:8b", 4_000)
        tracker.record_usage("tenant", "llama3.1:8b", 3_000)
        await sync.stop()  # clean shutdown flushes

        restarted, sync2 = await _gateway(engine)
        state = restarted.get_budget_state("tenant")
        assert state.tokens_used == 7_000
        assert state.request_count == 2
        with pytest.raises(TokenBudgetExceeded):
            restarted.check_budget("tenant", "llama3.1:8b", estimated_tokens=4_000)
        await sync2.stop()

    @pytest.mark.asyncio
    async def test_tier_cap_totals_survive_restart(self, engine):
        tracker, sync = await _gateway(engine)
        tracker.record_usage("a", "big:70b", 1_000)
        tracker.record_usage("b", "big:70b", 1_500)
        await sync.stop()

        restarted, sync2 = await _gateway(engine)
        # frontier cap is 3000 raw tokens across all keys: 2500 used
        restarted.check_budget("c", "big:70b", estimated_tokens=400)
        with pytest.raises(TokenBudgetExceeded) as exc:
            restarted.check_budget("c", "big:70b", estimated_tokens=600)
        assert exc.value.budget_type == "daily_tier_limit:frontier"
        await sync2.stop()

    @pytest.mark.asyncio
    async def test_dashboard_tier_changes_survive_restart(self, engine):
        tracker, sync = await _gateway(engine)
        tracker.add_tier("embedding", 0.1)
        tracker.assign_model("*embed*", "embedding")
        tracker.unassign_model("big:70b")
        await sync.save_catalog("admin")
        await sync.stop()

        restarted, sync2 = await _gateway(engine)
        assert restarted.resolve_tier("nomic-embed-text").name == "embedding"
        assert restarted.resolve_tier("big:70b") is None  # removal persisted too
        assert "embedding" in restarted.tiers
        await sync2.stop()


class TestSharedAcrossProcesses:
    @pytest.mark.asyncio
    async def test_each_process_sees_the_others_spend(self, engine):
        a, sync_a = await _gateway(engine)
        b, sync_b = await _gateway(engine)
        a.record_usage("tenant", "llama3.1:8b", 6_000)
        b.record_usage("tenant", "llama3.1:8b", 3_000)
        await sync_a.flush()
        await sync_b.flush()  # b now reads a's 6000 too
        assert b.get_budget_state("tenant").tokens_used == 9_000
        with pytest.raises(TokenBudgetExceeded):
            b.check_budget("tenant", "llama3.1:8b", estimated_tokens=2_000)
        await sync_a.flush()
        assert a.get_budget_state("tenant").tokens_used == 9_000
        await sync_a.stop()
        await sync_b.stop()

    @pytest.mark.asyncio
    async def test_catalog_change_reaches_other_process(self, engine):
        a, sync_a = await _gateway(engine)
        b, sync_b = await _gateway(engine)
        a.add_tier("cheap", 0.5)
        a.assign_model("tiny:1b", "cheap")
        await sync_a.save_catalog("admin")
        await sync_b._load_catalog()
        assert b.resolve_tier("tiny:1b").name == "cheap"
        await sync_a.stop()
        await sync_b.stop()


class TestWriteSafety:
    @pytest.mark.asyncio
    async def test_failed_write_keeps_usage_and_retries(self, engine, monkeypatch):
        tracker, sync = await _gateway(engine)
        tracker.record_usage("tenant", "llama3.1:8b", 5_000)

        async def down(rows):
            raise ConnectionError("database unavailable")

        real = sync._store.add_usage
        monkeypatch.setattr(sync._store, "add_usage", down)
        await sync.flush()
        assert sync.failures == 1
        assert tracker.get_budget_state("tenant").tokens_used == 5_000  # still counted

        monkeypatch.setattr(sync._store, "add_usage", real)
        await sync.flush()
        assert (await BudgetStore(engine).day_usage(tracker._today()))[0].weighted == 5_000
        await sync.stop()

    @pytest.mark.asyncio
    async def test_batch_being_written_still_counts(self, engine, monkeypatch):
        """A check made while a batch is in flight must not see it vanish."""
        tracker, sync = await _gateway(engine)
        tracker.record_usage("tenant", "llama3.1:8b", 9_000)
        seen_during_write = []
        real = sync._store.add_usage

        async def observe(rows):
            seen_during_write.append(tracker.get_budget_state("tenant").tokens_used)
            await real(rows)

        monkeypatch.setattr(sync._store, "add_usage", observe)
        await sync.flush()
        assert seen_during_write == [9_000]
        assert tracker.get_budget_state("tenant").tokens_used == 9_000  # not doubled
        await sync.stop()

    @pytest.mark.asyncio
    async def test_increments_not_overwrites(self, engine):
        store = BudgetStore(engine)
        from gateway.policy.token_budget import UsageRow

        await store.add_usage([UsageRow("2026-10-07", "k", "standard", 10, 10, 1)])
        await store.add_usage([UsageRow("2026-10-07", "k", "standard", 5, 5, 1)])
        (row,) = await store.day_usage("2026-10-07")
        assert (row.weighted, row.raw, row.requests) == (15, 15, 2)

    @pytest.mark.asyncio
    async def test_prune_old_days(self, engine):
        from gateway.policy.token_budget import UsageRow

        store = BudgetStore(engine)
        await store.add_usage([UsageRow("2020-01-01", "k", "standard", 1, 1, 1)])
        assert await store.prune(keep_days=30) == 1
        assert await store.day_usage("2020-01-01") == []


def test_without_a_database_past_days_are_dropped():
    tracker = TokenBudgetTracker(_config())
    tracker.record_usage("k", "llama3.1:8b", 100)
    tracker._pending["2020-01-01"] = tracker._pending.pop(tracker._today())
    tracker.record_usage("k", "llama3.1:8b", 100)  # triggers cleanup (not persisted)
    assert list(tracker._pending) == [tracker._today()]

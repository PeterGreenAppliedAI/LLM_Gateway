"""Tests for token budget tracking and enforcement."""

import pytest

from gateway.policy.token_budget import (
    TokenBudgetTracker,
    TokenBudgetConfig,
    TokenBudgetExceeded,
    ModelTierConfig,
    ModelAssignment,
)


@pytest.fixture
def config():
    return TokenBudgetConfig(
        enabled=True,
        default_daily_limit=10000,
        default_cost_multiplier=5.0,
        enforce_pre_request=True,
        model_tiers=[
            ModelTierConfig(name="frontier", cost_multiplier=15.0, daily_limit=50000),
            ModelTierConfig(name="midrange", cost_multiplier=3.0),
            ModelTierConfig(name="standard", cost_multiplier=1.0),
            ModelTierConfig(name="embedding", cost_multiplier=0.1),
        ],
        model_assignments=[
            ModelAssignment(model="llama3.1:70b", tier="frontier"),
            ModelAssignment(model="phi4:14b", tier="midrange"),
            ModelAssignment(model="llama3.1:8b", tier="standard"),
            ModelAssignment(model="*embed*", tier="embedding"),
        ],
    )


@pytest.fixture
def tracker(config):
    return TokenBudgetTracker(config)


class TestTierResolution:
    def test_exact_match(self, tracker):
        tier = tracker.resolve_tier("llama3.1:70b")
        assert tier is not None
        assert tier.name == "frontier"

    def test_glob_match(self, tracker):
        tier = tracker.resolve_tier("nomic-embed-text:latest")
        assert tier is not None
        assert tier.name == "embedding"

    def test_no_match_returns_none(self, tracker):
        tier = tracker.resolve_tier("gpt-5.4-turbo")
        assert tier is None

    def test_case_insensitive(self, tracker):
        tier = tracker.resolve_tier("Llama3.1:70B")
        assert tier is not None
        assert tier.name == "frontier"

    def test_unknown_model_uses_default_multiplier(self, tracker):
        assert tracker.get_cost_multiplier("gpt-5.4-turbo") == 5.0

    def test_known_model_uses_tier_multiplier(self, tracker):
        assert tracker.get_cost_multiplier("llama3.1:70b") == 15.0
        assert tracker.get_cost_multiplier("phi4:14b") == 3.0
        assert tracker.get_cost_multiplier("llama3.1:8b") == 1.0
        assert tracker.get_cost_multiplier("nomic-embed-text") == 0.1


class TestRuntimeAssignment:
    def test_assign_new_model(self, tracker):
        assert tracker.resolve_tier("gpt-5.4") is None  # unclassified
        assert tracker.get_cost_multiplier("gpt-5.4") == 5.0  # default

        # Assign it
        ok = tracker.assign_model("gpt-5.4", "frontier")
        assert ok is True
        assert tracker.get_cost_multiplier("gpt-5.4") == 15.0

    def test_assign_glob_pattern(self, tracker):
        ok = tracker.assign_model("gpt-*", "frontier")
        assert ok is True
        assert tracker.get_cost_multiplier("gpt-5.4") == 15.0
        assert tracker.get_cost_multiplier("gpt-6") == 15.0

    def test_assign_invalid_tier(self, tracker):
        ok = tracker.assign_model("some-model", "nonexistent")
        assert ok is False

    def test_unassign_model(self, tracker):
        assert tracker.get_cost_multiplier("llama3.1:70b") == 15.0
        tracker.unassign_model("llama3.1:70b")
        assert tracker.get_cost_multiplier("llama3.1:70b") == 5.0  # back to default

    def test_unassign_nonexistent(self, tracker):
        assert tracker.unassign_model("never-assigned") is False

    def test_reassign_model(self, tracker):
        assert tracker.get_cost_multiplier("llama3.1:8b") == 1.0  # standard
        tracker.assign_model("llama3.1:8b", "midrange")
        assert tracker.get_cost_multiplier("llama3.1:8b") == 3.0  # now midrange

    def test_model_assignments_property(self, tracker):
        assignments = tracker.model_assignments
        assert "llama3.1:70b" in assignments
        assert assignments["llama3.1:70b"] == "frontier"


class TestWeightedTokens:
    def test_frontier_15x(self, tracker):
        assert tracker.calculate_weighted_tokens(1000, "llama3.1:70b") == 15000

    def test_standard_1x(self, tracker):
        assert tracker.calculate_weighted_tokens(1000, "llama3.1:8b") == 1000

    def test_embedding_01x(self, tracker):
        assert tracker.calculate_weighted_tokens(1000, "nomic-embed-text") == 100

    def test_unknown_5x(self, tracker):
        assert tracker.calculate_weighted_tokens(1000, "gpt-5.4") == 5000


class TestBudgetCheck:
    def test_under_budget(self, tracker):
        state = tracker.check_budget("key1", model="llama3.1:8b", estimated_tokens=5000)
        assert state.daily_limit == 10000
        assert state.tokens_remaining == 10000

    def test_over_budget_raises(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 8000)
        with pytest.raises(TokenBudgetExceeded) as exc_info:
            tracker.check_budget("key1", model="llama3.1:8b", estimated_tokens=3000)
        assert exc_info.value.budget_type == "daily_key_limit"

    def test_frontier_model_burns_budget_faster(self, tracker):
        tracker.record_usage("key1", "llama3.1:70b", 500)  # 500 * 15x = 7500
        state = tracker.get_budget_state("key1")
        assert state.tokens_used == 7500
        assert state.tokens_remaining == 2500

    def test_unknown_model_burns_at_default_rate(self, tracker):
        tracker.record_usage("key1", "gpt-5.4-turbo", 1000)  # 1000 * 5x = 5000
        state = tracker.get_budget_state("key1")
        assert state.tokens_used == 5000

    def test_tier_global_cap(self, tracker):
        tracker.record_usage("key1", "llama3.1:70b", 40000)
        with pytest.raises(TokenBudgetExceeded) as exc_info:
            tracker.check_budget(
                "key2", model="llama3.1:70b", estimated_tokens=15000,
                daily_limit_override=10_000_000,
            )
        assert "tier" in exc_info.value.budget_type

    def test_different_keys_independent(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 9000)
        state = tracker.get_budget_state("key2")
        assert state.tokens_used == 0

    def test_override_daily_limit(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 9000)
        state = tracker.check_budget("key1", model="llama3.1:8b", estimated_tokens=1000, daily_limit_override=50000)
        assert state.daily_limit == 50000


class TestRecordUsage:
    def test_accumulates(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 1000)
        tracker.record_usage("key1", "llama3.1:8b", 2000)
        state = tracker.get_budget_state("key1")
        assert state.tokens_used == 3000

    def test_tracks_by_tier(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 1000)       # standard 1x = 1000
        tracker.record_usage("key1", "llama3.1:70b", 100)        # frontier 15x = 1500
        tracker.record_usage("key1", "nomic-embed-text", 5000)   # embedding 0.1x = 500
        state = tracker.get_budget_state("key1")
        assert state.tokens_used == 3000
        assert state.tier_usage["standard"] == 1000
        assert state.tier_usage["frontier"] == 1500
        assert state.tier_usage["embedding"] == 500

    def test_unknown_model_tracked_as_unclassified(self, tracker):
        tracker.record_usage("key1", "gpt-5.4", 1000)  # 5x = 5000
        state = tracker.get_budget_state("key1")
        assert state.tier_usage["unclassified"] == 5000

    def test_zero_tokens_ignored(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 0)
        state = tracker.get_budget_state("key1")
        assert state.tokens_used == 0


class TestDisabled:
    def test_disabled_allows_everything(self):
        config = TokenBudgetConfig(enabled=False)
        tracker = TokenBudgetTracker(config)
        tracker.check_budget("key1", model="llama3.1:70b", estimated_tokens=999999)

    def test_disabled_skips_recording(self):
        config = TokenBudgetConfig(enabled=False)
        tracker = TokenBudgetTracker(config)
        tracker.record_usage("key1", "llama3.1:8b", 5000)


class TestCleanup:
    def test_cleanup_stale_keys(self, tracker):
        tracker.record_usage("key1", "llama3.1:8b", 1000)
        tracker._usage["key1"].date = "2020-01-01"
        removed = tracker.cleanup_stale_keys()
        assert removed == 1
        assert "key1" not in tracker._usage

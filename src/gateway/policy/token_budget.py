"""Token budget tracking — daily token quotas per key and model tier.

Tracks cumulative token usage and enforces configurable daily budgets.

Design:
- Pre-request: estimate cost from max_tokens, reject if budget exhausted
- Post-request: record actual token usage
- Daily reset: budgets reset at midnight UTC
- Model tiers: named cost levels (frontier, midrange, standard, embedding)
- Model assignments: map model names to tiers (exact or glob patterns)
- Unknown models default to default_cost_multiplier (safe/expensive default)
- Assignments are manageable at runtime via API — no config reloads needed
"""

import fnmatch
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field


class ModelTierConfig(BaseModel):
    """A named cost tier.

    Tiers define cost levels. Models are assigned to tiers
    via model_assignments (not patterns on the tier itself).
    """

    name: str = Field(description="Tier name (e.g., frontier, midrange, standard, embedding)")
    cost_multiplier: float = Field(
        default=1.0,
        ge=0.0,
        le=1000.0,
        description="Token cost multiplier (1.0 = baseline, 15.0 = 15x cost)",
    )
    daily_limit: int | None = Field(
        default=None,
        ge=0,
        description="Optional daily token limit for this tier globally (None = no tier cap)",
    )


class ModelAssignment(BaseModel):
    """Maps a model name (or glob pattern) to a tier."""

    model: str = Field(description="Model name or glob pattern (e.g., 'phi4:14b', '*embed*')")
    tier: str = Field(description="Tier name to assign this model to")


class TokenBudgetConfig(BaseModel):
    """Configuration for daily token budgets."""

    enabled: bool = Field(default=False, description="Enable token budget enforcement")
    default_daily_limit: int = Field(
        default=1_000_000,
        ge=0,
        description="Default daily token budget per key (0 = unlimited)",
    )
    default_cost_multiplier: float = Field(
        default=5.0,
        ge=0.0,
        le=1000.0,
        description="Cost multiplier for models not assigned to any tier (safe default — classify to lower)",
    )
    model_tiers: list[ModelTierConfig] = Field(
        default_factory=list,
        max_length=50,
        description="Named cost tiers",
    )
    model_assignments: list[ModelAssignment] = Field(
        default_factory=list,
        max_length=500,
        description="Model-to-tier mappings (exact name or glob pattern)",
    )
    enforce_pre_request: bool = Field(
        default=True,
        description="Reject requests pre-dispatch if estimated cost exceeds remaining budget",
    )


class TokenBudgetExceeded(Exception):
    """Daily token budget has been exceeded."""

    def __init__(
        self,
        message: str,
        key: str,
        budget_type: str,
        used: int,
        limit: int,
        resets_at: str,
    ):
        super().__init__(message)
        self.key = key
        self.budget_type = budget_type
        self.used = used
        self.limit = limit
        self.resets_at = resets_at


@dataclass
class KeyUsage:
    """Token usage tracking for a single key on a single day."""

    date: str  # YYYY-MM-DD in UTC
    total_tokens: int = 0
    tokens_by_tier: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    request_count: int = 0


@dataclass
class BudgetState:
    """Current budget state for a key."""

    daily_limit: int
    tokens_used: int
    tokens_remaining: int
    tier_usage: dict[str, int]
    resets_at: str
    cost_multiplier_applied: float = 1.0


class TokenBudgetTracker:
    """Tracks daily token usage per key and enforces budgets.

    Model resolution:
    1. Check model_assignments (exact match first, then glob patterns)
    2. If no assignment matches → use default_cost_multiplier
    3. Unknown models are expensive by default — classify them cheaper via
       the dashboard or API

    Runtime model assignment:
    - assign_model("gpt-5.4", "frontier") — adds at runtime, no restart
    - unassign_model("gpt-5.4") — removes assignment

    Thread-safe for single-process. For distributed deployments,
    swap with a Redis-backed implementation.
    """

    def __init__(self, config: TokenBudgetConfig | None = None):
        self._config = config or TokenBudgetConfig()
        # key -> KeyUsage
        self._usage: dict[str, KeyUsage] = {}
        # tier_name -> total tokens used today (for global tier caps)
        self._tier_totals: dict[str, int] = defaultdict(int)
        self._tier_totals_date: str = ""

        # Build tier lookup
        self._tiers: dict[str, ModelTierConfig] = {t.name: t for t in self._config.model_tiers}

        # Runtime model assignments (mutable — can be updated via API)
        # model_pattern -> tier_name
        self._model_assignments: dict[str, str] = {
            a.model: a.tier for a in self._config.model_assignments
        }

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def model_assignments(self) -> dict[str, str]:
        """Current model-to-tier assignments."""
        return dict(self._model_assignments)

    @property
    def tiers(self) -> dict[str, ModelTierConfig]:
        """Configured tiers."""
        return dict(self._tiers)

    def assign_model(self, model: str, tier_name: str) -> bool:
        """Assign a model to a tier at runtime (no restart needed).

        Args:
            model: Model name or glob pattern
            tier_name: Tier to assign to (must exist in config)

        Returns:
            True if assigned, False if tier doesn't exist
        """
        if tier_name not in self._tiers:
            return False
        self._model_assignments[model] = tier_name
        return True

    def unassign_model(self, model: str) -> bool:
        """Remove a model assignment. Returns True if it existed."""
        return self._model_assignments.pop(model, None) is not None

    def _today(self) -> str:
        """Current UTC date string."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _tomorrow_midnight_utc(self) -> str:
        """ISO timestamp of next midnight UTC."""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if tomorrow <= now:
            tomorrow += timedelta(days=1)
        return tomorrow.isoformat()

    def _get_key_usage(self, key: str) -> KeyUsage:
        """Get or create today's usage for a key, resetting if day changed."""
        today = self._today()
        usage = self._usage.get(key)
        if usage is None or usage.date != today:
            usage = KeyUsage(date=today)
            self._usage[key] = usage
        return usage

    def _reset_tier_totals_if_needed(self) -> None:
        """Reset global tier totals at day boundary."""
        today = self._today()
        if self._tier_totals_date != today:
            self._tier_totals = defaultdict(int)
            self._tier_totals_date = today

    def resolve_tier(self, model: str) -> ModelTierConfig | None:
        """Find the tier for a model.

        Resolution order:
        1. Exact match in model_assignments
        2. Glob pattern match in model_assignments
        3. None (caller should use default_cost_multiplier)
        """
        if not model:
            return None

        model_lower = model.lower()

        # Exact match first
        for pattern, tier_name in self._model_assignments.items():
            if model_lower == pattern.lower():
                return self._tiers.get(tier_name)

        # Glob match
        for pattern, tier_name in self._model_assignments.items():
            if fnmatch.fnmatch(model_lower, pattern.lower()):
                return self._tiers.get(tier_name)

        return None

    def get_cost_multiplier(self, model: str) -> float:
        """Get the cost multiplier for a model.

        Returns the tier's multiplier if assigned, otherwise default_cost_multiplier.
        """
        tier = self.resolve_tier(model)
        if tier:
            return tier.cost_multiplier
        return self._config.default_cost_multiplier

    def calculate_weighted_tokens(self, tokens: int, model: str) -> int:
        """Calculate weighted token cost based on model tier."""
        multiplier = self.get_cost_multiplier(model)
        return int(tokens * multiplier)

    def check_budget(
        self,
        key: str,
        model: str = "",
        estimated_tokens: int = 0,
        daily_limit_override: int | None = None,
    ) -> BudgetState:
        """Check if a request fits within budget. Raises if not.

        Args:
            key: API key or client ID
            model: Model name (for tier resolution)
            estimated_tokens: Estimated tokens for this request (e.g., max_tokens)
            daily_limit_override: Per-key daily limit override (from DB)

        Raises:
            TokenBudgetExceeded: If budget would be exceeded
        """
        if not self._config.enabled:
            return BudgetState(
                daily_limit=0,
                tokens_used=0,
                tokens_remaining=0,
                tier_usage={},
                resets_at="",
            )

        usage = self._get_key_usage(key)
        self._reset_tier_totals_if_needed()

        daily_limit = daily_limit_override or self._config.default_daily_limit
        tier = self.resolve_tier(model)
        multiplier = tier.cost_multiplier if tier else self._config.default_cost_multiplier
        weighted_estimate = int(estimated_tokens * multiplier)
        resets_at = self._tomorrow_midnight_utc()

        # Check per-key daily budget
        if daily_limit > 0 and self._config.enforce_pre_request:
            if usage.total_tokens + weighted_estimate > daily_limit:
                raise TokenBudgetExceeded(
                    message=(
                        f"Daily token budget exceeded for key '{key}': "
                        f"{usage.total_tokens} used + {weighted_estimate} estimated "
                        f"> {daily_limit} limit"
                    ),
                    key=key,
                    budget_type="daily_key_limit",
                    used=usage.total_tokens,
                    limit=daily_limit,
                    resets_at=resets_at,
                )

        # Check per-tier global cap
        if tier and tier.daily_limit is not None and tier.daily_limit > 0:
            tier_used = self._tier_totals.get(tier.name, 0)
            # Tier caps use raw tokens (not weighted) since the cap is per-tier
            if tier_used + estimated_tokens > tier.daily_limit:
                raise TokenBudgetExceeded(
                    message=(
                        f"Daily tier '{tier.name}' budget exceeded: "
                        f"{tier_used} used + {estimated_tokens} estimated "
                        f"> {tier.daily_limit} limit"
                    ),
                    key=key,
                    budget_type=f"daily_tier_limit:{tier.name}",
                    used=tier_used,
                    limit=tier.daily_limit,
                    resets_at=resets_at,
                )

        return BudgetState(
            daily_limit=daily_limit,
            tokens_used=usage.total_tokens,
            tokens_remaining=max(0, daily_limit - usage.total_tokens) if daily_limit > 0 else 0,
            tier_usage=dict(usage.tokens_by_tier),
            resets_at=resets_at,
            cost_multiplier_applied=multiplier,
        )

    def record_usage(
        self,
        key: str,
        model: str,
        tokens: int,
    ) -> None:
        """Record actual token usage after a response.

        Args:
            key: API key or client ID
            model: Model name used
            tokens: Total tokens consumed (prompt + completion)
        """
        if not self._config.enabled or tokens <= 0:
            return

        usage = self._get_key_usage(key)
        self._reset_tier_totals_if_needed()

        tier = self.resolve_tier(model)
        multiplier = tier.cost_multiplier if tier else self._config.default_cost_multiplier
        weighted = int(tokens * multiplier)

        usage.total_tokens += weighted
        usage.request_count += 1

        tier_name = tier.name if tier else "unclassified"
        usage.tokens_by_tier[tier_name] += weighted

        # Update global tier totals (raw tokens for tier cap enforcement)
        if tier:
            self._tier_totals[tier.name] += tokens

    def get_budget_state(self, key: str, daily_limit_override: int | None = None) -> BudgetState:
        """Get current budget state for a key without checking/consuming."""
        usage = self._get_key_usage(key)
        daily_limit = daily_limit_override or self._config.default_daily_limit

        return BudgetState(
            daily_limit=daily_limit,
            tokens_used=usage.total_tokens,
            tokens_remaining=max(0, daily_limit - usage.total_tokens) if daily_limit > 0 else 0,
            tier_usage=dict(usage.tokens_by_tier),
            resets_at=self._tomorrow_midnight_utc(),
        )

    def cleanup_stale_keys(self) -> int:
        """Remove usage records from previous days. Returns count removed."""
        today = self._today()
        stale = [k for k, v in self._usage.items() if v.date != today]
        for k in stale:
            del self._usage[k]
        return len(stale)

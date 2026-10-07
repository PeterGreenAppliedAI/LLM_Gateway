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

Persistence (D-037): this tracker counts in memory, so checks stay fast.
BudgetSync (storage/budgets.py) writes the counts to the database every
few seconds and reads back the day's totals, so usage survives restarts
and every gateway process sees the others' spend. Usage is a persisted
baseline (today's totals from the database) plus deltas not yet written.
Tiers and model assignments changed at runtime are saved too.
"""

import fnmatch
import time
from dataclasses import dataclass
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
class UsageCounts:
    """Tokens spent by one client in one tier on one day."""

    weighted: int = 0  # tokens x tier multiplier: counts toward the key's budget
    raw: int = 0  # unweighted: counts toward the tier's global cap
    requests: int = 0

    def add(self, other: "UsageCounts") -> None:
        self.weighted += other.weighted
        self.raw += other.raw
        self.requests += other.requests


@dataclass
class UsageRow:
    """One (day, client, tier) total, as stored in budget_usage."""

    day: str
    client_id: str
    tier: str
    weighted: int
    raw: int
    requests: int


@dataclass
class BudgetState:
    """Current budget state for a key."""

    daily_limit: int
    tokens_used: int
    tokens_remaining: int
    tier_usage: dict[str, int]
    resets_at: str
    cost_multiplier_applied: float = 1.0
    request_count: int = 0


UNCLASSIFIED = "unclassified"

# Holds older than this stop counting (never settled: a bug or lost request)
RESERVATION_TTL = 600.0


@dataclass
class Reservation:
    key: str
    tier: str
    weighted: int
    raw: int
    created: float


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
        # Today's totals as last read from the database: key -> tier -> counts
        self._baseline_day = self._today()
        self._baseline: dict[str, dict[str, UsageCounts]] = {}
        # Usage recorded here and not yet written: day -> key -> tier -> counts
        self._pending: dict[str, dict[str, dict[str, UsageCounts]]] = {}
        # Being written right now: still counted, so a check made during the
        # write doesn't see the batch vanish and allow overspend
        self._flushing: dict[str, dict[str, dict[str, UsageCounts]]] = {}
        # Set by BudgetSync; without it past days' usage is simply dropped
        self.persisted = False
        # Admitted requests' estimated cost, until they finish (D-043)
        self._reservations: dict[str, Reservation] = {}

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

    def add_tier(self, name: str, cost_multiplier: float, daily_limit: int | None = None) -> bool:
        """Add or update a cost tier at runtime.

        Returns True if created, False if updated.
        """
        is_new = name not in self._tiers
        self._tiers[name] = ModelTierConfig(
            name=name, cost_multiplier=cost_multiplier, daily_limit=daily_limit
        )
        return is_new

    def remove_tier(self, name: str) -> bool:
        """Remove a tier. Fails if models are still assigned to it.

        Returns True if removed, False if not found.
        """
        if name not in self._tiers:
            return False
        # Don't remove if models reference it
        assigned = [m for m, t in self._model_assignments.items() if t == name]
        if assigned:
            return False
        del self._tiers[name]
        return True

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

    # -- usage: persisted baseline + unwritten deltas ---------------------

    def _today_usage(self, key: str) -> dict[str, UsageCounts]:
        """tier -> counts for key today (baseline + pending)."""
        today = self._today()
        merged: dict[str, UsageCounts] = {}
        sources = [
            self._pending.get(today, {}).get(key, {}),
            self._flushing.get(today, {}).get(key, {}),
        ]
        if self._baseline_day == today:
            sources.insert(0, self._baseline.get(key, {}))
        for source in sources:
            for tier, counts in source.items():
                merged.setdefault(tier, UsageCounts()).add(counts)
        return merged

    def _tier_raw_today(self, tier: str) -> int:
        today = self._today()
        total = 0
        if self._baseline_day == today:
            total += sum(t[tier].raw for t in self._baseline.values() if tier in t)
        for unwritten in (self._pending, self._flushing):
            total += sum(t[tier].raw for t in unwritten.get(today, {}).values() if tier in t)
        return total

    def keys_today(self) -> list[str]:
        """Clients with usage today."""
        today = self._today()
        keys = set(self._pending.get(today, {})) | set(self._flushing.get(today, {}))
        if self._baseline_day == today:
            keys |= set(self._baseline)
        return sorted(keys)

    def take_pending(self) -> list[UsageRow]:
        """Start writing the usage not yet persisted; it stays counted until
        confirm_written() or restore_pending()."""
        for day, keys in self._pending.items():
            for key, tiers in keys.items():
                for tier, counts in tiers.items():
                    self._counts(self._flushing, day, key, tier).add(counts)
        self._pending = {}
        return [
            UsageRow(day, key, tier, c.weighted, c.raw, c.requests)
            for day, keys in self._flushing.items()
            for key, tiers in keys.items()
            for tier, c in tiers.items()
        ]

    def confirm_written(self) -> None:
        """The batch is in the database: fold it into today's baseline."""
        today = self._today()
        if self._baseline_day == today:
            for key, tiers in self._flushing.get(today, {}).items():
                for tier, counts in tiers.items():
                    self._baseline.setdefault(key, {}).setdefault(tier, UsageCounts()).add(counts)
        self._flushing = {}

    def restore_pending(self) -> None:
        """The write failed: the batch goes back to pending, retried next time."""
        for day, keys in self._flushing.items():
            for key, tiers in keys.items():
                for tier, counts in tiers.items():
                    self._counts(self._pending, day, key, tier).add(counts)
        self._flushing = {}

    def set_baseline(self, day: str, rows: list[UsageRow]) -> None:
        """Replace today's persisted totals with what the database holds."""
        baseline: dict[str, dict[str, UsageCounts]] = {}
        for row in rows:
            baseline.setdefault(row.client_id, {})[row.tier] = UsageCounts(
                row.weighted, row.raw, row.requests
            )
        self._baseline_day = day
        self._baseline = baseline

    @staticmethod
    def _counts(
        store: dict[str, dict[str, dict[str, UsageCounts]]], day: str, key: str, tier: str
    ) -> UsageCounts:
        return store.setdefault(day, {}).setdefault(key, {}).setdefault(tier, UsageCounts())

    # -- tiers and assignments as one saved document ----------------------

    def catalog_document(self) -> dict:
        """Tiers and model assignments, for saving (D-037)."""
        return {
            "tiers": [t.model_dump() for t in self._tiers.values()],
            "assignments": dict(self._model_assignments),
        }

    def load_catalog(self, document: dict) -> None:
        """Replace tiers and assignments with a saved document."""
        self._tiers = {t["name"]: ModelTierConfig(**t) for t in document.get("tiers", [])}
        self._model_assignments = {
            model: tier
            for model, tier in document.get("assignments", {}).items()
            if tier in self._tiers
        }

    def _tomorrow_midnight_utc(self) -> str:
        """ISO timestamp of next midnight UTC."""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if tomorrow <= now:
            tomorrow += timedelta(days=1)
        return tomorrow.isoformat()

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

        usage = self._today_usage(key)
        # In flight counts as spent (D-043): admitted requests hold reservations
        tokens_used = sum(c.weighted for c in usage.values()) + self._reserved_weighted(key)

        daily_limit = daily_limit_override or self._config.default_daily_limit
        tier = self.resolve_tier(model)
        multiplier = tier.cost_multiplier if tier else self._config.default_cost_multiplier
        weighted_estimate = int(estimated_tokens * multiplier)
        resets_at = self._tomorrow_midnight_utc()

        # Check per-key daily budget
        if daily_limit > 0 and self._config.enforce_pre_request:
            # ">=" too: an exhausted budget refuses even a zero estimate
            if tokens_used >= daily_limit or tokens_used + weighted_estimate > daily_limit:
                raise TokenBudgetExceeded(
                    message=(
                        f"Daily token budget exceeded for key '{key}': "
                        f"{tokens_used} used + {weighted_estimate} estimated "
                        f"> {daily_limit} limit"
                    ),
                    key=key,
                    budget_type="daily_key_limit",
                    used=tokens_used,
                    limit=daily_limit,
                    resets_at=resets_at,
                )

        # Check per-tier global cap
        if tier and tier.daily_limit is not None and tier.daily_limit > 0:
            tier_used = self._tier_raw_today(tier.name) + self._reserved_raw(tier.name)
            # Tier caps use raw tokens (not weighted) since the cap is per-tier
            if tier_used >= tier.daily_limit or tier_used + estimated_tokens > tier.daily_limit:
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
            tokens_used=tokens_used,
            tokens_remaining=max(0, daily_limit - tokens_used) if daily_limit > 0 else 0,
            tier_usage={t: c.weighted for t, c in usage.items()},
            resets_at=resets_at,
            cost_multiplier_applied=multiplier,
            request_count=sum(c.requests for c in usage.values()),
        )

    # -- reservations (D-043): hard limits under concurrency ----------------

    def reserve(
        self,
        reservation_id: str,
        key: str,
        model: str,
        estimated_tokens: int,
        daily_limit_override: int | None = None,
    ) -> BudgetState:
        """Check the budget and hold the estimate in one step.

        Synchronous on purpose: no await between the check and the hold, so
        concurrent requests on one event loop can't all pass a check against
        the same remaining budget (they did, when only finished requests
        counted). Settle with record_usage(..., reservation_id) or release().

        Raises:
            TokenBudgetExceeded: The estimate doesn't fit what's left.
        """
        if not self._config.enabled:
            return self.check_budget(key, model, estimated_tokens, daily_limit_override)
        self._expire_reservations()
        state = self.check_budget(key, model, estimated_tokens, daily_limit_override)
        tier = self.resolve_tier(model)
        multiplier = tier.cost_multiplier if tier else self._config.default_cost_multiplier
        self._reservations[reservation_id] = Reservation(
            key=key,
            tier=tier.name if tier else UNCLASSIFIED,
            weighted=int(estimated_tokens * multiplier),
            raw=estimated_tokens,
            created=time.monotonic(),
        )
        return state

    def release(self, reservation_id: str) -> None:
        """Drop a hold without charging it (request failed before using tokens)."""
        self._reservations.pop(reservation_id, None)

    def _reserved_weighted(self, key: str) -> int:
        return sum(r.weighted for r in self._reservations.values() if r.key == key)

    def _reserved_raw(self, tier: str) -> int:
        return sum(r.raw for r in self._reservations.values() if r.tier == tier)

    def _expire_reservations(self) -> None:
        # A hold never settled (a bug, a lost request) stops blocking the
        # budget after RESERVATION_TTL; actual usage is still charged if it
        # arrives later
        cutoff = time.monotonic() - RESERVATION_TTL
        for rid in [rid for rid, r in self._reservations.items() if r.created < cutoff]:
            del self._reservations[rid]

    def record_usage(
        self,
        key: str,
        model: str,
        tokens: int,
        reservation_id: str | None = None,
    ) -> None:
        """Record actual token usage after a response.

        Args:
            key: API key or client ID
            model: Model name used
            tokens: Total tokens consumed (prompt + completion)
        """
        if reservation_id is not None:
            self._reservations.pop(reservation_id, None)  # actual usage replaces the hold
        if not self._config.enabled or tokens <= 0:
            return

        tier = self.resolve_tier(model)
        multiplier = tier.cost_multiplier if tier else self._config.default_cost_multiplier
        tier_name = tier.name if tier else UNCLASSIFIED
        # Raw tokens count toward the tier's global cap; weighted toward the key
        today = self._today()
        if not self.persisted and any(day != today for day in self._pending):
            self.cleanup_stale_keys()  # nothing will write past days
        self._counts(self._pending, today, key, tier_name).add(
            UsageCounts(weighted=int(tokens * multiplier), raw=tokens, requests=1)
        )

    def get_budget_state(self, key: str, daily_limit_override: int | None = None) -> BudgetState:
        """Get current budget state for a key without checking/consuming."""
        usage = self._today_usage(key)
        tokens_used = sum(c.weighted for c in usage.values())
        daily_limit = daily_limit_override or self._config.default_daily_limit

        return BudgetState(
            daily_limit=daily_limit,
            tokens_used=tokens_used,
            tokens_remaining=max(0, daily_limit - tokens_used) if daily_limit > 0 else 0,
            tier_usage={t: c.weighted for t, c in usage.items()},
            resets_at=self._tomorrow_midnight_utc(),
            request_count=sum(c.requests for c in usage.values()),
        )

    def cleanup_stale_keys(self) -> int:
        """Drop past days' unwritten usage when nothing persists it. Returns count removed."""
        today = self._today()
        stale = [day for day in self._pending if day != today]
        removed = sum(len(self._pending.pop(day)) for day in stale)
        if self._baseline_day != today:
            removed += len(self._baseline)
            self._baseline, self._baseline_day = {}, today
        return removed

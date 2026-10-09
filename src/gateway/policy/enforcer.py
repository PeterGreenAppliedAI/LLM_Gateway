"""Policy enforcer - coordinates all policy checks.

Per rule.md:
- Single Responsibility: Coordinates policy checks, doesn't implement them
- Explicit Boundaries: Clear enforcement contract
- Contracts: PolicyViolation for any policy failure

Per PRD Section 10:
- Max tokens per request
- Requests per minute (global and per user)
- Allowed providers per task
- Block execution if provider unhealthy
"""

from collections.abc import Collection, Sequence
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel, Field

from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest
from gateway.policy.rate_limiter import RateLimitConfig, RateLimiter, RateLimitExceeded
from gateway.policy.token_budget import TokenBudgetConfig, TokenBudgetExceeded, TokenBudgetTracker
from gateway.policy.token_limiter import TokenLimitConfig, TokenLimiter, TokenLimitExceeded
from gateway.state.ratelimit import RateWindowStore

# Budget reservations made during the current request, as (reservation_id,
# client). get_inference_auth installs the list and releases what's left
# when the response is done (D-043).
_BUDGET_HOLDS: ContextVar[list[tuple[str, str]] | None] = ContextVar("budget_holds", default=None)

# Tasks whose cost is the input alone (media is metered in its own units, D-021)
_NO_TEXT_OUTPUT = {
    TaskType.EMBEDDINGS,
    TaskType.SPEECH,
    TaskType.TRANSCRIPTION,
    TaskType.TRANSLATION,
}


def start_budget_holds() -> list[tuple[str, str]]:
    """Begin collecting this request's budget reservations; returns the list."""
    holds: list[tuple[str, str]] = []
    _BUDGET_HOLDS.set(holds)
    return holds


class PolicyViolation(Exception):
    """A policy has been violated."""

    def __init__(
        self,
        message: str,
        policy_type: str,
        code: str,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.policy_type = policy_type
        self.code = code
        self.retry_after = retry_after


class TaskProviderPolicy(BaseModel):
    """Policy for which providers can handle which tasks."""

    task: TaskType
    allowed_providers: set[str] = Field(default_factory=set)
    denied_providers: set[str] = Field(default_factory=set)


class PolicyConfig(BaseModel):
    """Configuration for policy enforcement."""

    enabled: bool = Field(default=True, description="Whether policy enforcement is enabled")
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    token_limit: TokenLimitConfig = Field(default_factory=TokenLimitConfig)
    token_budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig)

    # Provider-task mapping (optional - empty means all providers allowed for all tasks)
    task_policies: list[TaskProviderPolicy] = Field(
        default_factory=list,
        max_length=50,
        description="Task-specific provider policies",
    )


@dataclass
class PolicyCheckResult:
    """Result of policy check."""

    allowed: bool
    rate_limit_remaining: int | None = None
    rate_limit_reset: float | None = None
    adjusted_max_tokens: int | None = None
    violation_message: str | None = None
    violation_code: str | None = None


def _text_length(content) -> int:
    """Characters of text in message content: a string or content parts."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return 0


class PolicyEnforcer:
    """Coordinates policy enforcement across all policy types.

    Checks:
    1. Rate limits (per client/user)
    2. Token limits (per request)
    3. Provider-task authorization

    Usage:
        enforcer = PolicyEnforcer(config)
        await enforcer.enforce(request, rate_limit_key="client_123")
    """

    def __init__(
        self,
        config: PolicyConfig | None = None,
        rate_store: RateWindowStore | None = None,
        endpoint_names: Collection[str] | None = None,
    ):
        """Initialize policy enforcer.

        Args:
            config: Policy configuration. Uses defaults if not provided.
            rate_store: Where rate-limit windows are counted (gateway.state);
                defaults to this process's memory
            endpoint_names: Enabled endpoint names, used to strip an
                "endpoint/" pin off model names for budget tier resolution
        """
        self._config = config or PolicyConfig()
        self._endpoint_names = frozenset(endpoint_names or ())
        self._rate_limiter = RateLimiter(self._config.rate_limit, rate_store)
        self._token_limiter = TokenLimiter(self._config.token_limit)
        self._token_budget = TokenBudgetTracker(self._config.token_budget)

        # Build task -> provider policy lookup
        self._task_policies: dict[TaskType, TaskProviderPolicy] = {}
        for policy in self._config.task_policies:
            self._task_policies[policy.task] = policy

    @property
    def enabled(self) -> bool:
        """Check if policy enforcement is enabled."""
        return self._config.enabled

    def set_task_policies(self, policies: Sequence["TaskProviderPolicy"]) -> None:
        """Replace the task/endpoint policies at runtime (dashboard knob).

        Atomic swap of the lookup dict; in-flight requests keep whichever
        policy they already read.
        """
        self._task_policies = {policy.task: policy for policy in policies}

    def _bare_model(self, model: str) -> str:
        """Strip an "endpoint/" pin so budget tiers resolve on the real name.

        "gpu-node/premium-x" reserved under the pinned string misses the
        tier globs: the reservation gets the default (expensive) multiplier
        while the per-tier global cap goes unchecked — and settlement,
        which strips the pin, then disagrees with admission. Mirrors
        Dispatcher.parse_provider_from_model: only a configured endpoint
        name makes a pin ("meta-llama/..." stays a model name).
        """
        if "/" in model:
            prefix, rest = model.split("/", 1)
            if rest and prefix in self._endpoint_names:
                return rest
        return model

    async def enforce(
        self,
        request: InternalRequest,
        rate_limit_key: str | None = None,
        provider: str | None = None,
        allowed_models: list[str] | None = None,
        allowed_endpoints: list[str] | None = None,
        rate_limit_rpm: int | None = None,
        generations: Sequence[InternalRequest] | None = None,
        estimated_tokens: int | None = None,
    ) -> PolicyCheckResult:
        """Enforce all policies on a request.

        Args:
            request: The request to check
            rate_limit_key: Key for rate limiting (defaults to client_id or user_id)
            provider: Target provider (for task-provider policy check)
            allowed_models: Per-key model allowlist (from API key)
            allowed_endpoints: Per-key endpoint allowlist (from API key)
            rate_limit_rpm: Per-key rate limit override (from API key)
            generations: Every upstream request this one will be dispatched as
                (n choices, a completions prompt list). The budget reservation
                covers all of them; default: just `request`.
            estimated_tokens: The cost to reserve, when the caller knows it
                better than a text estimate (media, metered in its own units).

        Returns:
            PolicyCheckResult with enforcement details

        Raises:
            PolicyViolation: If any policy is violated
        """
        if not self._config.enabled:
            return PolicyCheckResult(allowed=True)

        # Determine rate limit key
        key = rate_limit_key or request.client_id or request.user_id or "anonymous"

        # 1. Check rate limits (with per-key override)
        try:
            rate_state = await self._rate_limiter.acquire(key, rpm_override=rate_limit_rpm)
        except RateLimitExceeded as e:
            raise PolicyViolation(
                message=str(e),
                policy_type="rate_limit",
                code="rate_limit_exceeded",
                retry_after=e.retry_after,
            )

        # 2. Check token limits
        try:
            validated_max_tokens = self._token_limiter.validate_max_tokens(request.max_tokens)
        except TokenLimitExceeded as e:
            raise PolicyViolation(
                message=str(e),
                policy_type="token_limit",
                code="token_limit_exceeded",
            )

        # 3. Check per-key model allowlist
        if allowed_models and request.model:
            import fnmatch

            model_allowed = any(
                fnmatch.fnmatch(request.model, pattern) for pattern in allowed_models
            )
            if not model_allowed:
                raise PolicyViolation(
                    message=f"Model '{request.model}' is not in allowed models for this API key",
                    policy_type="model_not_allowed",
                    code="model_not_allowed",
                )

        # 4. Check per-key endpoint allowlist
        if allowed_endpoints and request.preferred_provider:
            if request.preferred_provider not in allowed_endpoints:
                raise PolicyViolation(
                    message=f"Endpoint '{request.preferred_provider}' is not in allowed endpoints for this API key",
                    policy_type="endpoint_not_allowed",
                    code="endpoint_not_allowed",
                )

        # 5. Token budget (daily quotas): reserve the estimated cost now, so
        # concurrent requests can't all pass against the same remaining budget
        # (D-043). Settled with actual usage, or released if the request fails.
        if self._token_budget.enabled:
            try:
                self._token_budget.reserve(
                    reservation_id=request.request_id,
                    key=key,
                    model=self._bare_model(request.model or ""),
                    estimated_tokens=estimated_tokens
                    if estimated_tokens is not None
                    else sum(
                        self._estimate_tokens(g, validated_max_tokens)
                        for g in (generations or [request])
                    ),
                    daily_limit_override=None,  # TODO: per-key override from DB
                )
                holds = _BUDGET_HOLDS.get()
                if holds is not None:
                    holds.append((request.request_id, key))
            except TokenBudgetExceeded as e:
                raise PolicyViolation(
                    message=str(e),
                    policy_type="token_budget_exceeded",
                    code="token_budget_exceeded",
                )

        # 6. Check provider-task authorization
        if provider and request.task in self._task_policies:
            policy = self._task_policies[request.task]

            # Check denied list first
            if policy.denied_providers and provider in policy.denied_providers:
                raise PolicyViolation(
                    message=f"Provider '{provider}' is not allowed for task '{request.task.value}'",
                    policy_type="provider_task",
                    code="provider_denied_for_task",
                )

            # Check allowed list if specified
            if policy.allowed_providers and provider not in policy.allowed_providers:
                raise PolicyViolation(
                    message=f"Provider '{provider}' is not in allowed list for task '{request.task.value}'",
                    policy_type="provider_task",
                    code="provider_not_allowed_for_task",
                )

        # Determine if max_tokens was adjusted
        adjusted = None
        if validated_max_tokens != request.max_tokens and request.max_tokens is not None:
            adjusted = validated_max_tokens

        return PolicyCheckResult(
            allowed=True,
            rate_limit_remaining=rate_state.requests_remaining_minute,
            rate_limit_reset=rate_state.reset_minute,
            adjusted_max_tokens=adjusted,
        )

    async def check_rate_limit(self, key: str) -> PolicyCheckResult:
        """Check rate limit only (without consuming).

        Args:
            key: Rate limit key

        Returns:
            PolicyCheckResult with rate limit state
        """
        if not self._config.enabled or not self._rate_limiter.enabled:
            return PolicyCheckResult(allowed=True)

        state = await self._rate_limiter.check(key)

        return PolicyCheckResult(
            allowed=state.requests_remaining_minute > 0,
            rate_limit_remaining=state.requests_remaining_minute,
            rate_limit_reset=state.reset_minute,
        )

    def check_provider_allowed(self, task: TaskType, provider: str) -> bool:
        """Check if a provider is allowed for a task.

        Args:
            task: Task type
            provider: Provider name

        Returns:
            True if allowed
        """
        if not self._config.enabled:
            return True

        if task not in self._task_policies:
            return True  # No policy means allowed

        policy = self._task_policies[task]

        if policy.denied_providers and provider in policy.denied_providers:
            return False

        if policy.allowed_providers and provider not in policy.allowed_providers:
            return False

        return True

    def get_default_max_tokens(self) -> int:
        """Get the default max_tokens value."""
        return self._token_limiter.default_max_tokens

    async def reset_rate_limit(self, key: str) -> None:
        """Reset rate limit for a key (admin operation).

        Args:
            key: Rate limit key to reset
        """
        await self._rate_limiter.reset(key)

    async def reset_all_rate_limits(self) -> None:
        """Reset all rate limits (admin operation)."""
        await self._rate_limiter.reset_all()

    def record_token_usage(self, key: str, model: str, tokens: int) -> None:
        """Record actual token usage after a response completes.

        Settles this request's budget reservation (D-043), replacing the
        estimate with what was actually used.

        Args:
            key: Client ID or rate limit key
            model: Model name used
            tokens: Total tokens consumed (prompt + completion)
        """
        reservation_id = None
        for rid, holder in _BUDGET_HOLDS.get() or ():
            if holder == key:
                reservation_id = rid
                break
        self._token_budget.record_usage(key, model, tokens, reservation_id=reservation_id)

    def release_budget_holds(self, holds: list[tuple[str, str]]) -> None:
        """End of request: drop holds never settled (failed or abandoned requests)."""
        for rid, _ in holds:
            self._token_budget.release(rid)

    def _estimate_tokens(self, request: InternalRequest, max_tokens: int | None) -> int:
        """What a request may cost before it runs: its prompt plus the output it may produce.

        Prompt: ~4 characters per token over all text sent. Output: max_tokens,
        or the default when unset (an unset max_tokens used to estimate zero,
        so it never counted against the budget). Embeddings and media produce
        no text output.
        """
        chars = 0
        for message in request.messages or []:
            chars += len(message.content or "") or _text_length(message.content_parts)
        for item in request.input_data or []:
            chars += len(item) if isinstance(item, str) else 0
        chars += len(getattr(request, "prompt", None) or "")
        prompt_estimate = chars // 4 + 1
        if request.task in _NO_TEXT_OUTPUT:
            return prompt_estimate
        return prompt_estimate + (max_tokens or self._token_limiter.default_max_tokens)

    @property
    def token_budget(self) -> TokenBudgetTracker:
        """Access the token budget tracker (for querying state)."""
        return self._token_budget

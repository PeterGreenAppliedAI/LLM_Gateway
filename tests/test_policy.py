"""Tests for policy enforcement - rate limiting, token limits, and enforcer."""

import pytest

from gateway.models.common import TaskType
from gateway.models.internal import InternalRequest, Message, MessageRole
from gateway.policy.enforcer import (
    PolicyConfig,
    PolicyEnforcer,
    PolicyViolation,
    TaskProviderPolicy,
)
from gateway.policy.rate_limiter import RateLimitConfig, RateLimiter, RateLimitExceeded
from gateway.policy.token_limiter import TokenLimitConfig, TokenLimiter, TokenLimitExceeded

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def rate_limit_config() -> RateLimitConfig:
    """Strict rate limit config for testing."""
    return RateLimitConfig(
        enabled=True,
        requests_per_minute=5,
        requests_per_hour=20,
        burst_limit=3,
    )


@pytest.fixture
def token_limit_config() -> TokenLimitConfig:
    """Token limit config for testing."""
    return TokenLimitConfig(
        enabled=True,
        max_tokens_per_request=1000,
        max_context_tokens=4000,
        default_max_tokens=256,
    )


@pytest.fixture
def sample_request() -> InternalRequest:
    """Sample request for policy testing."""
    return InternalRequest(
        task=TaskType.CHAT,
        model="llama3.2",
        messages=[Message(role=MessageRole.USER, content="Hello")],
        client_id="test-client",
        user_id="test-user",
        max_tokens=500,
    )


# =============================================================================
# RateLimiter Tests
# =============================================================================


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_disabled_allows_all(self):
        """Disabled rate limiter allows all requests."""
        config = RateLimitConfig(enabled=False)
        limiter = RateLimiter(config)

        # Should never raise
        for _ in range(100):
            state = limiter.acquire("test-key")
            assert state.requests_remaining_minute > 0

    def test_burst_limit_enforced(self, rate_limit_config):
        """Burst limit is enforced."""
        limiter = RateLimiter(rate_limit_config)

        # Make burst_limit requests
        for _ in range(rate_limit_config.burst_limit):
            limiter.acquire("test-key")

        # Next should fail
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.acquire("test-key")

        assert exc_info.value.limit == rate_limit_config.burst_limit
        assert exc_info.value.retry_after > 0

    def test_minute_limit_enforced(self, rate_limit_config):
        """Per-minute limit is enforced."""
        # Use high burst to test minute limit
        config = RateLimitConfig(
            enabled=True,
            requests_per_minute=3,
            burst_limit=10,
        )
        limiter = RateLimiter(config)

        # Make requests_per_minute requests
        for _ in range(config.requests_per_minute):
            limiter.acquire("test-key")

        # Next should fail
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.acquire("test-key")

        assert "per minute" in str(exc_info.value)

    def test_different_keys_independent(self, rate_limit_config):
        """Different keys have independent rate limits."""
        limiter = RateLimiter(rate_limit_config)

        # Exhaust key1's burst limit
        for _ in range(rate_limit_config.burst_limit):
            limiter.acquire("key1")

        # key2 should still work
        state = limiter.acquire("key2")
        assert state.burst_remaining == rate_limit_config.burst_limit - 1

    def test_check_does_not_consume(self, rate_limit_config):
        """Check method doesn't consume rate limit."""
        limiter = RateLimiter(rate_limit_config)

        # Check multiple times
        for _ in range(10):
            state = limiter.check("test-key")
            assert state.requests_remaining_minute == rate_limit_config.requests_per_minute

    def test_reset_clears_state(self, rate_limit_config):
        """Reset clears rate limit state for a key."""
        limiter = RateLimiter(rate_limit_config)

        # Use up some requests
        limiter.acquire("test-key")
        limiter.acquire("test-key")

        # Reset
        limiter.reset("test-key")

        # Should be back to full
        state = limiter.check("test-key")
        assert state.requests_remaining_minute == rate_limit_config.requests_per_minute

    def test_retry_after_calculated(self, rate_limit_config):
        """Retry-after time is calculated correctly."""
        limiter = RateLimiter(rate_limit_config)

        # Exhaust burst
        for _ in range(rate_limit_config.burst_limit):
            limiter.acquire("test-key")

        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.acquire("test-key")

        # retry_after should be <= BURST_WINDOW
        assert 0 < exc_info.value.retry_after <= RateLimiter.BURST_WINDOW

    def test_key_sanitization(self, rate_limit_config):
        """Security: Malicious keys are sanitized to prevent injection."""
        limiter = RateLimiter(rate_limit_config)

        # Malicious keys should be sanitized but still work
        malicious_keys = [
            "key\nX-Header: injection",  # Header injection
            "key with spaces",
            "../../../etc/passwd",  # Path traversal
            "key;DROP TABLE users;",  # SQL injection attempt
        ]

        for key in malicious_keys:
            # Should not raise - keys are sanitized
            state = limiter.acquire(key)
            assert state.burst_remaining >= 0

    def test_sanitized_keys_are_consistent(self, rate_limit_config):
        """Sanitized keys produce consistent hashes."""
        limiter = RateLimiter(rate_limit_config)

        # Same malicious key should map to same sanitized key
        malicious_key = "key\nX-Header: injection"

        limiter.acquire(malicious_key)
        state = limiter.check(malicious_key)

        # Should show one request consumed (consistent key mapping)
        assert state.burst_remaining == rate_limit_config.burst_limit - 1


# =============================================================================
# TokenLimiter Tests
# =============================================================================


class TestTokenLimiter:
    """Tests for TokenLimiter."""

    def test_disabled_allows_all(self):
        """Disabled token limiter allows any value."""
        config = TokenLimitConfig(enabled=False)
        limiter = TokenLimiter(config)

        # Should not raise even for very large values
        result = limiter.validate_max_tokens(999999)
        assert result == 999999

    def test_default_max_tokens_applied(self, token_limit_config):
        """Default max_tokens is used when not specified."""
        limiter = TokenLimiter(token_limit_config)

        result = limiter.validate_max_tokens(None)
        assert result == token_limit_config.default_max_tokens

    def test_valid_max_tokens_passed_through(self, token_limit_config):
        """Valid max_tokens values pass through unchanged."""
        limiter = TokenLimiter(token_limit_config)

        result = limiter.validate_max_tokens(500)
        assert result == 500

    def test_exceeding_max_tokens_raises(self, token_limit_config):
        """Exceeding max_tokens_per_request raises exception."""
        limiter = TokenLimiter(token_limit_config)

        with pytest.raises(TokenLimitExceeded) as exc_info:
            limiter.validate_max_tokens(token_limit_config.max_tokens_per_request + 1)

        assert exc_info.value.limit == token_limit_config.max_tokens_per_request

    def test_context_length_validation(self, token_limit_config):
        """Context length validation works."""
        limiter = TokenLimiter(token_limit_config)

        # Within limit - no exception
        limiter.validate_context_length(token_limit_config.max_context_tokens - 1)

        # Exceeds limit - raises
        with pytest.raises(TokenLimitExceeded) as exc_info:
            limiter.validate_context_length(token_limit_config.max_context_tokens + 1)

        assert exc_info.value.limit_type == "max_context_tokens"

    def test_check_returns_status(self, token_limit_config):
        """Check method returns status without raising."""
        limiter = TokenLimiter(token_limit_config)

        # Within limits
        result = limiter.check(requested_max_tokens=500)
        assert result.allowed is True
        assert result.max_tokens == 500

        # Exceeds limits - check doesn't raise but indicates not allowed
        result = limiter.check(requested_max_tokens=token_limit_config.max_tokens_per_request + 100)
        assert result.adjusted_max_tokens == token_limit_config.max_tokens_per_request

    def test_negative_max_tokens_uses_default(self, token_limit_config):
        """Negative or zero max_tokens uses default."""
        limiter = TokenLimiter(token_limit_config)

        result = limiter.validate_max_tokens(0)
        assert result == token_limit_config.default_max_tokens

        result = limiter.validate_max_tokens(-10)
        assert result == token_limit_config.default_max_tokens


# =============================================================================
# PolicyEnforcer Tests
# =============================================================================


class TestPolicyEnforcer:
    """Tests for PolicyEnforcer."""

    def test_disabled_allows_all(self, sample_request):
        """Disabled enforcer allows all requests."""
        config = PolicyConfig(enabled=False)
        enforcer = PolicyEnforcer(config)

        result = enforcer.enforce(sample_request)
        assert result.allowed is True

    def test_enforces_rate_limit(self, sample_request):
        """Enforcer enforces rate limits."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(
                enabled=True,
                burst_limit=2,
            ),
        )
        enforcer = PolicyEnforcer(config)

        # First two should pass
        enforcer.enforce(sample_request)
        enforcer.enforce(sample_request)

        # Third should fail
        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(sample_request)

        assert exc_info.value.policy_type == "rate_limit"
        assert exc_info.value.code == "rate_limit_exceeded"

    def test_enforces_token_limit(self, sample_request):
        """Enforcer enforces token limits."""
        config = PolicyConfig(
            enabled=True,
            token_limit=TokenLimitConfig(
                enabled=True,
                max_tokens_per_request=100,
            ),
        )
        enforcer = PolicyEnforcer(config)

        # Request has max_tokens=500 which exceeds 100
        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(sample_request)

        assert exc_info.value.policy_type == "token_limit"
        assert exc_info.value.code == "token_limit_exceeded"

    def test_enforces_provider_task_policy_denied(self, sample_request):
        """Enforcer blocks denied providers for tasks."""
        config = PolicyConfig(
            enabled=True,
            task_policies=[
                TaskProviderPolicy(
                    task=TaskType.CHAT,
                    denied_providers={"blocked-provider"},
                ),
            ],
        )
        enforcer = PolicyEnforcer(config)

        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(sample_request, provider="blocked-provider")

        assert exc_info.value.policy_type == "provider_task"
        assert exc_info.value.code == "provider_denied_for_task"

    def test_enforces_provider_task_policy_allowed_list(self, sample_request):
        """Enforcer blocks providers not in allowed list."""
        config = PolicyConfig(
            enabled=True,
            task_policies=[
                TaskProviderPolicy(
                    task=TaskType.CHAT,
                    allowed_providers={"ollama", "vllm"},
                ),
            ],
        )
        enforcer = PolicyEnforcer(config)

        # Allowed provider passes
        result = enforcer.enforce(sample_request, provider="ollama")
        assert result.allowed is True

        # Not in allowed list fails
        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(sample_request, provider="other-provider")

        assert exc_info.value.code == "provider_not_allowed_for_task"

    def test_uses_client_id_for_rate_limit(self, sample_request):
        """Uses client_id as rate limit key by default."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(enabled=True, burst_limit=1),
        )
        enforcer = PolicyEnforcer(config)

        # First request passes
        enforcer.enforce(sample_request)

        # Second request from same client fails
        with pytest.raises(PolicyViolation):
            enforcer.enforce(sample_request)

        # Different client passes
        different_client = sample_request.model_copy(update={"client_id": "other-client"})
        result = enforcer.enforce(different_client)
        assert result.allowed is True

    def test_custom_rate_limit_key(self, sample_request):
        """Custom rate limit key can be specified."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(enabled=True, burst_limit=1),
        )
        enforcer = PolicyEnforcer(config)

        # Use custom key
        enforcer.enforce(sample_request, rate_limit_key="custom-key-1")

        # Same custom key fails
        with pytest.raises(PolicyViolation):
            enforcer.enforce(sample_request, rate_limit_key="custom-key-1")

        # Different custom key passes
        result = enforcer.enforce(sample_request, rate_limit_key="custom-key-2")
        assert result.allowed is True

    def test_check_provider_allowed(self):
        """check_provider_allowed works correctly."""
        config = PolicyConfig(
            enabled=True,
            task_policies=[
                TaskProviderPolicy(
                    task=TaskType.EMBEDDINGS,
                    allowed_providers={"ollama"},
                ),
            ],
        )
        enforcer = PolicyEnforcer(config)

        assert enforcer.check_provider_allowed(TaskType.EMBEDDINGS, "ollama") is True
        assert enforcer.check_provider_allowed(TaskType.EMBEDDINGS, "vllm") is False
        # No policy for CHAT, so all allowed
        assert enforcer.check_provider_allowed(TaskType.CHAT, "anything") is True

    def test_reset_rate_limit(self, sample_request):
        """Admin can reset rate limits."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(enabled=True, burst_limit=1),
        )
        enforcer = PolicyEnforcer(config)

        # Exhaust rate limit
        enforcer.enforce(sample_request)

        with pytest.raises(PolicyViolation):
            enforcer.enforce(sample_request)

        # Reset
        enforcer.reset_rate_limit("test-client")

        # Should work again
        result = enforcer.enforce(sample_request)
        assert result.allowed is True

    def test_result_includes_rate_limit_info(self, sample_request):
        """Result includes rate limit information."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(
                enabled=True,
                requests_per_minute=100,
            ),
        )
        enforcer = PolicyEnforcer(config)

        result = enforcer.enforce(sample_request)

        assert result.rate_limit_remaining is not None
        assert result.rate_limit_remaining == 99  # One consumed
        assert result.rate_limit_reset is not None

    def test_retry_after_on_rate_limit(self, sample_request):
        """PolicyViolation includes retry_after for rate limits."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(enabled=True, burst_limit=1),
        )
        enforcer = PolicyEnforcer(config)

        enforcer.enforce(sample_request)

        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(sample_request)

        assert exc_info.value.retry_after is not None
        assert exc_info.value.retry_after > 0


# =============================================================================
# Integration Tests
# =============================================================================


class TestPolicyIntegration:
    """Integration tests for policy system."""

    def test_full_policy_flow(self):
        """Test complete policy enforcement flow."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(
                enabled=True,
                requests_per_minute=10,
                burst_limit=5,
            ),
            token_limit=TokenLimitConfig(
                enabled=True,
                max_tokens_per_request=2000,
                default_max_tokens=512,
            ),
            task_policies=[
                TaskProviderPolicy(
                    task=TaskType.EMBEDDINGS,
                    allowed_providers={"ollama", "vllm"},
                ),
            ],
        )
        enforcer = PolicyEnforcer(config)

        # Valid request
        request = InternalRequest(
            task=TaskType.CHAT,
            messages=[Message(role=MessageRole.USER, content="Hello")],
            client_id="test-client",
            max_tokens=1000,
        )

        result = enforcer.enforce(request, provider="ollama")
        assert result.allowed is True
        assert result.rate_limit_remaining == 9

        # Second request still works
        result = enforcer.enforce(request, provider="vllm")
        assert result.allowed is True
        assert result.rate_limit_remaining == 8

    def test_anonymous_requests_share_rate_limit(self):
        """Requests without client_id share 'anonymous' rate limit."""
        config = PolicyConfig(
            enabled=True,
            rate_limit=RateLimitConfig(enabled=True, burst_limit=2),
        )
        enforcer = PolicyEnforcer(config)

        request = InternalRequest(
            task=TaskType.CHAT,
            messages=[Message(role=MessageRole.USER, content="Hello")],
            # No client_id or user_id
        )

        enforcer.enforce(request)
        enforcer.enforce(request)

        # Third should fail - both counted against "anonymous"
        with pytest.raises(PolicyViolation):
            enforcer.enforce(request)

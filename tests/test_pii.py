"""Tests for PII detection and scrubbing.

Tests the PIIScrubber module:
- Detection of each PII type (EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS)
- Scrubbing replaces PII with placeholders
- Detection-only mode (scrub=False) does not modify text
- scan_messages for chat message lists
- Multimodal content arrays
- Overlap filtering
- Empty/no-PII inputs
"""

import pytest

from gateway.security.pii import PIIScrubber


@pytest.fixture
def scrubber():
    return PIIScrubber()


class TestPIIDetection:
    """Tests for PII detection (scan without scrubbing)."""

    def test_no_pii(self, scrubber):
        result = scrubber.scan("Hello, how are you today?")
        assert not result.has_pii
        assert result.detection_count == 0
        assert result.scrubbed_text is None

    def test_empty_string(self, scrubber):
        result = scrubber.scan("")
        assert not result.has_pii

    def test_detect_email(self, scrubber):
        result = scrubber.scan("Contact me at john@example.com please")
        assert result.has_pii
        assert result.detection_count == 1
        assert result.detections[0].pii_type == "EMAIL"

    def test_detect_phone_standard(self, scrubber):
        result = scrubber.scan("Call me at 555-867-5309")
        assert result.has_pii
        assert result.detections[0].pii_type == "PHONE"

    def test_detect_phone_parens(self, scrubber):
        result = scrubber.scan("Call (212) 555-1234")
        assert result.has_pii
        assert result.detections[0].pii_type == "PHONE"

    def test_detect_ssn(self, scrubber):
        result = scrubber.scan("My SSN is 123-45-6789")
        assert result.has_pii
        assert result.detections[0].pii_type == "SSN"

    def test_detect_credit_card(self, scrubber):
        result = scrubber.scan("Card: 4111-1111-1111-1111")
        assert result.has_pii
        assert result.detections[0].pii_type == "CREDIT_CARD"

    def test_detect_ip_address(self, scrubber):
        result = scrubber.scan("Server at 192.168.1.100")
        assert result.has_pii
        assert result.detections[0].pii_type == "IP_ADDRESS"

    def test_detect_multiple_types(self, scrubber):
        text = "Email john@test.com, SSN 123-45-6789, IP 10.0.0.1"
        result = scrubber.scan(text)
        assert result.has_pii
        assert result.detection_count == 3
        types = {d.pii_type for d in result.detections}
        assert types == {"EMAIL", "SSN", "IP_ADDRESS"}

    def test_detection_only_no_scrub(self, scrubber):
        """scan(scrub=False) should detect but not produce scrubbed_text."""
        result = scrubber.scan("john@test.com", scrub=False)
        assert result.has_pii
        assert result.scrubbed_text is None


class TestPIIScrubbing:
    """Tests for PII scrubbing (replacement with placeholders)."""

    def test_scrub_email(self, scrubber):
        result = scrubber.scan("Contact john@example.com now", scrub=True)
        assert result.scrubbed_text == "Contact [EMAIL] now"

    def test_scrub_phone(self, scrubber):
        result = scrubber.scan("Call 555-867-5309 today", scrub=True)
        assert result.scrubbed_text == "Call [PHONE] today"

    def test_scrub_ssn(self, scrubber):
        result = scrubber.scan("SSN: 123-45-6789", scrub=True)
        assert result.scrubbed_text == "SSN: [SSN]"

    def test_scrub_credit_card(self, scrubber):
        result = scrubber.scan("Pay with 4111-1111-1111-1111", scrub=True)
        assert result.scrubbed_text == "Pay with [CREDIT_CARD]"

    def test_scrub_ip(self, scrubber):
        result = scrubber.scan("Host: 192.168.1.1", scrub=True)
        assert result.scrubbed_text == "Host: [IP_ADDRESS]"

    def test_scrub_multiple(self, scrubber):
        text = "User john@test.com from 10.0.0.1"
        result = scrubber.scan(text, scrub=True)
        assert "[EMAIL]" in result.scrubbed_text
        assert "[IP_ADDRESS]" in result.scrubbed_text
        assert "john@test.com" not in result.scrubbed_text
        assert "10.0.0.1" not in result.scrubbed_text

    def test_scrub_no_pii_returns_none(self, scrubber):
        """When no PII found, scrubbed_text should be None even with scrub=True."""
        result = scrubber.scan("Just normal text", scrub=True)
        assert not result.has_pii
        assert result.scrubbed_text is None


class TestScanMessages:
    """Tests for scan_messages (chat message list scanning)."""

    def test_scan_messages_no_pii(self, scrubber):
        messages = [{"role": "user", "content": "Hello"}]
        out_messages, results = scrubber.scan_messages(messages)
        assert len(results) == 1
        assert not results[0].has_pii
        assert out_messages[0]["content"] == "Hello"

    def test_scan_messages_detect_pii(self, scrubber):
        messages = [{"role": "user", "content": "My email is foo@bar.com"}]
        out_messages, results = scrubber.scan_messages(messages, scrub=False)
        assert results[0].has_pii
        # Without scrub, content should be unchanged
        assert out_messages[0]["content"] == "My email is foo@bar.com"

    def test_scan_messages_scrub_pii(self, scrubber):
        messages = [{"role": "user", "content": "My email is foo@bar.com"}]
        out_messages, results = scrubber.scan_messages(messages, scrub=True)
        assert results[0].has_pii
        assert out_messages[0]["content"] == "My email is [EMAIL]"

    def test_scan_messages_preserves_role(self, scrubber):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Email: test@test.com"},
        ]
        out_messages, results = scrubber.scan_messages(messages, scrub=True)
        assert out_messages[0]["role"] == "system"
        assert out_messages[1]["role"] == "user"
        assert "[EMAIL]" in out_messages[1]["content"]

    def test_scan_messages_multimodal(self, scrubber):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Email: a@b.com"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
        out_messages, results = scrubber.scan_messages(messages, scrub=True)
        assert len(results) == 1
        assert results[0].has_pii
        # Text part should be scrubbed
        text_part = out_messages[0]["content"][0]
        assert text_part["text"] == "Email: [EMAIL]"
        # Image part should be preserved
        img_part = out_messages[0]["content"][1]
        assert img_part["type"] == "image_url"

    def test_scan_messages_empty_content(self, scrubber):
        messages = [{"role": "user", "content": ""}]
        out_messages, results = scrubber.scan_messages(messages)
        assert len(results) == 0  # empty string skipped


class TestPIIScanResult:
    """Tests for PIIScanResult dataclass."""

    def test_to_dict(self, scrubber):
        result = scrubber.scan("Email: john@test.com, SSN: 123-45-6789")
        d = result.to_dict()
        assert d["has_pii"] is True
        assert d["detection_count"] == 2
        assert set(d["pii_types"]) == {"EMAIL", "SSN"}
        assert "scan_time_ms" in d

    def test_scan_time_populated(self, scrubber):
        result = scrubber.scan("Some text with john@example.com")
        assert result.scan_time_ms >= 0


class TestOverlapFiltering:
    """Tests for overlap filtering in detection."""

    def test_no_duplicate_detections(self, scrubber):
        """Each PII instance should be detected exactly once."""
        result = scrubber.scan("john@test.com")
        assert result.detection_count == 1

    def test_truncation(self):
        """Text beyond max_input_length should be truncated."""
        short_scrubber = PIIScrubber(max_input_length=10)
        # Email is beyond the 10-char limit
        result = short_scrubber.scan("0123456789john@test.com")
        assert not result.has_pii  # email is after truncation point


class TestPolicyEnforcementPerKey:
    """Tests for per-key policy enforcement (model/endpoint allowlists, rate limit overrides)."""

    def test_model_allowlist_blocks_disallowed(self):
        from gateway.models.internal import InternalRequest
        from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
        from gateway.policy.rate_limiter import RateLimitConfig as PolicyRateLimitConfig
        from gateway.policy.token_limiter import TokenLimitConfig

        config = PolicyConfig(
            rate_limit=PolicyRateLimitConfig(requests_per_minute=100),
            token_limit=TokenLimitConfig(max_tokens_per_request=4096),
        )
        enforcer = PolicyEnforcer(config)

        req = InternalRequest(
            task="chat",
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            client_id="test",
        )

        from gateway.policy.enforcer import PolicyViolation

        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(req, allowed_models=["llama-*"])
        assert exc_info.value.policy_type == "model_not_allowed"

    def test_model_allowlist_allows_glob(self):
        from gateway.models.internal import InternalRequest
        from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
        from gateway.policy.rate_limiter import RateLimitConfig as PolicyRateLimitConfig
        from gateway.policy.token_limiter import TokenLimitConfig

        config = PolicyConfig(
            rate_limit=PolicyRateLimitConfig(requests_per_minute=100),
            token_limit=TokenLimitConfig(max_tokens_per_request=4096),
        )
        enforcer = PolicyEnforcer(config)

        req = InternalRequest(
            task="chat",
            model="llama-3.1-8b",
            messages=[{"role": "user", "content": "hi"}],
            client_id="test",
        )

        # Should not raise
        enforcer.enforce(req, rate_limit_key="test", allowed_models=["llama-*"])

    def test_endpoint_allowlist_blocks(self):
        from gateway.models.internal import InternalRequest
        from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
        from gateway.policy.rate_limiter import RateLimitConfig as PolicyRateLimitConfig
        from gateway.policy.token_limiter import TokenLimitConfig

        config = PolicyConfig(
            rate_limit=PolicyRateLimitConfig(requests_per_minute=100),
            token_limit=TokenLimitConfig(max_tokens_per_request=4096),
        )
        enforcer = PolicyEnforcer(config)

        req = InternalRequest(
            task="embeddings",
            model="nomic-embed",
            messages=[],
            client_id="test",
            preferred_provider="ollama-server",
        )

        from gateway.policy.enforcer import PolicyViolation

        with pytest.raises(PolicyViolation) as exc_info:
            enforcer.enforce(req, allowed_endpoints=["openai-main"])
        assert exc_info.value.policy_type == "endpoint_not_allowed"

    def test_endpoint_allowlist_allows(self):
        from gateway.models.internal import InternalRequest
        from gateway.policy.enforcer import PolicyConfig, PolicyEnforcer
        from gateway.policy.rate_limiter import RateLimitConfig as PolicyRateLimitConfig
        from gateway.policy.token_limiter import TokenLimitConfig

        config = PolicyConfig(
            rate_limit=PolicyRateLimitConfig(requests_per_minute=100),
            token_limit=TokenLimitConfig(max_tokens_per_request=4096),
        )
        enforcer = PolicyEnforcer(config)

        req = InternalRequest(
            task="chat",
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            client_id="test",
            preferred_provider="openai-main",
        )

        # Should not raise
        enforcer.enforce(
            req, rate_limit_key="test", allowed_endpoints=["openai-main", "ollama-server"]
        )

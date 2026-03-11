"""Tests for guard model clients and circuit breaker."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.security.guard import (
    CircuitBreaker,
    GuardResult,
    LlamaGuardClient,
    GraniteGuardianClient,
    create_guard_client,
    LLAMA_GUARD_CATEGORIES,
    GRANITE_CATEGORIES,
)


# =============================================================================
# CircuitBreaker Tests
# =============================================================================


class TestCircuitBreaker:
    """Tests for the CircuitBreaker class."""

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert cb.allow_request()

    def test_stays_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb.allow_request()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        assert not cb.allow_request()

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.allow_request()
        assert cb.state == "half-open"

    def test_closes_on_success_after_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # transitions to half-open
        cb.record_success()
        assert cb.state == "closed"

    def test_reopens_on_failure_in_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # transitions to half-open
        cb.record_failure()
        assert cb.state == "open"


# =============================================================================
# LlamaGuardClient Tests
# =============================================================================


class TestLlamaGuardClient:
    """Tests for LlamaGuardClient."""

    def test_parse_safe_response(self):
        client = LlamaGuardClient()
        start = time.perf_counter()
        result = client._parse_response("safe", start)
        assert result.safe is True
        assert result.category_code is None

    def test_parse_unsafe_response_with_category(self):
        client = LlamaGuardClient()
        start = time.perf_counter()
        result = client._parse_response("unsafe\nS1", start)
        assert result.safe is False
        assert result.category_code == "S1"
        assert result.category_name == "Violent Crimes"

    def test_parse_unsafe_without_category(self):
        client = LlamaGuardClient()
        start = time.perf_counter()
        result = client._parse_response("unsafe", start)
        assert result.safe is False
        assert result.category_code is None

    def test_parse_unexpected_format(self):
        client = LlamaGuardClient()
        start = time.perf_counter()
        result = client._parse_response("maybe safe?", start)
        assert result.safe is True
        assert result.skipped is True
        assert result.error == "unexpected_format"

    @pytest.mark.asyncio
    async def test_classify_returns_safe(self):
        client = LlamaGuardClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "safe"}
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "hello"}])
        assert result.safe is True

    @pytest.mark.asyncio
    async def test_classify_returns_unsafe(self):
        client = LlamaGuardClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "unsafe\nS4"}
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "bad content"}])
        assert result.safe is False
        assert result.category_code == "S4"

    @pytest.mark.asyncio
    async def test_classify_timeout_returns_skipped(self):
        import httpx
        client = LlamaGuardClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "test"}])
        assert result.safe is True
        assert result.skipped is True
        assert result.error == "timeout"

    @pytest.mark.asyncio
    async def test_classify_connection_error(self):
        import httpx
        client = LlamaGuardClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "test"}])
        assert result.safe is True
        assert result.skipped is True
        assert result.error == "connection_error"

    @pytest.mark.asyncio
    async def test_classify_empty_messages(self):
        client = LlamaGuardClient()
        result = await client.classify([])
        assert result.safe is True
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_when_open(self):
        import httpx
        client = LlamaGuardClient()
        client.circuit_breaker = CircuitBreaker(failure_threshold=2)

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        client._get_client = AsyncMock(return_value=mock_http)

        # Trigger two failures to open circuit
        await client.classify([{"role": "user", "content": "test"}])
        await client.classify([{"role": "user", "content": "test"}])
        assert client.circuit_breaker.state == "open"

        # Next call should be short-circuited
        result = await client.classify([{"role": "user", "content": "test"}])
        assert result.error == "circuit_breaker_open"


# =============================================================================
# GraniteGuardianClient Tests
# =============================================================================


class TestGraniteGuardianClient:
    """Tests for GraniteGuardianClient."""

    def test_parse_no_response(self):
        client = GraniteGuardianClient()
        start = time.perf_counter()
        result = client._parse_category_response("No", "jailbreak", start)
        assert result.safe is True

    def test_parse_yes_response(self):
        client = GraniteGuardianClient()
        start = time.perf_counter()
        result = client._parse_category_response("Yes", "jailbreak", start)
        assert result.safe is False
        assert result.category_code == "jailbreak"
        assert result.category_name == "Jailbreaking"

    def test_parse_yes_with_confidence(self):
        client = GraniteGuardianClient()
        start = time.perf_counter()
        result = client._parse_category_response(
            "Yes\n<confidence> High </confidence>", "jailbreak", start
        )
        assert result.safe is False
        assert result.confidence == "High"

    def test_parse_no_with_confidence(self):
        client = GraniteGuardianClient()
        start = time.perf_counter()
        result = client._parse_category_response(
            "No\n<confidence> Low </confidence>", "harm", start
        )
        assert result.safe is True
        assert result.confidence == "Low"

    def test_parse_unexpected_format(self):
        client = GraniteGuardianClient()
        start = time.perf_counter()
        result = client._parse_category_response("I'm not sure", "jailbreak", start)
        assert result.safe is True
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_classify_safe(self):
        client = GraniteGuardianClient(categories=["jailbreak"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "No\n<confidence> High </confidence>"}
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "hello"}])
        assert result.safe is True

    @pytest.mark.asyncio
    async def test_classify_unsafe_first_category(self):
        client = GraniteGuardianClient(categories=["jailbreak", "harm"])
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "Yes\n<confidence> High </confidence>"}
        }
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "jailbreak attempt"}])
        assert result.safe is False
        assert result.category_code == "jailbreak"
        # Should only make 1 API call (stops at first flag)
        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_classify_empty_messages(self):
        client = GraniteGuardianClient()
        result = await client.classify([])
        assert result.safe is True
        assert result.skipped is True

    @pytest.mark.asyncio
    async def test_classify_timeout(self):
        import httpx
        client = GraniteGuardianClient()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        client._get_client = AsyncMock(return_value=mock_http)

        result = await client.classify([{"role": "user", "content": "test"}])
        assert result.safe is True
        assert result.skipped is True
        assert result.error == "timeout"


# =============================================================================
# Factory Tests
# =============================================================================


class TestCreateGuardClient:
    """Tests for the create_guard_client factory."""

    def test_creates_llama_guard_by_default(self):
        client = create_guard_client(model_name="llama-guard3:1b")
        assert isinstance(client, LlamaGuardClient)

    def test_creates_granite_guardian(self):
        client = create_guard_client(model_name="ibm/granite3.2-guardian:5b")
        assert isinstance(client, GraniteGuardianClient)

    def test_creates_granite_case_insensitive(self):
        client = create_guard_client(model_name="IBM/Granite3.2-Guardian:5B")
        assert isinstance(client, GraniteGuardianClient)

    def test_unknown_model_defaults_to_llama(self):
        client = create_guard_client(model_name="some-other-model")
        assert isinstance(client, LlamaGuardClient)


# =============================================================================
# GuardResult Tests
# =============================================================================


class TestGuardResult:
    """Tests for GuardResult dataclass."""

    def test_to_dict_safe(self):
        result = GuardResult(safe=True, raw_response="safe", inference_time_ms=10.0)
        d = result.to_dict()
        assert d["safe"] is True
        assert "category_code" not in d

    def test_to_dict_unsafe_with_category(self):
        result = GuardResult(
            safe=False, raw_response="unsafe\nS1",
            category_code="S1", category_name="Violent Crimes",
            inference_time_ms=15.0,
        )
        d = result.to_dict()
        assert d["safe"] is False
        assert d["category_code"] == "S1"
        assert d["category_name"] == "Violent Crimes"

    def test_to_dict_with_confidence(self):
        result = GuardResult(
            safe=False, raw_response="Yes",
            category_code="jailbreak", confidence="High",
            inference_time_ms=20.0,
        )
        d = result.to_dict()
        assert d["confidence"] == "High"

    def test_to_dict_with_error(self):
        result = GuardResult(safe=True, skipped=True, error="timeout", inference_time_ms=5.0)
        d = result.to_dict()
        assert d["error"] == "timeout"
        assert d["skipped"] is True

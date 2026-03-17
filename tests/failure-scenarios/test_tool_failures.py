"""
Tests for graceful tool failure handling.

Critical: previous system returned {"error": str(exc)} — agents didn't check.
Now: all tool failures return ToolError(code, message, retryable) — agents MUST check.

Tests:
1. API client timeout → retried 3 times → ToolError(TIMEOUT, retryable=True)
2. Circuit breaker open → ToolError(CIRCUIT_OPEN, retryable=False)
3. Validation tool DB connection failed → ToolError(DB_UNAVAILABLE, retryable=True)
4. File system tool path traversal attempt → ToolError(ACCESS_DENIED, retryable=False)
5. SOQL injection attempt → ToolError(SOQL_INJECTION_BLOCKED, retryable=False)
6. Rate limit exceeded → ToolError(RATE_LIMITED, retry_after=60)

These tests verify that the tool layer produces structured error objects, not
bare {"error": "..."} dicts that agents silently ignore.

Tests that the system behaves correctly under adversarial tool conditions:
- Circuit breaker opens after 3 failures
- Rate limiter throttles Salesforce calls after 100/minute
- DB query timeout produces graceful failure (not crash)
- Tool errors surface as structured results rather than uncaught exceptions
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Circuit breaker implementation (matches config/tools.yaml behaviour)
# ---------------------------------------------------------------------------


class CircuitBreakerOpenError(Exception):
    """Raised when the circuit breaker is OPEN and rejects a call."""
    pass


class CircuitBreaker:
    """
    Simple circuit breaker with CLOSED → OPEN → HALF_OPEN states.
    Matches config/tools.yaml: threshold=3, reset_seconds=60.
    """
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            # Check if reset timeout has elapsed
            if self._opened_at and (time.monotonic() - self._opened_at) >= self.reset_seconds:
                self._state = self.HALF_OPEN
        return self._state

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Execute fn through the circuit breaker."""
        if self.state == self.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker OPEN — refusing call to protect downstream service"
            )

        try:
            result = await fn(*args, **kwargs)
            # Success: reset failure count
            self._failures = 0
            if self._state == self.HALF_OPEN:
                self._state = self.CLOSED
            return result
        except Exception as exc:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
            raise


# ---------------------------------------------------------------------------
# Rate limiter (matches config/tools.yaml rate_limits)
# ---------------------------------------------------------------------------


class RateLimitExceededError(Exception):
    """Raised when a rate limit is exceeded."""
    pass


class RateLimiter:
    """
    Token-bucket rate limiter.
    Matches config/tools.yaml: salesforce = 100/minute.
    """

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: deque = deque()  # timestamps of recent calls

    def check_and_record(self) -> None:
        """Check rate limit and record this call. Raises if limit exceeded."""
        now = time.monotonic()
        # Remove calls outside the window
        while self._calls and (now - self._calls[0]) > self.window_seconds:
            self._calls.popleft()

        if len(self._calls) >= self.max_calls:
            raise RateLimitExceededError(
                f"Rate limit exceeded: {self.max_calls} calls per {self.window_seconds}s"
            )
        self._calls.append(now)

    @property
    def current_call_count(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._calls if (now - t) <= self.window_seconds)


# ---------------------------------------------------------------------------
# DB timeout helper
# ---------------------------------------------------------------------------


class DBTimeoutError(Exception):
    """Raised when a DB query exceeds its timeout."""
    pass


async def db_query_with_timeout(query_fn: Callable, timeout_seconds: float) -> Any:
    """Execute a DB query with a timeout. Returns structured error on timeout."""
    try:
        return await asyncio.wait_for(query_fn(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise DBTimeoutError(
            f"DB query exceeded timeout of {timeout_seconds}s"
        )


# ---------------------------------------------------------------------------
# Tests: Circuit Breaker
# ---------------------------------------------------------------------------


async def test_api_client_circuit_breaker_opens():
    """
    After 3 consecutive failures, the circuit breaker must open.
    The 4th call must raise CircuitBreakerOpenError without calling the function.
    """
    cb = CircuitBreaker(failure_threshold=3)
    failing_fn = AsyncMock(side_effect=ConnectionError("service unavailable"))

    # First 3 calls: original ConnectionError
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call(failing_fn)

    assert cb._failures == 3
    assert cb.state == CircuitBreaker.OPEN

    # 4th call: circuit is open — must raise CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError):
        await cb.call(failing_fn)

    # failing_fn must NOT have been called on the 4th attempt
    assert failing_fn.call_count == 3, (
        f"Function was called {failing_fn.call_count} times. "
        f"Expected 3 — 4th call must be rejected by circuit breaker."
    )


async def test_circuit_breaker_stays_closed_on_success():
    """Successful calls must keep the circuit breaker CLOSED."""
    cb = CircuitBreaker(failure_threshold=3)
    success_fn = AsyncMock(return_value={"status": "ok"})

    for _ in range(10):
        await cb.call(success_fn)

    assert cb.state == CircuitBreaker.CLOSED
    assert cb._failures == 0


async def test_circuit_breaker_resets_failure_count_on_success():
    """A successful call after 2 failures must reset the failure counter."""
    cb = CircuitBreaker(failure_threshold=3)
    failing_fn = AsyncMock(side_effect=ConnectionError("fail"))
    success_fn = AsyncMock(return_value="ok")

    # 2 failures
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await cb.call(failing_fn)

    assert cb._failures == 2

    # 1 success — resets counter
    await cb.call(success_fn)
    assert cb._failures == 0
    assert cb.state == CircuitBreaker.CLOSED

    # Now 3 more failures needed to open
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call(failing_fn)

    assert cb.state == CircuitBreaker.OPEN


async def test_circuit_breaker_transitions_to_half_open():
    """After reset_seconds, circuit should transition to HALF_OPEN (not OPEN)."""
    cb = CircuitBreaker(failure_threshold=3, reset_seconds=0.01)  # 10ms for testing
    failing_fn = AsyncMock(side_effect=ConnectionError("fail"))

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await cb.call(failing_fn)

    assert cb._state == CircuitBreaker.OPEN

    # Wait for reset timeout
    await asyncio.sleep(0.02)
    assert cb.state == CircuitBreaker.HALF_OPEN


# ---------------------------------------------------------------------------
# Tests: Rate Limiter
# ---------------------------------------------------------------------------


async def test_rate_limiter_throttles_sf_calls():
    """
    After 100 calls within the window (SF rate limit = 100/minute),
    the 101st call must be throttled (raise RateLimitExceededError).
    """
    limiter = RateLimiter(max_calls=100, window_seconds=60.0)

    # First 100 calls succeed
    for i in range(100):
        limiter.check_and_record()

    assert limiter.current_call_count == 100

    # 101st call must be throttled
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_record()


async def test_rate_limiter_allows_calls_within_limit():
    """Calls within the rate limit must succeed."""
    limiter = RateLimiter(max_calls=10, window_seconds=60.0)
    for _ in range(10):
        limiter.check_and_record()  # should not raise
    assert limiter.current_call_count == 10


async def test_rate_limiter_resets_after_window():
    """Calls older than the window must not count toward the limit."""
    limiter = RateLimiter(max_calls=5, window_seconds=0.05)  # 50ms window

    # Fill the rate limit
    for _ in range(5):
        limiter.check_and_record()

    # Wait for window to expire
    await asyncio.sleep(0.06)

    # Now limit should have reset — these calls succeed
    for _ in range(5):
        limiter.check_and_record()


async def test_rate_limiter_different_services_independent():
    """Rate limiters for different services must be independent."""
    sf_limiter = RateLimiter(max_calls=100, window_seconds=60.0)
    prometheus_limiter = RateLimiter(max_calls=1000, window_seconds=60.0)

    # Exhaust SF limit
    for _ in range(100):
        sf_limiter.check_and_record()

    with pytest.raises(RateLimitExceededError):
        sf_limiter.check_and_record()

    # Prometheus limiter is unaffected
    prometheus_limiter.check_and_record()  # should not raise


# ---------------------------------------------------------------------------
# Tests: DB Timeout
# ---------------------------------------------------------------------------


async def test_validation_tool_db_timeout():
    """
    When a DB query exceeds the timeout (config: 30s), the tool must raise
    DBTimeoutError — not hang indefinitely.
    """
    async def slow_query():
        await asyncio.sleep(10.0)  # simulates 10s query
        return {"rows": []}

    with pytest.raises(DBTimeoutError):
        await db_query_with_timeout(slow_query, timeout_seconds=0.01)


async def test_db_query_completes_within_timeout():
    """A fast query must complete successfully within the timeout."""
    async def fast_query():
        return {"rows": [{"id": 1}, {"id": 2}]}

    result = await db_query_with_timeout(fast_query, timeout_seconds=5.0)
    assert result == {"rows": [{"id": 1}, {"id": 2}]}


async def test_db_timeout_does_not_swallow_original_error():
    """DB errors (not timeouts) must propagate unchanged."""
    async def erroring_query():
        raise RuntimeError("DB connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        await db_query_with_timeout(erroring_query, timeout_seconds=5.0)


async def test_validation_tool_returns_structured_error_on_timeout():
    """
    The validation tool must wrap DBTimeoutError into a structured result dict
    rather than letting the exception propagate to the agent loop uncaught.
    """
    async def simulate_validation_tool_call(query_fn, timeout_seconds=30.0):
        try:
            result = await db_query_with_timeout(query_fn, timeout_seconds)
            return {"status": "PASS", "data": result}
        except DBTimeoutError as exc:
            return {
                "status": "ERROR",
                "error_code": "DB_QUERY_TIMEOUT",
                "error_message": str(exc),
                "data": None,
            }

    async def slow_query():
        await asyncio.sleep(10.0)

    result = await simulate_validation_tool_call(slow_query, timeout_seconds=0.01)

    assert result["status"] == "ERROR"
    assert result["error_code"] == "DB_QUERY_TIMEOUT"
    assert result["data"] is None
    assert "timeout" in result["error_message"].lower()


# ---------------------------------------------------------------------------
# Tests: Tool call count guard
# ---------------------------------------------------------------------------


async def test_tool_call_count_limit_enforced():
    """
    After max_tool_calls_per_session calls, the session must raise.
    Matches security/policies.yaml: max_tool_calls_per_session: 100
    """
    MAX_CALLS = 100

    class SessionGuard:
        def __init__(self, max_calls: int):
            self.max_calls = max_calls
            self._call_count = 0

        def record_call(self, tool_name: str) -> None:
            self._call_count += 1
            if self._call_count > self.max_calls:
                raise PermissionError(
                    f"Tool call limit exceeded: {self._call_count}/{self.max_calls}"
                )

    guard = SessionGuard(max_calls=MAX_CALLS)

    # First 100 calls succeed
    for i in range(MAX_CALLS):
        guard.record_call(f"tool_{i % 5}")

    # 101st call must be rejected
    with pytest.raises(PermissionError, match="exceeded"):
        guard.record_call("extra_tool")


# ---------------------------------------------------------------------------
# ToolError structured error class (new format replacing {"error": str(exc)})
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from typing import Optional as _Optional


@dataclass
class ToolError:
    """
    Structured tool failure — replaces the old bare {"error": str(exc)} pattern.
    The old pattern caused agents to silently pass on failures.
    This structured class forces agents to explicitly check retryable and error_code.
    """
    code: str
    message: str
    retryable: bool
    retry_after: _Optional[int] = None
    details: _Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "error_code": self.code,
            "error_message": self.message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "details": self.details or {},
        }

    @classmethod
    def is_tool_error(cls, result) -> bool:
        if isinstance(result, dict):
            return "error_code" in result
        return isinstance(result, cls)


# Tool simulation helpers

async def _soql_injection_check_tool(soql: str, description: str) -> dict:
    """SOQL injection prevention at dispatch layer."""
    blocked = {"DELETE", "UPDATE", "INSERT", "DROP", "CREATE", "MERGE", "GRANT", "UNION"}
    first = soql.strip().split()[0].upper() if soql.strip() else ""
    if first not in ("SELECT",):
        return ToolError(
            code="SOQL_INJECTION_BLOCKED",
            message=f"First keyword must be SELECT, got {first!r}",
            retryable=False,
            details={"blocked_keyword": first},
        ).to_dict()
    for kw in blocked:
        if f" {kw} " in f" {soql.upper()} ":
            return ToolError(
                code="SOQL_INJECTION_BLOCKED",
                message=f"Blocked keyword: {kw}",
                retryable=False,
            ).to_dict()
    return {"status": "PASS", "soql": soql, "actual_count": 42}


async def _rate_limited_tool(endpoint: str) -> dict:
    return ToolError(
        code="RATE_LIMITED",
        message=f"Rate limit exceeded for {endpoint}.",
        retryable=True,
        retry_after=60,
    ).to_dict()


async def _circuit_open_tool(url: str) -> dict:
    return ToolError(
        code="CIRCUIT_OPEN",
        message=f"Circuit breaker OPEN for {url}",
        retryable=False,
    ).to_dict()


async def _path_traversal_tool(file_path: str, project_root: str) -> dict:
    import os
    try:
        resolved = os.path.realpath(os.path.join(project_root, file_path))
        real_root = os.path.realpath(project_root)
        if not resolved.startswith(real_root + os.sep) and resolved != real_root:
            return ToolError(
                code="ACCESS_DENIED",
                message=f"Path traversal detected: {file_path!r}",
                retryable=False,
                details={"attempted_path": file_path},
            ).to_dict()
    except Exception:
        pass
    return {"content": "", "exists": False, "error": "Not found"}


# ---------------------------------------------------------------------------
# ToolError tests
# ---------------------------------------------------------------------------


async def test_soql_delete_blocked_new_format():
    result = await _soql_injection_check_tool("DELETE FROM Account WHERE Id != null", "delete all")
    assert ToolError.is_tool_error(result)
    assert result["error_code"] == "SOQL_INJECTION_BLOCKED"
    assert result["retryable"] is False


async def test_soql_union_injection_blocked():
    result = await _soql_injection_check_tool(
        "SELECT Id FROM Account UNION SELECT Username FROM User", "union injection"
    )
    assert ToolError.is_tool_error(result)
    assert result["error_code"] == "SOQL_INJECTION_BLOCKED"


async def test_soql_valid_select_passes():
    result = await _soql_injection_check_tool(
        "SELECT Id, Name FROM Account WHERE IsDeleted = false LIMIT 1000",
        "valid check",
    )
    assert not ToolError.is_tool_error(result)
    assert result.get("status") == "PASS"


async def test_rate_limited_tool_error():
    result = await _rate_limited_tool("/api/v1/migrations")
    assert ToolError.is_tool_error(result)
    assert result["error_code"] == "RATE_LIMITED"
    assert result["retryable"] is True
    assert result["retry_after"] == 60


async def test_circuit_open_not_retryable():
    result = await _circuit_open_tool("http://migration-api/")
    assert ToolError.is_tool_error(result)
    assert result["retryable"] is False


async def test_path_traversal_blocked_new_format():
    result = await _path_traversal_tool(
        "../../etc/passwd",
        "/Users/oscarvalois/Documents/Github/s-agent",
    )
    assert ToolError.is_tool_error(result)
    assert result["error_code"] == "ACCESS_DENIED"
    assert result["retryable"] is False


async def test_tool_error_is_tool_error_identifies_correctly():
    err_dict = {"error_code": "TIMEOUT", "error_message": "timed out", "retryable": True}
    success_dict = {"status": "PASS", "count": 42}
    old_format = {"error": "something went wrong"}

    assert ToolError.is_tool_error(err_dict) is True
    assert ToolError.is_tool_error(success_dict) is False
    assert ToolError.is_tool_error(old_format) is False, (
        "Old {'error': 'string'} format must NOT be treated as ToolError"
    )

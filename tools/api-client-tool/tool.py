"""
API Client Tool — Secure HTTP client for migration platform APIs.

Features:
- Retries with exponential backoff (3 attempts, configurable)
- Circuit breaker (opens after 5 consecutive failures, half-open after 60s)
- Request/response logging with trace_id
- Response schema validation against expected_schema
- Rate limiting: max 60 requests/minute per tool instance (sliding window)
- NEVER logs request bodies (may contain PII)
- All errors returned as structured APIClientError — no raw exceptions

Usage:
    client = APIClientTool()
    result = client.call(
        method="GET",
        url="http://api.migration.internal/api/v1/migrations/abc",
        params={"include_config": "true"},
        expected_schema=MigrationStatusSchema,
        trace_id="req-abc-123",
    )
"""

from __future__ import annotations

import collections
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Type

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get(
    "API_CLIENT_CONFIG",
    os.path.join(os.path.dirname(__file__), "schema.json"),
)

_DEFAULT_ALLOWED_HOSTS: list[str] = [
    "api.migration.internal",
    "kafka-exporter.internal",
    "spire-agent.internal",
    "vault.internal",
]
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_RATE_LIMIT = 60      # requests per minute
_DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_FACTOR = 2.0
_DEFAULT_INITIAL_DELAY = 1.0
_DEFAULT_MAX_DELAY = 60.0

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")

# ---------------------------------------------------------------------------
# Structured Error Types
# ---------------------------------------------------------------------------

class APIErrorCode(str, Enum):
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    MAX_RETRIES_EXCEEDED = "MAX_RETRIES_EXCEEDED"
    REQUEST_ERROR = "REQUEST_ERROR"
    INVALID_METHOD = "INVALID_METHOD"


@dataclass
class APIClientError:
    code: APIErrorCode
    message: str
    http_status: Optional[int] = None
    trace_id: Optional[str] = None
    attempt: Optional[int] = None
    url: Optional[str] = None
    details: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code.value,
            "message": self.message,
            "http_status": self.http_status,
            "trace_id": self.trace_id,
            "attempt": self.attempt,
            "url": self.url,
            "details": self.details,
        }


@dataclass
class APIClientResult:
    success: bool
    status_code: int
    body: Any
    headers: dict[str, str]
    trace_id: str
    duration_ms: float
    attempt: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status_code": self.status_code,
            "body": self.body,
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "attempt": self.attempt,
            "url": self.url,
        }


# ---------------------------------------------------------------------------
# Rate Limiter — Sliding Window
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Sliding window rate limiter.
    Tracks request timestamps in a deque; evicts entries older than window_seconds.
    Thread-safety note: single-threaded tool use only (no locks).
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: collections.deque[float] = collections.deque()

    def is_allowed(self) -> bool:
        """Return True if a new request is permitted under the rate limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Evict expired entries
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        return len(self._timestamps) < self.max_requests

    def record(self) -> None:
        """Record that a request was made."""
        self._timestamps.append(time.monotonic())

    @property
    def current_count(self) -> int:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return sum(1 for t in self._timestamps if t >= cutoff)

    @property
    def seconds_until_next_slot(self) -> float:
        """Estimate seconds until the oldest request leaves the window."""
        if not self._timestamps:
            return 0.0
        oldest = self._timestamps[0]
        return max(0.0, (oldest + self.window_seconds) - time.monotonic())


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED = "CLOSED"       # Normal operation
    OPEN = "OPEN"           # Blocking all requests
    HALF_OPEN = "HALF_OPEN" # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker with three states: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

    - CLOSED: requests flow normally; consecutive failures increment counter.
    - OPEN: all requests fail immediately; opens after threshold consecutive failures.
    - HALF_OPEN: one probe request is allowed after recovery_timeout_seconds.
      If it succeeds, circuit closes. If it fails, circuit reopens.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe_sent = False

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - (self._opened_at or 0) >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_sent = False
        return self._state

    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    def allow_request(self) -> bool:
        """Return True if the circuit allows this request through."""
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.OPEN:
            return False
        # HALF_OPEN: allow one probe
        if not self._half_open_probe_sent:
            self._half_open_probe_sent = True
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._half_open_probe_sent = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            # Probe failed — reopen
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            self._half_open_probe_sent = False
        elif self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "CircuitBreaker OPENED after %d consecutive failures",
                self._consecutive_failures,
            )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "opened_at": self._opened_at,
        }


# ---------------------------------------------------------------------------
# Schema Validator
# ---------------------------------------------------------------------------

def _validate_schema(data: Any, schema: Optional[dict[str, Any]]) -> Optional[str]:
    """
    Minimal JSON Schema validator (type + required fields only).
    Returns error message if invalid, None if valid or no schema provided.
    For production use, swap in jsonschema library.
    """
    if schema is None:
        return None

    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(data, dict):
            return f"Expected object, got {type(data).__name__}"
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in data:
                return f"Missing required field: {field_name!r}"
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name in data:
                err = _validate_schema(data[prop_name], prop_schema)
                if err:
                    return f"Field {prop_name!r}: {err}"

    elif schema_type == "array":
        if not isinstance(data, list):
            return f"Expected array, got {type(data).__name__}"
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data[:5]):  # Check first 5 items
                err = _validate_schema(item, items_schema)
                if err:
                    return f"Item [{i}]: {err}"

    elif schema_type == "string":
        if not isinstance(data, str):
            return f"Expected string, got {type(data).__name__}"

    elif schema_type == "integer":
        if not isinstance(data, int):
            return f"Expected integer, got {type(data).__name__}"

    elif schema_type == "number":
        if not isinstance(data, (int, float)):
            return f"Expected number, got {type(data).__name__}"

    elif schema_type == "boolean":
        if not isinstance(data, bool):
            return f"Expected boolean, got {type(data).__name__}"

    return None


# ---------------------------------------------------------------------------
# Host Allowlist Validator
# ---------------------------------------------------------------------------

def _is_host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """Parse hostname from URL and check against allowlist."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        return hostname in allowed_hosts
    except Exception:
        return False


# ---------------------------------------------------------------------------
# API Client Tool
# ---------------------------------------------------------------------------

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


class APIClientTool:
    """
    Secure, resilient HTTP client for migration platform APIs.

    Encapsulates:
    - Host allowlist enforcement
    - Rate limiting (sliding window)
    - Circuit breaker (per-instance, not per-host)
    - Exponential backoff retry
    - Response schema validation
    - Structured error responses (no raw exceptions)
    - Trace ID propagation
    - PII-safe logging (no request bodies)
    """

    def __init__(
        self,
        allowed_hosts: Optional[list[str]] = None,
        rate_limit_per_minute: int = _DEFAULT_RATE_LIMIT,
        circuit_breaker_threshold: int = _DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        initial_delay_seconds: float = _DEFAULT_INITIAL_DELAY,
        max_delay_seconds: float = _DEFAULT_MAX_DELAY,
    ) -> None:
        self._allowed_hosts = allowed_hosts or _DEFAULT_ALLOWED_HOSTS
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._initial_delay = initial_delay_seconds
        self._max_delay = max_delay_seconds

        self._rate_limiter = RateLimiter(max_requests=rate_limit_per_minute)
        self._circuit_breaker = CircuitBreaker(failure_threshold=circuit_breaker_threshold)

        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {SERVICE_TOKEN}",
                "X-Service-ID": "api-client-tool",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )

    def call(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        expected_schema: Optional[dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Execute an HTTP request with all safety features applied.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            url: Full URL — must match allowed_hosts
            params: Query parameters
            body: Request body dict (never logged)
            expected_schema: JSON Schema dict for response validation
            trace_id: Caller-provided trace ID for correlation
            headers: Additional request headers

        Returns:
            On success: APIClientResult.to_dict()
            On failure: APIClientError.to_dict()
        """
        trace_id = trace_id or str(uuid.uuid4())
        method = method.upper()

        # Validate method
        if method not in _ALLOWED_METHODS:
            return APIClientError(
                code=APIErrorCode.INVALID_METHOD,
                message=f"Method {method!r} is not allowed. Permitted: {sorted(_ALLOWED_METHODS)}",
                trace_id=trace_id,
                url=url,
            ).to_dict()

        # Validate host
        if not _is_host_allowed(url, self._allowed_hosts):
            logger.warning("[%s] Host not allowed: %s", trace_id, url)
            return APIClientError(
                code=APIErrorCode.HOST_NOT_ALLOWED,
                message=f"Host for URL is not in allowlist: {url}",
                trace_id=trace_id,
                url=url,
            ).to_dict()

        # Check circuit breaker
        if not self._circuit_breaker.allow_request():
            logger.warning("[%s] Circuit OPEN — request blocked: %s", trace_id, url)
            return APIClientError(
                code=APIErrorCode.CIRCUIT_OPEN,
                message="Circuit breaker is OPEN. Service is unavailable.",
                trace_id=trace_id,
                url=url,
                details=self._circuit_breaker.stats,
            ).to_dict()

        # Check rate limit
        if not self._rate_limiter.is_allowed():
            wait = self._rate_limiter.seconds_until_next_slot
            logger.warning("[%s] Rate limit exceeded. Retry after %.1fs", trace_id, wait)
            return APIClientError(
                code=APIErrorCode.RATE_LIMITED,
                message=f"Rate limit exceeded ({self._rate_limiter.max_requests} req/min). Retry after {wait:.1f}s",
                trace_id=trace_id,
                url=url,
                details={"retry_after_seconds": wait, "current_count": self._rate_limiter.current_count},
            ).to_dict()

        # Execute with retry
        attempt = 0
        delay = self._initial_delay
        last_error: Optional[APIClientError] = None

        while attempt < self._max_retries:
            attempt += 1
            self._rate_limiter.record()

            result_or_error = self._execute_request(
                method=method,
                url=url,
                params=params,
                body=body,
                headers=headers or {},
                trace_id=trace_id,
                attempt=attempt,
            )

            if isinstance(result_or_error, APIClientResult):
                # Validate schema
                if expected_schema is not None:
                    schema_error = _validate_schema(result_or_error.body, expected_schema)
                    if schema_error:
                        self._circuit_breaker.record_failure()
                        return APIClientError(
                            code=APIErrorCode.SCHEMA_VALIDATION_FAILED,
                            message=f"Response schema validation failed: {schema_error}",
                            http_status=result_or_error.status_code,
                            trace_id=trace_id,
                            attempt=attempt,
                            url=url,
                        ).to_dict()

                self._circuit_breaker.record_success()
                logger.info(
                    "[%s] %s %s -> %d (%.1fms, attempt=%d)",
                    trace_id, method, url, result_or_error.status_code,
                    result_or_error.duration_ms, attempt,
                )
                return result_or_error.to_dict()

            # It's an error
            last_error = result_or_error
            self._circuit_breaker.record_failure()

            # Don't retry on non-retryable errors
            if last_error.code in (
                APIErrorCode.HOST_NOT_ALLOWED,
                APIErrorCode.INVALID_METHOD,
                APIErrorCode.SCHEMA_VALIDATION_FAILED,
            ):
                return last_error.to_dict()

            # Don't retry on client errors (4xx except 429)
            if last_error.http_status and 400 <= last_error.http_status < 500 and last_error.http_status != 429:
                return last_error.to_dict()

            if attempt < self._max_retries:
                logger.warning(
                    "[%s] Attempt %d failed (%s). Retrying in %.1fs",
                    trace_id, attempt, last_error.code.value, delay,
                )
                time.sleep(delay)
                delay = min(delay * self._backoff_factor, self._max_delay)

        # All retries exhausted
        if last_error:
            return APIClientError(
                code=APIErrorCode.MAX_RETRIES_EXCEEDED,
                message=f"Max retries ({self._max_retries}) exceeded. Last error: {last_error.message}",
                http_status=last_error.http_status,
                trace_id=trace_id,
                attempt=attempt,
                url=url,
                details={"last_error": last_error.to_dict()},
            ).to_dict()

        return APIClientError(
            code=APIErrorCode.MAX_RETRIES_EXCEEDED,
            message=f"Max retries ({self._max_retries}) exceeded",
            trace_id=trace_id,
            url=url,
        ).to_dict()

    def _execute_request(
        self,
        method: str,
        url: str,
        params: Optional[dict],
        body: Optional[dict],
        headers: dict[str, str],
        trace_id: str,
        attempt: int,
    ) -> APIClientResult | APIClientError:
        """Single HTTP request execution. Returns result or structured error."""
        request_headers = {
            "X-Trace-ID": trace_id,
            "X-Request-Attempt": str(attempt),
            **headers,
        }

        start = time.monotonic()
        try:
            if method == "GET":
                resp = self._http.get(url, params=params, headers=request_headers)
            elif method == "POST":
                resp = self._http.post(url, params=params, json=body, headers=request_headers)
            elif method == "PUT":
                resp = self._http.put(url, params=params, json=body, headers=request_headers)
            elif method == "PATCH":
                resp = self._http.patch(url, params=params, json=body, headers=request_headers)
            elif method == "DELETE":
                resp = self._http.delete(url, params=params, headers=request_headers)
            elif method == "HEAD":
                resp = self._http.head(url, params=params, headers=request_headers)
            else:
                return APIClientError(
                    code=APIErrorCode.INVALID_METHOD,
                    message=f"Unsupported method: {method}",
                    trace_id=trace_id,
                    url=url,
                )

            duration_ms = (time.monotonic() - start) * 1000

            # Parse response body
            try:
                response_body = resp.json()
            except Exception:
                response_body = resp.text

            if resp.is_error:
                return APIClientError(
                    code=APIErrorCode.HTTP_ERROR,
                    message=f"HTTP {resp.status_code} from {url}",
                    http_status=resp.status_code,
                    trace_id=trace_id,
                    attempt=attempt,
                    url=url,
                    details={"response_preview": str(response_body)[:200]},
                )

            return APIClientResult(
                success=True,
                status_code=resp.status_code,
                body=response_body,
                headers=dict(resp.headers),
                trace_id=trace_id,
                duration_ms=round(duration_ms, 2),
                attempt=attempt,
                url=url,
            )

        except httpx.TimeoutException as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.warning("[%s] Timeout on %s %s after %.1fms", trace_id, method, url, duration_ms)
            return APIClientError(
                code=APIErrorCode.TIMEOUT,
                message=f"Request timed out after {self._timeout}s: {exc}",
                trace_id=trace_id,
                attempt=attempt,
                url=url,
            )

        except httpx.RequestError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.warning("[%s] Request error %s %s: %s", trace_id, method, url, exc)
            return APIClientError(
                code=APIErrorCode.REQUEST_ERROR,
                message=f"Request error: {type(exc).__name__}: {exc}",
                trace_id=trace_id,
                attempt=attempt,
                url=url,
            )

    @property
    def circuit_state(self) -> str:
        return self._circuit_breaker.state.value

    @property
    def rate_limit_stats(self) -> dict[str, Any]:
        return {
            "current_count": self._rate_limiter.current_count,
            "max_requests": self._rate_limiter.max_requests,
            "window_seconds": self._rate_limiter.window_seconds,
        }

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Module-level singleton (shared across tool invocations in same process)
# ---------------------------------------------------------------------------

_default_client: Optional[APIClientTool] = None


def get_default_client() -> APIClientTool:
    global _default_client
    if _default_client is None:
        _default_client = APIClientTool()
    return _default_client

"""
Abstract base HTTP client with common patterns for enterprise integrations.

Provides retry logic, circuit breaker, structured logging, correlation IDs,
rate limit handling, and telemetry hooks for all downstream HTTP clients.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Type

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class BaseClientError(Exception):
    """Base exception for all HTTP client errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.correlation_id = correlation_id

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={str(self)!r}, "
            f"status_code={self.status_code}, "
            f"correlation_id={self.correlation_id!r})"
        )


class AuthenticationError(BaseClientError):
    """Raised on 401/403 responses."""


class RateLimitError(BaseClientError):
    """Raised when the server returns 429 Too Many Requests."""

    def __init__(self, *args: Any, retry_after: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ServerError(BaseClientError):
    """Raised on 5xx responses."""


class CircuitOpenError(BaseClientError):
    """Raised when the circuit breaker is open."""


@dataclass
class CircuitBreaker:
    """
    Simple async circuit breaker.

    States:
      CLOSED   – normal operation, failures are counted.
      OPEN     – requests are rejected immediately.
      HALF_OPEN – a probe request is allowed; success closes it.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: Optional[float] = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def call(self, coro: Any) -> Any:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - (self._last_failure_time or 0) > self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker transitioned to HALF_OPEN")
                else:
                    raise CircuitOpenError("Circuit breaker is OPEN; rejecting request")

        try:
            result = await coro
            async with self._lock:
                self._on_success()
            return result
        except (ServerError, httpx.TimeoutException) as exc:
            async with self._lock:
                self._on_failure()
            raise exc

    def _on_success(self) -> None:
        self._failure_count = 0
        if self._state != CircuitState.CLOSED:
            logger.info("Circuit breaker closed after successful probe")
        self._state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker OPENED after %d consecutive failures",
                self._failure_count,
            )

    @property
    def state(self) -> CircuitState:
        return self._state


@dataclass
class RetryConfig:
    """Retry configuration for HTTP requests."""

    max_attempts: int = 3
    wait_min_seconds: float = 1.0
    wait_max_seconds: float = 60.0
    wait_multiplier: float = 2.0
    retryable_status_codes: Tuple[int, ...] = (429, 500, 502, 503, 504)


@dataclass
class ClientConfig:
    """Configuration for the base HTTP client."""

    base_url: str
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    retry: RetryConfig = field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    default_headers: Dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True


class BaseHTTPClient(ABC):
    """
    Abstract base class for all HTTP integration clients.

    Subclasses must implement:
      - ``_build_auth_headers`` – return auth headers for each request.
      - ``_on_auth_error``       – handle 401 (e.g. refresh token).

    Usage::

        async with MyConcreteClient(config) as client:
            data = await client.get("/endpoint", params={"q": "value"})
    """

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._circuit_breaker = config.circuit_breaker
        self._http: Optional[httpx.AsyncClient] = None
        self._request_count: int = 0
        self._error_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BaseHTTPClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """Initialise the underlying HTTP client."""
        limits = httpx.Limits(
            max_connections=self._config.max_connections,
            max_keepalive_connections=self._config.max_keepalive_connections,
        )
        timeout = httpx.Timeout(
            timeout=self._config.timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            limits=limits,
            timeout=timeout,
            verify=self._config.verify_ssl,
            headers=self._config.default_headers,
        )
        logger.info("HTTP client initialised for base_url=%s", self._config.base_url)

    async def close(self) -> None:
        """Gracefully shut down the underlying HTTP client."""
        if self._http:
            await self._http.aclose()
            logger.info(
                "HTTP client closed. requests=%d errors=%d",
                self._request_count,
                self._error_count,
            )

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def _build_auth_headers(self) -> Dict[str, str]:
        """Return authentication headers to be merged into every request."""

    @abstractmethod
    async def _on_auth_error(self, response: httpx.Response) -> bool:
        """
        Handle an authentication failure.

        Return True if the caller should retry the request (e.g. after a
        successful token refresh), False to propagate the error.
        """

    # ------------------------------------------------------------------
    # Core request execution
    # ------------------------------------------------------------------

    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        correlation_id: Optional[str] = None,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Execute an HTTP request with retry, circuit breaker, and logging.

        Args:
            method:         HTTP verb.
            path:           Path relative to ``base_url``.
            params:         URL query parameters.
            json:           JSON-serialisable request body.
            data:           Form-encoded or raw body.
            headers:        Extra headers (merged with auth + defaults).
            correlation_id: Propagated trace ID; generated if not provided.
            stream:         If True, return a streaming response (caller must
                            consume and close it).

        Returns:
            The :class:`httpx.Response` object.

        Raises:
            AuthenticationError: On 401/403 that cannot be recovered.
            RateLimitError:      On 429.
            ServerError:         On 5xx.
            CircuitOpenError:    When the circuit breaker is open.
        """
        if self._http is None:
            raise RuntimeError("Client not started. Use `async with` or call `start()`.")

        corr_id = correlation_id or str(uuid.uuid4())
        retry_cfg = self._config.retry

        async def _execute() -> httpx.Response:
            auth_headers = await self._build_auth_headers()
            merged_headers = {
                "X-Correlation-ID": corr_id,
                "X-Request-ID": str(uuid.uuid4()),
                **auth_headers,
                **(headers or {}),
            }

            log_extra = {
                "method": method.value,
                "path": path,
                "correlation_id": corr_id,
            }
            logger.debug("HTTP request initiated", extra=log_extra)

            start_ts = time.perf_counter()
            response = await self._http.request(  # type: ignore[union-attr]
                method.value,
                path,
                params=params,
                json=json,
                data=data,
                headers=merged_headers,
            )
            elapsed_ms = (time.perf_counter() - start_ts) * 1_000

            logger.info(
                "HTTP %s %s -> %d (%.1f ms) corr=%s",
                method.value,
                path,
                response.status_code,
                elapsed_ms,
                corr_id,
            )
            self._request_count += 1
            return response

        async def _execute_with_auth_retry() -> httpx.Response:
            resp = await _execute()

            if resp.status_code in (401, 403):
                should_retry = await self._on_auth_error(resp)
                if should_retry:
                    resp = await _execute()
                if resp.status_code in (401, 403):
                    self._error_count += 1
                    raise AuthenticationError(
                        f"Authentication failed: {resp.status_code}",
                        status_code=resp.status_code,
                        response_body=resp.text,
                        correlation_id=corr_id,
                    )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                self._error_count += 1
                raise RateLimitError(
                    "Rate limit exceeded",
                    status_code=429,
                    response_body=resp.text,
                    correlation_id=corr_id,
                    retry_after=retry_after,
                )

            if resp.status_code >= 500:
                self._error_count += 1
                raise ServerError(
                    f"Server error: {resp.status_code}",
                    status_code=resp.status_code,
                    response_body=resp.text,
                    correlation_id=corr_id,
                )

            return resp

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(retry_cfg.max_attempts),
                wait=wait_exponential(
                    multiplier=retry_cfg.wait_multiplier,
                    min=retry_cfg.wait_min_seconds,
                    max=retry_cfg.wait_max_seconds,
                ),
                retry=retry_if_exception_type((ServerError, RateLimitError, httpx.TimeoutException)),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    return await self._circuit_breaker.call(_execute_with_auth_retry())
        except RetryError as exc:
            raise ServerError(
                f"All {retry_cfg.max_attempts} retry attempts exhausted",
                correlation_id=corr_id,
            ) from exc

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request(HttpMethod.GET, path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request(HttpMethod.POST, path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request(HttpMethod.PUT, path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request(HttpMethod.PATCH, path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request(HttpMethod.DELETE, path, **kwargs)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = 200,
        page_param: str = "page",
        size_param: str = "pageSize",
        total_key: str = "totalCount",
        results_key: str = "records",
    ) -> AsyncGenerator[List[Dict[str, Any]], None]:
        """
        Async context manager that yields pages of results until exhausted.

        Yields:
            A list of record dicts for each page.
        """
        params = dict(params or {})
        params[size_param] = page_size
        page = 0
        fetched = 0
        total: Optional[int] = None

        while True:
            params[page_param] = page
            response = await self.get(path, params=params)
            response.raise_for_status()
            body = response.json()

            if total is None:
                total = body.get(total_key)

            records: List[Dict[str, Any]] = body.get(results_key, [])
            if not records:
                break

            yield records
            fetched += len(records)
            page += 1

            if total is not None and fetched >= total:
                break

    # ------------------------------------------------------------------
    # Metrics / introspection
    # ------------------------------------------------------------------

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "request_count": self._request_count,
            "error_count": self._error_count,
            "circuit_state": self._circuit_breaker.state.value,
        }

"""
FastAPI middleware stack for the Migration API Gateway.

Middleware (applied in reverse registration order):
  1. CorrelationIDMiddleware  – injects / propagates X-Correlation-ID
  2. RequestLoggingMiddleware – structured request/response logging
  3. RateLimitMiddleware      – sliding-window rate limiting (Redis-backed)
  4. JWTAuthMiddleware        – validates Bearer JWT tokens

Each middleware is implemented as a Starlette ``BaseHTTPMiddleware`` subclass
so it integrates cleanly with FastAPI's dependency injection system.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variables (propagated through the async call stack)
# ---------------------------------------------------------------------------

correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")
authenticated_user_ctx: ContextVar[Dict[str, Any]] = ContextVar("auth_user", default={})

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE-ME-IN-PRODUCTION")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "migration-api")
JWT_ISSUER = os.getenv("JWT_ISSUER", "migration-platform")

# Paths that don't require authentication
PUBLIC_PATHS: Set[str] = {
    "/health",
    "/health/live",
    "/health/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# Paths that don't count toward rate limits
RATE_LIMIT_EXEMPT: Set[str] = PUBLIC_PATHS | {"/metrics"}


# ---------------------------------------------------------------------------
# 1. Correlation ID Middleware
# ---------------------------------------------------------------------------


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Ensures every request has a Correlation ID.

    - Reads ``X-Correlation-ID`` from the incoming request.
    - Generates a new UUID if absent.
    - Stores it in a context variable for use by loggers and downstream services.
    - Echoes it back in the response header.
    """

    HEADER = "X-Correlation-ID"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        corr_id = request.headers.get(self.HEADER) or str(uuid.uuid4())
        correlation_id_ctx.set(corr_id)
        request.state.correlation_id = corr_id

        response = await call_next(request)
        response.headers[self.HEADER] = corr_id
        return response


# ---------------------------------------------------------------------------
# 2. Request Logging Middleware
# ---------------------------------------------------------------------------


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Emits structured log entries for every inbound request and outbound response.

    Excludes health-check paths to reduce noise.  Logs the response body on
    4xx/5xx for debugging (truncated to 2 KB).
    """

    EXCLUDED_PATHS: Set[str] = {"/health", "/health/live", "/health/ready", "/metrics"}
    MAX_BODY_LOG_BYTES: int = 2048

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)

        corr_id = getattr(request.state, "correlation_id", "")
        start_ts = time.perf_counter()

        log_extra = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.query_params),
            "client_ip": self._get_client_ip(request),
            "user_agent": request.headers.get("User-Agent", ""),
            "correlation_id": corr_id,
        }
        logger.info("Request started", extra=log_extra)

        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - start_ts) * 1_000
            logger.error(
                "Request raised unhandled exception",
                extra={**log_extra, "error": str(exc), "elapsed_ms": elapsed_ms},
                exc_info=True,
            )
            raise

        elapsed_ms = (time.perf_counter() - start_ts) * 1_000
        log_level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            log_level,
            "Request completed",
            extra={
                **log_extra,
                "status_code": response.status_code,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )
        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"


# ---------------------------------------------------------------------------
# 3. Rate Limit Middleware
# ---------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """
    In-process sliding-window rate limiter.

    For production, replace with a Redis-backed implementation using
    sorted sets (ZADD / ZREMRANGEBYSCORE / ZCARD pipeline).
    """

    def __init__(self, window_seconds: int = 60, max_requests: int = 100) -> None:
        self._window = window_seconds
        self._max = max_requests
        self._buckets: Dict[str, List[float]] = {}
        import asyncio
        self._lock = asyncio.Lock()

    async def is_allowed(self, key: str) -> Tuple[bool, int, int]:
        """
        Returns:
            (allowed, remaining, retry_after_seconds)
        """
        import asyncio

        now = time.monotonic()
        cutoff = now - self._window

        async with self._lock:
            timestamps = self._buckets.get(key, [])
            # Evict expired timestamps
            timestamps = [ts for ts in timestamps if ts > cutoff]

            if len(timestamps) >= self._max:
                oldest = timestamps[0]
                retry_after = int(oldest + self._window - now) + 1
                return False, 0, retry_after

            timestamps.append(now)
            self._buckets[key] = timestamps
            remaining = self._max - len(timestamps)
            return True, remaining, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter middleware.

    Keyed by:
      - Authenticated user ID (preferred)
      - Client IP address (fallback for unauthenticated requests)

    Returns HTTP 429 with ``Retry-After`` and ``X-RateLimit-*`` headers.
    """

    def __init__(
        self,
        app: ASGIApp,
        window_seconds: int = 60,
        max_requests: int = 200,
        burst_max_requests: int = 50,
        burst_window_seconds: int = 1,
    ) -> None:
        super().__init__(app)
        self._limiter = SlidingWindowRateLimiter(window_seconds, max_requests)
        self._burst_limiter = SlidingWindowRateLimiter(burst_window_seconds, burst_max_requests)
        self._window = window_seconds
        self._max = max_requests

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in RATE_LIMIT_EXEMPT:
            return await call_next(request)

        client_key = self._resolve_key(request)

        # Burst check (short window)
        burst_allowed, _, burst_retry_after = await self._burst_limiter.is_allowed(
            f"burst:{client_key}"
        )
        if not burst_allowed:
            return self._rate_limit_response(0, burst_retry_after, request)

        allowed, remaining, retry_after = await self._limiter.is_allowed(client_key)
        if not allowed:
            logger.warning(
                "Rate limit exceeded key=%s path=%s corr=%s",
                client_key,
                request.url.path,
                getattr(request.state, "correlation_id", ""),
            )
            return self._rate_limit_response(remaining, retry_after, request)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._max)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(self._window)
        return response

    @staticmethod
    def _resolve_key(request: Request) -> str:
        user = authenticated_user_ctx.get({})
        if user:
            return f"user:{user.get('sub', 'unknown')}"
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        ip = forwarded_for.split(",")[0].strip() if forwarded_for else (
            request.client.host if request.client else "unknown"
        )
        return f"ip:{ip}"

    @staticmethod
    def _rate_limit_response(remaining: int, retry_after: int, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rate_limit_exceeded",
                "message": "Too many requests. Please slow down.",
                "retry_after_seconds": retry_after,
                "correlation_id": getattr(request.state, "correlation_id", ""),
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Remaining": str(remaining),
            },
        )


# ---------------------------------------------------------------------------
# 4. JWT Auth Middleware
# ---------------------------------------------------------------------------


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates ``Authorization: Bearer <token>`` on every non-public path.

    Claims are decoded and stored in:
      - ``request.state.user``  (dict with sub, roles, etc.)
      - ``authenticated_user_ctx`` context variable

    Supports:
      - Symmetric HS256 / HS512 secrets
      - Asymmetric RS256 via JWKS endpoint (set ``JWKS_URL`` env var)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._jwks_url = os.getenv("JWKS_URL")
        self._jwks_cache: Optional[Dict[str, Any]] = None
        self._jwks_fetched_at: float = 0.0

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return self._auth_error("missing_token", "Bearer token is required")

        token = auth_header.removeprefix("Bearer ").strip()

        try:
            claims = await self._decode_token(token)
        except JWTError as exc:
            logger.warning(
                "JWT validation failed path=%s error=%s corr=%s",
                request.url.path,
                exc,
                getattr(request.state, "correlation_id", ""),
            )
            return self._auth_error("invalid_token", str(exc))

        request.state.user = claims
        authenticated_user_ctx.set(claims)

        logger.debug(
            "JWT validated sub=%s roles=%s path=%s",
            claims.get("sub"),
            claims.get("roles", []),
            request.url.path,
        )
        return await call_next(request)

    async def _decode_token(self, token: str) -> Dict[str, Any]:
        if self._jwks_url:
            key = await self._get_jwks_key(token)
            return jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=JWT_AUDIENCE,
                issuer=JWT_ISSUER,
            )
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )

    async def _get_jwks_key(self, token: str) -> Dict[str, Any]:
        """Fetch JWKS and return the matching public key."""
        now = time.monotonic()
        if not self._jwks_cache or (now - self._jwks_fetched_at) > 3600:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._jwks_url)  # type: ignore[arg-type]
                resp.raise_for_status()
                self._jwks_cache = resp.json()
                self._jwks_fetched_at = now

        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        for key in self._jwks_cache.get("keys", []):  # type: ignore[union-attr]
            if key.get("kid") == kid:
                return key
        raise JWTError(f"No matching JWK found for kid={kid}")

    @staticmethod
    def _auth_error(code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": code, "message": message},
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Role-based access control dependency
# ---------------------------------------------------------------------------


def require_roles(*required_roles: str) -> Callable:
    """
    FastAPI dependency that enforces role-based access.

    Usage::

        @router.post("/admin/start", dependencies=[Depends(require_roles("admin"))])
        async def start_migration(): ...
    """
    from fastapi import Depends

    async def _check(request: Request) -> None:
        user = getattr(request.state, "user", {})
        user_roles: List[str] = user.get("roles", [])
        for role in required_roles:
            if role not in user_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "insufficient_permissions",
                        "required_roles": list(required_roles),
                        "user_roles": user_roles,
                    },
                )

    return _check

"""
FastAPI Metrics Middleware — Legacy to Salesforce Migration
===========================================================
Records Prometheus metrics for every HTTP request/response.

Author: Platform Engineering Team
Version: 1.0.0
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match
from starlette.types import ASGIApp

from monitoring.metrics.prometheus_metrics import (
    http_request_duration_seconds,
    http_request_size_bytes,
    http_requests_in_flight,
    http_requests_total,
)


def _get_route_path(request: Request) -> str:
    """
    Extract the parameterized route path from a FastAPI request.

    Returns the route template (e.g., "/jobs/{job_id}") rather than the
    actual path ("/jobs/abc123") to prevent high cardinality in metrics.
    """
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


# Paths that should not be tracked in metrics (high-volume health endpoints)
DEFAULT_EXCLUDE_PATHS = {
    "/health",
    "/healthz",
    "/readyz",
    "/livez",
    "/metrics",
    "/ready",
    "/ping",
    "/favicon.ico",
}


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """
    Starlette/FastAPI middleware that records Prometheus HTTP metrics.

    Records:
      - http_requests_total: counter of all requests by method, path, status
      - http_request_duration_seconds: histogram of request duration
      - http_requests_in_flight: gauge of concurrent requests
      - http_request_size_bytes: histogram of request body sizes

    Usage:
        app = FastAPI()
        app.add_middleware(
            PrometheusMetricsMiddleware,
            environment="production",
            exclude_paths={"/health", "/metrics"},
        )
    """

    def __init__(
        self,
        app: ASGIApp,
        environment: str = "production",
        exclude_paths: set[str] | None = None,
        group_status_codes: bool = False,
    ) -> None:
        super().__init__(app)
        self._environment = environment
        self._exclude_paths = exclude_paths or DEFAULT_EXCLUDE_PATHS
        self._group_status_codes = group_status_codes  # If True: 200 → "2xx"

    def _format_status_code(self, status_code: int) -> str:
        if self._group_status_codes:
            return f"{status_code // 100}xx"
        return str(status_code)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip excluded paths
        if request.url.path in self._exclude_paths:
            return await call_next(request)

        method = request.method
        path = _get_route_path(request)

        # Track request size
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                http_request_size_bytes.labels(method=method, path=path).observe(
                    int(content_length)
                )
            except ValueError:
                pass

        # Track in-flight requests
        http_requests_in_flight.labels(environment=self._environment).inc()

        start_time = time.perf_counter()
        status_code = 500

        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            duration = time.perf_counter() - start_time

            http_requests_in_flight.labels(environment=self._environment).dec()

            formatted_status = self._format_status_code(status_code)

            http_requests_total.labels(
                method=method,
                path=path,
                status_code=formatted_status,
                environment=self._environment,
            ).inc()

            http_request_duration_seconds.labels(
                method=method,
                path=path,
                environment=self._environment,
            ).observe(duration)


def add_metrics_endpoint(app: FastAPI, path: str = "/metrics") -> None:
    """
    Add a Prometheus /metrics endpoint to a FastAPI application.

    The endpoint is protected and should only be accessible from within
    the cluster (enforced via network policy + optional auth).

    Args:
        app: The FastAPI application instance.
        path: The path to expose metrics on. Default: "/metrics"
    """
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi import Response as FastAPIResponse
    from monitoring.metrics.prometheus_metrics import MIGRATION_REGISTRY

    @app.get(path, include_in_schema=False)
    async def metrics_endpoint() -> FastAPIResponse:
        """Prometheus metrics endpoint."""
        data = generate_latest(MIGRATION_REGISTRY)
        return FastAPIResponse(
            content=data,
            media_type=CONTENT_TYPE_LATEST,
        )


def setup_metrics(
    app: FastAPI,
    environment: str = "production",
    service_name: str = "migration-platform",
    service_version: str = "unknown",
    metrics_path: str = "/metrics",
    exclude_paths: set[str] | None = None,
) -> None:
    """
    One-call setup for Prometheus metrics on a FastAPI application.

    Adds middleware and metrics endpoint.

    Args:
        app: FastAPI application.
        environment: Deployment environment label.
        service_name: Service name for Info metric.
        service_version: Service version for Info metric.
        metrics_path: Path for Prometheus scraping.
        exclude_paths: Paths to exclude from metrics tracking.
    """
    from monitoring.metrics.prometheus_metrics import initialize_metrics

    # Initialize service info metrics
    initialize_metrics(service_name, service_version, environment)

    # Add middleware
    app.add_middleware(
        PrometheusMetricsMiddleware,
        environment=environment,
        exclude_paths=exclude_paths,
    )

    # Add /metrics endpoint
    add_metrics_endpoint(app, metrics_path)

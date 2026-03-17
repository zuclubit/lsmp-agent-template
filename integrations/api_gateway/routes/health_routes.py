"""
Health check and readiness probe endpoints.

Endpoints
---------
GET /health/live   – Kubernetes liveness probe (is the process alive?)
GET /health/ready  – Kubernetes readiness probe (can it serve traffic?)
GET /health        – Human-readable aggregate health status
GET /health/deps   – Detailed dependency health (Salesforce, Kafka, Redis, etc.)

The readiness probe fails (503) if any CRITICAL dependency is unavailable,
allowing Kubernetes to remove the pod from the load balancer until it recovers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

# ---------------------------------------------------------------------------
# Dependency check registry
# ---------------------------------------------------------------------------


class DependencyStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class Criticality(str, Enum):
    CRITICAL = "critical"      # readiness fails if UNHEALTHY
    NON_CRITICAL = "non_critical"  # degrades gracefully


@dataclass
class DependencyCheck:
    name: str
    check_fn: Callable[[], Coroutine[Any, Any, "DependencyResult"]]
    criticality: Criticality = Criticality.CRITICAL
    timeout_seconds: float = 5.0


@dataclass
class DependencyResult:
    name: str
    status: DependencyStatus
    latency_ms: float
    message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class LivenessResponse(BaseModel):
    status: str = "alive"
    timestamp: datetime
    uptime_seconds: float
    version: str


class ReadinessResponse(BaseModel):
    status: str                         # "ready" | "not_ready"
    timestamp: datetime
    failing_dependencies: List[str]


class HealthResponse(BaseModel):
    status: str                         # "healthy" | "degraded" | "unhealthy"
    timestamp: datetime
    version: str
    uptime_seconds: float
    environment: str
    dependencies: Dict[str, Any]
    metrics: Dict[str, Any]


class DependencyHealthResponse(BaseModel):
    name: str
    status: str
    criticality: str
    latency_ms: float
    message: Optional[str]
    details: Dict[str, Any]
    checked_at: datetime


# ---------------------------------------------------------------------------
# Application state (set at startup)
# ---------------------------------------------------------------------------


_START_TIME: float = time.monotonic()
_APP_VERSION: str = os.getenv("APP_VERSION", "1.0.0")
_ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
_DEPENDENCY_CHECKS: List[DependencyCheck] = []


def register_dependency(check: DependencyCheck) -> None:
    """Register a health check for a downstream dependency."""
    _DEPENDENCY_CHECKS.append(check)
    logger.info("Health check registered: %s (criticality=%s)", check.name, check.criticality.value)


# ---------------------------------------------------------------------------
# Built-in dependency check implementations
# ---------------------------------------------------------------------------


async def _check_salesforce() -> DependencyResult:
    """Verify Salesforce API is reachable by fetching /limits."""
    from integrations.rest_clients.salesforce_client import SalesforceClient, SalesforceConfig

    start = time.perf_counter()
    try:
        sf_config = SalesforceConfig(
            client_id=os.getenv("SF_CLIENT_ID", ""),
            username=os.getenv("SF_USERNAME", ""),
            private_key_pem=os.getenv("SF_PRIVATE_KEY", ""),
        )
        async with SalesforceClient(sf_config) as sf:
            limits = await sf.limits()
        latency = (time.perf_counter() - start) * 1000
        api_remaining = limits.get("DailyApiRequests", {}).get("Remaining", "?")
        return DependencyResult(
            name="salesforce",
            status=DependencyStatus.HEALTHY,
            latency_ms=round(latency, 2),
            details={"api_requests_remaining": api_remaining},
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="salesforce",
            status=DependencyStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message=str(exc),
        )


async def _check_redis() -> DependencyResult:
    """Ping Redis and return latency."""
    start = time.perf_counter()
    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        client = aioredis.from_url(redis_url, socket_connect_timeout=3)
        await client.ping()
        await client.aclose()
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="redis",
            status=DependencyStatus.HEALTHY,
            latency_ms=round(latency, 2),
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="redis",
            status=DependencyStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message=str(exc),
        )


async def _check_kafka() -> DependencyResult:
    """Check Kafka broker connectivity (list topics)."""
    start = time.perf_counter()
    try:
        from confluent_kafka.admin import AdminClient  # type: ignore[import]

        admin = AdminClient(
            {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")}
        )
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(None, lambda: admin.list_topics(timeout=3))
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="kafka",
            status=DependencyStatus.HEALTHY,
            latency_ms=round(latency, 2),
            details={"topic_count": len(metadata.topics)},
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="kafka",
            status=DependencyStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message=str(exc),
        )


async def _check_database() -> DependencyResult:
    """Verify database connectivity with a cheap query."""
    start = time.perf_counter()
    try:
        import asyncpg  # type: ignore[import]

        db_url = os.getenv("DATABASE_URL", "postgresql://localhost/migration")
        conn = await asyncpg.connect(dsn=db_url, timeout=5.0)
        await conn.fetchval("SELECT 1")
        await conn.close()
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="database",
            status=DependencyStatus.HEALTHY,
            latency_ms=round(latency, 2),
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.perf_counter() - start) * 1000
        return DependencyResult(
            name="database",
            status=DependencyStatus.UNHEALTHY,
            latency_ms=round(latency, 2),
            message=str(exc),
        )


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------


async def _run_checks(
    checks: List[DependencyCheck],
) -> List[DependencyResult]:
    """Run all dependency checks concurrently with individual timeouts."""
    async def _run_one(check: DependencyCheck) -> DependencyResult:
        try:
            return await asyncio.wait_for(check.check_fn(), timeout=check.timeout_seconds)
        except asyncio.TimeoutError:
            return DependencyResult(
                name=check.name,
                status=DependencyStatus.UNHEALTHY,
                latency_ms=check.timeout_seconds * 1000,
                message=f"Check timed out after {check.timeout_seconds}s",
            )
        except Exception as exc:  # noqa: BLE001
            return DependencyResult(
                name=check.name,
                status=DependencyStatus.UNHEALTHY,
                latency_ms=0.0,
                message=str(exc),
            )

    tasks = [asyncio.create_task(_run_one(c)) for c in checks]
    return await asyncio.gather(*tasks)


def _aggregate_status(results: List[DependencyResult]) -> DependencyStatus:
    statuses = {r.status for r in results}
    if DependencyStatus.UNHEALTHY in statuses:
        return DependencyStatus.UNHEALTHY
    if DependencyStatus.DEGRADED in statuses:
        return DependencyStatus.DEGRADED
    return DependencyStatus.HEALTHY


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Kubernetes liveness probe",
)
async def liveness_probe() -> LivenessResponse:
    """
    Returns 200 if the process is alive.

    Kubernetes restarts the pod if this returns non-2xx.
    Should only fail for truly unrecoverable states (e.g. deadlock).
    """
    return LivenessResponse(
        status="alive",
        timestamp=datetime.now(timezone.utc),
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
        version=_APP_VERSION,
    )


@router.get(
    "/ready",
    summary="Kubernetes readiness probe",
)
async def readiness_probe(response: Response) -> ReadinessResponse:
    """
    Returns 200 only when ALL critical dependencies are healthy.

    Kubernetes removes the pod from the service load balancer when this
    endpoint returns 503, preventing traffic from reaching an unhealthy pod.
    """
    critical_checks = [c for c in _DEPENDENCY_CHECKS if c.criticality == Criticality.CRITICAL]
    results = await _run_checks(critical_checks)

    failing = [r.name for r in results if r.status == DependencyStatus.UNHEALTHY]
    is_ready = len(failing) == 0

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning("Readiness probe failed: %s", failing)

    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        timestamp=datetime.now(timezone.utc),
        failing_dependencies=failing,
    )


@router.get(
    "",
    response_model=HealthResponse,
    summary="Aggregate health status",
)
async def health_check(response: Response) -> HealthResponse:
    """
    Human-readable health summary.

    Runs all dependency checks and returns an aggregated status:
      - healthy   – all dependencies OK
      - degraded  – non-critical dependency unhealthy
      - unhealthy – critical dependency unhealthy
    """
    results = await _run_checks(_DEPENDENCY_CHECKS)
    aggregate = _aggregate_status(results)

    if aggregate == DependencyStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif aggregate == DependencyStatus.DEGRADED:
        response.status_code = status.HTTP_200_OK

    dep_summary = {
        r.name: {
            "status": r.status.value,
            "latency_ms": r.latency_ms,
            "message": r.message,
        }
        for r in results
    }

    return HealthResponse(
        status=aggregate.value,
        timestamp=datetime.now(timezone.utc),
        version=_APP_VERSION,
        uptime_seconds=round(time.monotonic() - _START_TIME, 1),
        environment=_ENVIRONMENT,
        dependencies=dep_summary,
        metrics={},
    )


@router.get(
    "/deps",
    summary="Detailed dependency health",
)
async def dependency_health() -> List[DependencyHealthResponse]:
    """
    Full per-dependency health report with timing and metadata.
    Useful for operations dashboards and alert routing.
    """
    results = await _run_checks(_DEPENDENCY_CHECKS)
    checks_by_name = {c.name: c for c in _DEPENDENCY_CHECKS}

    return [
        DependencyHealthResponse(
            name=r.name,
            status=r.status.value,
            criticality=checks_by_name.get(r.name, DependencyCheck(
                r.name, lambda: ..., Criticality.NON_CRITICAL  # type: ignore[arg-type]
            )).criticality.value,
            latency_ms=r.latency_ms,
            message=r.message,
            details=r.details,
            checked_at=r.checked_at,
        )
        for r in results
    ]

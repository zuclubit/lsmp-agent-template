"""
Prometheus Metrics Definitions — Legacy to Salesforce Migration
===============================================================
Defines all Prometheus metrics for the migration platform.

Metric naming convention: migration_<component>_<measurement>_<unit>

Author: Platform Engineering Team
Version: 1.0.0
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Generator, Optional

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    Summary,
    start_http_server,
    make_asgi_app,
)

# ---------------------------------------------------------------------------
# Custom registry (allows test isolation)
# ---------------------------------------------------------------------------
MIGRATION_REGISTRY = CollectorRegistry()


# ---------------------------------------------------------------------------
# Helper: create metrics in our custom registry
# ---------------------------------------------------------------------------

def _counter(name: str, documentation: str, labelnames: list[str] | None = None) -> Counter:
    return Counter(name, documentation, labelnames or [], registry=MIGRATION_REGISTRY)


def _gauge(name: str, documentation: str, labelnames: list[str] | None = None) -> Gauge:
    return Gauge(name, documentation, labelnames or [], registry=MIGRATION_REGISTRY)


def _histogram(name: str, documentation: str, labelnames: list[str] | None = None, buckets=None) -> Histogram:
    kwargs: dict[str, Any] = {"registry": MIGRATION_REGISTRY, "labelnames": labelnames or []}
    if buckets:
        kwargs["buckets"] = buckets
    return Histogram(name, documentation, **kwargs)


def _summary(name: str, documentation: str, labelnames: list[str] | None = None) -> Summary:
    return Summary(name, documentation, labelnames or [], registry=MIGRATION_REGISTRY)


def _info(name: str, documentation: str) -> Info:
    return Info(name, documentation, registry=MIGRATION_REGISTRY)


# ---------------------------------------------------------------------------
# Service Info
# ---------------------------------------------------------------------------

migration_service_info = _info(
    "migration_service",
    "Migration service version and metadata",
)

# ---------------------------------------------------------------------------
# Migration Job Metrics
# ---------------------------------------------------------------------------

migration_jobs_total = _counter(
    "migration_jobs_total",
    "Total number of migration jobs created",
    ["environment", "source_object", "status"],
)

migration_jobs_active = _gauge(
    "migration_jobs_active",
    "Number of currently active migration jobs",
    ["environment", "phase"],
)

migration_jobs_duration_seconds = _histogram(
    "migration_jobs_duration_seconds",
    "Duration of completed migration jobs in seconds",
    ["environment", "source_object", "status"],
    buckets=[60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400],  # 1m to 24h
)

migration_phase_duration_seconds = _histogram(
    "migration_phase_duration_seconds",
    "Duration of each migration phase in seconds",
    ["environment", "source_object", "phase", "status"],
    buckets=[1, 5, 10, 30, 60, 300, 600, 1800, 3600],
)

# ---------------------------------------------------------------------------
# Record Processing Metrics
# ---------------------------------------------------------------------------

migration_records_processed_total = _counter(
    "migration_records_processed_total",
    "Total number of records processed by the migration pipeline",
    ["environment", "source_object", "phase", "status"],
)

migration_records_in_flight = _gauge(
    "migration_records_in_flight",
    "Current number of records being processed",
    ["environment", "source_object", "phase"],
)

migration_records_per_second = _gauge(
    "migration_records_per_second",
    "Current record processing throughput (records/second)",
    ["environment", "source_object"],
)

migration_batch_size = _histogram(
    "migration_batch_size_records",
    "Number of records per migration batch",
    ["environment", "source_object", "phase"],
    buckets=[1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

migration_batch_duration_seconds = _histogram(
    "migration_batch_duration_seconds",
    "Time taken to process a single batch",
    ["environment", "source_object", "phase"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

migration_bytes_processed_total = _counter(
    "migration_bytes_processed_total",
    "Total bytes of data processed during migration",
    ["environment", "source_object"],
)

# ---------------------------------------------------------------------------
# Error and Retry Metrics
# ---------------------------------------------------------------------------

migration_errors_total = _counter(
    "migration_errors_total",
    "Total number of migration errors by type",
    ["environment", "source_object", "phase", "error_type"],
)

migration_retries_total = _counter(
    "migration_retries_total",
    "Total number of retry attempts",
    ["environment", "source_object", "phase", "reason"],
)

migration_dead_letter_queue_size = _gauge(
    "migration_dead_letter_queue_size",
    "Number of records in the dead-letter queue (failed after all retries)",
    ["environment", "source_object"],
)

migration_sla_breaches_total = _counter(
    "migration_sla_breaches_total",
    "Total number of SLA breaches (jobs exceeding target duration)",
    ["environment", "source_object", "sla_threshold"],
)

# ---------------------------------------------------------------------------
# Salesforce API Metrics
# ---------------------------------------------------------------------------

salesforce_api_requests_total = _counter(
    "salesforce_api_requests_total",
    "Total Salesforce API requests made",
    ["environment", "api_type", "object_name", "status_code"],
)

salesforce_api_duration_seconds = _histogram(
    "salesforce_api_duration_seconds",
    "Salesforce API request duration in seconds",
    ["environment", "api_type", "object_name"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
)

salesforce_api_rate_limit_remaining = _gauge(
    "salesforce_api_rate_limit_remaining",
    "Remaining Salesforce API calls for the current 24-hour window",
    ["environment", "org_id"],
)

salesforce_bulk_api_jobs_active = _gauge(
    "salesforce_bulk_api_jobs_active",
    "Number of active Salesforce Bulk API jobs",
    ["environment", "operation"],
)

salesforce_bulk_api_records_pending = _gauge(
    "salesforce_bulk_api_records_pending",
    "Records pending processing in Salesforce Bulk API jobs",
    ["environment", "object_name"],
)

salesforce_api_errors_total = _counter(
    "salesforce_api_errors_total",
    "Total Salesforce API errors",
    ["environment", "error_code", "object_name"],
)

# ---------------------------------------------------------------------------
# Database Metrics (Legacy Source DB)
# ---------------------------------------------------------------------------

legacy_db_query_duration_seconds = _histogram(
    "legacy_db_query_duration_seconds",
    "Legacy database query duration in seconds",
    ["environment", "operation", "table"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10],
)

legacy_db_connections_active = _gauge(
    "legacy_db_connections_active",
    "Active connections to the legacy database",
    ["environment"],
)

legacy_db_connections_pool_size = _gauge(
    "legacy_db_connections_pool_size",
    "Database connection pool size",
    ["environment", "state"],  # state: idle, active, waiting
)

legacy_db_errors_total = _counter(
    "legacy_db_errors_total",
    "Total legacy database errors",
    ["environment", "error_type", "table"],
)

# ---------------------------------------------------------------------------
# Data Quality Metrics
# ---------------------------------------------------------------------------

migration_validation_errors_total = _counter(
    "migration_validation_errors_total",
    "Total data validation errors encountered during migration",
    ["environment", "source_object", "field_name", "rule_type"],
)

migration_transformation_errors_total = _counter(
    "migration_transformation_errors_total",
    "Total transformation errors",
    ["environment", "source_object", "transformer", "error_type"],
)

migration_data_quality_score = _gauge(
    "migration_data_quality_score",
    "Data quality score (0-100) for the current/last migration run",
    ["environment", "source_object"],
)

migration_records_enriched_total = _counter(
    "migration_records_enriched_total",
    "Total records enriched (lookup values resolved, defaults applied)",
    ["environment", "source_object", "enrichment_type"],
)

# ---------------------------------------------------------------------------
# Kafka / Messaging Metrics
# ---------------------------------------------------------------------------

kafka_messages_produced_total = _counter(
    "kafka_messages_produced_total",
    "Total Kafka messages produced",
    ["environment", "topic", "status"],
)

kafka_messages_consumed_total = _counter(
    "kafka_messages_consumed_total",
    "Total Kafka messages consumed",
    ["environment", "topic", "consumer_group"],
)

kafka_consumer_lag = _gauge(
    "kafka_consumer_lag",
    "Consumer group lag (messages behind) per partition",
    ["environment", "topic", "partition", "consumer_group"],
)

kafka_producer_duration_seconds = _histogram(
    "kafka_producer_duration_seconds",
    "Time to produce a Kafka message",
    ["environment", "topic"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1],
)

# ---------------------------------------------------------------------------
# HTTP API Metrics (migration API service)
# ---------------------------------------------------------------------------

http_requests_total = _counter(
    "http_requests_total",
    "Total HTTP requests received by the migration API",
    ["method", "path", "status_code", "environment"],
)

http_request_duration_seconds = _histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path", "environment"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

http_requests_in_flight = _gauge(
    "http_requests_in_flight",
    "Current number of in-flight HTTP requests",
    ["environment"],
)

http_request_size_bytes = _histogram(
    "http_request_size_bytes",
    "HTTP request body size",
    ["method", "path"],
    buckets=[100, 1000, 10_000, 100_000, 1_000_000, 10_000_000],
)

# ---------------------------------------------------------------------------
# Security Metrics
# ---------------------------------------------------------------------------

auth_attempts_total = _counter(
    "auth_attempts_total",
    "Total authentication attempts",
    ["environment", "method", "outcome"],  # outcome: success, failure
)

authz_decisions_total = _counter(
    "authz_decisions_total",
    "Total authorization decisions",
    ["environment", "permission", "decision"],  # decision: allow, deny
)

secret_rotations_total = _counter(
    "secret_rotations_total",
    "Total secret rotations performed",
    ["environment", "secret_type"],
)

# ---------------------------------------------------------------------------
# System Health Metrics
# ---------------------------------------------------------------------------

component_health = _gauge(
    "component_health",
    "Health status of system components (1=healthy, 0=unhealthy)",
    ["environment", "component"],
)

# ---------------------------------------------------------------------------
# Context Managers for Easy Timing
# ---------------------------------------------------------------------------

@contextmanager
def time_migration_phase(
    source_object: str,
    phase: str,
    environment: str = "production",
) -> Generator[None, None, None]:
    """Context manager to time a migration phase."""
    start = time.perf_counter()
    status = "success"
    try:
        yield
    except Exception:
        status = "failure"
        raise
    finally:
        duration = time.perf_counter() - start
        migration_phase_duration_seconds.labels(
            environment=environment,
            source_object=source_object,
            phase=phase,
            status=status,
        ).observe(duration)


@contextmanager
def time_salesforce_api(
    api_type: str,
    object_name: str,
    environment: str = "production",
) -> Generator[dict[str, Any], None, None]:
    """
    Context manager to time a Salesforce API call.

    Usage:
        with time_salesforce_api("bulk", "Account") as ctx:
            response = await sf_client.bulk_insert(records)
            ctx["status_code"] = 200
    """
    start = time.perf_counter()
    ctx: dict[str, Any] = {"status_code": "200"}
    try:
        yield ctx
    except Exception:
        ctx["status_code"] = "error"
        salesforce_api_errors_total.labels(
            environment=environment,
            error_code="exception",
            object_name=object_name,
        ).inc()
        raise
    finally:
        duration = time.perf_counter() - start
        salesforce_api_duration_seconds.labels(
            environment=environment,
            api_type=api_type,
            object_name=object_name,
        ).observe(duration)
        salesforce_api_requests_total.labels(
            environment=environment,
            api_type=api_type,
            object_name=object_name,
            status_code=str(ctx["status_code"]),
        ).inc()


# ---------------------------------------------------------------------------
# Metric initialization
# ---------------------------------------------------------------------------

def initialize_metrics(service_name: str, version: str, environment: str) -> None:
    """Set service info metric. Call at application startup."""
    migration_service_info.info({
        "service_name": service_name,
        "version": version,
        "environment": environment,
    })
    # Initialize component health gauges
    for component in ["api", "legacy_db", "kafka", "vault", "salesforce_api"]:
        component_health.labels(environment=environment, component=component).set(1)

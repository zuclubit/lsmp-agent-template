"""
OpenTelemetry Tracing Configuration — Legacy to Salesforce Migration
====================================================================
Distributed tracing with auto-instrumentation support.

Exporters supported:
  - Jaeger (legacy, gRPC or HTTP)
  - Grafana Tempo (OTLP HTTP or gRPC)
  - Console (development)

Author: Platform Engineering Team
Version: 1.0.0
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as OTLPHTTPSpanExporter,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.kafka import KafkaInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.sampling import (
    ParentBased,
    TraceIdRatioBased,
    ALWAYS_ON,
    ALWAYS_OFF,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE_RATE = 0.1  # 10% of traces sampled in production
INSTRUMENTATION_LIBRARY = "migration-platform"


# ---------------------------------------------------------------------------
# Tracing Configuration
# ---------------------------------------------------------------------------

class TracingConfig:
    """Configuration for OpenTelemetry tracing."""

    def __init__(
        self,
        service_name: str = "migration-platform",
        service_version: str = "unknown",
        environment: str = "development",
        exporter_type: str = "otlp_grpc",  # otlp_grpc, otlp_http, jaeger, console
        otlp_endpoint: str | None = None,
        sample_rate: float = 1.0,
        enabled: bool = True,
    ) -> None:
        self.service_name = service_name
        self.service_version = service_version
        self.environment = environment
        self.exporter_type = exporter_type
        self.otlp_endpoint = otlp_endpoint or self._default_otlp_endpoint(exporter_type)
        self.sample_rate = sample_rate
        self.enabled = enabled

    @staticmethod
    def _default_otlp_endpoint(exporter_type: str) -> str:
        if "grpc" in exporter_type:
            return os.environ.get("OTLP_GRPC_ENDPOINT", "http://tempo.monitoring.svc.cluster.local:4317")
        return os.environ.get("OTLP_HTTP_ENDPOINT", "http://tempo.monitoring.svc.cluster.local:4318")

    @classmethod
    def from_env(cls) -> "TracingConfig":
        """Load configuration from environment variables."""
        return cls(
            service_name=os.environ.get("SERVICE_NAME", "migration-platform"),
            service_version=os.environ.get("SERVICE_VERSION", "unknown"),
            environment=os.environ.get("ENVIRONMENT", "development"),
            exporter_type=os.environ.get("OTEL_EXPORTER_TYPE", "otlp_grpc"),
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
            sample_rate=float(os.environ.get("OTEL_SAMPLE_RATE", "0.1")),
            enabled=os.environ.get("OTEL_TRACING_ENABLED", "true").lower() == "true",
        )


# ---------------------------------------------------------------------------
# Tracing Setup
# ---------------------------------------------------------------------------

def setup_tracing(config: TracingConfig | None = None) -> TracerProvider | None:
    """
    Initialize OpenTelemetry tracing.

    Args:
        config: Tracing configuration. Defaults to loading from environment.

    Returns:
        The configured TracerProvider, or None if tracing is disabled.
    """
    if config is None:
        config = TracingConfig.from_env()

    if not config.enabled:
        logger.info("OpenTelemetry tracing disabled")
        return None

    # Build resource attributes
    resource = Resource.create({
        SERVICE_NAME: config.service_name,
        SERVICE_VERSION: config.service_version,
        "deployment.environment": config.environment,
        "service.namespace": os.environ.get("POD_NAMESPACE", "migration-system"),
        "host.name": os.environ.get("HOSTNAME", "unknown"),
        "k8s.pod.name": os.environ.get("POD_NAME", "unknown"),
        "k8s.namespace.name": os.environ.get("POD_NAMESPACE", "migration-system"),
    })

    # Configure sampler
    if config.sample_rate >= 1.0:
        sampler = ALWAYS_ON
    elif config.sample_rate <= 0.0:
        sampler = ALWAYS_OFF
    else:
        # ParentBased respects parent span's sampling decision
        sampler = ParentBased(root=TraceIdRatioBased(config.sample_rate))

    # Create provider
    provider = TracerProvider(resource=resource, sampler=sampler)

    # Configure exporter
    exporter = _build_exporter(config)

    if config.environment == "development":
        # Synchronous export in development for immediate feedback
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        # Async batch export in production
        batch_processor = BatchSpanProcessor(
            exporter,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
            max_queue_size=2048,
            export_timeout_millis=30000,
        )
        provider.add_span_processor(batch_processor)

    # Set as global provider
    trace.set_tracer_provider(provider)

    # Configure propagators (W3C TraceContext + Baggage)
    from opentelemetry import propagate
    propagate.set_global_textmap(
        CompositePropagator([
            TraceContextTextMapPropagator(),
            W3CBaggagePropagator(),
        ])
    )

    logger.info(
        "OpenTelemetry tracing initialized",
        extra={
            "service": config.service_name,
            "exporter": config.exporter_type,
            "endpoint": config.otlp_endpoint,
            "sample_rate": config.sample_rate,
        },
    )

    return provider


def _build_exporter(config: TracingConfig):
    """Build the appropriate span exporter based on configuration."""
    if config.exporter_type == "console":
        return ConsoleSpanExporter()

    if config.exporter_type == "otlp_grpc":
        return OTLPSpanExporter(
            endpoint=config.otlp_endpoint,
            insecure=config.environment == "development",
            headers={
                "x-scope-orgid": os.environ.get("OTEL_TENANT_ID", "migration"),
            },
        )

    if config.exporter_type == "otlp_http":
        return OTLPHTTPSpanExporter(
            endpoint=f"{config.otlp_endpoint}/v1/traces",
            headers={
                "x-scope-orgid": os.environ.get("OTEL_TENANT_ID", "migration"),
            },
        )

    raise ValueError(f"Unknown exporter type: {config.exporter_type}")


# ---------------------------------------------------------------------------
# Auto-Instrumentation
# ---------------------------------------------------------------------------

def setup_auto_instrumentation(
    app=None,  # FastAPI app
    db_engine=None,  # SQLAlchemy engine
    enable_kafka: bool = True,
    enable_redis: bool = True,
    enable_httpx: bool = True,
) -> None:
    """
    Configure automatic instrumentation for common libraries.

    Args:
        app: FastAPI application instance for HTTP instrumentation.
        db_engine: SQLAlchemy engine for DB query tracing.
        enable_kafka: Whether to instrument Kafka producers/consumers.
        enable_redis: Whether to instrument Redis calls.
        enable_httpx: Whether to instrument httpx HTTP client.
    """
    # FastAPI
    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="/health,/metrics,/ready",
            server_request_hook=_fastapi_server_request_hook,
            tracer_provider=trace.get_tracer_provider(),
        )
        logger.info("FastAPI auto-instrumentation configured")

    # SQLAlchemy
    if db_engine is not None:
        SQLAlchemyInstrumentor().instrument(
            engine=db_engine,
            service="legacy-database",
            enable_commenter=True,
        )
        logger.info("SQLAlchemy auto-instrumentation configured")

    # httpx
    if enable_httpx:
        HTTPXClientInstrumentor().instrument(
            tracer_provider=trace.get_tracer_provider(),
        )
        logger.info("httpx auto-instrumentation configured")

    # Kafka
    if enable_kafka:
        try:
            KafkaInstrumentor().instrument()
            logger.info("Kafka auto-instrumentation configured")
        except Exception as e:
            logger.warning(f"Kafka instrumentation failed (optional): {e}")

    # Redis
    if enable_redis:
        try:
            RedisInstrumentor().instrument()
            logger.info("Redis auto-instrumentation configured")
        except Exception as e:
            logger.warning(f"Redis instrumentation failed (optional): {e}")


def _fastapi_server_request_hook(span, scope: dict) -> None:
    """Hook to add custom attributes to FastAPI server spans."""
    if span and span.is_recording():
        headers = dict(scope.get("headers", []))
        correlation_id = headers.get(b"x-correlation-id", b"").decode()
        if correlation_id:
            span.set_attribute("migration.correlation_id", correlation_id)

        user_id = headers.get(b"x-user-id", b"").decode()
        if user_id:
            span.set_attribute("enduser.id", user_id)


# ---------------------------------------------------------------------------
# Tracer factory
# ---------------------------------------------------------------------------

def get_tracer(name: str) -> trace.Tracer:
    """Get a tracer for the given instrumentation scope."""
    return trace.get_tracer(name, tracer_provider=trace.get_tracer_provider())


# Application-level tracer
_migration_tracer = None


def migration_tracer() -> trace.Tracer:
    """Get the migration platform tracer."""
    global _migration_tracer
    if _migration_tracer is None:
        _migration_tracer = get_tracer(INSTRUMENTATION_LIBRARY)
    return _migration_tracer


# ---------------------------------------------------------------------------
# Span context managers and decorators
# ---------------------------------------------------------------------------

@contextmanager
def migration_span(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
) -> Generator[trace.Span, None, None]:
    """
    Context manager for creating a migration-specific span.

    Usage:
        with migration_span("extract_accounts", attributes={"object": "Account"}) as span:
            span.set_attribute("records.count", 1000)
            records = await extract_accounts()
    """
    tracer = migration_tracer()
    with tracer.start_as_current_span(name, kind=kind) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, str(value))
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


@contextmanager
def migration_job_span(
    migration_id: str,
    phase: str,
    source_object: str,
    environment: str = "production",
) -> Generator[trace.Span, None, None]:
    """Span for a complete migration job phase."""
    with migration_span(
        f"migration.{phase}",
        kind=SpanKind.INTERNAL,
        attributes={
            "migration.id": migration_id,
            "migration.phase": phase,
            "migration.source_object": source_object,
            "deployment.environment": environment,
        },
    ) as span:
        yield span


@contextmanager
def salesforce_api_span(
    operation: str,
    object_name: str,
    api_type: str = "rest",
) -> Generator[trace.Span, None, None]:
    """Span for a Salesforce API call."""
    with migration_span(
        f"salesforce.{api_type}.{operation}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "salesforce",
            "db.operation": operation,
            "db.name": object_name,
            "salesforce.api_type": api_type,
            "peer.service": "salesforce",
        },
    ) as span:
        yield span


@contextmanager
def db_query_span(
    operation: str,
    table: str,
    db_name: str = "legacy_db",
) -> Generator[trace.Span, None, None]:
    """Span for a database query."""
    with migration_span(
        f"db.{operation}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system": "mssql",
            "db.name": db_name,
            "db.operation": operation,
            "db.sql.table": table,
            "peer.service": "legacy-database",
        },
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Trace ID extraction for correlation with logs
# ---------------------------------------------------------------------------

def get_current_trace_id() -> str | None:
    """Get the current trace ID as a hex string, or None if not in a span."""
    current_span = trace.get_current_span()
    ctx = current_span.get_span_context()
    if ctx and ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return None


def get_current_span_id() -> str | None:
    """Get the current span ID as a hex string."""
    current_span = trace.get_current_span()
    ctx = current_span.get_span_context()
    if ctx and ctx.is_valid:
        return format(ctx.span_id, "016x")
    return None


def get_trace_context() -> dict[str, str | None]:
    """Return trace context for log correlation."""
    return {
        "trace_id": get_current_trace_id(),
        "span_id": get_current_span_id(),
    }

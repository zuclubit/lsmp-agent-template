"""
FastAPI application entry point for the Migration API Gateway.

Startup sequence
----------------
1. Initialise structured logging (JSON in production, pretty in development)
2. Load environment-specific settings via pydantic-settings
3. Register middleware stack (correlation IDs → logging → rate limiting → JWT)
4. Mount routers (health, migrations, ...)
5. Register dependency health checks
6. Start background services (OutboxRelay, metrics server)
7. Configure OpenAPI metadata

Run locally:
    uvicorn integrations.api_gateway.main:app --reload --port 8000

Production (gunicorn + uvicorn workers):
    gunicorn integrations.api_gateway.main:app \
        -k uvicorn.workers.UvicornWorker \
        -w 4 --bind 0.0.0.0:8000
"""

from __future__ import annotations

import logging
import logging.config
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from integrations.api_gateway.middleware import (
    CorrelationIDMiddleware,
    JWTAuthMiddleware,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
)
from integrations.api_gateway.routes.health_routes import (
    Criticality,
    DependencyCheck,
    _check_database,
    _check_kafka,
    _check_redis,
    _check_salesforce,
    register_dependency,
    router as health_router,
)
from integrations.api_gateway.routes.migration_routes import router as migration_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    environment = os.getenv("ENVIRONMENT", "production")
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    if environment == "development":
        fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
        logging.basicConfig(level=log_level, format=fmt)
    else:
        # JSON structured logging for log aggregation pipelines
        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "json": {
                        "()": "pythonjsonlogger.jsonlogger.JsonFormatter",  # type: ignore[attr-defined]
                        "fmt": "%(asctime)s %(name)s %(levelname)s %(message)s",
                        "rename_fields": {"asctime": "timestamp", "levelname": "level"},
                    }
                },
                "handlers": {
                    "console": {
                        "class": "logging.StreamHandler",
                        "formatter": "json",
                        "stream": "ext://sys.stdout",
                    }
                },
                "root": {"level": log_level, "handlers": ["console"]},
                "loggers": {
                    "uvicorn": {"level": "WARNING", "propagate": True},
                    "uvicorn.access": {"level": "WARNING", "propagate": True},
                },
            }
        )


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of background services."""
    configure_logging()
    logger.info(
        "Migration API Gateway starting up version=%s env=%s",
        os.getenv("APP_VERSION", "1.0.0"),
        os.getenv("ENVIRONMENT", "production"),
    )

    # Register dependency health checks
    _register_health_checks()

    # Start outbox relay (background task)
    import asyncio
    outbox_task = asyncio.create_task(_start_outbox_relay())

    logger.info("Migration API Gateway ready to serve requests")
    yield

    # Shutdown
    logger.info("Migration API Gateway shutting down")
    outbox_task.cancel()
    try:
        await outbox_task
    except asyncio.CancelledError:
        pass
    logger.info("Migration API Gateway shutdown complete")


def _register_health_checks() -> None:
    """Register all downstream dependency checks."""
    register_dependency(DependencyCheck(
        name="salesforce",
        check_fn=_check_salesforce,
        criticality=Criticality.CRITICAL,
        timeout_seconds=10.0,
    ))
    register_dependency(DependencyCheck(
        name="database",
        check_fn=_check_database,
        criticality=Criticality.CRITICAL,
        timeout_seconds=5.0,
    ))
    register_dependency(DependencyCheck(
        name="redis",
        check_fn=_check_redis,
        criticality=Criticality.CRITICAL,
        timeout_seconds=3.0,
    ))
    register_dependency(DependencyCheck(
        name="kafka",
        check_fn=_check_kafka,
        criticality=Criticality.NON_CRITICAL,
        timeout_seconds=5.0,
    ))


async def _start_outbox_relay() -> None:
    """Start the transactional outbox relay in the background."""
    try:
        from integrations.message_queues.event_publisher import (
            InMemoryOutboxStore,
            OutboxRelay,
        )

        store = InMemoryOutboxStore()

        # Adapter is selected based on environment variable
        broker = os.getenv("MESSAGE_BROKER", "memory")
        if broker == "azure_service_bus":
            from integrations.message_queues.event_publisher import AzureServiceBusAdapter
            adapter = AzureServiceBusAdapter(os.environ["AZURE_SERVICEBUS_CONNECTION_STRING"])
        elif broker == "aws_sqs":
            from integrations.message_queues.event_publisher import AWSSQSAdapter
            adapter = AWSSQSAdapter(region=os.getenv("AWS_REGION", "us-east-1"))
        else:
            # No-op adapter for local development
            from integrations.message_queues.event_publisher import BrokerAdapter

            class _NoOpAdapter(BrokerAdapter):
                async def send_batch(self, topic: str, messages: Any) -> None:
                    logger.debug("NoOp broker: %d messages for topic=%s", len(messages), topic)

                async def close(self) -> None:
                    pass

            adapter = _NoOpAdapter()

        relay = OutboxRelay(store=store, adapter=adapter, poll_interval_seconds=5.0)
        await relay.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("Outbox relay failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """
    Construct and configure the FastAPI application.

    Separating construction into a factory makes the app easier to test
    (instantiate a fresh app per test, no shared global state).
    """
    app = FastAPI(
        title="Migration API Gateway",
        description=(
            "Enterprise API gateway for the Legacy-to-Salesforce migration platform.\n\n"
            "Provides REST endpoints for:\n"
            "- Controlling migration runs (start, pause, resume, cancel)\n"
            "- Querying migration status and error reports\n"
            "- Health checks for Kubernetes probes\n"
            "- Retrying failed record batches\n\n"
            "All endpoints (except /health/*) require a valid JWT Bearer token."
        ),
        version=os.getenv("APP_VERSION", "1.0.0"),
        contact={
            "name": "Platform Engineering",
            "email": "platform@example.com",
        },
        license_info={"name": "Proprietary"},
        lifespan=lifespan,
        docs_url="/docs" if os.getenv("ENABLE_SWAGGER", "true").lower() == "true" else None,
        redoc_url="/redoc" if os.getenv("ENABLE_REDOC", "true").lower() == "true" else None,
    )

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    allowed_origins = os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,https://admin.example.com"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-ID", "X-RateLimit-Remaining"],
    )

    # ------------------------------------------------------------------
    # Custom middleware (applied last-to-first, i.e. outermost → innermost)
    # ------------------------------------------------------------------
    app.add_middleware(JWTAuthMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        window_seconds=60,
        max_requests=int(os.getenv("RATE_LIMIT_RPM", "200")),
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIDMiddleware)

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    app.include_router(health_router)
    app.include_router(migration_router, prefix="/api/v1")

    # ------------------------------------------------------------------
    # Exception handlers
    # ------------------------------------------------------------------
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        corr_id = getattr(request.state, "correlation_id", "")
        logger.warning(
            "Request validation error path=%s corr=%s errors=%s",
            request.url.path,
            corr_id,
            exc.errors(),
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Request body or parameters are invalid",
                "details": exc.errors(),
                "correlation_id": corr_id,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        corr_id = getattr(request.state, "correlation_id", "")
        logger.error(
            "Unhandled exception path=%s corr=%s",
            request.url.path,
            corr_id,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "message": "An unexpected error occurred",
                "correlation_id": corr_id,
            },
        )

    return app


# ---------------------------------------------------------------------------
# Application instance (module-level for uvicorn / gunicorn)
# ---------------------------------------------------------------------------

app = create_app()

"""
Structured Logging Configuration — Legacy to Salesforce Migration
=================================================================
Uses structlog for structured JSON logging with:
  - Correlation ID propagation via contextvars
  - Sensitive field sanitization
  - Multiple output formats (JSON prod, colored dev)
  - Log level management
  - OpenTelemetry trace correlation

Author: Platform Engineering Team
Version: 1.0.0
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, Processor

# ---------------------------------------------------------------------------
# Context Variables for Request Correlation
# ---------------------------------------------------------------------------

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
migration_id_var: ContextVar[str] = ContextVar("migration_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
span_id_var: ContextVar[str] = ContextVar("span_id", default="")


def get_correlation_id() -> str:
    cid = correlation_id_var.get()
    if not cid:
        cid = str(uuid.uuid4())
        correlation_id_var.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    correlation_id_var.set(cid)


def set_request_context(
    correlation_id: str | None = None,
    request_id: str | None = None,
    user_id: str | None = None,
    migration_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> None:
    """Set request context for all log messages in this async context."""
    if correlation_id:
        correlation_id_var.set(correlation_id)
    if request_id:
        request_id_var.set(request_id)
    if user_id:
        user_id_var.set(user_id)
    if migration_id:
        migration_id_var.set(migration_id)
    if trace_id:
        trace_id_var.set(trace_id)
    if span_id:
        span_id_var.set(span_id)


# ---------------------------------------------------------------------------
# Sensitive Field Patterns
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "api_key", "apikey", "client_secret", "authorization", "auth",
    "ssn", "social_security_number", "tax_id", "credit_card", "card_number",
    "cvv", "bank_account", "routing_number", "private_key",
    "x-api-key", "x-auth-token", "cookie", "set-cookie",
    "session_id", "session_token",
})

SENSITIVE_VALUE_PREFIXES = ("eyJ", "Bearer ", "Token ", "Basic ")  # JWTs, Bearer tokens


def is_sensitive(key: str) -> bool:
    return key.lower().replace("-", "_") in SENSITIVE_KEYS


def sanitize_value(key: str, value: Any) -> Any:
    """Redact sensitive values before logging."""
    if not isinstance(value, (str, bytes)):
        return value
    str_val = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
    if is_sensitive(key):
        return "[REDACTED]"
    if any(str_val.startswith(prefix) for prefix in SENSITIVE_VALUE_PREFIXES):
        return "[REDACTED_TOKEN]"
    # JWT pattern
    if str_val.startswith("eyJ") and str_val.count(".") >= 2:
        return "[REDACTED_JWT]"
    return value


# ---------------------------------------------------------------------------
# Custom Structlog Processors
# ---------------------------------------------------------------------------

def add_correlation_id(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Add correlation ID from context variable to every log event."""
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def add_request_context(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Add full request context to log event."""
    req_id = request_id_var.get()
    if req_id:
        event_dict["request_id"] = req_id

    uid = user_id_var.get()
    if uid:
        event_dict["user_id"] = uid

    mid = migration_id_var.get()
    if mid:
        event_dict["migration_id"] = mid

    tid = trace_id_var.get()
    if tid:
        event_dict["trace_id"] = tid

    sid = span_id_var.get()
    if sid:
        event_dict["span_id"] = sid

    return event_dict


def add_service_context(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Add service-level context to every log event."""
    event_dict.setdefault("service", os.environ.get("SERVICE_NAME", "migration-platform"))
    event_dict.setdefault("version", os.environ.get("SERVICE_VERSION", "unknown"))
    event_dict.setdefault("environment", os.environ.get("ENVIRONMENT", "development"))
    event_dict.setdefault("host", os.environ.get("HOSTNAME", "unknown"))
    event_dict.setdefault("namespace", os.environ.get("POD_NAMESPACE", "unknown"))
    return event_dict


def sanitize_sensitive_fields(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Redact sensitive fields from log events."""
    sanitized: dict[str, Any] = {}
    for key, value in event_dict.items():
        if is_sensitive(key):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = {
                k: sanitize_value(k, v) for k, v in value.items()
            }
        else:
            sanitized[key] = sanitize_value(key, value)
    return sanitized


def truncate_long_values(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """Truncate excessively long string values to prevent log flooding."""
    MAX_STR_LEN = 2000
    for key, value in event_dict.items():
        if isinstance(value, str) and len(value) > MAX_STR_LEN:
            event_dict[key] = value[:MAX_STR_LEN] + f"...[truncated, original_len={len(value)}]"
    return event_dict


def add_log_sampling(logger: Any, method: str, event_dict: EventDict) -> EventDict:
    """
    Optionally sample high-volume DEBUG logs to reduce noise.

    Controlled by LOG_SAMPLE_RATE environment variable (0.0-1.0).
    0.1 = keep 10% of DEBUG events. INFO and above always kept.
    """
    if method != "debug":
        return event_dict

    sample_rate = float(os.environ.get("LOG_DEBUG_SAMPLE_RATE", "1.0"))
    if sample_rate >= 1.0:
        return event_dict

    import random
    if random.random() > sample_rate:
        raise structlog.DropEvent()

    return event_dict


# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------

def _get_log_level() -> int:
    level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return level_map.get(level_str, logging.INFO)


def _is_json_output() -> bool:
    """Return True if JSON output should be used (production)."""
    env = os.environ.get("ENVIRONMENT", "development").lower()
    fmt = os.environ.get("LOG_FORMAT", "").lower()
    if fmt == "json":
        return True
    if fmt == "text":
        return False
    return env in ("production", "staging", "prod", "stage")


def configure_logging(
    log_level: int | None = None,
    json_output: bool | None = None,
    additional_processors: list[Processor] | None = None,
) -> None:
    """
    Configure structlog and standard library logging.

    Call once at application startup.

    Args:
        log_level: Logging level (e.g., logging.INFO). Defaults to LOG_LEVEL env var.
        json_output: If True, output JSON. Defaults to auto-detect from ENVIRONMENT.
        additional_processors: Extra processors to add to the chain.
    """
    if log_level is None:
        log_level = _get_log_level()
    if json_output is None:
        json_output = _is_json_output()

    # Standard stdlib logging configuration
    stdlib_logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.processors.JSONRenderer(),
                "foreign_pre_chain": _get_pre_chain(),
            },
            "colored": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.dev.ConsoleRenderer(colors=True),
                "foreign_pre_chain": _get_pre_chain(),
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json" if json_output else "colored",
                "level": log_level,
            },
        },
        "root": {
            "handlers": ["stdout"],
            "level": log_level,
        },
        "loggers": {
            # Reduce noise from chatty libraries
            "uvicorn": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "boto3": {"level": "WARNING"},
            "botocore": {"level": "WARNING"},
            "azure": {"level": "WARNING"},
            "kafka": {"level": "WARNING"},
            "sqlalchemy.engine": {"level": "WARNING"},
            # Our application loggers
            "migration": {"level": log_level},
            "security": {"level": log_level},
        },
    }

    logging.config.dictConfig(stdlib_logging_config)

    # Structlog configuration
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_correlation_id,
        add_request_context,
        add_service_context,
        sanitize_sensitive_fields,
        truncate_long_values,
        add_log_sampling,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    if additional_processors:
        # Insert before the wrap_for_formatter
        processors = processors[:-1] + additional_processors + processors[-1:]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _get_pre_chain() -> list[Processor]:
    """Return the processor chain for stdlib log records passed through structlog."""
    return [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_correlation_id,
        add_service_context,
        sanitize_sensitive_fields,
    ]


# ---------------------------------------------------------------------------
# FastAPI Request Logging Middleware
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware:
    """
    ASGI middleware for structured request/response logging.

    Adds correlation ID to every request and logs request details.
    """

    def __init__(self, app, exclude_paths: list[str] | None = None) -> None:
        self.app = app
        self.exclude_paths = set(exclude_paths or ["/health", "/metrics", "/ready"])
        self._logger = structlog.get_logger("migration.http")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        import time

        # Extract or generate correlation ID
        headers = dict(scope.get("headers", []))
        correlation_id = (
            headers.get(b"x-correlation-id", b"").decode()
            or headers.get(b"x-request-id", b"").decode()
            or str(uuid.uuid4())
        )

        request_id = str(uuid.uuid4())
        set_request_context(
            correlation_id=correlation_id,
            request_id=request_id,
        )

        method = scope.get("method", "UNKNOWN")
        query = scope.get("query_string", b"").decode()
        client = scope.get("client", ("unknown", 0))
        client_ip = client[0] if client else "unknown"

        start_time = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000
            log_method = self._logger.warning if status_code >= 400 else self._logger.info

            log_method(
                "http_request",
                method=method,
                path=path,
                query=query if query else None,
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
                client_ip=client_ip,
                correlation_id=correlation_id,
                request_id=request_id,
            )


# ---------------------------------------------------------------------------
# Convenience function for getting a logger
# ---------------------------------------------------------------------------

def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structlog logger bound to the given name."""
    return structlog.get_logger(name)

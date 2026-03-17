"""
MCP Access Logging Middleware — observability layer for all MCP server interactions.

Provides:
  - MCPAccessLogger: logs every MCP server request with full context
  - MCPAccessMetrics: Prometheus counters and histograms for MCP traffic
  - MCPAccessLogger.wrap_server(server): adds transparent logging middleware
    to any MCP server object without modifying the server implementation

Every request log entry includes:
  server_name, agent_name, operation, resource_path, allowed,
  denial_reason, response_size_bytes, duration_ms

Compliance: FedRAMP AU-2 (Auditable Events), AU-12 (Audit Generation).
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from monitoring.structured_audit_log import (
    AuditEntry,
    AuditEventType,
    AuditLogger,
    _get_default_logger,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Prometheus metrics
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Histogram, REGISTRY
    _PROMETHEUS_AVAILABLE = True

    _mcp_requests_total = Counter(
        "mcp_requests_total",
        "Total MCP server requests by server, agent, operation, and result",
        ["server", "agent", "operation", "result"],
    )
    _mcp_denied_total = Counter(
        "mcp_denied_total",
        "Total MCP requests that were denied",
        ["server", "agent", "operation", "denial_reason_code"],
    )
    _mcp_response_size_bytes = Histogram(
        "mcp_response_size_bytes",
        "Size of MCP server responses in bytes",
        ["server", "operation"],
        buckets=[64, 256, 1024, 4096, 16384, 65536, 262144, 1048576],
    )
    _mcp_request_duration_ms = Histogram(
        "mcp_request_duration_ms",
        "MCP request duration in milliseconds",
        ["server", "operation", "result"],
        buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
    )
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    _mcp_requests_total = None
    _mcp_denied_total = None
    _mcp_response_size_bytes = None
    _mcp_request_duration_ms = None


# ---------------------------------------------------------------------------
# MCPAccessRecord dataclass
# ---------------------------------------------------------------------------


@dataclass
class MCPAccessRecord:
    """
    Structured record for a single MCP server access event.

    Sensitive fields are stored as hashes or omitted entirely.
    resource_path is stored as a hash to avoid logging file paths that
    might contain partial PII or classified path components.
    """
    server_name: str
    agent_name: str
    operation: str
    resource_path_hash: str     # SHA-256 of resource_path — never raw path
    resource_path_prefix: str   # First 32 chars (safe for most paths)
    allowed: bool
    denial_reason: str
    response_size_bytes: int
    duration_ms: int
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    trace_id: str = ""
    tenant_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# MCPAccessMetrics
# ---------------------------------------------------------------------------


class MCPAccessMetrics:
    """
    Prometheus metrics for MCP server traffic.

    Counters:
      - mcp_requests_total{server, agent, operation, result}
      - mcp_denied_total{server, agent, operation, denial_reason_code}

    Histograms:
      - mcp_response_size_bytes{server, operation}
      - mcp_request_duration_ms{server, operation, result}
    """

    def record(
        self,
        server_name: str,
        agent_name: str,
        operation: str,
        allowed: bool,
        denial_reason: str,
        response_size_bytes: int,
        duration_ms: float,
    ) -> None:
        """Record metrics for a single MCP request."""
        if not _PROMETHEUS_AVAILABLE:
            return

        result = "ALLOW" if allowed else "DENY"

        try:
            if _mcp_requests_total:
                _mcp_requests_total.labels(
                    server=server_name,
                    agent=agent_name,
                    operation=operation,
                    result=result,
                ).inc()

            if not allowed and _mcp_denied_total:
                # Extract a short denial code (first word of denial_reason)
                denial_code = denial_reason.split(":")[0][:32] if denial_reason else "UNKNOWN"
                _mcp_denied_total.labels(
                    server=server_name,
                    agent=agent_name,
                    operation=operation,
                    denial_reason_code=denial_code,
                ).inc()

            if response_size_bytes > 0 and _mcp_response_size_bytes:
                _mcp_response_size_bytes.labels(
                    server=server_name,
                    operation=operation,
                ).observe(response_size_bytes)

            if _mcp_request_duration_ms:
                _mcp_request_duration_ms.labels(
                    server=server_name,
                    operation=operation,
                    result=result,
                ).observe(duration_ms)

        except Exception as exc:
            # Metrics must never break the main execution path
            logger.debug("Prometheus metric recording failed: %s", exc)


# Singleton metrics instance
_metrics = MCPAccessMetrics()


# ---------------------------------------------------------------------------
# MCPAccessLogger
# ---------------------------------------------------------------------------


class MCPAccessLogger:
    """
    Logs every MCP server request with full observability context.

    Features:
    - Structured JSONL log (appended to mcp_access.jsonl)
    - Prometheus metrics via MCPAccessMetrics
    - Integration with structured_audit_log.AuditLogger for tamper-evident records
    - Thread-safe (RLock)

    Usage (direct)::

        mcp_logger = MCPAccessLogger()
        mcp_logger.log(
            server_name="filesystem-server",
            agent_name="security-agent",
            operation="read_file",
            resource_path="./docs/api/openapi.yaml",
            allowed=True,
            denial_reason="",
            response_size_bytes=4096,
            duration_ms=12,
        )

    Usage (middleware)::

        mcp_logger = MCPAccessLogger()
        logged_server = mcp_logger.wrap_server(my_mcp_server)
        # All calls to logged_server.handle() are transparently logged
    """

    def __init__(
        self,
        log_path: Path = Path(".audit/mcp_access.jsonl"),
        audit_logger: Optional[AuditLogger] = None,
        metrics: Optional[MCPAccessMetrics] = None,
    ) -> None:
        self._log_path = log_path
        self._audit_logger = audit_logger or _get_default_logger()
        self._metrics = metrics or _metrics
        self._lock = threading.RLock()

        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        server_name: str,
        agent_name: str,
        operation: str,
        resource_path: str,
        allowed: bool,
        denial_reason: str = "",
        response_size_bytes: int = 0,
        duration_ms: int = 0,
        trace_id: str = "",
        tenant_id: str = "",
    ) -> MCPAccessRecord:
        """
        Log a single MCP access event.

        Returns the MCPAccessRecord for downstream correlation.
        """
        resource_path_hash = hashlib.sha256(resource_path.encode("utf-8")).hexdigest()
        # Safe prefix: first 32 chars only (avoids logging full paths with PII)
        resource_path_prefix = resource_path[:32]

        record = MCPAccessRecord(
            server_name=server_name,
            agent_name=agent_name,
            operation=operation,
            resource_path_hash=resource_path_hash,
            resource_path_prefix=resource_path_prefix,
            allowed=allowed,
            denial_reason=denial_reason,
            response_size_bytes=response_size_bytes,
            duration_ms=duration_ms,
            trace_id=trace_id,
            tenant_id=tenant_id,
        )

        # 1. Write to local JSONL
        with self._lock:
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(record.to_json() + "\n")
                    fh.flush()
            except OSError as exc:
                logger.error("MCP access log write failed: %s", exc)

        # 2. Update Prometheus metrics
        self._metrics.record(
            server_name=server_name,
            agent_name=agent_name,
            operation=operation,
            allowed=allowed,
            denial_reason=denial_reason,
            response_size_bytes=response_size_bytes,
            duration_ms=float(duration_ms),
        )

        # 3. Write to tamper-evident audit log (denials only — for security audit trail)
        if not allowed:
            try:
                audit_entry = AuditEntry(
                    event_type=AuditEventType.MCP_ACCESS,
                    timestamp_utc=record.timestamp_utc,
                    trace_id=trace_id or _generate_trace_id(),
                    agent_name=agent_name,
                    tenant_id=tenant_id or "unknown",
                    job_id="unknown",
                    action=f"mcp:{server_name}:{operation}",
                    input_hash=resource_path_hash,
                    output_summary=(
                        f"DENIED: server={server_name} op={operation} "
                        f"reason={denial_reason[:200]}"
                    ),
                    result="DENY",
                    duration_ms=duration_ms,
                    rule_id=denial_reason.split(":")[0][:32] if denial_reason else None,
                    tool_calls=[],
                    gate_decisions=[],
                    tokens_used=0,
                )
                self._audit_logger.log(audit_entry)
            except Exception as exc:
                logger.warning("Failed to write MCP denial to audit log: %s", exc)

        return record

    def wrap_server(self, server: Any, server_name: str = "", agent_name: str = "") -> Any:
        """
        Add transparent logging middleware to an MCP server object.

        This wraps the server's `handle` method (or `handle_http` / `get_runbook` /
        `read` / `write` if present) so that every call is logged without modifying
        the server's implementation.

        Supports servers with any combination of:
          - handle(operation, resource_path, agent_name, ...)
          - handle_http(method, path, body, ...)
          - get_runbook(runbook_name, ...)
          - read(session_id, ...) / write(session_id, ...)

        Returns a wrapped proxy object.
        """
        resolved_server_name = server_name or getattr(server, "__class__", type(server)).__name__
        mcp_logger = self

        class _LoggingProxy:
            """Transparent proxy that logs all MCP server calls."""

            def __init__(self, wrapped: Any) -> None:
                self._wrapped = wrapped

            def __getattr__(self, name: str) -> Any:
                attr = getattr(self._wrapped, name)
                if not callable(attr) or name.startswith("_"):
                    return attr

                @functools.wraps(attr)
                def _logged_method(*args: Any, **kwargs: Any) -> Any:
                    start = time.monotonic()
                    # Determine resource path from arguments
                    resource_path = _extract_resource(name, args, kwargs)
                    caller_agent = agent_name or kwargs.get("agent_name", "unknown")

                    try:
                        result = attr(*args, **kwargs)
                        duration_ms = int((time.monotonic() - start) * 1000)
                        # Determine allowed/denied from result
                        allowed, denial_reason, response_size = _extract_result_metadata(result)
                        mcp_logger.log(
                            server_name=resolved_server_name,
                            agent_name=caller_agent,
                            operation=name,
                            resource_path=resource_path,
                            allowed=allowed,
                            denial_reason=denial_reason,
                            response_size_bytes=response_size,
                            duration_ms=duration_ms,
                        )
                        return result
                    except Exception as exc:
                        duration_ms = int((time.monotonic() - start) * 1000)
                        mcp_logger.log(
                            server_name=resolved_server_name,
                            agent_name=caller_agent,
                            operation=name,
                            resource_path=resource_path,
                            allowed=False,
                            denial_reason=f"EXCEPTION: {type(exc).__name__}",
                            response_size_bytes=0,
                            duration_ms=duration_ms,
                        )
                        raise

                return _logged_method

            def __repr__(self) -> str:
                return f"<MCPLoggingProxy wrapping {self._wrapped!r}>"

        return _LoggingProxy(server)


# ---------------------------------------------------------------------------
# Helpers for proxy metadata extraction
# ---------------------------------------------------------------------------


def _extract_resource(method_name: str, args: tuple, kwargs: dict) -> str:
    """
    Attempt to extract a human-readable resource identifier from method arguments.

    Falls back to the method name if no resource can be determined.
    """
    # Common keyword argument names that identify the resource
    for key in ("resource_path", "path", "runbook_name", "entry_key", "file_path"):
        if key in kwargs:
            return str(kwargs[key])

    # Positional argument heuristics by method name
    if method_name in ("handle", "handle_http") and len(args) >= 2:
        # handle(operation, resource_path, ...) or handle_http(method, path, ...)
        return str(args[1])
    if method_name == "get_runbook" and args:
        return str(args[0])
    if method_name in ("read", "write") and args:
        # read(session_id, agent_id, ..., entry_key) — return session_id as resource
        return str(args[0])

    return f"<{method_name}>"


def _extract_result_metadata(result: Any) -> tuple[bool, str, int]:
    """
    Extract (allowed, denial_reason, response_size_bytes) from an MCP response object.

    Supports:
    - Objects with .status, .denial_reason, .body attributes (MCPResponse)
    - Dicts with "status", "error" keys
    - Plain strings (treat as successful response)
    """
    # MCPResponse-like objects
    if hasattr(result, "status") and hasattr(result, "denial_reason"):
        status_value = result.status
        # Status codes: 200/201 = OK, 401/403/405/429 = denied
        allowed = int(status_value) in (200, 201)
        denial_reason = result.denial_reason if not allowed else ""
        body = result.body
        if body is not None:
            try:
                response_size = len(json.dumps(body, default=str).encode("utf-8"))
            except Exception:
                response_size = len(str(body).encode("utf-8"))
        else:
            response_size = 0
        return allowed, denial_reason, response_size

    # Dict response
    if isinstance(result, dict):
        allowed = "error" not in result
        denial_reason = result.get("error", "")
        response_size = len(json.dumps(result, default=str).encode("utf-8"))
        return allowed, denial_reason, response_size

    # String response
    if isinstance(result, str):
        allowed = True
        response_size = len(result.encode("utf-8"))
        return allowed, "", response_size

    # Default: treat as success
    return True, "", 0


def _generate_trace_id() -> str:
    import uuid
    return str(uuid.uuid4())

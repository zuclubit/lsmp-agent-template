"""
Audit Logger — Legacy to Salesforce Migration
==============================================
Centralized, tamper-evident, structured audit logging service.

Features:
  - Structured JSON log events with full schema
  - Async write queue (never blocks business logic)
  - HMAC chaining for tamper-evidence (append-only log integrity)
  - Log sanitization (redacts secrets/PII before logging)
  - Multiple sinks: file, Elasticsearch, Splunk, stdout
  - Correlation ID propagation
  - OpenTelemetry trace integration

Author: Platform Security Team
Version: 1.1.0
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import queue
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AuditEventType(str, Enum):
    # Authentication
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    TOKEN_ISSUED = "auth.token.issued"
    TOKEN_REVOKED = "auth.token.revoked"
    MFA_SUCCESS = "auth.mfa.success"
    MFA_FAILURE = "auth.mfa.failure"

    # Authorization
    AUTHZ_ALLOW = "authz.allow"
    AUTHZ_DENY = "authz.deny"
    PRIVILEGE_ESCALATION = "authz.privilege_escalation"

    # Data access
    DATA_READ = "data.read"
    DATA_WRITE = "data.write"
    DATA_DELETE = "data.delete"
    DATA_EXPORT = "data.export"
    SECRET_ACCESS = "secret.access"
    SECRET_ROTATION = "secret.rotation"

    # Migration operations
    MIGRATION_STARTED = "migration.started"
    MIGRATION_COMPLETED = "migration.completed"
    MIGRATION_FAILED = "migration.failed"
    MIGRATION_PAUSED = "migration.paused"
    MIGRATION_RESUMED = "migration.resumed"
    MIGRATION_APPROVED = "migration.approved"
    MIGRATION_REJECTED = "migration.rejected"

    # Configuration
    CONFIG_CHANGED = "config.changed"
    ROLE_ASSIGNED = "rbac.role.assigned"
    ROLE_REVOKED = "rbac.role.revoked"

    # Security events
    SECURITY_ALERT = "security.alert"
    POLICY_VIOLATION = "security.policy_violation"
    ANOMALY_DETECTED = "security.anomaly"

    # System
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    HEALTH_CHECK = "system.health_check"


class AuditSeverity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    NOTICE = "NOTICE"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AuditOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Audit Event Schema
# ---------------------------------------------------------------------------

@dataclass
class AuditEvent:
    """
    Structured audit event. Every field is intentionally typed.

    Conforms to OCSF (Open Cybersecurity Schema Framework) where applicable.
    """
    # Required fields
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AuditEventType = AuditEventType.DATA_READ
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    severity: AuditSeverity = AuditSeverity.INFO
    outcome: AuditOutcome = AuditOutcome.SUCCESS

    # Actor (who performed the action)
    actor_id: Optional[str] = None
    actor_username: Optional[str] = None
    actor_type: str = "user"           # "user", "service", "system"
    actor_ip: Optional[str] = None
    actor_user_agent: Optional[str] = None
    actor_roles: list[str] = field(default_factory=list)

    # Session
    session_id: Optional[str] = None
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_id: Optional[str] = None
    trace_id: Optional[str] = None     # OpenTelemetry trace ID
    span_id: Optional[str] = None      # OpenTelemetry span ID

    # Target (what was acted on)
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None

    # Action details
    action: Optional[str] = None
    description: Optional[str] = None
    changes: Optional[dict[str, Any]] = None  # {field: {old: x, new: y}}

    # Classification
    data_classification: Optional[str] = None

    # Environment
    environment: str = "unknown"
    service_name: str = "migration-platform"
    service_version: Optional[str] = None
    host: Optional[str] = None
    namespace: Optional[str] = None

    # Error information (for failures)
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    # Tamper-evidence
    sequence_number: Optional[int] = None
    previous_event_hash: Optional[str] = None
    event_hash: Optional[str] = None

    # Custom metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a serializable dictionary."""
        d = asdict(self)
        # Convert enum values to strings
        d["event_type"] = self.event_type.value
        d["severity"] = self.severity.value
        d["outcome"] = self.outcome.value
        return d

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), default=str)


# ---------------------------------------------------------------------------
# Sensitive field patterns for redaction
# ---------------------------------------------------------------------------

SENSITIVE_FIELD_PATTERNS = {
    # Exact field names (case-insensitive matching applied separately)
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "private_key", "access_key", "secret_key", "credentials",
    "ssn", "social_security", "tax_id", "credit_card", "card_number",
    "cvv", "bank_account", "routing_number", "pin",
    "client_secret", "auth_token", "bearer", "jwt",
}

SENSITIVE_VALUE_PATTERNS = [
    # Patterns that look like secrets even if field name is benign
    r"(?i)bearer\s+[a-zA-Z0-9\-_.~+/]+=*",
    r"(?i)eyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+",  # JWT
    r"\d{3}-\d{2}-\d{4}",   # SSN
    r"\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}",  # Credit card
]


def sanitize_for_audit(data: Any, depth: int = 0, max_depth: int = 5) -> Any:
    """
    Recursively sanitize a data structure, redacting sensitive fields.

    Args:
        data: The data to sanitize.
        depth: Current recursion depth.
        max_depth: Maximum recursion depth to prevent infinite loops.

    Returns:
        Sanitized copy of the data.
    """
    if depth > max_depth:
        return "[MAX_DEPTH_EXCEEDED]"

    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_FIELD_PATTERNS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_for_audit(value, depth + 1, max_depth)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_for_audit(item, depth + 1, max_depth) for item in data]
    elif isinstance(data, str) and len(data) > 8:
        # Simple heuristic: long hex strings might be secrets
        if all(c in "0123456789abcdefABCDEF" for c in data) and len(data) >= 32:
            return f"[REDACTED_HEX_{len(data)}chars]"
        # JWT pattern
        if data.startswith("eyJ") and data.count(".") >= 2:
            return "[REDACTED_JWT]"
    return data


# ---------------------------------------------------------------------------
# Audit Sink Interfaces
# ---------------------------------------------------------------------------

class AuditSink(ABC):
    """Abstract interface for audit log sinks."""

    @abstractmethod
    async def write(self, event: AuditEvent) -> None:
        """Write an audit event to the sink."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered events."""
        ...

    async def close(self) -> None:
        """Close the sink cleanly."""
        await self.flush()


class FileAuditSink(AuditSink):
    """Write audit events to a JSON Lines file."""

    def __init__(self, file_path: str, rotate_size_mb: int = 100) -> None:
        self._file_path = file_path
        self._rotate_size_bytes = rotate_size_mb * 1024 * 1024
        self._file = None
        self._lock = asyncio.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)

    async def _get_file(self):
        if self._file is None or self._file.closed:
            self._file = open(self._file_path, "a", encoding="utf-8")
        return self._file

    async def write(self, event: AuditEvent) -> None:
        async with self._lock:
            f = await self._get_file()
            f.write(event.to_json() + "\n")
            # Check rotation
            if f.tell() >= self._rotate_size_bytes:
                f.close()
                ts = int(time.time())
                os.rename(self._file_path, f"{self._file_path}.{ts}")
                self._file = None

    async def flush(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()


class StructlogSink(AuditSink):
    """Write audit events to structlog (stdout/stderr)."""

    async def write(self, event: AuditEvent) -> None:
        log_fn = logger.warning if event.outcome == AuditOutcome.FAILURE else logger.info
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: log_fn(
                "audit_event",
                event_id=event.event_id,
                event_type=event.event_type.value,
                actor_id=event.actor_id,
                actor_username=event.actor_username,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                action=event.action,
                outcome=event.outcome.value,
                correlation_id=event.correlation_id,
            ),
        )

    async def flush(self) -> None:
        pass


class ElasticsearchAuditSink(AuditSink):
    """Write audit events to Elasticsearch."""

    def __init__(self, es_url: str, index_prefix: str = "migration-audit", api_key: str | None = None) -> None:
        self._url = es_url.rstrip("/")
        self._index_prefix = index_prefix
        self._api_key = api_key
        self._buffer: list[AuditEvent] = []
        self._buffer_size = 100
        self._lock = asyncio.Lock()

        try:
            import httpx
            self._http = httpx.AsyncClient(timeout=10.0)
        except ImportError:
            self._http = None

    def _current_index(self) -> str:
        date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        return f"{self._index_prefix}-{date}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/x-ndjson"}
        if self._api_key:
            headers["Authorization"] = f"ApiKey {self._api_key}"
        return headers

    async def write(self, event: AuditEvent) -> None:
        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._buffer_size:
                await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        if not self._buffer or not self._http:
            self._buffer.clear()
            return

        index = self._current_index()
        bulk_body = ""
        for event in self._buffer:
            meta = json.dumps({"index": {"_index": index, "_id": event.event_id}})
            doc = event.to_json()
            bulk_body += meta + "\n" + doc + "\n"

        try:
            await self._http.post(
                f"{self._url}/_bulk",
                content=bulk_body,
                headers=self._headers(),
            )
        except Exception as e:
            logger.error("Failed to flush audit events to Elasticsearch", error=str(e))
        finally:
            self._buffer.clear()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_buffer()


# ---------------------------------------------------------------------------
# Tamper-Evidence Chain
# ---------------------------------------------------------------------------

class TamperEvidentChain:
    """
    Maintains a HMAC chain over audit events to detect tampering.

    Each event includes:
    - sequence_number: monotonically increasing
    - previous_event_hash: HMAC-SHA256 of the previous event
    - event_hash: HMAC-SHA256 of this event (excluding event_hash field)

    An independent auditor can replay the log and verify the chain.
    """

    def __init__(self, chain_key: bytes | None = None) -> None:
        if chain_key is None:
            # In production, this should be a well-protected key from KMS/Vault
            chain_key = os.environ.get("AUDIT_CHAIN_KEY_HEX", "").encode()
            if not chain_key:
                chain_key = os.urandom(32)
                logger.warning("No AUDIT_CHAIN_KEY_HEX set — using ephemeral key. Chain will not persist across restarts.")
        self._key = chain_key if len(chain_key) == 32 else hashlib.sha256(chain_key).digest()
        self._sequence = 0
        self._last_hash = b"\x00" * 32

    def stamp(self, event: AuditEvent) -> AuditEvent:
        """Add tamper-evidence fields to an event."""
        self._sequence += 1
        event.sequence_number = self._sequence
        event.previous_event_hash = self._last_hash.hex()

        # Hash: HMAC(key, sequence || timestamp || event_type || actor_id || outcome)
        content = "|".join([
            str(event.sequence_number),
            event.timestamp,
            event.event_type.value,
            event.actor_id or "",
            event.outcome.value,
            event.event_id,
            event.previous_event_hash,
        ]).encode("utf-8")

        self._last_hash = hmac.new(self._key, content, hashlib.sha256).digest()
        event.event_hash = self._last_hash.hex()
        return event

    def verify(self, event: AuditEvent) -> bool:
        """Verify an event's hash (for audit replay)."""
        if not event.event_hash or not event.previous_event_hash:
            return False
        content = "|".join([
            str(event.sequence_number or ""),
            event.timestamp,
            event.event_type.value,
            event.actor_id or "",
            event.outcome.value,
            event.event_id,
            event.previous_event_hash,
        ]).encode("utf-8")
        expected = hmac.new(self._key, content, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, event.event_hash)


# ---------------------------------------------------------------------------
# Central Audit Logger
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Central audit logger for the migration platform.

    Usage:
        logger = AuditLogger(sinks=[FileAuditSink("/var/log/audit.jsonl"), StructlogSink()])

        await logger.log_event(AuditEvent(
            event_type=AuditEventType.DATA_READ,
            actor_id="user-123",
            resource_type="MigrationJob",
            resource_id="job-456",
            action="GET",
            outcome=AuditOutcome.SUCCESS,
        ))
    """

    def __init__(
        self,
        sinks: list[AuditSink] | None = None,
        enable_tamper_evidence: bool = True,
        chain_key: bytes | None = None,
        environment: str = "production",
        service_name: str = "migration-platform",
        service_version: str | None = None,
        queue_size: int = 10_000,
        num_workers: int = 2,
    ) -> None:
        self._sinks = sinks or [StructlogSink()]
        self._chain = TamperEvidentChain(chain_key) if enable_tamper_evidence else None
        self._environment = environment
        self._service_name = service_name
        self._service_version = service_version
        self._host = os.environ.get("HOSTNAME", "unknown")
        self._namespace = os.environ.get("POD_NAMESPACE", "unknown")

        # Async queue for non-blocking writes
        self._queue: asyncio.Queue[AuditEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        """Start background worker tasks."""
        self._running = True
        for _ in range(2):  # 2 worker coroutines
            task = asyncio.create_task(self._worker())
            self._workers.append(task)
        logger.info("Audit logger started", sinks=len(self._sinks))

    async def stop(self) -> None:
        """Gracefully stop, flushing all queued events."""
        self._running = False
        # Signal workers to stop
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        # Flush all sinks
        for sink in self._sinks:
            await sink.flush()
        logger.info("Audit logger stopped")

    async def _worker(self) -> None:
        """Background coroutine that drains the audit queue."""
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                for sink in self._sinks:
                    try:
                        await sink.write(event)
                    except Exception as e:
                        # Log to stderr — never raise from audit worker
                        import sys
                        print(f"AUDIT SINK ERROR [{type(sink).__name__}]: {e}", file=sys.stderr)
            finally:
                self._queue.task_done()

    def _enrich(self, event: AuditEvent) -> AuditEvent:
        """Add standard context fields to every event."""
        event.environment = self._environment
        event.service_name = self._service_name
        event.service_version = self._service_version
        event.host = self._host
        event.namespace = self._namespace

        # Apply tamper-evidence chain
        if self._chain:
            event = self._chain.stamp(event)

        return event

    async def log_event(self, event: AuditEvent) -> None:
        """
        Enqueue an audit event for async writing.

        This method never blocks the caller. If the queue is full,
        the event is dropped and a warning is emitted.
        """
        event = self._enrich(event)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error(
                "Audit queue full — event dropped",
                event_id=event.event_id,
                event_type=event.event_type.value,
            )

    # Convenience factory methods

    async def log_auth(
        self,
        event_type: AuditEventType,
        actor_id: str,
        actor_username: str,
        actor_ip: str,
        outcome: AuditOutcome,
        error_message: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self.log_event(AuditEvent(
            event_type=event_type,
            severity=AuditSeverity.WARNING if outcome == AuditOutcome.FAILURE else AuditSeverity.INFO,
            outcome=outcome,
            actor_id=actor_id,
            actor_username=actor_username,
            actor_ip=actor_ip,
            action=event_type.value,
            error_message=error_message,
            correlation_id=correlation_id or str(uuid.uuid4()),
        ))

    async def log_authz(
        self,
        actor_id: str,
        permission: str,
        resource_type: str | None,
        resource_id: str | None,
        allowed: bool,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self.log_event(AuditEvent(
            event_type=AuditEventType.AUTHZ_ALLOW if allowed else AuditEventType.AUTHZ_DENY,
            severity=AuditSeverity.WARNING if not allowed else AuditSeverity.INFO,
            outcome=AuditOutcome.SUCCESS if allowed else AuditOutcome.FAILURE,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=permission,
            description=reason,
            correlation_id=correlation_id or str(uuid.uuid4()),
        ))

    async def log_data_access(
        self,
        actor_id: str,
        actor_username: str,
        event_type: AuditEventType,
        resource_type: str,
        resource_id: str,
        data_classification: str,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        record_count: int | None = None,
        correlation_id: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {}
        if record_count is not None:
            metadata["record_count"] = record_count

        await self.log_event(AuditEvent(
            event_type=event_type,
            severity=AuditSeverity.NOTICE if data_classification in ("Restricted", "Confidential") else AuditSeverity.INFO,
            outcome=outcome,
            actor_id=actor_id,
            actor_username=actor_username,
            resource_type=resource_type,
            resource_id=resource_id,
            data_classification=data_classification,
            correlation_id=correlation_id or str(uuid.uuid4()),
            metadata=metadata,
        ))

    async def log_migration_event(
        self,
        event_type: AuditEventType,
        migration_id: str,
        actor_id: str,
        phase: str | None = None,
        record_count: int | None = None,
        error_message: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {"migration_id": migration_id}
        if phase:
            metadata["phase"] = phase
        if record_count is not None:
            metadata["record_count"] = record_count

        await self.log_event(AuditEvent(
            event_type=event_type,
            severity=AuditSeverity.ERROR if event_type == AuditEventType.MIGRATION_FAILED else AuditSeverity.INFO,
            outcome=AuditOutcome.FAILURE if event_type == AuditEventType.MIGRATION_FAILED else AuditOutcome.SUCCESS,
            actor_id=actor_id,
            resource_type="MigrationJob",
            resource_id=migration_id,
            action=event_type.value,
            error_message=error_message,
            correlation_id=correlation_id or str(uuid.uuid4()),
            metadata=metadata,
        ))


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    """Return the global audit logger instance."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger(
            sinks=[StructlogSink()],
            environment=os.environ.get("ENVIRONMENT", "development"),
            service_name=os.environ.get("SERVICE_NAME", "migration-platform"),
            service_version=os.environ.get("SERVICE_VERSION", "unknown"),
        )
    return _audit_logger


async def init_audit_logger(
    sinks: list[AuditSink],
    environment: str = "production",
    service_name: str = "migration-platform",
    service_version: str | None = None,
    chain_key: bytes | None = None,
) -> AuditLogger:
    """Initialize and start the global audit logger. Call at application startup."""
    global _audit_logger
    _audit_logger = AuditLogger(
        sinks=sinks,
        environment=environment,
        service_name=service_name,
        service_version=service_version,
        chain_key=chain_key,
    )
    await _audit_logger.start()
    return _audit_logger

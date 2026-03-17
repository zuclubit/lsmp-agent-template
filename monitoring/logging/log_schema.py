"""
Log Event Schema — Legacy to Salesforce Migration
==================================================
Pydantic models for structured log events.

These schemas serve as the canonical definition for:
  - Application log events
  - Audit events
  - Metric events
  - Migration lifecycle events

Author: Platform Engineering Team
Version: 1.0.0
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogEventCategory(str, Enum):
    HTTP = "http"
    AUTH = "auth"
    AUTHZ = "authz"
    DATA = "data"
    MIGRATION = "migration"
    SYSTEM = "system"
    SECURITY = "security"
    PERFORMANCE = "performance"
    AUDIT = "audit"


class MigrationPhase(str, Enum):
    EXTRACTION = "extraction"
    VALIDATION = "validation"
    TRANSFORMATION = "transformation"
    LOADING = "loading"
    VERIFICATION = "verification"
    ROLLBACK = "rollback"


class MigrationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------------------
# Base Log Event
# ---------------------------------------------------------------------------

class BaseLogEvent(BaseModel):
    """Base schema for all log events."""

    model_config = {"extra": "allow", "populate_by_name": True}

    # Identity
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    level: LogLevel = LogLevel.INFO
    category: LogEventCategory = LogEventCategory.SYSTEM

    # Message
    event: str = Field(..., description="Primary log message / event name")
    message: Optional[str] = Field(None, description="Human-readable description")

    # Service context
    service: str = Field(default="migration-platform")
    version: Optional[str] = None
    environment: str = Field(default="unknown")
    host: Optional[str] = None
    namespace: Optional[str] = None

    # Request correlation
    correlation_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None

    # Error details
    error: Optional[str] = None
    error_type: Optional[str] = None
    stack_trace: Optional[str] = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    def to_log_dict(self) -> dict[str, Any]:
        """Export as a flat dictionary for JSON logging."""
        data = self.model_dump(mode="json", exclude_none=True)
        data["timestamp"] = self.timestamp.isoformat()
        data["level"] = self.level.value
        data["category"] = self.category.value
        return data


# ---------------------------------------------------------------------------
# HTTP Request/Response Log Event
# ---------------------------------------------------------------------------

class HttpLogEvent(BaseLogEvent):
    """Log event for HTTP requests."""

    category: Literal[LogEventCategory.HTTP] = LogEventCategory.HTTP

    # Request
    method: str
    path: str
    query_string: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None

    # Response
    status_code: int
    response_size_bytes: Optional[int] = None
    duration_ms: float

    # Auth
    user_id: Optional[str] = None
    auth_method: Optional[str] = None

    @model_validator(mode="after")
    def set_level_from_status(self) -> "HttpLogEvent":
        if self.status_code >= 500:
            self.level = LogLevel.ERROR
        elif self.status_code >= 400:
            self.level = LogLevel.WARNING
        return self


# ---------------------------------------------------------------------------
# Authentication Log Event
# ---------------------------------------------------------------------------

class AuthLogEvent(BaseLogEvent):
    """Log event for authentication events."""

    category: Literal[LogEventCategory.AUTH] = LogEventCategory.AUTH

    action: str  # login, logout, token_issued, token_revoked, mfa_success, mfa_failure
    actor_id: Optional[str] = None
    actor_username: Optional[str] = None
    actor_ip: Optional[str] = None
    actor_user_agent: Optional[str] = None
    auth_method: Optional[str] = None  # oidc, jwt, apikey, k8s
    mfa_method: Optional[str] = None   # fido2, totp, push
    success: bool = True
    failure_reason: Optional[str] = None
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Authorization Log Event
# ---------------------------------------------------------------------------

class AuthzLogEvent(BaseLogEvent):
    """Log event for authorization decisions."""

    category: Literal[LogEventCategory.AUTHZ] = LogEventCategory.AUTHZ

    actor_id: str
    actor_roles: list[str] = Field(default_factory=list)
    permission_requested: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    decision: Literal["ALLOW", "DENY"]
    reason: Optional[str] = None
    matched_role: Optional[str] = None

    @model_validator(mode="after")
    def set_level_from_decision(self) -> "AuthzLogEvent":
        if self.decision == "DENY":
            self.level = LogLevel.WARNING
        return self


# ---------------------------------------------------------------------------
# Migration Operation Log Event
# ---------------------------------------------------------------------------

class MigrationLogEvent(BaseLogEvent):
    """Log event for migration job lifecycle events."""

    category: Literal[LogEventCategory.MIGRATION] = LogEventCategory.MIGRATION

    migration_id: str
    phase: Optional[MigrationPhase] = None
    status: Optional[MigrationStatus] = None
    actor_id: Optional[str] = None

    # Progress metrics
    records_total: Optional[int] = None
    records_processed: Optional[int] = None
    records_succeeded: Optional[int] = None
    records_failed: Optional[int] = None
    records_skipped: Optional[int] = None

    # Timing
    phase_start_time: Optional[datetime] = None
    phase_end_time: Optional[datetime] = None
    phase_duration_seconds: Optional[float] = None

    # Throughput
    records_per_second: Optional[float] = None
    bytes_processed: Optional[int] = None

    # Batch info
    batch_id: Optional[str] = None
    batch_size: Optional[int] = None
    batch_number: Optional[int] = None
    total_batches: Optional[int] = None

    # Source/target
    source_object: Optional[str] = None   # e.g., "Account", "Contact"
    target_object: Optional[str] = None   # Salesforce object
    salesforce_job_id: Optional[str] = None

    @model_validator(mode="after")
    def set_level_from_status(self) -> "MigrationLogEvent":
        if self.status == MigrationStatus.FAILED:
            self.level = LogLevel.ERROR
        elif self.status == MigrationStatus.COMPLETED:
            self.level = LogLevel.INFO
        return self

    @model_validator(mode="after")
    def compute_throughput(self) -> "MigrationLogEvent":
        if self.phase_duration_seconds and self.records_processed and self.phase_duration_seconds > 0:
            self.records_per_second = round(self.records_processed / self.phase_duration_seconds, 2)
        return self


# ---------------------------------------------------------------------------
# Data Access Log Event
# ---------------------------------------------------------------------------

class DataAccessLogEvent(BaseLogEvent):
    """Log event for data access (for audit trail)."""

    category: Literal[LogEventCategory.DATA] = LogEventCategory.DATA

    operation: str  # read, write, delete, export, import
    actor_id: str
    actor_username: Optional[str] = None
    resource_type: str
    resource_id: Optional[str] = None
    data_classification: Optional[str] = None

    # Scope
    record_count: Optional[int] = None
    fields_accessed: Optional[list[str]] = None

    # Result
    success: bool = True
    rows_affected: Optional[int] = None

    # Source/destination
    source_system: Optional[str] = None
    target_system: Optional[str] = None


# ---------------------------------------------------------------------------
# Performance Log Event
# ---------------------------------------------------------------------------

class PerformanceLogEvent(BaseLogEvent):
    """Log event for performance measurements."""

    category: Literal[LogEventCategory.PERFORMANCE] = LogEventCategory.PERFORMANCE

    operation: str
    duration_ms: float
    success: bool = True

    # Resource usage
    cpu_percent: Optional[float] = None
    memory_mb: Optional[float] = None

    # External calls
    db_query_count: Optional[int] = None
    db_duration_ms: Optional[float] = None
    api_call_count: Optional[int] = None
    api_duration_ms: Optional[float] = None
    cache_hits: Optional[int] = None
    cache_misses: Optional[int] = None

    # SLA tracking
    sla_threshold_ms: Optional[float] = None
    sla_breached: Optional[bool] = None

    @model_validator(mode="after")
    def check_sla(self) -> "PerformanceLogEvent":
        if self.sla_threshold_ms is not None:
            self.sla_breached = self.duration_ms > self.sla_threshold_ms
            if self.sla_breached:
                self.level = LogLevel.WARNING
        return self


# ---------------------------------------------------------------------------
# Security Event Log
# ---------------------------------------------------------------------------

class SecurityLogEvent(BaseLogEvent):
    """Log event for security-relevant events."""

    category: Literal[LogEventCategory.SECURITY] = LogEventCategory.SECURITY

    threat_type: str  # injection_attempt, brute_force, anomaly, policy_violation, etc.
    actor_id: Optional[str] = None
    actor_ip: Optional[str] = None
    severity: str = "medium"  # low, medium, high, critical
    details: Optional[str] = None
    mitigated: bool = False
    mitigation_action: Optional[str] = None

    @model_validator(mode="after")
    def set_level_from_severity(self) -> "SecurityLogEvent":
        severity_map = {
            "low": LogLevel.INFO,
            "medium": LogLevel.WARNING,
            "high": LogLevel.ERROR,
            "critical": LogLevel.CRITICAL,
        }
        self.level = severity_map.get(self.severity.lower(), LogLevel.WARNING)
        return self


# ---------------------------------------------------------------------------
# System Event Log
# ---------------------------------------------------------------------------

class SystemLogEvent(BaseLogEvent):
    """Log event for system lifecycle events."""

    category: Literal[LogEventCategory.SYSTEM] = LogEventCategory.SYSTEM

    event_type: str  # startup, shutdown, health_check, config_reload, etc.
    component: Optional[str] = None
    details: Optional[dict[str, Any]] = None

    # Health check specific
    healthy: Optional[bool] = None
    checks: Optional[dict[str, bool]] = None


# ---------------------------------------------------------------------------
# Union type for all log events
# ---------------------------------------------------------------------------

AnyLogEvent = Union[
    HttpLogEvent,
    AuthLogEvent,
    AuthzLogEvent,
    MigrationLogEvent,
    DataAccessLogEvent,
    PerformanceLogEvent,
    SecurityLogEvent,
    SystemLogEvent,
    BaseLogEvent,
]

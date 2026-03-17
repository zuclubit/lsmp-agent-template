"""
Pydantic v2 schemas for migration pipeline events.

Events flow through:
  Legacy System → Kafka → Migration Service → Salesforce
                                           → Audit Log
                                           → Notification Queue

All events share a common envelope (``MigrationEventEnvelope``) and carry
a strongly-typed payload specific to the event type.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class MigrationPhase(str, Enum):
    EXTRACTION = "extraction"
    VALIDATION = "validation"
    TRANSFORMATION = "transformation"
    LOAD = "load"
    RECONCILIATION = "reconciliation"
    COMPLETED = "completed"
    ROLLBACK = "rollback"


class ObjectType(str, Enum):
    ACCOUNT = "Account"
    CONTACT = "Contact"
    OPPORTUNITY = "Opportunity"
    LEAD = "Lead"
    CASE = "Case"
    PRODUCT = "Product"
    ORDER = "Order"
    CUSTOM = "Custom"


class FailureCategory(str, Enum):
    VALIDATION_ERROR = "validation_error"
    TRANSFORMATION_ERROR = "transformation_error"
    SALESFORCE_API_ERROR = "salesforce_api_error"
    DUPLICATE_RECORD = "duplicate_record"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    DATA_TYPE_MISMATCH = "data_type_mismatch"
    RATE_LIMIT = "rate_limit"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Base event envelope
# ---------------------------------------------------------------------------

PayloadT = TypeVar("PayloadT")


class MigrationEventEnvelope(BaseModel, Generic[PayloadT]):
    """
    Standard envelope for all migration pipeline events.

    Carries routing metadata and a typed payload.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # Identity
    event_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique event identifier (UUID4)",
    )
    correlation_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Traces a business operation across multiple events",
    )
    causation_id: Optional[str] = Field(
        None,
        description="event_id of the event that caused this one",
    )

    # Routing
    event_type: str = Field(description="Dot-separated event type, e.g. account.migrated")
    topic: str = Field(description="Target queue / topic name")
    version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")

    # Context
    source_service: str = Field(description="Originating microservice name")
    environment: str = Field(default="production")
    batch_id: Optional[str] = None
    migration_run_id: Optional[str] = None

    # Timing
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    published_at: Optional[datetime] = None

    # Payload
    payload: PayloadT

    # Retry metadata
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) < 2:
            raise ValueError(
                f"event_type must be dot-separated with at least 2 parts, got '{v}'"
            )
        return v.lower()

    def with_retry(self) -> "MigrationEventEnvelope[PayloadT]":
        """Return a copy of this event with incremented retry count."""
        copy = self.model_copy(deep=True)
        copy.retry_count += 1
        copy.event_id = str(uuid.uuid4())
        return copy

    @property
    def is_retryable(self) -> bool:
        return self.retry_count < self.max_retries


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class RecordExtractedPayload(BaseModel):
    """Emitted when a record is successfully read from the legacy system."""

    model_config = ConfigDict(str_strip_whitespace=True)

    legacy_id: str
    object_type: ObjectType
    raw_data: Dict[str, Any]
    source_system: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    record_hash: Optional[str] = Field(
        None,
        description="SHA-256 of the serialised raw_data for change detection",
    )
    is_delta: bool = Field(
        default=False,
        description="True if this is a delta extraction (changed since last run)",
    )


class FieldValidationIssue(BaseModel):
    """A single field-level issue found during validation."""

    field_name: str
    issue_code: str        # e.g. "MISSING_REQUIRED", "INVALID_FORMAT"
    message: str
    severity: EventSeverity = EventSeverity.ERROR
    raw_value: Optional[Any] = None
    suggested_fix: Optional[str] = None


class RecordValidatedPayload(BaseModel):
    """Emitted after the validation agent has processed a record."""

    model_config = ConfigDict(str_strip_whitespace=True)

    legacy_id: str
    object_type: ObjectType
    validation_passed: bool
    issues: List[FieldValidationIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    validated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validator_version: str = "1.0"

    @model_validator(mode="after")
    def count_issues(self) -> "RecordValidatedPayload":
        self.error_count = sum(1 for i in self.issues if i.severity == EventSeverity.ERROR)
        self.warning_count = sum(1 for i in self.issues if i.severity == EventSeverity.WARNING)
        return self


class RecordTransformedPayload(BaseModel):
    """Emitted after a legacy record has been mapped to the SF target schema."""

    model_config = ConfigDict(str_strip_whitespace=True)

    legacy_id: str
    object_type: ObjectType
    transformed_data: Dict[str, Any]
    fields_mapped: int = 0
    fields_defaulted: int = 0
    fields_dropped: int = 0
    transformation_warnings: List[str] = Field(default_factory=list)
    transformed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RecordLoadedPayload(BaseModel):
    """Emitted after a record is successfully written to Salesforce."""

    model_config = ConfigDict(str_strip_whitespace=True)

    legacy_id: str
    salesforce_id: str
    object_type: ObjectType
    operation: Literal["insert", "update", "upsert"] = "insert"
    was_created: bool = True
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    api_call_duration_ms: Optional[float] = None


class RecordFailedPayload(BaseModel):
    """Emitted when a record fails at any stage of the pipeline."""

    model_config = ConfigDict(str_strip_whitespace=True)

    legacy_id: str
    object_type: ObjectType
    phase: MigrationPhase
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    error_message: str
    error_code: Optional[str] = None
    error_stack: Optional[str] = None
    failed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_retryable: bool = True
    retry_count: int = 0


class BatchStartedPayload(BaseModel):
    """Emitted when a migration batch begins processing."""

    batch_id: str
    migration_run_id: str
    object_type: ObjectType
    total_records: int
    estimated_duration_seconds: Optional[int] = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_by: Optional[str] = None


class BatchCompletedPayload(BaseModel):
    """Emitted when a migration batch finishes (success or partial failure)."""

    batch_id: str
    migration_run_id: str
    object_type: ObjectType
    total_records: int
    successful_records: int
    failed_records: int
    skipped_records: int = 0
    started_at: datetime
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: Optional[float] = None
    success_rate: Optional[float] = None
    errors_summary: List[Dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def compute_derived(self) -> "BatchCompletedPayload":
        if self.total_records:
            self.success_rate = round(self.successful_records / self.total_records, 4)
        self.duration_seconds = (
            self.completed_at - self.started_at
        ).total_seconds()
        return self


class MigrationRunSummaryPayload(BaseModel):
    """
    High-level summary emitted at the end of a full migration run.
    Consumed by the documentation agent and reporting dashboards.
    """

    migration_run_id: str
    environment: str
    total_objects_migrated: int
    total_records_processed: int
    total_records_succeeded: int
    total_records_failed: int
    total_records_skipped: int
    overall_success_rate: float
    duration_seconds: float
    phases_completed: List[MigrationPhase]
    object_summaries: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    top_errors: List[Dict[str, Any]] = Field(default_factory=list)
    run_started_at: datetime
    run_completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_by: Optional[str] = None


class AgentDecisionPayload(BaseModel):
    """Records a decision made by an AI agent during the migration."""

    agent_name: str
    decision_type: str    # e.g. "pause_migration", "skip_record", "retry_batch"
    rationale: str
    confidence_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    affected_records: List[str] = Field(default_factory=list)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    human_override_required: bool = False


# ---------------------------------------------------------------------------
# Typed event aliases (convenience)
# ---------------------------------------------------------------------------

RecordExtractedEvent = MigrationEventEnvelope[RecordExtractedPayload]
RecordValidatedEvent = MigrationEventEnvelope[RecordValidatedPayload]
RecordTransformedEvent = MigrationEventEnvelope[RecordTransformedPayload]
RecordLoadedEvent = MigrationEventEnvelope[RecordLoadedPayload]
RecordFailedEvent = MigrationEventEnvelope[RecordFailedPayload]
BatchStartedEvent = MigrationEventEnvelope[BatchStartedPayload]
BatchCompletedEvent = MigrationEventEnvelope[BatchCompletedPayload]
MigrationRunSummaryEvent = MigrationEventEnvelope[MigrationRunSummaryPayload]
AgentDecisionEvent = MigrationEventEnvelope[AgentDecisionPayload]

# Union type for deserialisation
AnyMigrationEvent = Union[
    RecordExtractedEvent,
    RecordValidatedEvent,
    RecordTransformedEvent,
    RecordLoadedEvent,
    RecordFailedEvent,
    BatchStartedEvent,
    BatchCompletedEvent,
    MigrationRunSummaryEvent,
    AgentDecisionEvent,
]


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_record_extracted_event(
    legacy_id: str,
    object_type: ObjectType,
    raw_data: Dict[str, Any],
    source_system: str,
    batch_id: Optional[str] = None,
    migration_run_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> RecordExtractedEvent:
    return MigrationEventEnvelope(
        event_type="record.extracted",
        topic="migration.events.extracted",
        source_service="extraction-service",
        batch_id=batch_id,
        migration_run_id=migration_run_id,
        correlation_id=correlation_id or str(uuid.uuid4()),
        payload=RecordExtractedPayload(
            legacy_id=legacy_id,
            object_type=object_type,
            raw_data=raw_data,
            source_system=source_system,
        ),
    )


def make_record_failed_event(
    legacy_id: str,
    object_type: ObjectType,
    phase: MigrationPhase,
    error_message: str,
    failure_category: FailureCategory = FailureCategory.UNKNOWN,
    causation_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> RecordFailedEvent:
    return MigrationEventEnvelope(
        event_type="record.failed",
        topic="migration.events.failed",
        source_service="migration-service",
        causation_id=causation_id,
        correlation_id=correlation_id or str(uuid.uuid4()),
        payload=RecordFailedPayload(
            legacy_id=legacy_id,
            object_type=object_type,
            phase=phase,
            failure_category=failure_category,
            error_message=error_message,
        ),
    )

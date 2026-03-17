"""
Domain events for the migration bounded context.

Domain events are immutable value objects that record something significant
that happened within the domain.  They carry enough information for event
handlers to act without needing to query back for the aggregate.

Convention:
  - Named in past tense: MigrationStarted, RecordMigrated, etc.
  - Frozen dataclasses to enforce immutability.
  - No framework dependencies – pure Python.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_event_id() -> UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainEvent:
    """
    Base class for all domain events.

    Every event carries:
      - event_id:       Globally unique identifier for deduplication / idempotency.
      - occurred_on:    UTC timestamp when the event was raised.
      - correlation_id: Traces causally related events across aggregates/services.
      - aggregate_id:   ID of the aggregate that raised the event.
      - aggregate_type: Fully-qualified type name (string) of the aggregate.
    """

    event_id: UUID = field(default_factory=_new_event_id)
    occurred_on: datetime = field(default_factory=_utcnow)
    correlation_id: Optional[str] = field(default=None)
    aggregate_id: str = field(default="")
    aggregate_type: str = field(default="")

    @property
    def event_type(self) -> str:
        """Dot-qualified event type name used as message routing key."""
        return f"migration.{self.__class__.__name__}"


# ---------------------------------------------------------------------------
# Migration phase enum (shared across events)
# ---------------------------------------------------------------------------


class MigrationPhase(str, Enum):
    """Ordered phases of a migration job."""
    PREREQUISITE_CHECK = "prerequisite_check"
    DATA_EXTRACTION = "data_extraction"
    DATA_VALIDATION = "data_validation"
    DATA_TRANSFORMATION = "data_transformation"
    DATA_LOAD = "data_load"
    POST_LOAD_VERIFICATION = "post_load_verification"
    RECONCILIATION = "reconciliation"
    COMPLETION = "completion"


class MigrationStatus(str, Enum):
    """High-level migration job lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


class RecordType(str, Enum):
    ACCOUNT = "Account"
    CONTACT = "Contact"
    OPPORTUNITY = "Opportunity"
    LEAD = "Lead"
    CASE = "Case"


# ---------------------------------------------------------------------------
# Migration lifecycle events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationStarted(DomainEvent):
    """
    Raised when a new migration job transitions from PENDING → RUNNING.

    Fields:
      migration_job_id:   UUID of the MigrationJob aggregate.
      initiated_by:       Identity (user/service account) that triggered the run.
      source_system:      Human-readable name of the legacy source (e.g. "ERP_v2").
      target_org_id:      Salesforce organisation ID (18-char).
      record_types:       Which record types are included in this migration run.
      estimated_records:  Total number of records expected across all types.
      dry_run:            When True, no writes are performed in Salesforce.
    """

    migration_job_id: str = ""
    initiated_by: str = ""
    source_system: str = ""
    target_org_id: str = ""
    record_types: tuple[str, ...] = field(default_factory=tuple)
    estimated_records: int = 0
    dry_run: bool = False

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        # aggregate_id mirrors migration_job_id for base-class consumers
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class MigrationPaused(DomainEvent):
    """
    Raised when an operator pauses a running migration.

    The migration can be resumed from the last completed checkpoint.
    """

    migration_job_id: str = ""
    paused_by: str = ""
    reason: str = ""
    last_completed_phase: Optional[str] = None
    records_processed_so_far: int = 0

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class MigrationResumed(DomainEvent):
    """Raised when a paused migration is resumed."""

    migration_job_id: str = ""
    resumed_by: str = ""
    resuming_from_phase: str = ""

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class PhaseCompleted(DomainEvent):
    """
    Raised when a migration phase finishes successfully.

    Carries phase-level metrics so consumers can build progress dashboards
    without polling the database.
    """

    migration_job_id: str = ""
    phase: str = ""
    duration_seconds: float = 0.0
    records_processed: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    next_phase: Optional[str] = None
    phase_metadata: dict[str, Any] = field(default_factory=dict)

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)

    @property
    def success_rate(self) -> float:
        if self.records_processed == 0:
            return 1.0
        return self.records_succeeded / self.records_processed


@dataclass(frozen=True)
class RecordMigrated(DomainEvent):
    """
    Raised for each individual record that is successfully written to Salesforce.

    High-volume event: handlers should batch-process these (e.g. flush to a
    write-ahead log rather than writing to a relational DB per event).
    """

    migration_job_id: str = ""
    legacy_record_id: str = ""
    salesforce_record_id: str = ""
    record_type: str = ""
    phase: str = ""
    transformation_warnings: tuple[str, ...] = field(default_factory=tuple)

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class RecordMigrationFailed(DomainEvent):
    """
    Raised when a single record cannot be migrated.

    Contains enough context to reconstruct the failed record for manual
    triage or automated retry.
    """

    migration_job_id: str = ""
    legacy_record_id: str = ""
    record_type: str = ""
    phase: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    raw_legacy_data: dict[str, Any] = field(default_factory=dict)

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class MigrationFailed(DomainEvent):
    """
    Raised when the migration job itself fails (as opposed to individual records).

    This is a terminal event: the job transitions to FAILED status.
    """

    migration_job_id: str = ""
    failed_phase: str = ""
    error_code: str = ""
    error_message: str = ""
    records_succeeded: int = 0
    records_failed: int = 0
    rollback_required: bool = False
    stack_trace: Optional[str] = None

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class MigrationCompleted(DomainEvent):
    """
    Raised when all phases complete successfully and reconciliation passes.

    This is the terminal success event.
    """

    migration_job_id: str = ""
    duration_seconds: float = 0.0
    total_records: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    phases_completed: tuple[str, ...] = field(default_factory=tuple)
    report_url: Optional[str] = None

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)

    @property
    def success_rate(self) -> float:
        if self.total_records == 0:
            return 1.0
        return self.records_succeeded / self.total_records

    @property
    def is_fully_successful(self) -> bool:
        return self.records_failed == 0 and self.records_skipped == 0


@dataclass(frozen=True)
class MigrationRolledBack(DomainEvent):
    """
    Raised after a failed migration is rolled back in the target system.
    """

    migration_job_id: str = ""
    rolled_back_by: str = ""
    records_deleted_in_sf: int = 0
    rollback_duration_seconds: float = 0.0
    partial_rollback: bool = False
    notes: str = ""

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)


@dataclass(frozen=True)
class ValidationCompleted(DomainEvent):
    """Raised when the data-validation phase produces its result."""

    migration_job_id: str = ""
    total_records_checked: int = 0
    records_passed: int = 0
    records_with_warnings: int = 0
    records_with_errors: int = 0
    validation_rule_results: dict[str, int] = field(default_factory=dict)
    blocking_errors_found: bool = False

    aggregate_type: str = field(default="MigrationJob", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", self.migration_job_id)

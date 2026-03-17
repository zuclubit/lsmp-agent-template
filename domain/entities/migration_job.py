"""
MigrationJob aggregate root.

This is the central aggregate for the migration bounded context.  It
tracks the full lifecycle of a single migration run, enforces state-machine
transitions, records phase progress, and raises domain events.

State machine:
    PENDING → RUNNING → PAUSED → RUNNING (resume)
                      → FAILED  → ROLLED_BACK
                      → COMPLETED

The aggregate holds a collection of PhaseRecord value objects, one per
completed/active phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from domain.events.migration_events import (
    DomainEvent,
    MigrationCompleted,
    MigrationFailed,
    MigrationPaused,
    MigrationResumed,
    MigrationRolledBack,
    MigrationStarted,
    MigrationStatus,
    MigrationPhase,
    PhaseCompleted,
    ValidationCompleted,
)
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    InvalidStateTransition,
    ValidationError,
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Phase record value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseRecord:
    """
    Immutable record of a single phase execution within a migration job.

    Stored as part of the aggregate's phase history.
    """

    phase: MigrationPhase
    started_at: datetime
    completed_at: Optional[datetime] = None
    records_processed: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    error_message: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def success_rate(self) -> float:
        if self.records_processed == 0:
            return 1.0
        return self.records_succeeded / self.records_processed

    def complete(
        self,
        records_processed: int,
        records_succeeded: int,
        records_failed: int,
        records_skipped: int = 0,
        metadata: Optional[dict] = None,
    ) -> "PhaseRecord":
        """Return a new completed PhaseRecord (immutable update pattern)."""
        return PhaseRecord(
            phase=self.phase,
            started_at=self.started_at,
            completed_at=_utcnow(),
            records_processed=records_processed,
            records_succeeded=records_succeeded,
            records_failed=records_failed,
            records_skipped=records_skipped,
            metadata=metadata or self.metadata,
        )

    def fail(self, error_message: str) -> "PhaseRecord":
        """Return a new failed PhaseRecord."""
        return PhaseRecord(
            phase=self.phase,
            started_at=self.started_at,
            completed_at=_utcnow(),
            records_processed=self.records_processed,
            records_succeeded=self.records_succeeded,
            records_failed=self.records_failed,
            error_message=error_message,
            metadata=self.metadata,
        )


# ---------------------------------------------------------------------------
# MigrationConfig value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationConfig:
    """
    Immutable configuration snapshot captured when the migration is started.

    Stored on the aggregate so all decisions can be reproduced from history.
    """

    source_system: str
    target_org_id: str
    record_types: tuple[str, ...]
    batch_size: int = 200
    max_retries: int = 3
    dry_run: bool = False
    phases_to_run: tuple[str, ...] = field(
        default_factory=lambda: tuple(p.value for p in MigrationPhase)
    )
    error_threshold_percent: float = 5.0  # abort if error rate exceeds this
    notification_emails: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.batch_size <= 0 or self.batch_size > 2000:
            raise ValidationError(
                "batch_size", self.batch_size, "batch_size must be between 1 and 2000"
            )
        if not self.source_system:
            raise ValidationError("source_system", self.source_system, "source_system cannot be blank")
        if not self.record_types:
            raise ValidationError("record_types", self.record_types, "at least one record_type is required")
        if not (0.0 <= self.error_threshold_percent <= 100.0):
            raise ValidationError(
                "error_threshold_percent",
                self.error_threshold_percent,
                "error_threshold_percent must be between 0 and 100",
            )


# ---------------------------------------------------------------------------
# Aggregate counters (mutable roll-up kept on aggregate for quick access)
# ---------------------------------------------------------------------------


@dataclass
class MigrationCounters:
    total_records: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0

    @property
    def records_processed(self) -> int:
        return self.records_succeeded + self.records_failed + self.records_skipped

    @property
    def error_rate_percent(self) -> float:
        if self.total_records == 0:
            return 0.0
        return (self.records_failed / self.total_records) * 100.0

    @property
    def completion_percent(self) -> float:
        if self.total_records == 0:
            return 0.0
        return (self.records_processed / self.total_records) * 100.0

    def add_phase_result(self, phase_record: PhaseRecord) -> None:
        self.records_succeeded += phase_record.records_succeeded
        self.records_failed += phase_record.records_failed
        self.records_skipped += phase_record.records_skipped


# ---------------------------------------------------------------------------
# MigrationJob aggregate root
# ---------------------------------------------------------------------------


class MigrationJob:
    """
    MigrationJob aggregate root.

    Tracks a single end-to-end migration run from source system to Salesforce.

    Usage::

        job = MigrationJob.create(config=..., initiated_by="admin@example.com")
        job.start()
        job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        job.complete_phase(MigrationPhase.DATA_EXTRACTION, records_processed=5000, ...)
        job.complete()
        events = job.collect_events()
    """

    # Valid state transitions: from_state → {valid_to_states}
    _TRANSITIONS: dict[MigrationStatus, frozenset[MigrationStatus]] = {
        MigrationStatus.PENDING: frozenset({MigrationStatus.RUNNING}),
        MigrationStatus.RUNNING: frozenset({
            MigrationStatus.PAUSED,
            MigrationStatus.FAILED,
            MigrationStatus.COMPLETED,
        }),
        MigrationStatus.PAUSED: frozenset({
            MigrationStatus.RUNNING,
            MigrationStatus.FAILED,
        }),
        MigrationStatus.FAILED: frozenset({MigrationStatus.ROLLED_BACK}),
        MigrationStatus.COMPLETED: frozenset(),
        MigrationStatus.ROLLED_BACK: frozenset(),
    }

    def __init__(
        self,
        job_id: UUID,
        config: MigrationConfig,
        initiated_by: str,
        status: MigrationStatus,
        counters: MigrationCounters,
        phase_history: list[PhaseRecord],
        current_phase: Optional[MigrationPhase],
        started_at: Optional[datetime],
        completed_at: Optional[datetime],
        created_at: datetime,
        updated_at: datetime,
        version: int = 0,
    ) -> None:
        self._job_id = job_id
        self._config = config
        self._initiated_by = initiated_by
        self._status = status
        self._counters = counters
        self._phase_history: list[PhaseRecord] = phase_history
        self._current_phase = current_phase
        self._started_at = started_at
        self._completed_at = completed_at
        self._created_at = created_at
        self._updated_at = updated_at
        self._version = version
        self._domain_events: list[DomainEvent] = []

    # ------------------------------------------------------------------
    # Factory method
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        config: MigrationConfig,
        initiated_by: str,
        total_records: int = 0,
    ) -> "MigrationJob":
        if not initiated_by or not initiated_by.strip():
            raise ValidationError("initiated_by", initiated_by, "initiated_by cannot be blank")
        if total_records < 0:
            raise ValidationError("total_records", total_records, "total_records cannot be negative")

        now = _utcnow()
        counters = MigrationCounters(total_records=total_records)

        return cls(
            job_id=uuid.uuid4(),
            config=config,
            initiated_by=initiated_by.strip(),
            status=MigrationStatus.PENDING,
            counters=counters,
            phase_history=[],
            current_phase=None,
            started_at=None,
            completed_at=None,
            created_at=now,
            updated_at=now,
            version=0,
        )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def job_id(self) -> UUID:
        return self._job_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MigrationJob):
            return NotImplemented
        return self._job_id == other._job_id

    def __hash__(self) -> int:
        return hash(self._job_id)

    def __repr__(self) -> str:
        return (
            f"MigrationJob(id={self._job_id}, status={self._status.value}, "
            f"phase={self._current_phase})"
        )

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> MigrationConfig:
        return self._config

    @property
    def initiated_by(self) -> str:
        return self._initiated_by

    @property
    def status(self) -> MigrationStatus:
        return self._status

    @property
    def current_phase(self) -> Optional[MigrationPhase]:
        return self._current_phase

    @property
    def counters(self) -> MigrationCounters:
        return self._counters

    @property
    def phase_history(self) -> list[PhaseRecord]:
        return list(self._phase_history)  # defensive copy

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    @property
    def completed_at(self) -> Optional[datetime]:
        return self._completed_at

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def version(self) -> int:
        return self._version

    @property
    def is_running(self) -> bool:
        return self._status == MigrationStatus.RUNNING

    @property
    def is_terminal(self) -> bool:
        return self._status in (MigrationStatus.COMPLETED, MigrationStatus.ROLLED_BACK)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self._started_at is None:
            return None
        end = self._completed_at or _utcnow()
        return (end - self._started_at).total_seconds()

    # ------------------------------------------------------------------
    # Domain events
    # ------------------------------------------------------------------

    def collect_events(self) -> list[DomainEvent]:
        events = list(self._domain_events)
        self._domain_events.clear()
        return events

    # ------------------------------------------------------------------
    # State machine helpers
    # ------------------------------------------------------------------

    def _transition_to(self, new_status: MigrationStatus) -> None:
        allowed = self._TRANSITIONS.get(self._status, frozenset())
        if new_status not in allowed:
            raise InvalidStateTransition(
                entity="MigrationJob",
                from_state=self._status.value,
                to_state=new_status.value,
            )
        self._status = new_status
        self._updated_at = _utcnow()
        self._version += 1

    # ------------------------------------------------------------------
    # Lifecycle commands
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Transition PENDING → RUNNING."""
        self._transition_to(MigrationStatus.RUNNING)
        self._started_at = self._updated_at

        self._domain_events.append(
            MigrationStarted(
                migration_job_id=str(self._job_id),
                initiated_by=self._initiated_by,
                source_system=self._config.source_system,
                target_org_id=self._config.target_org_id,
                record_types=self._config.record_types,
                estimated_records=self._counters.total_records,
                dry_run=self._config.dry_run,
            )
        )

    def pause(self, paused_by: str, reason: str = "") -> None:
        """Transition RUNNING → PAUSED."""
        self._transition_to(MigrationStatus.PAUSED)

        self._domain_events.append(
            MigrationPaused(
                migration_job_id=str(self._job_id),
                paused_by=paused_by,
                reason=reason,
                last_completed_phase=(
                    self._phase_history[-1].phase.value if self._phase_history else None
                ),
                records_processed_so_far=self._counters.records_processed,
            )
        )

    def resume(self, resumed_by: str) -> None:
        """Transition PAUSED → RUNNING."""
        self._transition_to(MigrationStatus.RUNNING)

        resuming_from = (
            self._phase_history[-1].phase.value if self._phase_history else "start"
        )
        self._domain_events.append(
            MigrationResumed(
                migration_job_id=str(self._job_id),
                resumed_by=resumed_by,
                resuming_from_phase=resuming_from,
            )
        )

    def fail(
        self,
        failed_phase: str,
        error_code: str,
        error_message: str,
        stack_trace: Optional[str] = None,
    ) -> None:
        """Transition RUNNING|PAUSED → FAILED."""
        self._transition_to(MigrationStatus.FAILED)

        self._domain_events.append(
            MigrationFailed(
                migration_job_id=str(self._job_id),
                failed_phase=failed_phase,
                error_code=error_code,
                error_message=error_message,
                records_succeeded=self._counters.records_succeeded,
                records_failed=self._counters.records_failed,
                rollback_required=self._counters.records_succeeded > 0,
                stack_trace=stack_trace,
            )
        )

    def complete(self, report_url: Optional[str] = None) -> None:
        """Transition RUNNING → COMPLETED."""
        self._transition_to(MigrationStatus.COMPLETED)
        self._completed_at = self._updated_at

        self._domain_events.append(
            MigrationCompleted(
                migration_job_id=str(self._job_id),
                duration_seconds=self.duration_seconds or 0.0,
                total_records=self._counters.total_records,
                records_succeeded=self._counters.records_succeeded,
                records_failed=self._counters.records_failed,
                records_skipped=self._counters.records_skipped,
                phases_completed=tuple(p.phase.value for p in self._phase_history if p.is_complete),
                report_url=report_url,
            )
        )

    def rollback(self, rolled_back_by: str, records_deleted: int, partial: bool = False) -> None:
        """Transition FAILED → ROLLED_BACK."""
        self._transition_to(MigrationStatus.ROLLED_BACK)

        self._domain_events.append(
            MigrationRolledBack(
                migration_job_id=str(self._job_id),
                rolled_back_by=rolled_back_by,
                records_deleted_in_sf=records_deleted,
                rollback_duration_seconds=0.0,
                partial_rollback=partial,
            )
        )

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def begin_phase(self, phase: MigrationPhase) -> None:
        """Record the start of a new migration phase."""
        if self._status != MigrationStatus.RUNNING:
            raise BusinessRuleViolation(
                rule="PHASE_REQUIRES_RUNNING_JOB",
                message=f"Cannot begin phase '{phase.value}' – job is not RUNNING (status={self._status.value})",
            )
        self._current_phase = phase
        self._phase_history.append(
            PhaseRecord(phase=phase, started_at=_utcnow())
        )
        self._updated_at = _utcnow()
        self._version += 1

    def complete_phase(
        self,
        phase: MigrationPhase,
        records_processed: int,
        records_succeeded: int,
        records_failed: int,
        records_skipped: int = 0,
        metadata: Optional[dict] = None,
    ) -> None:
        """Mark the given phase as completed and update aggregate counters."""
        matching = [p for p in self._phase_history if p.phase == phase and not p.is_complete]
        if not matching:
            raise BusinessRuleViolation(
                rule="PHASE_NOT_STARTED",
                message=f"Phase '{phase.value}' has not been started",
            )

        idx = self._phase_history.index(matching[-1])
        completed_record = matching[-1].complete(
            records_processed=records_processed,
            records_succeeded=records_succeeded,
            records_failed=records_failed,
            records_skipped=records_skipped,
            metadata=metadata,
        )
        self._phase_history[idx] = completed_record
        self._counters.add_phase_result(completed_record)

        # Determine next phase
        phase_order = list(MigrationPhase)
        current_idx = phase_order.index(phase)
        next_phase_val = phase_order[current_idx + 1].value if current_idx + 1 < len(phase_order) else None

        self._domain_events.append(
            PhaseCompleted(
                migration_job_id=str(self._job_id),
                phase=phase.value,
                duration_seconds=completed_record.duration_seconds or 0.0,
                records_processed=records_processed,
                records_succeeded=records_succeeded,
                records_failed=records_failed,
                next_phase=next_phase_val,
                phase_metadata=metadata or {},
            )
        )

        # Auto-abort if error rate exceeds threshold (business rule)
        if self._counters.error_rate_percent > self._config.error_threshold_percent:
            self.fail(
                failed_phase=phase.value,
                error_code="ERROR_THRESHOLD_EXCEEDED",
                error_message=(
                    f"Error rate {self._counters.error_rate_percent:.1f}% exceeds "
                    f"configured threshold {self._config.error_threshold_percent:.1f}%"
                ),
            )
        else:
            self._updated_at = _utcnow()
            self._version += 1

    def record_validation_result(
        self,
        total_checked: int,
        passed: int,
        warnings: int,
        errors: int,
        rule_results: dict[str, int],
        blocking_errors: bool,
    ) -> None:
        """Record the result of the data-validation phase."""
        self._domain_events.append(
            ValidationCompleted(
                migration_job_id=str(self._job_id),
                total_records_checked=total_checked,
                records_passed=passed,
                records_with_warnings=warnings,
                records_with_errors=errors,
                validation_rule_results=rule_results,
                blocking_errors_found=blocking_errors,
            )
        )
        if blocking_errors:
            self.fail(
                failed_phase=MigrationPhase.DATA_VALIDATION.value,
                error_code="BLOCKING_VALIDATION_ERRORS",
                error_message=f"Data validation found {errors} blocking error(s) – migration aborted",
            )
        self._updated_at = _utcnow()
        self._version += 1

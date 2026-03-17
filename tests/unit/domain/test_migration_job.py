"""
Unit tests for MigrationJob aggregate root.

Covers job lifecycle, state machine transitions, phase management,
progress tracking, error threshold enforcement, and domain event emission.

Module under test: domain/entities/migration_job.py
Pattern: AAA (Arrange – Act – Assert), grouped by behaviour.
"""

from __future__ import annotations

import pytest

from domain.entities.migration_job import (
    MigrationConfig,
    MigrationCounters,
    MigrationJob,
    PhaseRecord,
)
from domain.events.migration_events import (
    MigrationCompleted,
    MigrationFailed,
    MigrationPaused,
    MigrationResumed,
    MigrationRolledBack,
    MigrationStarted,
    MigrationPhase,
    MigrationStatus,
    PhaseCompleted,
)
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    InvalidStateTransition,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Shared helpers / factories
# ---------------------------------------------------------------------------


def _make_config(
    *,
    source_system: str = "legacy-crm-v2",
    target_org_id: str = "00Dxx0000001gERAAY",
    record_types: tuple[str, ...] = ("Account",),
    batch_size: int = 200,
    dry_run: bool = False,
    error_threshold: float = 5.0,
) -> MigrationConfig:
    return MigrationConfig(
        source_system=source_system,
        target_org_id=target_org_id,
        record_types=record_types,
        batch_size=batch_size,
        dry_run=dry_run,
        error_threshold_percent=error_threshold,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> MigrationConfig:
    return _make_config()


@pytest.fixture()
def pending_job(config: MigrationConfig) -> MigrationJob:
    """A freshly created PENDING job."""
    return MigrationJob.create(
        config=config,
        initiated_by="migration-admin@example.com",
        total_records=50_000,
    )


@pytest.fixture()
def running_job(pending_job: MigrationJob) -> MigrationJob:
    """A job that has been started (RUNNING)."""
    pending_job.start()
    pending_job.collect_events()  # drain creation events
    return pending_job


# ===========================================================================
# 1. MigrationConfig value object validation
# ===========================================================================


class TestMigrationConfig:
    """MigrationConfig is validated at construction time."""

    def test_valid_config_is_created(self, config: MigrationConfig) -> None:
        """Standard config should be constructed without errors."""
        assert config.source_system == "legacy-crm-v2"
        assert config.batch_size == 200

    @pytest.mark.parametrize(
        "batch_size",
        [0, -1, 2001],
        ids=["zero", "negative", "exceeds_max"],
    )
    def test_invalid_batch_size_raises(self, batch_size: int) -> None:
        """batch_size outside [1, 2000] must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationConfig(
                source_system="legacy",
                target_org_id="00Dxx",
                record_types=("Account",),
                batch_size=batch_size,
            )
        assert exc_info.value.field == "batch_size"

    def test_blank_source_system_raises(self) -> None:
        """Blank source_system must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationConfig(
                source_system="",
                target_org_id="00Dxx",
                record_types=("Account",),
            )
        assert exc_info.value.field == "source_system"

    def test_empty_record_types_raises(self) -> None:
        """At least one record_type is required."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationConfig(
                source_system="legacy",
                target_org_id="00Dxx",
                record_types=(),
            )
        assert exc_info.value.field == "record_types"

    @pytest.mark.parametrize(
        "threshold",
        [-0.1, 100.1, 200.0],
        ids=["below_zero", "just_above_100", "way_above_100"],
    )
    def test_invalid_error_threshold_raises(self, threshold: float) -> None:
        """error_threshold_percent outside [0, 100] must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationConfig(
                source_system="legacy",
                target_org_id="00Dxx",
                record_types=("Account",),
                error_threshold_percent=threshold,
            )
        assert exc_info.value.field == "error_threshold_percent"


# ===========================================================================
# 2. Job creation
# ===========================================================================


class TestMigrationJobCreation:
    """MigrationJob.create() factory — initial state and invariants."""

    def test_creates_with_pending_status(self, pending_job: MigrationJob) -> None:
        """Freshly created job must be in PENDING status."""
        assert pending_job.status == MigrationStatus.PENDING

    def test_assigns_unique_job_id(self, config: MigrationConfig) -> None:
        """Each call to create() produces a distinct UUID."""
        j1 = MigrationJob.create(config=config, initiated_by="admin@example.com")
        j2 = MigrationJob.create(config=config, initiated_by="admin@example.com")
        assert j1.job_id != j2.job_id

    def test_stores_initiated_by_stripped(self, config: MigrationConfig) -> None:
        """initiated_by is stored stripped of whitespace."""
        job = MigrationJob.create(config=config, initiated_by="  admin@example.com  ")
        assert job.initiated_by == "admin@example.com"

    def test_stores_total_records(self, pending_job: MigrationJob) -> None:
        """total_records is stored on the counters."""
        assert pending_job.counters.total_records == 50_000

    def test_blank_initiated_by_raises(self, config: MigrationConfig) -> None:
        """Blank initiated_by must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationJob.create(config=config, initiated_by="   ")
        assert exc_info.value.field == "initiated_by"

    def test_negative_total_records_raises(self, config: MigrationConfig) -> None:
        """Negative total_records must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            MigrationJob.create(config=config, initiated_by="admin", total_records=-1)
        assert exc_info.value.field == "total_records"

    def test_is_not_running_or_terminal_initially(self, pending_job: MigrationJob) -> None:
        """A new job is neither running nor terminal."""
        assert pending_job.is_running is False
        assert pending_job.is_terminal is False


# ===========================================================================
# 3. Lifecycle state transitions
# ===========================================================================


class TestMigrationJobLifecycle:
    """PENDING → RUNNING → PAUSED → RUNNING → COMPLETED lifecycle."""

    def test_start_transitions_to_running(self, pending_job: MigrationJob) -> None:
        """start() must move PENDING → RUNNING."""
        pending_job.start()
        assert pending_job.status == MigrationStatus.RUNNING
        assert pending_job.is_running is True

    def test_start_sets_started_at(self, pending_job: MigrationJob) -> None:
        """started_at must be populated after start()."""
        pending_job.start()
        assert pending_job.started_at is not None

    def test_start_emits_migration_started_event(self, pending_job: MigrationJob) -> None:
        """MigrationStarted domain event must be emitted by start()."""
        pending_job.start()
        events = pending_job.collect_events()
        started = [e for e in events if isinstance(e, MigrationStarted)]
        assert len(started) == 1
        assert started[0].initiated_by == "migration-admin@example.com"

    def test_cannot_start_twice(self, running_job: MigrationJob) -> None:
        """Starting an already-RUNNING job must raise InvalidStateTransition."""
        with pytest.raises(InvalidStateTransition):
            running_job.start()

    def test_pause_transitions_running_to_paused(self, running_job: MigrationJob) -> None:
        """pause() must move RUNNING → PAUSED."""
        running_job.pause(paused_by="operator@example.com", reason="Maintenance window")
        assert running_job.status == MigrationStatus.PAUSED

    def test_pause_emits_migration_paused_event(self, running_job: MigrationJob) -> None:
        """MigrationPaused event must be emitted."""
        running_job.pause(paused_by="ops@example.com", reason="Scheduled")
        events = running_job.collect_events()
        paused = [e for e in events if isinstance(e, MigrationPaused)]
        assert len(paused) == 1
        assert paused[0].paused_by == "ops@example.com"

    def test_resume_transitions_paused_to_running(self, running_job: MigrationJob) -> None:
        """resume() must move PAUSED → RUNNING."""
        running_job.pause("ops", "test pause")
        running_job.collect_events()
        running_job.resume("ops@example.com")
        assert running_job.status == MigrationStatus.RUNNING

    def test_resume_emits_migration_resumed_event(self, running_job: MigrationJob) -> None:
        """MigrationResumed event must be emitted."""
        running_job.pause("ops", "test")
        running_job.collect_events()
        running_job.resume("admin@example.com")
        events = running_job.collect_events()
        resumed = [e for e in events if isinstance(e, MigrationResumed)]
        assert len(resumed) == 1
        assert resumed[0].resumed_by == "admin@example.com"

    def test_complete_transitions_running_to_completed(self, running_job: MigrationJob) -> None:
        """complete() must move RUNNING → COMPLETED."""
        running_job.complete()
        assert running_job.status == MigrationStatus.COMPLETED
        assert running_job.is_terminal is True

    def test_complete_sets_completed_at(self, running_job: MigrationJob) -> None:
        """completed_at is set after complete()."""
        running_job.complete()
        assert running_job.completed_at is not None

    def test_complete_emits_migration_completed_event(self, running_job: MigrationJob) -> None:
        """MigrationCompleted domain event must be emitted."""
        running_job.complete()
        events = running_job.collect_events()
        completed = [e for e in events if isinstance(e, MigrationCompleted)]
        assert len(completed) == 1

    def test_cannot_complete_from_pending(self, pending_job: MigrationJob) -> None:
        """Completing a PENDING job must raise InvalidStateTransition."""
        with pytest.raises(InvalidStateTransition):
            pending_job.complete()

    def test_fail_transitions_running_to_failed(self, running_job: MigrationJob) -> None:
        """fail() must move RUNNING → FAILED."""
        running_job.fail(
            failed_phase="data_load",
            error_code="SF_LIMIT",
            error_message="Governor limits exceeded",
        )
        assert running_job.status == MigrationStatus.FAILED

    def test_fail_emits_migration_failed_event(self, running_job: MigrationJob) -> None:
        """MigrationFailed event must be emitted with error context."""
        running_job.fail("data_load", "SF_ERR", "Some error")
        events = running_job.collect_events()
        failed = [e for e in events if isinstance(e, MigrationFailed)]
        assert len(failed) == 1
        assert failed[0].error_code == "SF_ERR"

    def test_rollback_transitions_failed_to_rolled_back(
        self, running_job: MigrationJob
    ) -> None:
        """rollback() must move FAILED → ROLLED_BACK."""
        running_job.fail("data_load", "ERR", "error")
        running_job.collect_events()
        running_job.rollback(rolled_back_by="admin", records_deleted=100)
        assert running_job.status == MigrationStatus.ROLLED_BACK
        assert running_job.is_terminal is True

    def test_rollback_emits_migration_rolled_back_event(
        self, running_job: MigrationJob
    ) -> None:
        """MigrationRolledBack event must be emitted."""
        running_job.fail("phase", "ERR", "msg")
        running_job.collect_events()
        running_job.rollback(rolled_back_by="admin", records_deleted=50)
        events = running_job.collect_events()
        rolled_back = [e for e in events if isinstance(e, MigrationRolledBack)]
        assert len(rolled_back) == 1

    def test_cannot_rollback_completed_job(self, running_job: MigrationJob) -> None:
        """Completed jobs cannot be rolled back."""
        running_job.complete()
        running_job.collect_events()
        with pytest.raises(InvalidStateTransition):
            running_job.rollback("admin", 0)


# ===========================================================================
# 4. Phase management
# ===========================================================================


class TestMigrationJobPhaseManagement:
    """begin_phase() and complete_phase() track progress per-phase."""

    def test_begin_phase_sets_current_phase(self, running_job: MigrationJob) -> None:
        """begin_phase() stores the active phase on the aggregate."""
        running_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        assert running_job.current_phase == MigrationPhase.DATA_EXTRACTION

    def test_begin_phase_adds_to_phase_history(self, running_job: MigrationJob) -> None:
        """Each begin_phase() call appends to phase_history."""
        running_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        assert len(running_job.phase_history) == 1

    def test_begin_phase_requires_running_status(self, pending_job: MigrationJob) -> None:
        """begin_phase() on a non-RUNNING job raises BusinessRuleViolation."""
        with pytest.raises(BusinessRuleViolation) as exc_info:
            pending_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        assert exc_info.value.rule == "PHASE_REQUIRES_RUNNING_JOB"

    def test_complete_phase_marks_phase_complete(self, running_job: MigrationJob) -> None:
        """complete_phase() marks the started phase as complete."""
        running_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        running_job.complete_phase(
            MigrationPhase.DATA_EXTRACTION,
            records_processed=10_000,
            records_succeeded=9_900,
            records_failed=100,
        )
        phase = running_job.phase_history[0]
        assert phase.is_complete is True
        assert phase.records_processed == 10_000

    def test_complete_phase_emits_phase_completed_event(
        self, running_job: MigrationJob
    ) -> None:
        """PhaseCompleted event is emitted when a phase finishes."""
        running_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        running_job.complete_phase(
            MigrationPhase.DATA_EXTRACTION,
            records_processed=5_000,
            records_succeeded=5_000,
            records_failed=0,
        )
        events = running_job.collect_events()
        phase_events = [e for e in events if isinstance(e, PhaseCompleted)]
        assert len(phase_events) == 1
        assert phase_events[0].records_processed == 5_000

    def test_complete_phase_not_started_raises(self, running_job: MigrationJob) -> None:
        """Completing a phase that was never begun raises PHASE_NOT_STARTED."""
        with pytest.raises(BusinessRuleViolation) as exc_info:
            running_job.complete_phase(
                MigrationPhase.DATA_EXTRACTION,
                records_processed=0,
                records_succeeded=0,
                records_failed=0,
            )
        assert exc_info.value.rule == "PHASE_NOT_STARTED"

    def test_phase_history_returns_defensive_copy(self, running_job: MigrationJob) -> None:
        """Mutating the returned phase_history list must not affect the aggregate."""
        running_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        history = running_job.phase_history
        history.clear()
        assert len(running_job.phase_history) == 1


# ===========================================================================
# 5. Error threshold auto-abort
# ===========================================================================


class TestErrorThresholdAbort:
    """complete_phase() auto-fails the job when error rate exceeds threshold."""

    def test_exceeding_error_threshold_fails_job(self) -> None:
        """Job auto-transitions to FAILED when error rate > threshold."""
        cfg = _make_config(error_threshold=5.0)
        job = MigrationJob.create(config=cfg, initiated_by="admin", total_records=1_000)
        job.start()
        job.begin_phase(MigrationPhase.DATA_LOAD)
        # 10 % failure rate — above 5 % threshold
        job.complete_phase(
            MigrationPhase.DATA_LOAD,
            records_processed=1_000,
            records_succeeded=900,
            records_failed=100,
        )
        assert job.status == MigrationStatus.FAILED

    def test_below_error_threshold_stays_running(self) -> None:
        """Job stays RUNNING when error rate is within the threshold."""
        cfg = _make_config(error_threshold=5.0)
        job = MigrationJob.create(config=cfg, initiated_by="admin", total_records=1_000)
        job.start()
        job.begin_phase(MigrationPhase.DATA_LOAD)
        # 3 % failure rate — within 5 % threshold
        job.complete_phase(
            MigrationPhase.DATA_LOAD,
            records_processed=1_000,
            records_succeeded=970,
            records_failed=30,
        )
        assert job.status == MigrationStatus.RUNNING


# ===========================================================================
# 6. Progress counters
# ===========================================================================


class TestMigrationCounters:
    """MigrationCounters computed properties."""

    def test_completion_percent_zero_when_no_records(self) -> None:
        """completion_percent is 0.0 when total_records is 0."""
        counters = MigrationCounters(total_records=0)
        assert counters.completion_percent == 0.0

    def test_completion_percent_calculated(self) -> None:
        """completion_percent = processed / total * 100."""
        counters = MigrationCounters(total_records=1_000)
        counters.records_succeeded = 500
        assert counters.completion_percent == 50.0

    def test_error_rate_percent_zero_when_no_records(self) -> None:
        """error_rate_percent is 0.0 when total_records is 0."""
        counters = MigrationCounters(total_records=0)
        assert counters.error_rate_percent == 0.0

    def test_error_rate_percent_calculated(self) -> None:
        """error_rate_percent = failed / total * 100."""
        counters = MigrationCounters(total_records=100)
        counters.records_failed = 5
        assert counters.error_rate_percent == 5.0

    def test_records_processed_sums_succeeded_failed_skipped(self) -> None:
        """records_processed = succeeded + failed + skipped."""
        counters = MigrationCounters(total_records=100)
        counters.records_succeeded = 80
        counters.records_failed = 10
        counters.records_skipped = 5
        assert counters.records_processed == 95


# ===========================================================================
# 7. PhaseRecord value object
# ===========================================================================


class TestPhaseRecord:
    """PhaseRecord immutable value object."""

    def test_success_rate_100_when_no_failures(self) -> None:
        """success_rate is 1.0 when all processed records succeed."""
        from datetime import datetime, timezone

        pr = PhaseRecord(
            phase=MigrationPhase.DATA_EXTRACTION,
            started_at=datetime.now(timezone.utc),
            records_processed=100,
            records_succeeded=100,
            records_failed=0,
        )
        assert pr.success_rate == 1.0

    def test_success_rate_calculated_correctly(self) -> None:
        """success_rate = succeeded / processed."""
        from datetime import datetime, timezone

        pr = PhaseRecord(
            phase=MigrationPhase.DATA_EXTRACTION,
            started_at=datetime.now(timezone.utc),
            records_processed=200,
            records_succeeded=180,
            records_failed=20,
        )
        assert pr.success_rate == 0.9

    def test_is_complete_false_without_completed_at(self) -> None:
        """Phase is not complete until completed_at is set."""
        from datetime import datetime, timezone

        pr = PhaseRecord(
            phase=MigrationPhase.DATA_EXTRACTION,
            started_at=datetime.now(timezone.utc),
        )
        assert pr.is_complete is False

    def test_duration_seconds_none_before_completion(self) -> None:
        """duration_seconds is None while the phase is still running."""
        from datetime import datetime, timezone

        pr = PhaseRecord(
            phase=MigrationPhase.DATA_EXTRACTION,
            started_at=datetime.now(timezone.utc),
        )
        assert pr.duration_seconds is None


# ===========================================================================
# 8. Identity and equality
# ===========================================================================


class TestMigrationJobEquality:
    """MigrationJob identity is based on job_id."""

    def test_same_job_equals_itself(self, pending_job: MigrationJob) -> None:
        """A job must equal itself."""
        assert pending_job == pending_job

    def test_different_jobs_not_equal(self, config: MigrationConfig) -> None:
        """Two separately created jobs must not be equal."""
        j1 = MigrationJob.create(config=config, initiated_by="admin")
        j2 = MigrationJob.create(config=config, initiated_by="admin")
        assert j1 != j2

    def test_jobs_usable_as_set_members(self, config: MigrationConfig) -> None:
        """Jobs with different IDs occupy distinct set slots."""
        j1 = MigrationJob.create(config=config, initiated_by="admin")
        j2 = MigrationJob.create(config=config, initiated_by="admin")
        assert len({j1, j2}) == 2

"""
PauseMigration use case.

Handles PauseMigrationCommand:
  1. Loads the running MigrationJob aggregate.
  2. Calls job.pause() which enforces the state-machine transition.
  3. Persists the updated aggregate.
  4. Publishes the MigrationPaused domain event.
  5. Sends a notification.
  6. Returns the updated job DTO.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from application.commands.migration_commands import PauseMigrationCommand
from application.dto.migration_dto import MigrationJobDTO, PhaseProgressDTO
from domain.entities.migration_job import MigrationJob
from domain.exceptions.domain_exceptions import (
    EntityNotFound,
    MigrationJobNotFound,
    ValidationError,
)
from domain.repositories.migration_repository import MigrationRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secondary ports
# ---------------------------------------------------------------------------


class EventPublisher(Protocol):
    async def publish_all(self, events: list) -> None: ...


class NotificationPort(Protocol):
    async def notify_migration_paused(self, job_dto: MigrationJobDTO, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class PauseMigrationUseCase:
    """Handles PauseMigrationCommand."""

    def __init__(
        self,
        migration_repository: MigrationRepository,
        event_publisher: EventPublisher,
        notification_port: NotificationPort,
    ) -> None:
        self._migration_repo = migration_repository
        self._event_publisher = event_publisher
        self._notification_port = notification_port

    async def execute(self, command: PauseMigrationCommand) -> MigrationJobDTO:
        """
        Execute the PauseMigrationCommand.

        Raises:
            ValidationError:       If job_id is missing.
            MigrationJobNotFound:  If no job exists with the given id.
            InvalidStateTransition: If the job is not in a RUNNING state.
        """
        logger.info(
            "PauseMigration: job_id=%s paused_by=%s reason=%r",
            command.job_id,
            command.issued_by,
            command.reason,
        )

        # ------------------------------------------------------------------
        # 1. Validate command
        # ------------------------------------------------------------------
        if not command.job_id:
            raise ValidationError("job_id", command.job_id, "job_id is required")

        # ------------------------------------------------------------------
        # 2. Load aggregate
        # ------------------------------------------------------------------
        try:
            job_uuid = UUID(command.job_id)
        except ValueError:
            raise ValidationError("job_id", command.job_id, "job_id must be a valid UUID")

        job = await self._migration_repo.find_by_id(job_uuid)
        if job is None:
            raise MigrationJobNotFound(job_id=command.job_id)

        # ------------------------------------------------------------------
        # 3. Apply domain behaviour (raises InvalidStateTransition if invalid)
        # ------------------------------------------------------------------
        job.pause(
            paused_by=command.issued_by or "system",
            reason=command.reason,
        )
        logger.info("MigrationJob %s paused successfully", command.job_id)

        # ------------------------------------------------------------------
        # 4. Persist updated aggregate
        # ------------------------------------------------------------------
        saved_job = await self._migration_repo.save(job)

        # ------------------------------------------------------------------
        # 5. Publish domain events
        # ------------------------------------------------------------------
        events = saved_job.collect_events()
        await self._event_publisher.publish_all(events)

        # ------------------------------------------------------------------
        # 6. Send notification
        # ------------------------------------------------------------------
        job_dto = _to_dto(saved_job)
        try:
            await self._notification_port.notify_migration_paused(job_dto, command.reason)
        except Exception as exc:
            logger.warning("Failed to send pause notification: %s", exc)

        return job_dto


# ---------------------------------------------------------------------------
# DTO assembly (module-level helper)
# ---------------------------------------------------------------------------


def _to_dto(job: MigrationJob) -> MigrationJobDTO:
    phases = [
        PhaseProgressDTO(
            phase=p.phase.value,
            status="completed" if p.is_complete else "running",
            started_at=p.started_at.isoformat() if p.started_at else None,
            completed_at=p.completed_at.isoformat() if p.completed_at else None,
            duration_seconds=p.duration_seconds,
            records_processed=p.records_processed,
            records_succeeded=p.records_succeeded,
            records_failed=p.records_failed,
            records_skipped=p.records_skipped,
            success_rate=p.success_rate,
        )
        for p in job.phase_history
    ]
    return MigrationJobDTO(
        job_id=str(job.job_id),
        status=job.status.value,
        source_system=job.config.source_system,
        target_org_id=job.config.target_org_id,
        initiated_by=job.initiated_by,
        dry_run=job.config.dry_run,
        batch_size=job.config.batch_size,
        error_threshold_percent=job.config.error_threshold_percent,
        record_types=list(job.config.record_types),
        total_records=job.counters.total_records,
        records_succeeded=job.counters.records_succeeded,
        records_failed=job.counters.records_failed,
        records_skipped=job.counters.records_skipped,
        completion_percent=job.counters.completion_percent,
        error_rate_percent=job.counters.error_rate_percent,
        current_phase=job.current_phase.value if job.current_phase else None,
        phases=phases,
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        created_at=job.created_at.isoformat(),
        duration_seconds=job.duration_seconds,
    )

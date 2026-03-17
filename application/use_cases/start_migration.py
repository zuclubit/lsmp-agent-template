"""
StartMigration use case.

Orchestrates the initiation of a new migration run:
  1. Validates all prerequisites (Salesforce connectivity, no active job, etc.)
  2. Creates and persists the MigrationJob aggregate.
  3. Publishes the MigrationStarted domain event.
  4. Returns the created job DTO.

Follows the Command/Handler pattern:
  - Input:  StartMigrationCommand (immutable command VO)
  - Output: MigrationJobDTO
  - Side effects: persists MigrationJob, publishes events, sends notification.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Protocol

from application.commands.migration_commands import StartMigrationCommand
from application.dto.migration_dto import MigrationJobDTO, PhaseProgressDTO
from domain.entities.migration_job import MigrationConfig, MigrationJob
from domain.events.migration_events import MigrationPhase, MigrationStatus
from domain.exceptions.domain_exceptions import (
    MigrationAlreadyInProgress,
    MigrationPrerequisiteNotMet,
    ValidationError,
)
from domain.repositories.migration_repository import MigrationRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secondary port: prerequisite checker
# ---------------------------------------------------------------------------


class PrerequisiteChecker(Protocol):
    """
    Secondary port: checks that all preconditions for migration are satisfied.

    Implemented in the adapters layer (e.g. by a class that actually calls
    the Salesforce API).  Declared here as a Protocol so the use case only
    depends on an interface, not a concrete implementation.
    """

    async def check_salesforce_connectivity(self, org_id: str) -> bool:
        """Return True if the Salesforce org is reachable and authenticated."""
        ...

    async def check_salesforce_permissions(self, org_id: str, record_types: list[str]) -> list[str]:
        """
        Return a list of missing permissions.
        Empty list = all permissions satisfied.
        """
        ...

    async def check_source_system_connectivity(self, source_system: str) -> bool:
        """Return True if the legacy data source is accessible."""
        ...

    async def estimate_record_counts(self, source_system: str, record_types: list[str]) -> dict[str, int]:
        """Return a mapping of record_type → estimated count."""
        ...


class EventPublisher(Protocol):
    """Secondary port: publishes domain events to the event bus."""

    async def publish_all(self, events: list) -> None:
        ...


class NotificationPort(Protocol):
    """Secondary port: sends notifications."""

    async def notify_migration_started(self, job_dto: MigrationJobDTO) -> None:
        ...


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class StartMigrationUseCase:
    """
    Application service that handles StartMigrationCommand.

    Dependencies are injected via constructor (dependency inversion).
    All dependencies are referenced by interface (Protocol), not by
    concrete implementation class.
    """

    def __init__(
        self,
        migration_repository: MigrationRepository,
        prerequisite_checker: PrerequisiteChecker,
        event_publisher: EventPublisher,
        notification_port: NotificationPort,
    ) -> None:
        self._migration_repo = migration_repository
        self._prerequisite_checker = prerequisite_checker
        self._event_publisher = event_publisher
        self._notification_port = notification_port

    async def execute(self, command: StartMigrationCommand) -> MigrationJobDTO:
        """
        Execute the StartMigrationCommand.

        Raises:
            ValidationError:             If the command payload is invalid.
            MigrationAlreadyInProgress:  If another job is running/paused.
            MigrationPrerequisiteNotMet: If connectivity or permission checks fail.
        """
        logger.info(
            "StartMigration: issued_by=%s source=%s dry_run=%s",
            command.issued_by,
            command.source_system,
            command.dry_run,
        )

        # ------------------------------------------------------------------
        # 1. Validate command input
        # ------------------------------------------------------------------
        self._validate_command(command)

        # ------------------------------------------------------------------
        # 2. Guard: no active migration in progress
        # ------------------------------------------------------------------
        active_job = await self._migration_repo.find_active()
        if active_job is not None:
            raise MigrationAlreadyInProgress(existing_job_id=active_job.job_id)

        # ------------------------------------------------------------------
        # 3. Run prerequisite checks (skip in dry-run for faster iteration)
        # ------------------------------------------------------------------
        if not command.dry_run:
            await self._run_prerequisite_checks(command)

        # ------------------------------------------------------------------
        # 4. Estimate total record count for the job
        # ------------------------------------------------------------------
        total_records = 0
        if not command.dry_run:
            try:
                counts = await self._prerequisite_checker.estimate_record_counts(
                    command.source_system,
                    list(command.record_types),
                )
                total_records = sum(counts.values())
                logger.info("Estimated record counts: %s (total=%d)", counts, total_records)
            except Exception as exc:
                logger.warning("Could not estimate record counts: %s", exc)

        # ------------------------------------------------------------------
        # 5. Build MigrationConfig value object
        # ------------------------------------------------------------------
        phases = (
            tuple(command.phases_to_run)
            if command.phases_to_run
            else tuple(p.value for p in MigrationPhase)
        )

        config = MigrationConfig(
            source_system=command.source_system,
            target_org_id=command.target_org_id,
            record_types=tuple(command.record_types),
            batch_size=command.batch_size,
            max_retries=command.max_retries,
            dry_run=command.dry_run,
            phases_to_run=phases,
            error_threshold_percent=command.error_threshold_percent,
            notification_emails=tuple(command.notification_emails),
        )

        # ------------------------------------------------------------------
        # 6. Create aggregate and start it (raises MigrationStarted event)
        # ------------------------------------------------------------------
        job = MigrationJob.create(
            config=config,
            initiated_by=command.issued_by,
            total_records=total_records,
        )
        job.start()

        # ------------------------------------------------------------------
        # 7. Persist
        # ------------------------------------------------------------------
        saved_job = await self._migration_repo.save(job)
        logger.info("MigrationJob persisted: %s", saved_job.job_id)

        # ------------------------------------------------------------------
        # 8. Publish domain events
        # ------------------------------------------------------------------
        events = saved_job.collect_events()
        await self._event_publisher.publish_all(events)
        logger.debug("Published %d domain events", len(events))

        # ------------------------------------------------------------------
        # 9. Send notification
        # ------------------------------------------------------------------
        job_dto = self._to_dto(saved_job)
        try:
            await self._notification_port.notify_migration_started(job_dto)
        except Exception as exc:
            # Notification failure must not abort the migration
            logger.warning("Failed to send start notification: %s", exc)

        return job_dto

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_command(command: StartMigrationCommand) -> None:
        """Validate structural correctness of the command payload."""
        if not command.source_system:
            raise ValidationError("source_system", command.source_system, "source_system is required")
        if not command.target_org_id:
            raise ValidationError("target_org_id", command.target_org_id, "target_org_id is required")
        if not command.record_types:
            raise ValidationError("record_types", command.record_types, "at least one record type must be specified")
        if not command.issued_by:
            raise ValidationError("issued_by", command.issued_by, "issued_by is required")
        if command.batch_size < 1 or command.batch_size > 2000:
            raise ValidationError(
                "batch_size", command.batch_size, "batch_size must be between 1 and 2000"
            )

    async def _run_prerequisite_checks(self, command: StartMigrationCommand) -> None:
        """Run all prerequisite checks and raise if any fail."""
        # Salesforce connectivity
        sf_ok = await self._prerequisite_checker.check_salesforce_connectivity(
            command.target_org_id
        )
        if not sf_ok:
            raise MigrationPrerequisiteNotMet(
                prerequisite="SALESFORCE_CONNECTIVITY",
                detail=f"Cannot connect to Salesforce org {command.target_org_id}",
            )

        # Salesforce permissions
        missing_permissions = await self._prerequisite_checker.check_salesforce_permissions(
            command.target_org_id,
            list(command.record_types),
        )
        if missing_permissions:
            raise MigrationPrerequisiteNotMet(
                prerequisite="SALESFORCE_PERMISSIONS",
                detail=f"Missing Salesforce permissions: {', '.join(missing_permissions)}",
            )

        # Source system connectivity
        source_ok = await self._prerequisite_checker.check_source_system_connectivity(
            command.source_system
        )
        if not source_ok:
            raise MigrationPrerequisiteNotMet(
                prerequisite="SOURCE_SYSTEM_CONNECTIVITY",
                detail=f"Cannot connect to source system '{command.source_system}'",
            )

    @staticmethod
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

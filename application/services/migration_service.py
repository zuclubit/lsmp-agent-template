"""
MigrationService – application service orchestrating migration operations.

This service coordinates between:
  - Domain entities and aggregates
  - Repository ports (account, migration job)
  - Domain event publishing
  - Notification sending

It is the primary entry point for application-level migration logic that does
not fit neatly into a single use case (e.g. phased orchestration, batch
processing loops, progress monitoring).

Design:
  - Depends only on abstract ports (Protocols / ABCs), not on concrete adapters.
  - Stateless: all state lives in aggregates and repositories.
  - Async throughout to support non-blocking I/O.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional, Protocol
from uuid import UUID

from application.dto.migration_dto import AccountMigrationResultDTO, MigrationJobDTO, PhaseProgressDTO
from domain.entities.account import Account
from domain.entities.migration_job import MigrationJob
from domain.events.migration_events import MigrationPhase, MigrationStatus
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    MigrationJobNotFound,
    SalesforceApiError,
    ValidationError,
)
from domain.repositories.account_repository import AccountRepository, AccountCriteria
from domain.repositories.migration_repository import MigrationRepository
from domain.value_objects.salesforce_id import SalesforceId

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secondary ports
# ---------------------------------------------------------------------------


class EventPublisher(Protocol):
    async def publish_all(self, events: list) -> None: ...


class SalesforceAccountPort(Protocol):
    """Port: writes Account records to Salesforce."""

    async def upsert_account(self, payload: dict[str, Any]) -> str:
        """Return the Salesforce record ID (18-char) on success."""
        ...

    async def upsert_accounts_bulk(
        self, payloads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Bulk upsert; returns list of {legacy_id, sf_id, status, error}."""
        ...


class NotificationPort(Protocol):
    async def notify_phase_completed(self, job_dto: MigrationJobDTO, phase: str) -> None: ...
    async def notify_migration_failed(self, job_dto: MigrationJobDTO, error: str) -> None: ...
    async def notify_migration_completed(self, job_dto: MigrationJobDTO) -> None: ...


# ---------------------------------------------------------------------------
# Application service
# ---------------------------------------------------------------------------


class MigrationService:
    """
    Orchestrates the full migration pipeline for Account (and Contact) records.

    Lifecycle:
        1. run_extraction_phase()       – reads from legacy DB, stages accounts
        2. run_validation_phase()       – validates staged data (delegates)
        3. run_transformation_phase()   – applies field mappings, enrichment
        4. run_load_phase()             – writes to Salesforce in batches
        5. run_verification_phase()     – reconciles counts and spot-checks
        6. run_reconciliation_phase()   – generates final diff report
    """

    def __init__(
        self,
        migration_repository: MigrationRepository,
        account_repository: AccountRepository,
        salesforce_account_port: SalesforceAccountPort,
        event_publisher: EventPublisher,
        notification_port: NotificationPort,
    ) -> None:
        self._migration_repo = migration_repository
        self._account_repo = account_repository
        self._sf_account_port = salesforce_account_port
        self._event_publisher = event_publisher
        self._notification_port = notification_port

    # ------------------------------------------------------------------
    # Public orchestration method
    # ------------------------------------------------------------------

    async def run_load_phase(self, job_id: UUID) -> MigrationJobDTO:
        """
        Execute the DATA_LOAD phase for the given migration job.

        Iterates through all unmigrated accounts in batches, upserts them to
        Salesforce, marks them as migrated, and updates counters on the job.
        """
        job = await self._load_job(job_id)
        logger.info("Starting DATA_LOAD phase for job %s", job_id)

        phase = MigrationPhase.DATA_LOAD
        job.begin_phase(phase)
        await self._migration_repo.save(job)

        total_processed = 0
        total_succeeded = 0
        total_failed = 0
        total_skipped = 0

        try:
            async for batch in self._iter_account_batches(job.config.batch_size):
                if job.status != MigrationStatus.RUNNING:
                    logger.warning("Job %s is no longer RUNNING; halting load phase", job_id)
                    break

                batch_results = await self._process_account_batch(
                    batch=batch,
                    job=job,
                    dry_run=job.config.dry_run,
                )

                succeeded = sum(1 for r in batch_results if r.status == "succeeded")
                failed = sum(1 for r in batch_results if r.status == "failed")
                skipped = sum(1 for r in batch_results if r.status == "skipped")

                total_processed += len(batch_results)
                total_succeeded += succeeded
                total_failed += failed
                total_skipped += skipped

                logger.info(
                    "Batch processed: +%d succeeded, +%d failed, +%d skipped "
                    "(running totals: %d/%d/%d)",
                    succeeded, failed, skipped,
                    total_succeeded, total_failed, total_skipped,
                )

                # Publish events accumulated on account aggregates
                events: list = []
                for account in batch:
                    events.extend(account.collect_events())
                if events:
                    await self._event_publisher.publish_all(events)

        except Exception as exc:
            logger.exception("DATA_LOAD phase failed: %s", exc)
            job.fail(
                failed_phase=phase.value,
                error_code="LOAD_PHASE_ERROR",
                error_message=str(exc),
            )
            await self._migration_repo.save(job)
            events = job.collect_events()
            await self._event_publisher.publish_all(events)
            job_dto = self._to_dto(job)
            await self._notification_port.notify_migration_failed(job_dto, str(exc))
            return job_dto

        # Complete the phase
        job.complete_phase(
            phase=phase,
            records_processed=total_processed,
            records_succeeded=total_succeeded,
            records_failed=total_failed,
            records_skipped=total_skipped,
        )
        saved_job = await self._migration_repo.save(job)
        events = saved_job.collect_events()
        await self._event_publisher.publish_all(events)

        job_dto = self._to_dto(saved_job)
        await self._notification_port.notify_phase_completed(job_dto, phase.value)
        return job_dto

    async def complete_job(self, job_id: UUID, report_url: Optional[str] = None) -> MigrationJobDTO:
        """Transition the job to COMPLETED state."""
        job = await self._load_job(job_id)
        job.complete(report_url=report_url)
        saved_job = await self._migration_repo.save(job)
        events = saved_job.collect_events()
        await self._event_publisher.publish_all(events)
        job_dto = self._to_dto(saved_job)
        await self._notification_port.notify_migration_completed(job_dto)
        return job_dto

    async def get_job_status(self, job_id: UUID) -> MigrationJobDTO:
        """Return the current status of a migration job."""
        job = await self._load_job(job_id)
        return self._to_dto(job)

    async def get_active_job(self) -> Optional[MigrationJobDTO]:
        """Return the currently active job, or None."""
        job = await self._migration_repo.find_active()
        if job is None:
            return None
        return self._to_dto(job)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_job(self, job_id: UUID) -> MigrationJob:
        job = await self._migration_repo.find_by_id(job_id)
        if job is None:
            raise MigrationJobNotFound(job_id=job_id)
        return job

    async def _iter_account_batches(
        self, batch_size: int
    ) -> AsyncIterator[list[Account]]:
        """Yield successive batches of unmigrated accounts."""
        offset = 0
        while True:
            criteria = AccountCriteria(
                is_migrated=False,
                limit=batch_size,
                offset=offset,
            )
            result = await self._account_repo.find_by_criteria(criteria)
            if not result.items:
                break
            yield result.items
            if not result.has_more:
                break
            offset += batch_size

    async def _process_account_batch(
        self,
        batch: list[Account],
        job: MigrationJob,
        dry_run: bool,
    ) -> list[AccountMigrationResultDTO]:
        """Upsert a batch of accounts to Salesforce and update domain state."""
        payloads = [acct.to_salesforce_payload() for acct in batch]

        if dry_run:
            # In dry-run mode, simulate success without writing
            return [
                AccountMigrationResultDTO(
                    legacy_id=acct.legacy_id,
                    name=acct.name,
                    status="dry_run",
                    salesforce_id=None,
                )
                for acct in batch
            ]

        try:
            bulk_results = await self._sf_account_port.upsert_accounts_bulk(payloads)
        except SalesforceApiError as exc:
            logger.error("Bulk upsert API error: %s", exc)
            return [
                AccountMigrationResultDTO(
                    legacy_id=acct.legacy_id,
                    name=acct.name,
                    status="failed",
                    error_code=exc.sf_error_code,
                    error_message=exc.sf_message,
                )
                for acct in batch
            ]

        # Map results back to domain accounts
        results_by_legacy_id = {r["legacy_id"]: r for r in bulk_results}
        dto_results: list[AccountMigrationResultDTO] = []

        accounts_to_save: list[Account] = []
        for acct in batch:
            result = results_by_legacy_id.get(acct.legacy_id, {})
            if result.get("status") == "succeeded" and result.get("sf_id"):
                try:
                    sf_id = SalesforceId(result["sf_id"])
                    acct.mark_migrated(
                        salesforce_id=sf_id,
                        migration_job_id=str(job.job_id),
                    )
                    accounts_to_save.append(acct)
                    dto_results.append(
                        AccountMigrationResultDTO(
                            legacy_id=acct.legacy_id,
                            name=acct.name,
                            status="succeeded",
                            salesforce_id=str(sf_id),
                        )
                    )
                except (BusinessRuleViolation, ValidationError) as exc:
                    logger.warning("Could not mark account %s as migrated: %s", acct.legacy_id, exc)
                    dto_results.append(
                        AccountMigrationResultDTO(
                            legacy_id=acct.legacy_id,
                            name=acct.name,
                            status="failed",
                            error_code="DOMAIN_ERROR",
                            error_message=str(exc),
                        )
                    )
            else:
                dto_results.append(
                    AccountMigrationResultDTO(
                        legacy_id=acct.legacy_id,
                        name=acct.name,
                        status="failed",
                        error_code=result.get("error_code", "UNKNOWN"),
                        error_message=result.get("error_message", "Unknown error"),
                    )
                )

        if accounts_to_save:
            await self._account_repo.save_batch(accounts_to_save)

        return dto_results

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

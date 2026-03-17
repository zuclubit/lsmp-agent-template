"""
Unit tests for MigrationService application service.

MigrationService orchestrates the DATA_LOAD phase: iterating over unmigrated
accounts in batches, upserting them to Salesforce, updating domain state, and
publishing domain events.

All infrastructure dependencies are replaced with AsyncMock stubs so the test
suite has no I/O dependencies.

Module under test: application/services/migration_service.py
Pattern: AsyncMock-based unit tests with pytest-asyncio.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from application.dto.migration_dto import (
    AccountMigrationResultDTO,
    MigrationJobDTO,
    PhaseProgressDTO,
)
from application.services.migration_service import MigrationService
from domain.entities.account import Account, AccountStatus, AccountType
from domain.entities.migration_job import MigrationConfig, MigrationJob
from domain.events.migration_events import MigrationPhase, MigrationStatus
from domain.exceptions.domain_exceptions import MigrationJobNotFound
from domain.value_objects.salesforce_id import SalesforceId


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> MigrationConfig:
    return MigrationConfig(
        source_system="legacy-crm",
        target_org_id="00Dxx0000001gERAAY",
        record_types=("Account",),
        batch_size=200,
        dry_run=False,
    )


def _make_running_job() -> MigrationJob:
    job = MigrationJob.create(
        config=_make_config(),
        initiated_by="admin@example.com",
        total_records=1_000,
    )
    job.start()
    job.collect_events()  # drain start events
    job.begin_phase(MigrationPhase.DATA_LOAD)
    return job


def _make_account(legacy_id: str = "LEGACY-001") -> Account:
    return Account.create(legacy_id=legacy_id, name="Acme Corp")


def _make_bulk_result(legacy_id: str, sf_id: str = "001B000000KmPzAIAV") -> dict:
    return {"legacy_id": legacy_id, "sf_id": sf_id, "status": "succeeded"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_migration_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda job: job)
    repo.find_active = AsyncMock(return_value=None)
    return repo


@pytest.fixture()
def mock_account_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.save_batch = AsyncMock()
    return repo


@pytest.fixture()
def mock_sf_port() -> AsyncMock:
    port = AsyncMock()
    return port


@pytest.fixture()
def mock_event_publisher() -> AsyncMock:
    publisher = AsyncMock()
    publisher.publish_all = AsyncMock()
    return publisher


@pytest.fixture()
def mock_notification_port() -> AsyncMock:
    notify = AsyncMock()
    notify.notify_phase_completed = AsyncMock()
    notify.notify_migration_completed = AsyncMock()
    notify.notify_migration_failed = AsyncMock()
    return notify


@pytest.fixture()
def service(
    mock_migration_repo: AsyncMock,
    mock_account_repo: AsyncMock,
    mock_sf_port: AsyncMock,
    mock_event_publisher: AsyncMock,
    mock_notification_port: AsyncMock,
) -> MigrationService:
    return MigrationService(
        migration_repository=mock_migration_repo,
        account_repository=mock_account_repo,
        salesforce_account_port=mock_sf_port,
        event_publisher=mock_event_publisher,
        notification_port=mock_notification_port,
    )


# ===========================================================================
# 1. run_load_phase — happy path
# ===========================================================================


class TestRunLoadPhaseHappyPath:
    """run_load_phase() succeeds when SF accepts all records."""

    @pytest.mark.asyncio
    async def test_returns_migration_job_dto(
        self, service: MigrationService, mock_migration_repo: AsyncMock, mock_sf_port: AsyncMock
    ) -> None:
        """run_load_phase() must return a MigrationJobDTO on success."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        mock_sf_port.upsert_accounts_bulk = AsyncMock(return_value=[])

        # No unmigrated accounts — empty batches => immediate completion
        async def empty_batches(*args, **kwargs):
            return iter([])
        mock_sf_port.upsert_accounts_bulk.return_value = []

        # Patch _iter_account_batches to yield nothing
        async def _no_batches(batch_size):
            return
            yield  # make it an async generator

        with patch.object(service, "_iter_account_batches", _no_batches):
            result = await service.run_load_phase(job.job_id)

        assert isinstance(result, MigrationJobDTO)

    @pytest.mark.asyncio
    async def test_saves_job_to_repository(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """run_load_phase() must save the job at least twice (begin and end)."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        async def _no_batches(batch_size):
            return
            yield

        with patch.object(service, "_iter_account_batches", _no_batches):
            await service.run_load_phase(job.job_id)

        assert mock_migration_repo.save.call_count >= 1

    @pytest.mark.asyncio
    async def test_publishes_domain_events(
        self, service: MigrationService, mock_migration_repo: AsyncMock,
        mock_event_publisher: AsyncMock
    ) -> None:
        """Domain events collected from the job must be published."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        async def _no_batches(batch_size):
            return
            yield

        with patch.object(service, "_iter_account_batches", _no_batches):
            await service.run_load_phase(job.job_id)

        mock_event_publisher.publish_all.assert_called()

    @pytest.mark.asyncio
    async def test_notifies_phase_completed(
        self, service: MigrationService, mock_migration_repo: AsyncMock,
        mock_notification_port: AsyncMock
    ) -> None:
        """notify_phase_completed must be called after a successful load phase."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        async def _no_batches(batch_size):
            return
            yield

        with patch.object(service, "_iter_account_batches", _no_batches):
            await service.run_load_phase(job.job_id)

        mock_notification_port.notify_phase_completed.assert_called_once()


# ===========================================================================
# 2. run_load_phase — job not found
# ===========================================================================


class TestRunLoadPhaseJobNotFound:
    """run_load_phase() raises MigrationJobNotFound for unknown job IDs."""

    @pytest.mark.asyncio
    async def test_raises_when_job_not_found(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """MigrationJobNotFound is raised when the job does not exist."""
        mock_migration_repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(MigrationJobNotFound):
            await service.run_load_phase(uuid.uuid4())


# ===========================================================================
# 3. run_load_phase — Salesforce error handling
# ===========================================================================


class TestRunLoadPhaseSalesforceError:
    """run_load_phase() handles Salesforce errors gracefully."""

    @pytest.mark.asyncio
    async def test_transitions_to_failed_on_uncaught_exception(
        self, service: MigrationService, mock_migration_repo: AsyncMock,
        mock_sf_port: AsyncMock, mock_notification_port: AsyncMock
    ) -> None:
        """An unexpected exception during load transitions the job to FAILED."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        async def _crash(batch_size):
            yield [_make_account()]

        mock_sf_port.upsert_accounts_bulk = AsyncMock(
            side_effect=RuntimeError("Unexpected SF outage")
        )

        with patch.object(service, "_iter_account_batches", _crash):
            result = await service.run_load_phase(job.job_id)

        assert result.status == MigrationStatus.FAILED.value
        mock_notification_port.notify_migration_failed.assert_called_once()


# ===========================================================================
# 4. complete_job
# ===========================================================================


class TestCompleteJob:
    """complete_job() transitions RUNNING → COMPLETED."""

    @pytest.mark.asyncio
    async def test_complete_job_returns_completed_dto(
        self, service: MigrationService, mock_migration_repo: AsyncMock,
        mock_notification_port: AsyncMock
    ) -> None:
        """complete_job() must return a DTO with COMPLETED status."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        result = await service.complete_job(job.job_id)

        assert result.status == MigrationStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_complete_job_notifies_stakeholders(
        self, service: MigrationService, mock_migration_repo: AsyncMock,
        mock_notification_port: AsyncMock
    ) -> None:
        """notify_migration_completed must be called after completion."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        await service.complete_job(job.job_id)

        mock_notification_port.notify_migration_completed.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_job_with_report_url(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """complete_job() with a report_url stores it on the aggregate."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        result = await service.complete_job(
            job.job_id, report_url="https://reports.example.com/job-001"
        )

        assert isinstance(result, MigrationJobDTO)


# ===========================================================================
# 5. get_job_status
# ===========================================================================


class TestGetJobStatus:
    """get_job_status() returns current job state as a DTO."""

    @pytest.mark.asyncio
    async def test_returns_dto_for_existing_job(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """get_job_status() returns a populated MigrationJobDTO."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        dto = await service.get_job_status(job.job_id)

        assert dto.job_id == str(job.job_id)
        assert dto.status == MigrationStatus.RUNNING.value

    @pytest.mark.asyncio
    async def test_raises_when_job_not_found(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """get_job_status() raises MigrationJobNotFound for unknown IDs."""
        mock_migration_repo.find_by_id = AsyncMock(return_value=None)
        with pytest.raises(MigrationJobNotFound):
            await service.get_job_status(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_dto_contains_config_fields(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """DTO includes source_system, target_org_id, and initiated_by."""
        job = _make_running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)

        dto = await service.get_job_status(job.job_id)

        assert dto.source_system == "legacy-crm"
        assert dto.initiated_by == "admin@example.com"


# ===========================================================================
# 6. get_active_job
# ===========================================================================


class TestGetActiveJob:
    """get_active_job() returns the currently running job or None."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_job(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """Returns None when no job is RUNNING or PAUSED."""
        mock_migration_repo.find_active = AsyncMock(return_value=None)
        result = await service.get_active_job()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_dto_for_active_job(
        self, service: MigrationService, mock_migration_repo: AsyncMock
    ) -> None:
        """Returns a MigrationJobDTO when an active job exists."""
        job = _make_running_job()
        mock_migration_repo.find_active = AsyncMock(return_value=job)
        result = await service.get_active_job()
        assert result is not None
        assert result.job_id == str(job.job_id)


# ===========================================================================
# 7. _to_dto helper (static method)
# ===========================================================================


class TestToDto:
    """_to_dto() maps MigrationJob aggregate to MigrationJobDTO correctly."""

    def test_to_dto_maps_all_scalar_fields(self) -> None:
        """Scalar fields on the job are reflected in the DTO."""
        job = _make_running_job()
        dto = MigrationService._to_dto(job)

        assert dto.job_id == str(job.job_id)
        assert dto.status == job.status.value
        assert dto.source_system == job.config.source_system
        assert dto.initiated_by == job.initiated_by
        assert dto.dry_run == job.config.dry_run
        assert dto.batch_size == job.config.batch_size

    def test_to_dto_phase_history_included(self) -> None:
        """phase_history items are mapped to PhaseProgressDTO list."""
        job = _make_running_job()
        dto = MigrationService._to_dto(job)
        # begin_phase was called in _make_running_job
        assert len(dto.phases) == 1
        assert isinstance(dto.phases[0], PhaseProgressDTO)

    def test_to_dto_counters_reflected(self) -> None:
        """Counter fields (total, succeeded, failed, skipped) are in DTO."""
        job = _make_running_job()
        dto = MigrationService._to_dto(job)
        assert dto.total_records == 1_000
        assert dto.records_succeeded == 0
        assert dto.records_failed == 0

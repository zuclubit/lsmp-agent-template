"""
Unit tests for application use cases (Clean Architecture).

Each use case is tested in complete isolation — infrastructure ports are
replaced with AsyncMock stubs so tests run without any I/O.

Modules under test:
  - application/use_cases/start_migration.py  → StartMigrationUseCase
  - application/use_cases/pause_migration.py  → PauseMigrationUseCase

Pattern: One class per use case, AsyncMock for all secondary ports.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.commands.migration_commands import (
    PauseMigrationCommand,
    StartMigrationCommand,
)
from application.dto.migration_dto import MigrationJobDTO
from application.use_cases.pause_migration import PauseMigrationUseCase
from application.use_cases.start_migration import StartMigrationUseCase
from domain.entities.migration_job import MigrationConfig, MigrationJob
from domain.events.migration_events import MigrationPhase, MigrationStatus
from domain.exceptions.domain_exceptions import (
    InvalidStateTransition,
    MigrationAlreadyInProgress,
    MigrationJobNotFound,
    MigrationPrerequisiteNotMet,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config() -> MigrationConfig:
    return MigrationConfig(
        source_system="legacy-erp",
        target_org_id="00Dxx0000001gERAAY",
        record_types=("Account",),
        batch_size=200,
    )


def _running_job() -> MigrationJob:
    job = MigrationJob.create(
        config=_make_config(),
        initiated_by="admin@example.com",
        total_records=5_000,
    )
    job.start()
    job.collect_events()
    return job


def _pending_job() -> MigrationJob:
    return MigrationJob.create(
        config=_make_config(),
        initiated_by="admin@example.com",
        total_records=5_000,
    )


# ---------------------------------------------------------------------------
# StartMigrationUseCase fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_migration_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_active = AsyncMock(return_value=None)
    repo.save = AsyncMock(side_effect=lambda job: job)
    return repo


@pytest.fixture()
def mock_prerequisite_checker() -> AsyncMock:
    checker = AsyncMock()
    checker.check_salesforce_connectivity = AsyncMock(return_value=True)
    checker.check_salesforce_permissions = AsyncMock(return_value=[])
    checker.check_source_system_connectivity = AsyncMock(return_value=True)
    checker.estimate_record_counts = AsyncMock(return_value={"Account": 5_000})
    return checker


@pytest.fixture()
def mock_event_publisher() -> AsyncMock:
    publisher = AsyncMock()
    publisher.publish_all = AsyncMock()
    return publisher


@pytest.fixture()
def mock_notification() -> AsyncMock:
    notify = AsyncMock()
    notify.notify_migration_started = AsyncMock()
    notify.notify_migration_paused = AsyncMock()
    return notify


@pytest.fixture()
def start_use_case(
    mock_migration_repo: AsyncMock,
    mock_prerequisite_checker: AsyncMock,
    mock_event_publisher: AsyncMock,
    mock_notification: AsyncMock,
) -> StartMigrationUseCase:
    return StartMigrationUseCase(
        migration_repository=mock_migration_repo,
        prerequisite_checker=mock_prerequisite_checker,
        event_publisher=mock_event_publisher,
        notification_port=mock_notification,
    )


@pytest.fixture()
def valid_start_command() -> StartMigrationCommand:
    return StartMigrationCommand(
        command_id=str(uuid.uuid4()),
        issued_by="admin@example.com",
        source_system="legacy-erp",
        target_org_id="00Dxx0000001gERAAY",
        record_types=("Account", "Contact"),
        batch_size=200,
        dry_run=False,
        error_threshold_percent=5.0,
    )


@pytest.fixture()
def dry_run_start_command() -> StartMigrationCommand:
    return StartMigrationCommand(
        command_id=str(uuid.uuid4()),
        issued_by="dev@example.com",
        source_system="legacy-erp",
        target_org_id="00Dxx0000001gERAAY",
        record_types=("Account",),
        batch_size=200,
        dry_run=True,
    )


# ===========================================================================
# StartMigrationUseCase
# ===========================================================================


class TestStartMigrationUseCaseHappyPath:
    """StartMigrationUseCase — successful execution."""

    @pytest.mark.asyncio
    async def test_returns_migration_job_dto(
        self, start_use_case: StartMigrationUseCase, valid_start_command: StartMigrationCommand
    ) -> None:
        """execute() returns a MigrationJobDTO on success."""
        result = await start_use_case.execute(valid_start_command)
        assert isinstance(result, MigrationJobDTO)

    @pytest.mark.asyncio
    async def test_returned_dto_has_running_status(
        self, start_use_case: StartMigrationUseCase, valid_start_command: StartMigrationCommand
    ) -> None:
        """The job immediately transitions to RUNNING after creation."""
        result = await start_use_case.execute(valid_start_command)
        assert result.status == MigrationStatus.RUNNING.value

    @pytest.mark.asyncio
    async def test_persists_job_to_repository(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """Job must be saved to the repository exactly once."""
        await start_use_case.execute(valid_start_command)
        mock_migration_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_publishes_domain_events(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_event_publisher: AsyncMock,
    ) -> None:
        """Domain events collected from the job must be published."""
        await start_use_case.execute(valid_start_command)
        mock_event_publisher.publish_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_started_notification(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_notification: AsyncMock,
    ) -> None:
        """notify_migration_started must be called after job is created."""
        await start_use_case.execute(valid_start_command)
        mock_notification.notify_migration_started.assert_called_once()

    @pytest.mark.asyncio
    async def test_dto_reflects_command_source_system(
        self, start_use_case: StartMigrationUseCase, valid_start_command: StartMigrationCommand
    ) -> None:
        """DTO.source_system matches the command's source_system."""
        result = await start_use_case.execute(valid_start_command)
        assert result.source_system == valid_start_command.source_system

    @pytest.mark.asyncio
    async def test_dry_run_skips_prerequisite_checks(
        self,
        start_use_case: StartMigrationUseCase,
        dry_run_start_command: StartMigrationCommand,
        mock_prerequisite_checker: AsyncMock,
    ) -> None:
        """dry_run=True must skip Salesforce connectivity checks."""
        await start_use_case.execute(dry_run_start_command)
        mock_prerequisite_checker.check_salesforce_connectivity.assert_not_called()
        mock_prerequisite_checker.check_source_system_connectivity.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_skips_record_count_estimation(
        self,
        start_use_case: StartMigrationUseCase,
        dry_run_start_command: StartMigrationCommand,
        mock_prerequisite_checker: AsyncMock,
    ) -> None:
        """dry_run=True must skip record count estimation."""
        await start_use_case.execute(dry_run_start_command)
        mock_prerequisite_checker.estimate_record_counts.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_abort_migration(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_notification: AsyncMock,
    ) -> None:
        """A notification error must not propagate — migration proceeds normally."""
        mock_notification.notify_migration_started.side_effect = RuntimeError(
            "Email service down"
        )
        result = await start_use_case.execute(valid_start_command)
        assert result.status == MigrationStatus.RUNNING.value


class TestStartMigrationUseCaseGuards:
    """StartMigrationUseCase — validation and guard checks."""

    @pytest.mark.asyncio
    async def test_raises_if_migration_already_in_progress(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """Raises MigrationAlreadyInProgress when another job is active."""
        mock_migration_repo.find_active = AsyncMock(return_value=_running_job())
        with pytest.raises(MigrationAlreadyInProgress):
            await start_use_case.execute(valid_start_command)

    @pytest.mark.asyncio
    async def test_raises_if_source_system_blank(
        self, start_use_case: StartMigrationUseCase
    ) -> None:
        """Blank source_system raises ValidationError before any DB call."""
        cmd = StartMigrationCommand(
            issued_by="admin",
            source_system="",
            target_org_id="00Dxx",
            record_types=("Account",),
        )
        with pytest.raises(ValidationError) as exc_info:
            await start_use_case.execute(cmd)
        assert exc_info.value.field == "source_system"

    @pytest.mark.asyncio
    async def test_raises_if_target_org_id_blank(
        self, start_use_case: StartMigrationUseCase
    ) -> None:
        """Blank target_org_id raises ValidationError."""
        cmd = StartMigrationCommand(
            issued_by="admin",
            source_system="legacy",
            target_org_id="",
            record_types=("Account",),
        )
        with pytest.raises(ValidationError) as exc_info:
            await start_use_case.execute(cmd)
        assert exc_info.value.field == "target_org_id"

    @pytest.mark.asyncio
    async def test_raises_if_record_types_empty(
        self, start_use_case: StartMigrationUseCase
    ) -> None:
        """Empty record_types raises ValidationError."""
        cmd = StartMigrationCommand(
            issued_by="admin",
            source_system="legacy",
            target_org_id="00Dxx",
            record_types=(),
        )
        with pytest.raises(ValidationError) as exc_info:
            await start_use_case.execute(cmd)
        assert exc_info.value.field == "record_types"

    @pytest.mark.asyncio
    async def test_raises_if_issued_by_blank(
        self, start_use_case: StartMigrationUseCase
    ) -> None:
        """Blank issued_by raises ValidationError."""
        cmd = StartMigrationCommand(
            issued_by="",
            source_system="legacy",
            target_org_id="00Dxx",
            record_types=("Account",),
        )
        with pytest.raises(ValidationError) as exc_info:
            await start_use_case.execute(cmd)
        assert exc_info.value.field == "issued_by"

    @pytest.mark.asyncio
    async def test_raises_if_salesforce_unreachable(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_prerequisite_checker: AsyncMock,
    ) -> None:
        """Raises MigrationPrerequisiteNotMet when SF connectivity check fails."""
        mock_prerequisite_checker.check_salesforce_connectivity = AsyncMock(return_value=False)
        with pytest.raises(MigrationPrerequisiteNotMet) as exc_info:
            await start_use_case.execute(valid_start_command)
        assert exc_info.value.prerequisite == "SALESFORCE_CONNECTIVITY"

    @pytest.mark.asyncio
    async def test_raises_if_permissions_missing(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_prerequisite_checker: AsyncMock,
    ) -> None:
        """Raises MigrationPrerequisiteNotMet when required permissions are absent."""
        mock_prerequisite_checker.check_salesforce_permissions = AsyncMock(
            return_value=["Account: create", "Contact: create"]
        )
        with pytest.raises(MigrationPrerequisiteNotMet) as exc_info:
            await start_use_case.execute(valid_start_command)
        assert exc_info.value.prerequisite == "SALESFORCE_PERMISSIONS"

    @pytest.mark.asyncio
    async def test_raises_if_source_system_unreachable(
        self,
        start_use_case: StartMigrationUseCase,
        valid_start_command: StartMigrationCommand,
        mock_prerequisite_checker: AsyncMock,
    ) -> None:
        """Raises MigrationPrerequisiteNotMet when source system is down."""
        mock_prerequisite_checker.check_source_system_connectivity = AsyncMock(
            return_value=False
        )
        with pytest.raises(MigrationPrerequisiteNotMet) as exc_info:
            await start_use_case.execute(valid_start_command)
        assert exc_info.value.prerequisite == "SOURCE_SYSTEM_CONNECTIVITY"

    @pytest.mark.parametrize(
        "batch_size",
        [0, -1, 2001],
        ids=["zero", "negative", "exceeds_max"],
    )
    @pytest.mark.asyncio
    async def test_raises_for_invalid_batch_size(
        self, start_use_case: StartMigrationUseCase, batch_size: int
    ) -> None:
        """Invalid batch_size raises ValidationError before any I/O."""
        cmd = StartMigrationCommand(
            issued_by="admin",
            source_system="legacy",
            target_org_id="00Dxx",
            record_types=("Account",),
            batch_size=batch_size,
        )
        with pytest.raises(ValidationError) as exc_info:
            await start_use_case.execute(cmd)
        assert exc_info.value.field == "batch_size"


# ===========================================================================
# PauseMigrationUseCase
# ===========================================================================


@pytest.fixture()
def pause_use_case(
    mock_migration_repo: AsyncMock,
    mock_event_publisher: AsyncMock,
    mock_notification: AsyncMock,
) -> PauseMigrationUseCase:
    return PauseMigrationUseCase(
        migration_repository=mock_migration_repo,
        event_publisher=mock_event_publisher,
        notification_port=mock_notification,
    )


class TestPauseMigrationUseCaseHappyPath:
    """PauseMigrationUseCase — successful pause of a running job."""

    @pytest.mark.asyncio
    async def test_returns_paused_dto(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """execute() returns a DTO with PAUSED status."""
        job = _running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        cmd = PauseMigrationCommand(
            issued_by="ops@example.com",
            job_id=str(job.job_id),
            reason="Scheduled maintenance",
        )
        result = await pause_use_case.execute(cmd)
        assert result.status == MigrationStatus.PAUSED.value

    @pytest.mark.asyncio
    async def test_persists_paused_job(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """Job is saved to the repository after pausing."""
        job = _running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        cmd = PauseMigrationCommand(
            issued_by="ops@example.com",
            job_id=str(job.job_id),
            reason="Test",
        )
        await pause_use_case.execute(cmd)
        mock_migration_repo.save.assert_called()

    @pytest.mark.asyncio
    async def test_publishes_paused_event(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
        mock_event_publisher: AsyncMock,
    ) -> None:
        """Domain events from the pause transition must be published."""
        job = _running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        cmd = PauseMigrationCommand(
            issued_by="ops@example.com",
            job_id=str(job.job_id),
            reason="Quota window",
        )
        await pause_use_case.execute(cmd)
        mock_event_publisher.publish_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_pause_notification(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
        mock_notification: AsyncMock,
    ) -> None:
        """notify_migration_paused must be called with the reason."""
        job = _running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        cmd = PauseMigrationCommand(
            issued_by="ops@example.com",
            job_id=str(job.job_id),
            reason="API rate limit approaching",
        )
        await pause_use_case.execute(cmd)
        mock_notification.notify_migration_paused.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_abort_pause(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
        mock_notification: AsyncMock,
    ) -> None:
        """A notification error must not propagate — pause still succeeds."""
        job = _running_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        mock_notification.notify_migration_paused.side_effect = RuntimeError("SMTP down")
        cmd = PauseMigrationCommand(
            issued_by="ops@example.com",
            job_id=str(job.job_id),
            reason="Test",
        )
        result = await pause_use_case.execute(cmd)
        assert result.status == MigrationStatus.PAUSED.value


class TestPauseMigrationUseCaseGuards:
    """PauseMigrationUseCase — validation and guard checks."""

    @pytest.mark.asyncio
    async def test_raises_if_job_id_blank(
        self, pause_use_case: PauseMigrationUseCase
    ) -> None:
        """Blank job_id raises ValidationError."""
        cmd = PauseMigrationCommand(issued_by="ops", job_id="", reason="test")
        with pytest.raises(ValidationError) as exc_info:
            await pause_use_case.execute(cmd)
        assert exc_info.value.field == "job_id"

    @pytest.mark.asyncio
    async def test_raises_if_job_id_not_uuid(
        self, pause_use_case: PauseMigrationUseCase
    ) -> None:
        """Non-UUID job_id raises ValidationError."""
        cmd = PauseMigrationCommand(issued_by="ops", job_id="not-a-uuid", reason="test")
        with pytest.raises(ValidationError) as exc_info:
            await pause_use_case.execute(cmd)
        assert exc_info.value.field == "job_id"

    @pytest.mark.asyncio
    async def test_raises_if_job_not_found(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """Raises MigrationJobNotFound when job ID does not exist."""
        mock_migration_repo.find_by_id = AsyncMock(return_value=None)
        cmd = PauseMigrationCommand(
            issued_by="ops", job_id=str(uuid.uuid4()), reason="test"
        )
        with pytest.raises(MigrationJobNotFound):
            await pause_use_case.execute(cmd)

    @pytest.mark.asyncio
    async def test_raises_if_job_not_running(
        self,
        pause_use_case: PauseMigrationUseCase,
        mock_migration_repo: AsyncMock,
    ) -> None:
        """Pausing a PENDING job raises InvalidStateTransition."""
        job = _pending_job()
        mock_migration_repo.find_by_id = AsyncMock(return_value=job)
        cmd = PauseMigrationCommand(
            issued_by="ops", job_id=str(job.job_id), reason="test"
        )
        with pytest.raises(InvalidStateTransition):
            await pause_use_case.execute(cmd)

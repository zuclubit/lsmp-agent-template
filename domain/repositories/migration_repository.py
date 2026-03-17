"""
Abstract MigrationJob repository interface (port).

Defines the persistence contract for MigrationJob aggregates.
Lives in the domain layer with no infrastructure dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from domain.entities.migration_job import MigrationJob
from domain.events.migration_events import MigrationStatus, MigrationPhase


# ---------------------------------------------------------------------------
# Criteria / filter value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationJobCriteria:
    """Filter criteria for querying migration jobs."""

    statuses: Optional[frozenset[MigrationStatus]] = None
    initiated_by: Optional[str] = None
    source_system: Optional[str] = None
    started_after: Optional[datetime] = None
    started_before: Optional[datetime] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    dry_run_only: Optional[bool] = None
    limit: int = 50
    offset: int = 0
    order_by: str = "created_at"
    order_asc: bool = False  # newest first by default


@dataclass(frozen=True)
class MigrationSummary:
    """
    Lightweight projection of a MigrationJob for list views.

    Avoids loading the full aggregate (including all phase history) when only
    summary information is needed (e.g., dashboard, report index).
    """

    job_id: UUID
    status: MigrationStatus
    source_system: str
    initiated_by: str
    dry_run: bool
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    current_phase: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime

    @property
    def success_rate(self) -> float:
        if self.total_records == 0:
            return 1.0
        return self.records_succeeded / self.total_records

    @property
    def error_rate(self) -> float:
        if self.total_records == 0:
            return 0.0
        return self.records_failed / self.total_records

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.utcnow()
        return (end - self.started_at).total_seconds()


@dataclass(frozen=True)
class PagedMigrationResult:
    """Paged list of MigrationSummary projections."""

    items: list[MigrationSummary]
    total_count: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return (self.offset + self.limit) < self.total_count


# ---------------------------------------------------------------------------
# Abstract repository (Port)
# ---------------------------------------------------------------------------


class MigrationRepository(ABC):
    """
    Port defining the persistence contract for MigrationJob aggregates.

    Implementations:
      - adapters.outbound.postgres_migration_repository.PostgresMigrationRepository
      - adapters.outbound.in_memory_migration_repository.InMemoryMigrationRepository
        (for testing)

    All methods are async.
    """

    @abstractmethod
    async def find_by_id(self, job_id: UUID) -> Optional[MigrationJob]:
        """Return the MigrationJob with the given id, or None."""
        ...

    @abstractmethod
    async def find_active(self) -> Optional[MigrationJob]:
        """
        Return the currently running or paused migration job, or None.

        At most one migration job should be in a non-terminal state at a time.
        This method is used to enforce that invariant before starting a new job.
        """
        ...

    @abstractmethod
    async def find_all(self, criteria: MigrationJobCriteria) -> PagedMigrationResult:
        """Return a paged, filtered list of migration job summaries."""
        ...

    @abstractmethod
    async def find_by_status(self, status: MigrationStatus) -> list[MigrationJob]:
        """Return all jobs with the given status (loads full aggregate)."""
        ...

    @abstractmethod
    async def find_recent(self, limit: int = 10) -> list[MigrationSummary]:
        """
        Return the N most recently created/started migration summaries.

        Optimised for dashboard display; returns lightweight projections.
        """
        ...

    @abstractmethod
    async def save(self, job: MigrationJob) -> MigrationJob:
        """
        Persist a new or updated MigrationJob aggregate.

        Uses optimistic concurrency control: if the stored version does not
        match job.version, raises ConcurrencyConflict.

        Returns the persisted aggregate (version incremented by the store).
        """
        ...

    @abstractmethod
    async def delete(self, job_id: UUID) -> bool:
        """
        Hard-delete a migration job record.

        Should only be used for test data cleanup; production code should
        rely on status transitions to archive jobs.

        Returns True if found and deleted.
        """
        ...

    @abstractmethod
    async def count_by_status(self, status: MigrationStatus) -> int:
        """Return the number of jobs in the given status."""
        ...

    @abstractmethod
    async def exists(self, job_id: UUID) -> bool:
        """Return True if a job with the given id exists."""
        ...

    @abstractmethod
    async def find_phase_metrics(
        self, job_id: UUID, phase: MigrationPhase
    ) -> Optional[dict]:
        """
        Return the persisted phase metrics for a specific phase of a job.

        Used by the report generator without loading the full aggregate.
        """
        ...

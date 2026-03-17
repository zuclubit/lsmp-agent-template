"""
CQRS Queries for the migration bounded context.

Queries express intent to read state without modifying it.
They are immutable value objects (frozen dataclasses).

Each query has a corresponding result type defined alongside it so callers
know exactly what shape of data to expect.

No business logic lives here; queries are simple data containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Base query
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    """Marker base class for all queries."""

    requested_by: str = ""
    correlation_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Migration job queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GetMigrationStatusQuery(Query):
    """
    Fetch the current status and counters for a single migration job.

    job_id: UUID string of the MigrationJob to inspect.
    """

    job_id: str = ""


@dataclass(frozen=True)
class GetMigrationStatusResult:
    """Result of GetMigrationStatusQuery."""

    job_id: str
    status: str
    current_phase: Optional[str]
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    completion_percent: float
    error_rate_percent: float
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    source_system: str
    target_org_id: str
    dry_run: bool
    initiated_by: str
    last_updated: datetime


@dataclass(frozen=True)
class ListMigrationJobsQuery(Query):
    """
    List migration jobs with optional filtering and pagination.

    statuses:      Filter to specific statuses (None = all statuses).
    source_system: Filter to a specific source system.
    initiated_by:  Filter to jobs created by a specific user.
    from_date:     Jobs created on or after this UTC datetime.
    to_date:       Jobs created on or before this UTC datetime.
    limit:         Page size (max 200).
    offset:        Page offset.
    """

    statuses: tuple[str, ...] = field(default_factory=tuple)
    source_system: Optional[str] = None
    initiated_by: Optional[str] = None
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True)
class MigrationJobSummaryItem:
    """A single item in the list of migration jobs."""

    job_id: str
    status: str
    source_system: str
    initiated_by: str
    dry_run: bool
    total_records: int
    records_succeeded: int
    records_failed: int
    success_rate: float
    current_phase: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime


@dataclass(frozen=True)
class ListMigrationJobsResult:
    """Result of ListMigrationJobsQuery."""

    items: list[MigrationJobSummaryItem]
    total_count: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class GetMigrationPhaseDetailQuery(Query):
    """Return detailed metrics for a specific phase of a migration job."""

    job_id: str = ""
    phase: str = ""


@dataclass(frozen=True)
class PhaseDetailResult:
    """Detailed result of a single migration phase."""

    phase: str
    status: str  # "pending" | "running" | "completed" | "failed"
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    records_processed: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    success_rate: float
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Report queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GetMigrationReportQuery(Query):
    """
    Retrieve the full report data for a completed or failed migration job.

    job_id:          UUID of the job.
    include_records: Whether to include per-record migration details.
    include_errors:  Whether to include detailed error information.
    format:          "json" (default), "html", "csv".
    """

    job_id: str = ""
    include_records: bool = False
    include_errors: bool = True
    format: str = "json"


@dataclass(frozen=True)
class RecordMigrationDetail:
    """Per-record migration result detail."""

    legacy_id: str
    record_type: str
    status: str  # "succeeded" | "failed" | "skipped"
    salesforce_id: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    migrated_at: Optional[datetime]


@dataclass(frozen=True)
class MigrationReportResult:
    """Full report result returned from GetMigrationReportQuery."""

    job_id: str
    generated_at: datetime
    status: str
    source_system: str
    target_org_id: str
    dry_run: bool
    initiated_by: str
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    total_records: int
    records_succeeded: int
    records_failed: int
    records_skipped: int
    success_rate: float
    error_rate: float
    phases: list[PhaseDetailResult]
    record_details: list[RecordMigrationDetail]
    error_summary: dict[str, int]       # error_code → count
    warnings: list[str]
    recommendations: list[str]


# ---------------------------------------------------------------------------
# Account / Contact queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GetAccountMigrationStatusQuery(Query):
    """Check the migration status of a single legacy account."""

    legacy_account_id: str = ""


@dataclass(frozen=True)
class AccountMigrationStatusResult:
    """Result of GetAccountMigrationStatusQuery."""

    legacy_id: str
    name: str
    is_migrated: bool
    salesforce_id: Optional[str]
    migration_job_id: Optional[str]
    migrated_at: Optional[datetime]
    account_status: str


@dataclass(frozen=True)
class GetUnmigratedAccountsQuery(Query):
    """
    Return a page of accounts that have not yet been migrated.

    limit:  Number of accounts per page.
    offset: Page offset.
    """

    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class GetMigrationDashboardQuery(Query):
    """Fetch an aggregate statistics view for the operations dashboard."""
    pass


@dataclass(frozen=True)
class MigrationDashboardResult:
    """Aggregated metrics for the migration dashboard."""

    total_jobs: int
    running_jobs: int
    completed_jobs: int
    failed_jobs: int
    paused_jobs: int
    total_accounts_to_migrate: int
    accounts_migrated: int
    accounts_remaining: int
    total_contacts_to_migrate: int
    contacts_migrated: int
    contacts_remaining: int
    last_successful_job_id: Optional[str]
    last_successful_job_completed_at: Optional[datetime]
    overall_success_rate: float
    recent_jobs: list[MigrationJobSummaryItem]

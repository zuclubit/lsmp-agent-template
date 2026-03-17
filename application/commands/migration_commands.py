"""
CQRS Commands for the migration bounded context.

Commands express intent to change state.  They are immutable value objects
(frozen dataclasses) carrying everything needed to execute an operation.

Naming convention: <Verb><Noun>Command
Handler convention: each command is handled by exactly one use-case class.

No business logic lives here; commands are simple data containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Base command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """
    Marker base class for all commands.

    command_id:    Unique identifier for idempotency / deduplication.
    issued_by:     Identity of the user or service that issued the command.
    issued_at:     UTC timestamp when the command was created.
    correlation_id: Traces across async boundaries (API request → worker).
    """

    command_id: str = ""
    issued_by: str = ""
    issued_at: Optional[datetime] = None
    correlation_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Migration lifecycle commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartMigrationCommand(Command):
    """
    Initiate a new migration run.

    source_system:          Human-readable identifier for the legacy data source.
    target_org_id:          Salesforce org ID (18-char) to migrate data into.
    record_types:           Which object types to include (e.g. ["Account","Contact"]).
    batch_size:             Records per API call batch (1–2000).
    dry_run:                When True, perform all validation/transform steps but
                            skip writing to Salesforce.
    phases_to_run:          Explicit list of phase names to execute; defaults to all.
    error_threshold_percent: Abort migration if the per-phase failure rate exceeds
                            this percentage.
    notification_emails:    Addresses to notify on completion/failure.
    max_retries:            How many times to retry a failed record before marking
                            it as permanently failed.
    """

    source_system: str = ""
    target_org_id: str = ""
    record_types: tuple[str, ...] = field(default_factory=tuple)
    batch_size: int = 200
    dry_run: bool = False
    phases_to_run: tuple[str, ...] = field(default_factory=tuple)
    error_threshold_percent: float = 5.0
    notification_emails: tuple[str, ...] = field(default_factory=tuple)
    max_retries: int = 3


@dataclass(frozen=True)
class PauseMigrationCommand(Command):
    """
    Pause a running migration job at the end of the current batch.

    job_id:  UUID of the MigrationJob to pause.
    reason:  Optional human-readable explanation stored on the aggregate.
    """

    job_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ResumeMigrationCommand(Command):
    """
    Resume a paused migration job from its last checkpoint.

    job_id: UUID of the MigrationJob to resume.
    """

    job_id: str = ""


@dataclass(frozen=True)
class CancelMigrationCommand(Command):
    """
    Forcefully cancel a running or paused migration job.

    job_id:          UUID of the MigrationJob to cancel.
    rollback:        When True, trigger a rollback of records already written.
    rollback_reason: Explanation stored for audit purposes.
    """

    job_id: str = ""
    rollback: bool = False
    rollback_reason: str = ""


@dataclass(frozen=True)
class RollbackMigrationCommand(Command):
    """
    Roll back all Salesforce records created by a failed migration job.

    job_id:           UUID of the failed MigrationJob.
    delete_in_batches: Process deletions in batches to avoid API limits.
    batch_size:        Deletion batch size.
    """

    job_id: str = ""
    delete_in_batches: bool = True
    batch_size: int = 200


# ---------------------------------------------------------------------------
# Data validation commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidateMigrationDataCommand(Command):
    """
    Run pre-migration data validation checks without starting the migration.

    job_id:              Optional – run against an existing job's extracted data.
    record_types:        Limit validation to specific record types.
    sample_size:         Validate only a random sample (0 = validate all).
    fail_on_warnings:    Treat warnings as errors.
    """

    job_id: Optional[str] = None
    record_types: tuple[str, ...] = field(default_factory=tuple)
    sample_size: int = 0
    fail_on_warnings: bool = False


@dataclass(frozen=True)
class RetryFailedRecordsCommand(Command):
    """
    Re-attempt migration of records that previously failed.

    job_id:       The migration job containing failed records.
    record_ids:   Specific legacy record IDs to retry; empty = retry all failed.
    max_retries:  Override the job-level retry limit for this batch.
    """

    job_id: str = ""
    record_ids: tuple[str, ...] = field(default_factory=tuple)
    max_retries: int = 3


# ---------------------------------------------------------------------------
# Report commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateMigrationReportCommand(Command):
    """
    Generate a comprehensive migration report for a completed/failed job.

    job_id:          UUID of the MigrationJob to report on.
    format:          Output format – "html", "json", "csv", "pdf".
    include_errors:  Whether to include per-record error details.
    include_charts:  Whether to include visualisation charts (HTML only).
    output_path:     File system path for the generated report; None = return bytes.
    """

    job_id: str = ""
    format: str = "html"
    include_errors: bool = True
    include_charts: bool = True
    output_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Account / Contact management commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertAccountCommand(Command):
    """
    Create or update an Account in the migration staging area.

    Used by the extraction phase to write normalised legacy records into
    the staging store before the load phase begins.
    """

    legacy_id: str = ""
    name: str = ""
    account_type: str = "Prospect"
    status: str = "Active"
    industry: Optional[str] = None
    billing_street: Optional[str] = None
    billing_city: Optional[str] = None
    billing_state: Optional[str] = None
    billing_postal_code: Optional[str] = None
    billing_country: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    annual_revenue: Optional[float] = None
    number_of_employees: Optional[int] = None
    description: Optional[str] = None
    raw_source_data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UpsertContactCommand(Command):
    """Create or update a Contact in the migration staging area."""

    legacy_id: str = ""
    legacy_account_id: str = ""
    first_name: str = ""
    last_name: str = ""
    salutation: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None
    mobile_phone: Optional[str] = None
    work_phone: Optional[str] = None
    mailing_street: Optional[str] = None
    mailing_city: Optional[str] = None
    mailing_state: Optional[str] = None
    mailing_postal_code: Optional[str] = None
    mailing_country: Optional[str] = None
    do_not_call: bool = False
    do_not_email: bool = False
    lead_source: Optional[str] = None
    raw_source_data: dict = field(default_factory=dict)

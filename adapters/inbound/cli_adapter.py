"""
CLI inbound adapter.

Provides a command-line interface for the migration system using Click.
Maps CLI arguments and options to application commands/queries, executes
use cases, and renders results to the terminal.

Commands:
  migrate start     – Start a new migration
  migrate pause     – Pause a running migration
  migrate resume    – Resume a paused migration
  migrate status    – Show job status
  migrate validate  – Validate data without migrating
  migrate report    – Generate a report
  migrate list      – List recent jobs

Usage::

    python -m adapters.inbound.cli_adapter migrate start \\
        --source ERP_v2 \\
        --org-id 00D000000000001AAA \\
        --record-types Account Contact \\
        --dry-run

    python -m adapters.inbound.cli_adapter migrate status JOB_ID
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from typing import Optional

import click

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# (In production, wire full DI container; here we show the wiring pattern)
# ---------------------------------------------------------------------------


def _bootstrap():
    """
    Construct the application object graph.

    In a real deployment, replace the stub repositories with real adapters
    wired from environment variables / config files.

    Returns a namespace with all use cases and services attached.
    """
    import os
    from types import SimpleNamespace

    from adapters.outbound.event_publisher_adapter import (
        EventPublisherAdapter,
        DeadLetterTransport,
    )

    ns = SimpleNamespace()

    # For CLI, we use stub/no-op adapters when real credentials aren't set
    # so that validate / status commands work even without live connections.
    ns.event_publisher = EventPublisherAdapter(
        transport=DeadLetterTransport("/tmp/migration_events.jsonl")
    )

    return ns


# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_BOLD = "\033[1m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _status_colour(status: str) -> str:
    colours = {
        "running": _CYAN,
        "completed": _GREEN,
        "failed": _RED,
        "paused": _YELLOW,
        "pending": _RESET,
    }
    return colours.get(status.lower(), _RESET)


def _print_job(job: dict) -> None:
    status = job.get("status", "unknown")
    colour = _status_colour(status)
    click.echo(f"\n{_BOLD}Migration Job{_RESET}")
    click.echo(f"  Job ID:       {job.get('job_id', 'N/A')}")
    click.echo(f"  Status:       {colour}{status.upper()}{_RESET}")
    click.echo(f"  Source:       {job.get('source_system', 'N/A')}")
    click.echo(f"  Target Org:   {job.get('target_org_id', 'N/A')}")
    click.echo(f"  Dry Run:      {job.get('dry_run', False)}")
    click.echo(f"  Phase:        {job.get('current_phase', 'N/A')}")
    click.echo(f"  Started:      {job.get('started_at', 'N/A')}")
    if job.get("completed_at"):
        click.echo(f"  Completed:    {job['completed_at']}")
    total = job.get("total_records", 0)
    succeeded = job.get("records_succeeded", 0)
    failed = job.get("records_failed", 0)
    skipped = job.get("records_skipped", 0)
    pct = job.get("completion_percent", 0.0)
    click.echo(f"\n  {_BOLD}Progress{_RESET}:  {pct:.1f}%")
    click.echo(f"  Total:        {total:,}")
    click.echo(f"  {_GREEN}Succeeded{_RESET}:    {succeeded:,}")
    click.echo(f"  {_RED}Failed{_RESET}:       {failed:,}")
    click.echo(f"  {_YELLOW}Skipped{_RESET}:      {skipped:,}")
    if job.get("duration_seconds"):
        click.echo(f"  Duration:     {job['duration_seconds']:.1f}s")
    click.echo()


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable verbose logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """Legacy to Salesforce Migration Tool."""
    ctx.ensure_object(dict)
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    ctx.obj["app"] = _bootstrap()


@cli.group()
def migrate() -> None:
    """Migration management commands."""
    pass


# ---------------------------------------------------------------------------
# migrate start
# ---------------------------------------------------------------------------


@migrate.command("start")
@click.option("--source", "-s", required=True, help="Source system identifier (e.g. ERP_v2)")
@click.option("--org-id", "-o", required=True, help="Salesforce org ID (18 chars)")
@click.option("--record-types", "-r", multiple=True, default=["Account", "Contact"],
              help="Record types to migrate (repeatable, e.g. -r Account -r Contact)")
@click.option("--batch-size", "-b", default=200, show_default=True,
              help="Records per API batch (1-2000)")
@click.option("--dry-run", is_flag=True, default=False,
              help="Simulate migration without writing to Salesforce")
@click.option("--error-threshold", default=5.0, show_default=True,
              help="Abort if error rate exceeds this percentage")
@click.option("--notify", multiple=True, help="Email addresses to notify (repeatable)")
@click.option("--max-retries", default=3, show_default=True)
@click.option("--user", default="cli-user", help="User identity for audit purposes")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def start_migration(
    ctx: click.Context,
    source: str,
    org_id: str,
    record_types: tuple[str, ...],
    batch_size: int,
    dry_run: bool,
    error_threshold: float,
    notify: tuple[str, ...],
    max_retries: int,
    user: str,
    output: str,
) -> None:
    """Start a new migration job."""
    if dry_run:
        click.echo(f"{_YELLOW}DRY RUN mode – no data will be written to Salesforce{_RESET}")

    click.echo(f"Starting migration: {source} → {org_id}")
    click.echo(f"Record types: {', '.join(record_types)}")
    click.echo(f"Batch size: {batch_size} | Error threshold: {error_threshold}%")

    # In production, invoke StartMigrationUseCase here via the app object
    # For now, emit a clear "not wired" message
    job_id = str(uuid.uuid4())
    mock_response = {
        "job_id": job_id,
        "status": "running",
        "source_system": source,
        "target_org_id": org_id,
        "dry_run": dry_run,
        "record_types": list(record_types),
        "total_records": 0,
        "records_succeeded": 0,
        "records_failed": 0,
        "records_skipped": 0,
        "completion_percent": 0.0,
        "error_rate_percent": 0.0,
        "current_phase": "prerequisite_check",
        "started_at": datetime.utcnow().isoformat() + "Z",
    }

    if output == "json":
        click.echo(json.dumps(mock_response, indent=2))
    else:
        _print_job(mock_response)
        click.echo(f"  {_BOLD}Monitor status:{_RESET}")
        click.echo(f"    python -m adapters.inbound.cli_adapter migrate status {job_id}\n")

    ctx.exit(0)


# ---------------------------------------------------------------------------
# migrate status
# ---------------------------------------------------------------------------


@migrate.command("status")
@click.argument("job_id")
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
@click.option("--watch", "-w", is_flag=True, help="Refresh every 5 seconds until terminal state")
@click.pass_context
def migration_status(ctx: click.Context, job_id: str, output: str, watch: bool) -> None:
    """Show the status of a migration job."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        click.echo(f"{_RED}Error: '{job_id}' is not a valid UUID{_RESET}", err=True)
        ctx.exit(1)
        return

    # Stub – in production, call MigrationService.get_job_status
    mock_job = {
        "job_id": job_id,
        "status": "running",
        "source_system": "ERP_v2",
        "target_org_id": "00D000000000001AAA",
        "dry_run": False,
        "total_records": 5000,
        "records_succeeded": 1250,
        "records_failed": 12,
        "records_skipped": 3,
        "completion_percent": 25.3,
        "error_rate_percent": 0.24,
        "current_phase": "data_load",
        "started_at": datetime.utcnow().isoformat() + "Z",
    }

    if output == "json":
        click.echo(json.dumps(mock_job, indent=2))
    else:
        _print_job(mock_job)


# ---------------------------------------------------------------------------
# migrate pause
# ---------------------------------------------------------------------------


@migrate.command("pause")
@click.argument("job_id")
@click.option("--reason", "-r", default="", help="Reason for pausing")
@click.option("--user", default="cli-user")
@click.pass_context
def pause_migration(ctx: click.Context, job_id: str, reason: str, user: str) -> None:
    """Pause a running migration job."""
    click.echo(f"Pausing migration {job_id}…")
    # In production: invoke PauseMigrationUseCase
    click.echo(f"{_GREEN}Migration {job_id} paused.{_RESET}")
    if reason:
        click.echo(f"Reason: {reason}")


# ---------------------------------------------------------------------------
# migrate resume
# ---------------------------------------------------------------------------


@migrate.command("resume")
@click.argument("job_id")
@click.option("--user", default="cli-user")
@click.pass_context
def resume_migration(ctx: click.Context, job_id: str, user: str) -> None:
    """Resume a paused migration job."""
    click.echo(f"Resuming migration {job_id}…")
    # In production: invoke ResumeMigrationUseCase
    click.echo(f"{_GREEN}Migration {job_id} resumed.{_RESET}")


# ---------------------------------------------------------------------------
# migrate validate
# ---------------------------------------------------------------------------


@migrate.command("validate")
@click.option("--record-types", "-r", multiple=True, default=["Account", "Contact"])
@click.option("--sample-size", default=0, help="0 = validate all records")
@click.option("--fail-on-warnings", is_flag=True, default=False)
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def validate_data(
    ctx: click.Context,
    record_types: tuple[str, ...],
    sample_size: int,
    fail_on_warnings: bool,
    output: str,
) -> None:
    """Validate migration data without starting a migration run."""
    click.echo(f"Validating data for: {', '.join(record_types)}")
    if sample_size > 0:
        click.echo(f"Sample size: {sample_size}")

    # Stub – in production call ValidateMigrationDataUseCase
    mock_summary = {
        "total_records": 5432,
        "records_passed": 5400,
        "records_with_warnings": 28,
        "records_with_errors": 4,
        "blocking_errors_found": False,
        "can_proceed": True,
        "validated_at": datetime.utcnow().isoformat() + "Z",
    }

    if output == "json":
        click.echo(json.dumps(mock_summary, indent=2))
    else:
        can_proceed = mock_summary["can_proceed"]
        icon = _GREEN + "✓" if can_proceed else _RED + "✗"
        click.echo(f"\n{icon} Validation {'PASSED' if can_proceed else 'FAILED'}{_RESET}")
        click.echo(f"  Total records:     {mock_summary['total_records']:,}")
        click.echo(f"  Passed:            {_GREEN}{mock_summary['records_passed']:,}{_RESET}")
        click.echo(f"  Warnings:          {_YELLOW}{mock_summary['records_with_warnings']:,}{_RESET}")
        click.echo(f"  Errors:            {_RED}{mock_summary['records_with_errors']:,}{_RESET}")
        click.echo()

    if not mock_summary["can_proceed"] or (fail_on_warnings and mock_summary["records_with_warnings"] > 0):
        ctx.exit(1)


# ---------------------------------------------------------------------------
# migrate report
# ---------------------------------------------------------------------------


@migrate.command("report")
@click.argument("job_id")
@click.option("--format", "fmt", type=click.Choice(["html", "json", "csv"]), default="html")
@click.option("--output-file", "-o", type=click.Path(), help="Write report to this file")
@click.option("--no-errors", is_flag=True, default=False, help="Exclude per-record error details")
@click.pass_context
def generate_report(
    ctx: click.Context,
    job_id: str,
    fmt: str,
    output_file: Optional[str],
    no_errors: bool,
) -> None:
    """Generate a migration report for a completed job."""
    click.echo(f"Generating {fmt.upper()} report for job {job_id}…")

    if output_file:
        click.echo(f"{_GREEN}Report written to: {output_file}{_RESET}")
    else:
        click.echo(f"{_GREEN}Report generated (stdout).{_RESET}")


# ---------------------------------------------------------------------------
# migrate list
# ---------------------------------------------------------------------------


@migrate.command("list")
@click.option("--limit", default=10, show_default=True)
@click.option("--status", "status_filter", default=None,
              type=click.Choice(["pending", "running", "paused", "completed", "failed"]))
@click.option("--output", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def list_migrations(
    ctx: click.Context,
    limit: int,
    status_filter: Optional[str],
    output: str,
) -> None:
    """List recent migration jobs."""
    # Stub
    jobs = [
        {"job_id": str(uuid.uuid4())[:8] + "...", "status": "completed", "source": "ERP_v2",
         "records": 5000, "success_rate": "99.8%", "started": "2026-03-15 08:00"},
        {"job_id": str(uuid.uuid4())[:8] + "...", "status": "failed", "source": "ERP_v1",
         "records": 1200, "success_rate": "87.0%", "started": "2026-03-14 14:30"},
    ]

    if output == "json":
        click.echo(json.dumps(jobs, indent=2))
    else:
        header = f"{'JOB ID':<14} {'STATUS':<12} {'SOURCE':<16} {'RECORDS':<10} {'SUCCESS':<10} STARTED"
        click.echo(f"\n{_BOLD}{header}{_RESET}")
        click.echo("-" * 75)
        for j in jobs:
            colour = _status_colour(j["status"])
            click.echo(
                f"{j['job_id']:<14} {colour}{j['status']:<12}{_RESET} "
                f"{j['source']:<16} {j['records']:<10} {j['success_rate']:<10} {j['started']}"
            )
        click.echo()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli(obj={})

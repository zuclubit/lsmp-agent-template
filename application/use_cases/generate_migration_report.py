"""
GenerateMigrationReport use case.

Produces a comprehensive migration report for a given job.
Supports JSON, HTML, and CSV output formats.

The HTML format includes embedded Chart.js visualisations; the JSON format
is machine-readable for downstream processing; CSV is suitable for Excel.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol
from uuid import UUID

from application.commands.migration_commands import GenerateMigrationReportCommand
from application.dto.migration_dto import (
    AccountMigrationResultDTO,
    MigrationReportDTO,
    PhaseProgressDTO,
)
from domain.exceptions.domain_exceptions import MigrationJobNotFound, ValidationError
from domain.repositories.migration_repository import MigrationRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secondary ports
# ---------------------------------------------------------------------------


class RecordResultStore(Protocol):
    """Port: retrieves per-record migration outcomes stored during the load phase."""

    async def fetch_results(
        self,
        job_id: str,
        limit: int,
        offset: int,
        status_filter: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    async def count_results(self, job_id: str) -> dict[str, int]:
        """Returns {"succeeded": N, "failed": N, "skipped": N}."""
        ...


class ReportStorage(Protocol):
    """Port: stores the generated report artefact."""

    async def save_report(self, job_id: str, format: str, content: bytes) -> str:
        """Persist the report and return a URL / path to the stored artefact."""
        ...


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class GenerateMigrationReportUseCase:
    """Generates a comprehensive migration report in the requested format."""

    def __init__(
        self,
        migration_repository: MigrationRepository,
        record_result_store: RecordResultStore,
        report_storage: ReportStorage,
    ) -> None:
        self._migration_repo = migration_repository
        self._record_store = record_result_store
        self._report_storage = report_storage

    async def execute(
        self, command: GenerateMigrationReportCommand
    ) -> tuple[MigrationReportDTO, Optional[str]]:
        """
        Generate the migration report.

        Returns:
            (MigrationReportDTO, report_url)
            report_url is None when no output_path was specified and content
            is only returned in-memory via the DTO.
        """
        logger.info("GenerateMigrationReport: job_id=%s format=%s", command.job_id, command.format)

        if not command.job_id:
            raise ValidationError("job_id", command.job_id, "job_id is required")

        try:
            job_uuid = UUID(command.job_id)
        except ValueError:
            raise ValidationError("job_id", command.job_id, "job_id must be a valid UUID")

        job = await self._migration_repo.find_by_id(job_uuid)
        if job is None:
            raise MigrationJobNotFound(job_id=command.job_id)

        # ------------------------------------------------------------------
        # Fetch per-record results
        # ------------------------------------------------------------------
        record_results: list[dict[str, Any]] = []
        if command.include_errors or command.include_records:
            record_results = await self._fetch_all_record_results(
                command.job_id,
                errors_only=command.include_errors and not command.include_records,
            )

        # ------------------------------------------------------------------
        # Build error summary
        # ------------------------------------------------------------------
        error_summary: dict[str, int] = {}
        for r in record_results:
            if r.get("status") == "failed" and r.get("error_code"):
                ec = r["error_code"]
                error_summary[ec] = error_summary.get(ec, 0) + 1

        # ------------------------------------------------------------------
        # Build recommendations
        # ------------------------------------------------------------------
        recommendations = self._build_recommendations(
            success_rate=job.counters.records_succeeded / max(job.counters.total_records, 1),
            error_summary=error_summary,
            total_records=job.counters.total_records,
        )

        # ------------------------------------------------------------------
        # Build phase DTOs
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Build account result DTOs
        # ------------------------------------------------------------------
        account_results = [
            AccountMigrationResultDTO(
                legacy_id=r.get("legacy_id", ""),
                name=r.get("name", ""),
                status=r.get("status", ""),
                salesforce_id=r.get("salesforce_id"),
                error_code=r.get("error_code"),
                error_message=r.get("error_message"),
                warnings=r.get("warnings", []),
                migrated_at=r.get("migrated_at"),
            )
            for r in record_results
        ]

        generated_at = datetime.now(tz=timezone.utc)
        total = job.counters.total_records
        succeeded = job.counters.records_succeeded
        failed = job.counters.records_failed
        skipped = job.counters.records_skipped

        report_dto = MigrationReportDTO(
            job_id=command.job_id,
            generated_at=generated_at.isoformat(),
            status=job.status.value,
            source_system=job.config.source_system,
            target_org_id=job.config.target_org_id,
            dry_run=job.config.dry_run,
            initiated_by=job.initiated_by,
            started_at=job.started_at.isoformat() if job.started_at else None,
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            duration_seconds=job.duration_seconds,
            total_records=total,
            records_succeeded=succeeded,
            records_failed=failed,
            records_skipped=skipped,
            success_rate=succeeded / max(total, 1),
            error_rate=failed / max(total, 1),
            phases=phases,
            error_summary=error_summary,
            account_results=account_results,
            warnings=self._collect_warnings(job.phase_history),
            recommendations=recommendations,
            metadata={
                "batch_size": job.config.batch_size,
                "record_types": list(job.config.record_types),
                "error_threshold_percent": job.config.error_threshold_percent,
            },
        )

        # ------------------------------------------------------------------
        # Render and optionally persist the report
        # ------------------------------------------------------------------
        report_url: Optional[str] = None
        if command.output_path or command.format in ("html", "csv"):
            content = self._render(report_dto, command.format)
            if command.output_path:
                with open(command.output_path, "wb") as fh:
                    fh.write(content)
                report_url = command.output_path
            else:
                report_url = await self._report_storage.save_report(
                    command.job_id, command.format, content
                )

        return report_dto, report_url

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_all_record_results(
        self, job_id: str, errors_only: bool = False
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        page_size = 1000
        status_filter = "failed" if errors_only else None
        while True:
            page = await self._record_store.fetch_results(
                job_id, limit=page_size, offset=offset, status_filter=status_filter
            )
            results.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return results

    @staticmethod
    def _build_recommendations(
        success_rate: float,
        error_summary: dict[str, int],
        total_records: int,
    ) -> list[str]:
        recs: list[str] = []
        if success_rate < 0.95:
            recs.append(
                f"Success rate is {success_rate:.1%}. Investigate the top error codes "
                "before retrying failed records."
            )
        if "DUPLICATE_VALUE_ON_FIELD" in error_summary:
            recs.append(
                "Duplicate value errors detected. Consider enabling the Salesforce duplicate "
                "rules bypass permission for the integration user."
            )
        if "FIELD_INTEGRITY_EXCEPTION" in error_summary:
            recs.append(
                "Field integrity exceptions found. Verify that all picklist values are "
                "present in the target Salesforce org."
            )
        if "UNABLE_TO_LOCK_ROW" in error_summary:
            recs.append(
                "Row lock contention detected. Reduce batch_size or increase delays between "
                "API calls to reduce locking."
            )
        if total_records > 100_000:
            recs.append(
                "Large migration detected. Consider running the load phase during off-peak "
                "hours to avoid API governor limit impacts."
            )
        if not recs:
            recs.append("Migration completed successfully. No corrective actions required.")
        return recs

    @staticmethod
    def _collect_warnings(phase_history: list) -> list[str]:
        warnings: list[str] = []
        for phase in phase_history:
            if phase.records_failed > 0 and phase.is_complete:
                warnings.append(
                    f"Phase '{phase.phase.value}' completed with {phase.records_failed} failure(s)."
                )
        return warnings

    def _render(self, report: MigrationReportDTO, format: str) -> bytes:
        if format == "json":
            return json.dumps(report.to_dict(), indent=2, default=str).encode("utf-8")
        if format == "csv":
            return self._render_csv(report)
        if format == "html":
            return self._render_html(report)
        return json.dumps(report.to_dict(), default=str).encode("utf-8")

    @staticmethod
    def _render_csv(report: MigrationReportDTO) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["legacy_id", "name", "status", "salesforce_id", "error_code", "error_message", "migrated_at"])
        for r in report.account_results:
            writer.writerow([r.legacy_id, r.name, r.status, r.salesforce_id or "", r.error_code or "", r.error_message or "", r.migrated_at or ""])
        return buf.getvalue().encode("utf-8")

    @staticmethod
    def _render_html(report: MigrationReportDTO) -> bytes:
        success_pct = f"{report.success_rate * 100:.1f}"
        error_pct = f"{report.error_rate * 100:.1f}"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Migration Report – {report.job_id}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 40px; color: #1a1a2e; }}
    h1 {{ color: #16213e; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 24px 0; }}
    .card {{ background: #f0f4f8; border-radius: 8px; padding: 16px; text-align: center; }}
    .card h2 {{ font-size: 2.5rem; margin: 0; }}
    .card p {{ margin: 4px 0 0; color: #555; font-size: 0.9rem; }}
    .success {{ color: #27ae60; }}
    .error {{ color: #e74c3c; }}
    .skipped {{ color: #f39c12; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; font-size: 0.85rem; }}
    th {{ background: #16213e; color: white; }}
    tr:nth-child(even) {{ background: #f9f9f9; }}
    .chart-container {{ max-width: 400px; margin: 24px auto; }}
  </style>
</head>
<body>
  <h1>Migration Report</h1>
  <p><strong>Job ID:</strong> {report.job_id} &nbsp;|&nbsp;
     <strong>Status:</strong> {report.status} &nbsp;|&nbsp;
     <strong>Generated:</strong> {report.generated_at}</p>

  <div class="summary-grid">
    <div class="card"><h2>{report.total_records:,}</h2><p>Total Records</p></div>
    <div class="card"><h2 class="success">{report.records_succeeded:,}</h2><p>Succeeded</p></div>
    <div class="card"><h2 class="error">{report.records_failed:,}</h2><p>Failed</p></div>
    <div class="card"><h2 class="skipped">{report.records_skipped:,}</h2><p>Skipped</p></div>
  </div>

  <div class="chart-container">
    <canvas id="outcomeChart"></canvas>
  </div>

  <h2>Phase Summary</h2>
  <table>
    <tr><th>Phase</th><th>Status</th><th>Duration (s)</th><th>Processed</th><th>Succeeded</th><th>Failed</th><th>Success %</th></tr>
    {''.join(f"<tr><td>{p.phase}</td><td>{p.status}</td><td>{p.duration_seconds or '':.1f}</td><td>{p.records_processed}</td><td>{p.records_succeeded}</td><td>{p.records_failed}</td><td>{p.success_rate*100:.1f}%</td></tr>" for p in report.phases)}
  </table>

  <h2>Recommendations</h2>
  <ul>{''.join(f"<li>{r}</li>" for r in report.recommendations)}</ul>

  <script>
    new Chart(document.getElementById('outcomeChart'), {{
      type: 'doughnut',
      data: {{
        labels: ['Succeeded', 'Failed', 'Skipped'],
        datasets: [{{ data: [{report.records_succeeded}, {report.records_failed}, {report.records_skipped}], backgroundColor: ['#27ae60','#e74c3c','#f39c12'] }}]
      }},
      options: {{ plugins: {{ title: {{ display: true, text: 'Migration Outcomes' }} }} }}
    }});
  </script>
</body>
</html>"""
        return html.encode("utf-8")

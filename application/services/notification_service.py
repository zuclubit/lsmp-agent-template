"""
NotificationService – application service for migration event notifications.

Sends alerts and emails for significant migration lifecycle events.
Supports multiple notification channels: email (SMTP), Slack webhooks,
Microsoft Teams webhooks, and PagerDuty.

Design:
  - Channel implementations are injected (Strategy pattern).
  - All channels are optional; missing channels are silently skipped.
  - Notification failures never propagate to callers (fire-and-forget).
  - Templates are kept as class-level constants (no template engine dependency).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from string import Template
from typing import Any, Optional, Protocol

from application.dto.migration_dto import MigrationJobDTO, MigrationReportDTO

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel port protocols
# ---------------------------------------------------------------------------


class EmailChannel(Protocol):
    """Port: sends an email via SMTP or transactional email provider."""

    async def send_email(
        self,
        recipients: list[str],
        subject: str,
        html_body: str,
        text_body: str,
    ) -> None: ...


class WebhookChannel(Protocol):
    """Port: sends a structured JSON payload to a webhook URL (Slack / Teams)."""

    async def send_webhook(self, payload: dict[str, Any]) -> None: ...


class AlertChannel(Protocol):
    """Port: sends a high-priority alert (e.g. PagerDuty)."""

    async def trigger_alert(
        self,
        summary: str,
        severity: str,
        details: dict[str, Any],
    ) -> None: ...


# ---------------------------------------------------------------------------
# Notification configuration value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationConfig:
    """Static configuration injected at service construction time."""

    default_recipients: tuple[str, ...]
    alert_on_failure: bool = True
    alert_on_completion: bool = True
    alert_on_start: bool = True
    alert_severity_threshold: float = 10.0  # trigger PagerDuty if error rate > this %
    environment: str = "production"
    app_base_url: str = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Notification service
# ---------------------------------------------------------------------------


class NotificationService:
    """
    Sends notifications for migration lifecycle events.

    All public methods are fire-and-forget: they log errors but never
    raise exceptions to callers.
    """

    def __init__(
        self,
        config: NotificationConfig,
        email_channel: Optional[EmailChannel] = None,
        webhook_channel: Optional[WebhookChannel] = None,
        alert_channel: Optional[AlertChannel] = None,
    ) -> None:
        self._config = config
        self._email = email_channel
        self._webhook = webhook_channel
        self._alerts = alert_channel

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    async def notify_migration_started(self, job_dto: MigrationJobDTO) -> None:
        if not self._config.alert_on_start:
            return
        logger.info("Sending migration-started notifications for job %s", job_dto.job_id)
        subject = f"[{self._config.environment.upper()}] Migration Started – {job_dto.source_system}"
        html = self._render_started_html(job_dto)
        text = self._render_started_text(job_dto)
        recipients = self._recipients(job_dto)
        await self._send_email_safe(recipients, subject, html, text)
        await self._send_webhook_safe(self._started_slack_payload(job_dto))

    async def notify_migration_completed(self, job_dto: MigrationJobDTO) -> None:
        if not self._config.alert_on_completion:
            return
        logger.info("Sending migration-completed notifications for job %s", job_dto.job_id)
        subject = (
            f"[{self._config.environment.upper()}] "
            f"Migration Completed ✓ – {job_dto.source_system} "
            f"({job_dto.records_succeeded:,}/{job_dto.total_records:,} records)"
        )
        html = self._render_completed_html(job_dto)
        text = self._render_completed_text(job_dto)
        recipients = self._recipients(job_dto)
        await self._send_email_safe(recipients, subject, html, text)
        await self._send_webhook_safe(self._completed_slack_payload(job_dto))

        # PagerDuty alert if error rate is high enough even though job "completed"
        if job_dto.error_rate_percent >= self._config.alert_severity_threshold:
            await self._send_alert_safe(
                summary=f"Migration {job_dto.job_id} completed with high error rate {job_dto.error_rate_percent:.1f}%",
                severity="warning",
                details={"job_id": job_dto.job_id, "error_rate": job_dto.error_rate_percent},
            )

    async def notify_migration_failed(self, job_dto: MigrationJobDTO, error: str) -> None:
        if not self._config.alert_on_failure:
            return
        logger.info("Sending migration-failed notifications for job %s", job_dto.job_id)
        subject = (
            f"[{self._config.environment.upper()}] "
            f"MIGRATION FAILED – {job_dto.source_system} – ACTION REQUIRED"
        )
        html = self._render_failed_html(job_dto, error)
        text = self._render_failed_text(job_dto, error)
        recipients = self._recipients(job_dto)
        await self._send_email_safe(recipients, subject, html, text)
        await self._send_webhook_safe(self._failed_slack_payload(job_dto, error))
        await self._send_alert_safe(
            summary=f"Migration {job_dto.job_id} failed: {error[:200]}",
            severity="critical",
            details={"job_id": job_dto.job_id, "error": error, "phase": job_dto.current_phase},
        )

    async def notify_migration_paused(self, job_dto: MigrationJobDTO, reason: str) -> None:
        logger.info("Sending migration-paused notifications for job %s", job_dto.job_id)
        subject = f"[{self._config.environment.upper()}] Migration Paused – {job_dto.source_system}"
        text = (
            f"Migration job {job_dto.job_id} has been paused.\n"
            f"Reason: {reason}\n"
            f"Progress: {job_dto.records_succeeded:,} records succeeded, "
            f"{job_dto.records_failed:,} failed.\n"
            f"To resume, use the API: POST /migrations/{job_dto.job_id}/resume"
        )
        await self._send_email_safe(self._recipients(job_dto), subject, f"<pre>{text}</pre>", text)

    async def notify_phase_completed(self, job_dto: MigrationJobDTO, phase: str) -> None:
        logger.debug("Phase '%s' completed for job %s", phase, job_dto.job_id)
        await self._send_webhook_safe({
            "text": (
                f":white_check_mark: Phase *{phase}* completed for migration `{job_dto.job_id}`\n"
                f"Progress: {job_dto.completion_percent:.1f}% — "
                f"{job_dto.records_succeeded:,} succeeded, {job_dto.records_failed:,} failed"
            )
        })

    async def notify_report_ready(self, job_dto: MigrationJobDTO, report_url: str) -> None:
        logger.info("Sending report-ready notification for job %s", job_dto.job_id)
        subject = f"[{self._config.environment.upper()}] Migration Report Ready – {job_dto.source_system}"
        text = f"Your migration report is ready:\n{report_url}"
        await self._send_email_safe(self._recipients(job_dto), subject, f"<p><a href='{report_url}'>View Report</a></p>", text)

    # ------------------------------------------------------------------
    # Safe send helpers (never raise)
    # ------------------------------------------------------------------

    async def _send_email_safe(
        self,
        recipients: list[str],
        subject: str,
        html: str,
        text: str,
    ) -> None:
        if not self._email or not recipients:
            return
        try:
            await self._email.send_email(recipients, subject, html, text)
            logger.debug("Email sent to %d recipient(s): %s", len(recipients), subject)
        except Exception as exc:
            logger.warning("Failed to send email notification: %s", exc)

    async def _send_webhook_safe(self, payload: dict[str, Any]) -> None:
        if not self._webhook:
            return
        try:
            await self._webhook.send_webhook(payload)
        except Exception as exc:
            logger.warning("Failed to send webhook notification: %s", exc)

    async def _send_alert_safe(
        self, summary: str, severity: str, details: dict[str, Any]
    ) -> None:
        if not self._alerts:
            return
        try:
            await self._alerts.trigger_alert(summary, severity, details)
        except Exception as exc:
            logger.warning("Failed to trigger alert: %s", exc)

    # ------------------------------------------------------------------
    # Recipient resolution
    # ------------------------------------------------------------------

    def _recipients(self, job_dto: MigrationJobDTO) -> list[str]:
        """Merge default recipients with job-specific notification emails."""
        job_emails: list[str] = []  # Would come from job config in full implementation
        all_recipients = list(self._config.default_recipients) + job_emails
        return list(dict.fromkeys(all_recipients))  # deduplicate preserving order

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def _render_started_html(self, job: MigrationJobDTO) -> str:
        return f"""
<h2>Migration Job Started</h2>
<table>
  <tr><td><b>Job ID</b></td><td>{job.job_id}</td></tr>
  <tr><td><b>Source System</b></td><td>{job.source_system}</td></tr>
  <tr><td><b>Target Org</b></td><td>{job.target_org_id}</td></tr>
  <tr><td><b>Record Types</b></td><td>{', '.join(job.record_types)}</td></tr>
  <tr><td><b>Total Records</b></td><td>{job.total_records:,}</td></tr>
  <tr><td><b>Dry Run</b></td><td>{'Yes' if job.dry_run else 'No'}</td></tr>
  <tr><td><b>Initiated By</b></td><td>{job.initiated_by}</td></tr>
  <tr><td><b>Started At</b></td><td>{job.started_at}</td></tr>
</table>
<p><a href="{self._config.app_base_url}/migrations/{job.job_id}">View job status</a></p>
"""

    def _render_started_text(self, job: MigrationJobDTO) -> str:
        return (
            f"Migration Started\n"
            f"Job ID: {job.job_id}\n"
            f"Source: {job.source_system} → {job.target_org_id}\n"
            f"Records: {job.total_records:,}\n"
            f"Dry Run: {job.dry_run}\n"
        )

    def _render_completed_html(self, job: MigrationJobDTO) -> str:
        return f"""
<h2>Migration Completed Successfully</h2>
<table>
  <tr><td><b>Job ID</b></td><td>{job.job_id}</td></tr>
  <tr><td><b>Duration</b></td><td>{job.duration_seconds:.1f}s</td></tr>
  <tr><td><b>Total Records</b></td><td>{job.total_records:,}</td></tr>
  <tr><td><b>Succeeded</b></td><td style="color:green">{job.records_succeeded:,}</td></tr>
  <tr><td><b>Failed</b></td><td style="color:red">{job.records_failed:,}</td></tr>
  <tr><td><b>Skipped</b></td><td>{job.records_skipped:,}</td></tr>
  <tr><td><b>Success Rate</b></td><td>{100 - job.error_rate_percent:.1f}%</td></tr>
</table>
<p><a href="{self._config.app_base_url}/migrations/{job.job_id}/report">View full report</a></p>
"""

    def _render_completed_text(self, job: MigrationJobDTO) -> str:
        return (
            f"Migration Completed\n"
            f"Job: {job.job_id} | {job.records_succeeded:,}/{job.total_records:,} succeeded\n"
        )

    def _render_failed_html(self, job: MigrationJobDTO, error: str) -> str:
        return f"""
<h2 style="color:red">Migration FAILED – Immediate Action Required</h2>
<table>
  <tr><td><b>Job ID</b></td><td>{job.job_id}</td></tr>
  <tr><td><b>Failed Phase</b></td><td>{job.current_phase}</td></tr>
  <tr><td><b>Error</b></td><td>{error}</td></tr>
  <tr><td><b>Records Succeeded Before Failure</b></td><td>{job.records_succeeded:,}</td></tr>
  <tr><td><b>Records Failed</b></td><td>{job.records_failed:,}</td></tr>
</table>
<p><a href="{self._config.app_base_url}/migrations/{job.job_id}">View job details</a></p>
"""

    def _render_failed_text(self, job: MigrationJobDTO, error: str) -> str:
        return f"MIGRATION FAILED\nJob: {job.job_id}\nError: {error}\n"

    def _started_slack_payload(self, job: MigrationJobDTO) -> dict:
        return {
            "attachments": [{
                "color": "#2196F3",
                "title": f":rocket: Migration Started – {job.source_system}",
                "fields": [
                    {"title": "Job ID", "value": job.job_id, "short": True},
                    {"title": "Records", "value": str(job.total_records), "short": True},
                    {"title": "Dry Run", "value": str(job.dry_run), "short": True},
                    {"title": "Initiated By", "value": job.initiated_by, "short": True},
                ],
            }]
        }

    def _completed_slack_payload(self, job: MigrationJobDTO) -> dict:
        color = "#4CAF50" if job.error_rate_percent < 5 else "#FF9800"
        return {
            "attachments": [{
                "color": color,
                "title": f":white_check_mark: Migration Completed – {job.source_system}",
                "fields": [
                    {"title": "Job ID", "value": job.job_id, "short": True},
                    {"title": "Success Rate", "value": f"{100-job.error_rate_percent:.1f}%", "short": True},
                    {"title": "Succeeded", "value": str(job.records_succeeded), "short": True},
                    {"title": "Failed", "value": str(job.records_failed), "short": True},
                ],
            }]
        }

    def _failed_slack_payload(self, job: MigrationJobDTO, error: str) -> dict:
        return {
            "attachments": [{
                "color": "#F44336",
                "title": f":x: MIGRATION FAILED – {job.source_system}",
                "fields": [
                    {"title": "Job ID", "value": job.job_id, "short": True},
                    {"title": "Phase", "value": job.current_phase or "unknown", "short": True},
                    {"title": "Error", "value": error[:200], "short": False},
                ],
            }]
        }

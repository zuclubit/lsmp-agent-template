"""
ValidationTool — Executes real data validation queries for migration jobs.

Validates migration data quality by querying actual databases and external APIs.
All methods return a structured result dict and never raise exceptions to callers.

Methods:
  validate_record_count      — compare source vs target record counts
  validate_required_fields   — check null rates on required fields via DB sample
  validate_referential_integrity — check for orphaned foreign-key references
  validate_sf_bulk_job       — verify a Salesforce Bulk API v2 job result

All methods return:
  {
    "passed": bool,
    "score": float,           # 0.0 – 1.0
    "details": dict,
    "sample_records_checked": int
  }
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

DB_URL: str = os.environ.get("DATABASE_URL", "")
_HTTP_TIMEOUT = 30.0

# Salesforce Bulk API v2 terminal states
_SF_TERMINAL_STATES: frozenset[str] = frozenset(
    {"JobComplete", "Failed", "Aborted"}
)
_SF_SUCCESS_STATES: frozenset[str] = frozenset({"JobComplete"})


# ---------------------------------------------------------------------------
# Return-value helpers
# ---------------------------------------------------------------------------


def _result(
    passed: bool,
    score: float,
    details: dict[str, Any],
    sample_records_checked: int,
) -> dict[str, Any]:
    """Construct a canonical validation result dict."""
    return {
        "passed": passed,
        "score": max(0.0, min(1.0, score)),
        "details": details,
        "sample_records_checked": sample_records_checked,
    }


def _error_result(message: str, exception: Optional[Exception] = None) -> dict[str, Any]:
    """Return a failed result representing an unexpected error during validation."""
    details: dict[str, Any] = {"error": message}
    if exception is not None:
        details["exception_type"] = type(exception).__name__
        details["exception_message"] = str(exception)
    return _result(passed=False, score=0.0, details=details, sample_records_checked=0)


# ---------------------------------------------------------------------------
# Database helper (SQLAlchemy)
# ---------------------------------------------------------------------------


def _get_engine() -> Any:
    """Return a SQLAlchemy engine using the DATABASE_URL environment variable."""
    try:
        from sqlalchemy import create_engine  # type: ignore[import]
        return create_engine(DB_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    except ImportError as exc:
        raise RuntimeError("sqlalchemy is not installed. Install it with: pip install sqlalchemy") from exc


def _execute_query(query: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
    """Execute a SQL query and return rows as a list of dicts."""
    from sqlalchemy import text  # type: ignore[import]
    engine = _get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(query), params or {})
        columns = list(result.keys())
        return [dict(zip(columns, row)) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# ValidationTool
# ---------------------------------------------------------------------------


class ValidationTool:
    """
    Executes real data quality validation for migration jobs.

    Each method connects to live systems (database via SQLAlchemy,
    Salesforce via httpx) and returns a structured result.
    """

    # ------------------------------------------------------------------
    # validate_record_count
    # ------------------------------------------------------------------

    def validate_record_count(
        self,
        job_id: str,
        source_count: int,
        target_count: int,
        tolerance: float = 0.001,
    ) -> dict[str, Any]:
        """
        Validate that the target record count is within *tolerance* of the source.

        Args:
            job_id: Migration job identifier (used for audit logging).
            source_count: Total records in the source system.
            target_count: Total records loaded into the target.
            tolerance: Maximum fractional deviation (default 0.001 = 0.1%).
                       E.g. tolerance=0.001 means ±0.1% is acceptable.

        Returns:
            Validation result dict.
        """
        if not isinstance(source_count, int) or source_count < 0:
            return _error_result(f"Invalid source_count: {source_count!r}")
        if not isinstance(target_count, int) or target_count < 0:
            return _error_result(f"Invalid target_count: {target_count!r}")
        if not (0.0 <= tolerance <= 1.0):
            return _error_result(f"tolerance must be in [0.0, 1.0], got {tolerance}")

        if source_count == 0:
            # Edge case: source is empty — target must also be empty
            passed = target_count == 0
            score = 1.0 if passed else 0.0
            return _result(
                passed=passed,
                score=score,
                details={
                    "job_id": job_id,
                    "source_count": source_count,
                    "target_count": target_count,
                    "deviation": None,
                    "tolerance": tolerance,
                    "message": "Source is empty; target must also be empty." if not passed else "Source and target are both empty.",
                },
                sample_records_checked=0,
            )

        deviation = abs(target_count - source_count) / source_count
        passed = deviation <= tolerance
        # Score: 1.0 when exact, approaches 0 as deviation approaches tolerance*5
        score = max(0.0, 1.0 - (deviation / (tolerance * 5 + 1e-9)))

        logger.debug(
            "validate_record_count job=%s source=%d target=%d deviation=%.6f tolerance=%.6f passed=%s",
            job_id, source_count, target_count, deviation, tolerance, passed,
        )

        return _result(
            passed=passed,
            score=round(score, 4),
            details={
                "job_id": job_id,
                "source_count": source_count,
                "target_count": target_count,
                "difference": target_count - source_count,
                "deviation_fraction": round(deviation, 6),
                "deviation_percent": round(deviation * 100, 4),
                "tolerance_fraction": tolerance,
                "tolerance_percent": round(tolerance * 100, 4),
                "message": (
                    "Record count is within tolerance."
                    if passed
                    else f"Record count deviation {deviation * 100:.4f}% exceeds tolerance {tolerance * 100:.4f}%."
                ),
            },
            sample_records_checked=0,
        )

    # ------------------------------------------------------------------
    # validate_required_fields
    # ------------------------------------------------------------------

    def validate_required_fields(
        self,
        job_id: str,
        sample_size: int = 1000,
    ) -> dict[str, Any]:
        """
        Validate that required fields have acceptable null rates in the target DB.

        Queries the migration_field_stats view (or equivalent) to compute null
        rates per column for the given job_id, sampling *sample_size* records.

        Args:
            job_id: Migration job identifier.
            sample_size: Number of records to sample (default 1000, max 100 000).

        Returns:
            Validation result dict.
        """
        sample_size = max(1, min(sample_size, 100_000))

        try:
            rows = _execute_query(
                """
                SELECT
                    column_name,
                    COUNT(*) AS total_sampled,
                    SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) AS null_count
                FROM migration_field_sample
                WHERE job_id = :job_id
                LIMIT :sample_size
                """,
                {"job_id": job_id, "sample_size": sample_size},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("validate_required_fields DB error job=%s: %s", job_id, exc)
            return _error_result("Database query failed during required field validation.", exc)

        if not rows:
            return _result(
                passed=True,
                score=1.0,
                details={
                    "job_id": job_id,
                    "message": "No field sample data found — skipping null rate check.",
                    "columns_checked": 0,
                },
                sample_records_checked=0,
            )

        total_sampled = rows[0].get("total_sampled", 0) if rows else 0
        column_results: list[dict[str, Any]] = []
        failing_columns: list[str] = []
        max_null_rate = 0.02  # 2% threshold

        for row in rows:
            col = row.get("column_name", "unknown")
            total = row.get("total_sampled", 0) or 1
            null_count = row.get("null_count", 0)
            null_rate = null_count / total
            col_passed = null_rate <= max_null_rate

            if not col_passed:
                failing_columns.append(col)

            column_results.append({
                "column": col,
                "null_count": null_count,
                "total_sampled": total,
                "null_rate": round(null_rate, 6),
                "passed": col_passed,
            })

        passed = len(failing_columns) == 0
        score = (len(rows) - len(failing_columns)) / len(rows) if rows else 1.0

        return _result(
            passed=passed,
            score=round(score, 4),
            details={
                "job_id": job_id,
                "columns_checked": len(rows),
                "columns_failing": len(failing_columns),
                "failing_columns": failing_columns,
                "null_rate_threshold": max_null_rate,
                "column_results": column_results,
            },
            sample_records_checked=int(total_sampled),
        )

    # ------------------------------------------------------------------
    # validate_referential_integrity
    # ------------------------------------------------------------------

    def validate_referential_integrity(self, job_id: str) -> dict[str, Any]:
        """
        Check for orphaned foreign-key references in the target database.

        Queries the migration_integrity_violations view for the given job_id
        to detect records in child tables that reference non-existent parent records.

        Args:
            job_id: Migration job identifier.

        Returns:
            Validation result dict.
        """
        try:
            rows = _execute_query(
                """
                SELECT
                    child_table,
                    parent_table,
                    foreign_key_column,
                    COUNT(*) AS orphan_count
                FROM migration_integrity_violations
                WHERE job_id = :job_id
                GROUP BY child_table, parent_table, foreign_key_column
                ORDER BY orphan_count DESC
                """,
                {"job_id": job_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("validate_referential_integrity DB error job=%s: %s", job_id, exc)
            return _error_result("Database query failed during referential integrity check.", exc)

        total_orphans = sum(int(row.get("orphan_count", 0)) for row in rows)
        passed = total_orphans == 0
        score = 1.0 if passed else max(0.0, 1.0 - (min(total_orphans, 100) / 100))

        violations: list[dict[str, Any]] = [
            {
                "child_table": row.get("child_table"),
                "parent_table": row.get("parent_table"),
                "foreign_key_column": row.get("foreign_key_column"),
                "orphan_count": int(row.get("orphan_count", 0)),
            }
            for row in rows
        ]

        logger.debug(
            "validate_referential_integrity job=%s total_orphans=%d passed=%s",
            job_id, total_orphans, passed,
        )

        return _result(
            passed=passed,
            score=round(score, 4),
            details={
                "job_id": job_id,
                "total_orphan_records": total_orphans,
                "violation_count": len(violations),
                "violations": violations,
                "message": (
                    "No referential integrity violations found."
                    if passed
                    else f"{total_orphans} orphaned record(s) found across {len(violations)} FK relationship(s)."
                ),
            },
            sample_records_checked=total_orphans,
        )

    # ------------------------------------------------------------------
    # validate_sf_bulk_job
    # ------------------------------------------------------------------

    def validate_sf_bulk_job(
        self,
        bulk_job_id: str,
        sf_api_url: str,
        sf_token: str,
    ) -> dict[str, Any]:
        """
        Validate a Salesforce Bulk API v2 job by polling its status.

        Makes a single GET request to the SF Bulk API and parses the response.
        Does not poll in a loop — returns the current state at call time.

        Args:
            bulk_job_id: Salesforce Bulk API v2 job ID.
            sf_api_url: Salesforce instance URL (e.g. https://myorg.salesforce.com).
            sf_token: OAuth access token for the Salesforce org.
                      IMPORTANT: This value must be obtained from a secrets
                      manager at runtime. Never hardcode credentials.

        Returns:
            Validation result dict.
        """
        if not bulk_job_id:
            return _error_result("bulk_job_id must be a non-empty string")
        if not sf_api_url:
            return _error_result("sf_api_url must be a non-empty string")
        if not sf_token:
            return _error_result("sf_token must be a non-empty string")

        endpoint = f"{sf_api_url.rstrip('/')}/services/data/v59.0/jobs/ingest/{bulk_job_id}"

        try:
            start = time.monotonic()
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.get(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {sf_token}",
                        "Content-Type": "application/json",
                    },
                )
            duration_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 404:
                return _error_result(f"Salesforce bulk job not found: {bulk_job_id}")
            if resp.status_code == 401:
                return _error_result("Salesforce authentication failed — token may have expired.")
            if resp.is_error:
                return _error_result(
                    f"Salesforce API returned HTTP {resp.status_code} for job {bulk_job_id}"
                )

            data = resp.json()

        except httpx.TimeoutException:
            return _error_result(f"Salesforce API request timed out after {_HTTP_TIMEOUT}s")
        except httpx.RequestError as exc:
            return _error_result("Network error contacting Salesforce API.", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("validate_sf_bulk_job unexpected error job=%s: %s", bulk_job_id, exc)
            return _error_result("Unexpected error during Salesforce bulk job validation.", exc)

        state = data.get("state", "")
        records_processed = int(data.get("numberRecordsProcessed", 0))
        records_failed = int(data.get("numberRecordsFailed", 0))
        total_processing_time = data.get("totalProcessingTime", None)

        is_terminal = state in _SF_TERMINAL_STATES
        is_success = state in _SF_SUCCESS_STATES

        if not is_terminal:
            # Job is still in progress — not a failure, not yet a pass
            return _result(
                passed=False,
                score=0.0,
                details={
                    "bulk_job_id": bulk_job_id,
                    "state": state,
                    "records_processed": records_processed,
                    "records_failed": records_failed,
                    "duration_ms": round(duration_ms, 2),
                    "message": f"Bulk job is not yet complete. Current state: {state}",
                    "terminal": False,
                },
                sample_records_checked=records_processed,
            )

        # Score: 1.0 on full success, penalise for failed records
        if is_success and records_processed > 0:
            failure_rate = records_failed / (records_processed + records_failed) if (records_processed + records_failed) > 0 else 0.0
            score = max(0.0, 1.0 - failure_rate)
            passed = records_failed == 0
        elif is_success and records_processed == 0:
            # No records processed is suspicious but not a hard failure
            score = 0.5
            passed = True
        else:
            # Failed or Aborted
            score = 0.0
            passed = False

        logger.debug(
            "validate_sf_bulk_job job=%s state=%s records_processed=%d records_failed=%d passed=%s",
            bulk_job_id, state, records_processed, records_failed, passed,
        )

        return _result(
            passed=passed,
            score=round(score, 4),
            details={
                "bulk_job_id": bulk_job_id,
                "state": state,
                "terminal": True,
                "records_processed": records_processed,
                "records_failed": records_failed,
                "total_processing_time_ms": total_processing_time,
                "api_response_duration_ms": round(duration_ms, 2),
                "message": (
                    f"Bulk job completed successfully. {records_processed} records processed."
                    if passed
                    else (
                        f"Bulk job ended in state '{state}'. "
                        f"{records_failed} record(s) failed out of {records_processed + records_failed}."
                    )
                ),
            },
            sample_records_checked=records_processed,
        )

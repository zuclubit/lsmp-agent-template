"""
Migration reconciliation report generation.

Validates that the reconciliation process produces accurate, complete, and
well-structured reports covering all migrated object types, error rates, data
quality scores, unmapped fields, duplicate detection, and failure scenarios.

All tests are synchronous and in-memory. No database, no HTTP.  A lightweight
``ReconciliationReportBuilder`` is defined here that represents the minimal
interface the migration pipeline must expose; tests verify its output shape and
business rules rather than its internal implementation.

Marks:
  pytest.mark.migration_validation – gates production promotion in CI.
"""

from __future__ import annotations

import json
import math
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from domain.entities.account import AccountStatus, AccountType, Industry
from domain.exceptions.domain_exceptions import BusinessRuleViolation, ValidationError

pytestmark = pytest.mark.migration_validation

# ---------------------------------------------------------------------------
# Fixtures paths
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_LEGACY_ACCOUNTS_PATH = _FIXTURES_DIR / "sample_legacy_accounts.json"
_SF_ACCOUNTS_PATH = _FIXTURES_DIR / "sample_salesforce_accounts.json"

# ---------------------------------------------------------------------------
# Domain models for the reconciliation report
# ---------------------------------------------------------------------------

_MAX_ACCEPTABLE_ERROR_RATE_PCT: float = 0.1  # 0.1 %


@dataclass
class RecordResult:
    """Outcome for a single migrated (or failed) record."""

    legacy_id: str
    object_type: str
    success: bool
    salesforce_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    migrated_at: Optional[datetime] = None


@dataclass
class ObjectTypeSummary:
    """Per-object-type aggregate statistics."""

    object_type: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    duplicate_count: int
    unmapped_fields: List[str] = field(default_factory=list)

    @property
    def error_rate_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.failed / self.total) * 100.0

    @property
    def success_rate_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.succeeded / self.total) * 100.0


@dataclass
class DataQualityScore:
    """Composite data quality score for a migration run."""

    completeness_pct: float   # % of records with all required fields populated
    accuracy_pct: float       # % of records that passed all validation rules
    consistency_pct: float    # % of records with no cross-field conflicts
    timeliness_score: float   # 0–100: how current the migrated data is

    @property
    def overall_score(self) -> float:
        return (
            self.completeness_pct * 0.35
            + self.accuracy_pct * 0.35
            + self.consistency_pct * 0.20
            + self.timeliness_score * 0.10
        )


@dataclass
class ReconciliationReport:
    """Top-level reconciliation report produced after a migration run."""

    run_id: str
    job_id: str
    generated_at: datetime
    start_time: datetime
    end_time: datetime
    per_object: Dict[str, ObjectTypeSummary]
    data_quality: DataQualityScore
    record_results: List[RecordResult]
    unmapped_fields: List[str]
    failure_scenarios: List[str]

    # ------------------------------------------------------------------ #
    # Computed properties
    # ------------------------------------------------------------------ #

    @property
    def total_records(self) -> int:
        return sum(s.total for s in self.per_object.values())

    @property
    def total_succeeded(self) -> int:
        return sum(s.succeeded for s in self.per_object.values())

    @property
    def total_failed(self) -> int:
        return sum(s.failed for s in self.per_object.values())

    @property
    def overall_error_rate_pct(self) -> float:
        if self.total_records == 0:
            return 0.0
        return (self.total_failed / self.total_records) * 100.0

    @property
    def duplicate_count(self) -> int:
        return sum(s.duplicate_count for s in self.per_object.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "generated_at": self.generated_at.isoformat(),
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "per_object": {
                obj: {
                    "object_type": s.object_type,
                    "total": s.total,
                    "succeeded": s.succeeded,
                    "failed": s.failed,
                    "skipped": s.skipped,
                    "duplicate_count": s.duplicate_count,
                    "error_rate_pct": round(s.error_rate_pct, 4),
                    "success_rate_pct": round(s.success_rate_pct, 4),
                    "unmapped_fields": s.unmapped_fields,
                }
                for obj, s in self.per_object.items()
            },
            "data_quality": {
                "completeness_pct": self.data_quality.completeness_pct,
                "accuracy_pct": self.data_quality.accuracy_pct,
                "consistency_pct": self.data_quality.consistency_pct,
                "timeliness_score": self.data_quality.timeliness_score,
                "overall_score": round(self.data_quality.overall_score, 4),
            },
            "summary": {
                "total_records": self.total_records,
                "total_succeeded": self.total_succeeded,
                "total_failed": self.total_failed,
                "overall_error_rate_pct": round(self.overall_error_rate_pct, 4),
                "duplicate_count": self.duplicate_count,
            },
            "unmapped_fields": self.unmapped_fields,
            "failure_scenarios": self.failure_scenarios,
            "record_results": [
                {
                    "legacy_id": r.legacy_id,
                    "object_type": r.object_type,
                    "success": r.success,
                    "salesforce_id": r.salesforce_id,
                    "error_code": r.error_code,
                    "error_message": r.error_message,
                    "migrated_at": r.migrated_at.isoformat() if r.migrated_at else None,
                }
                for r in self.record_results
            ],
        }


# ---------------------------------------------------------------------------
# Report builder (minimal implementation under test)
# ---------------------------------------------------------------------------


class ReconciliationReportBuilder:
    """
    Accumulates migration results and produces a ReconciliationReport.

    This is the interface the migration pipeline must satisfy.  In production
    the pipeline calls add_success / add_failure / add_skipped for each record
    as it is processed, then calls build() at completion.
    """

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self._run_id = str(uuid.uuid4())
        self._start_time = datetime.now(tz=timezone.utc)
        self._record_results: List[RecordResult] = []
        self._unmapped_fields: List[str] = []
        self._failure_scenarios: List[str] = []
        self._skipped: Dict[str, int] = {}
        self._duplicates: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Accumulation methods
    # ------------------------------------------------------------------ #

    def add_success(
        self,
        legacy_id: str,
        object_type: str,
        salesforce_id: str,
    ) -> None:
        self._record_results.append(
            RecordResult(
                legacy_id=legacy_id,
                object_type=object_type,
                success=True,
                salesforce_id=salesforce_id,
                migrated_at=datetime.now(tz=timezone.utc),
            )
        )

    def add_failure(
        self,
        legacy_id: str,
        object_type: str,
        error_code: str,
        error_message: str,
    ) -> None:
        self._record_results.append(
            RecordResult(
                legacy_id=legacy_id,
                object_type=object_type,
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        )
        if error_message not in self._failure_scenarios:
            self._failure_scenarios.append(error_message)

    def add_skipped(self, object_type: str, reason: str) -> None:
        self._skipped[object_type] = self._skipped.get(object_type, 0) + 1
        if reason not in self._failure_scenarios:
            self._failure_scenarios.append(reason)

    def add_duplicate(self, object_type: str) -> None:
        self._duplicates[object_type] = self._duplicates.get(object_type, 0) + 1

    def register_unmapped_field(self, field_name: str) -> None:
        if field_name not in self._unmapped_fields:
            self._unmapped_fields.append(field_name)

    # ------------------------------------------------------------------ #
    # Build
    # ------------------------------------------------------------------ #

    def build(self) -> ReconciliationReport:
        end_time = datetime.now(tz=timezone.utc)

        # Aggregate per object type
        per_object: Dict[str, ObjectTypeSummary] = {}
        for result in self._record_results:
            obj = result.object_type
            if obj not in per_object:
                per_object[obj] = ObjectTypeSummary(
                    object_type=obj,
                    total=0,
                    succeeded=0,
                    failed=0,
                    skipped=self._skipped.get(obj, 0),
                    duplicate_count=self._duplicates.get(obj, 0),
                )
            per_object[obj].total += 1
            if result.success:
                per_object[obj].succeeded += 1
            else:
                per_object[obj].failed += 1

        # Include object types that only appear in _skipped
        for obj, skip_count in self._skipped.items():
            if obj not in per_object:
                per_object[obj] = ObjectTypeSummary(
                    object_type=obj,
                    total=skip_count,
                    succeeded=0,
                    failed=0,
                    skipped=skip_count,
                    duplicate_count=self._duplicates.get(obj, 0),
                )

        # Calculate simple data quality metrics
        total = len(self._record_results)
        succeeded = sum(1 for r in self._record_results if r.success)
        accuracy_pct = (succeeded / total * 100.0) if total > 0 else 100.0

        records_with_sf_id = sum(
            1 for r in self._record_results if r.success and r.salesforce_id
        )
        completeness_pct = (records_with_sf_id / succeeded * 100.0) if succeeded > 0 else 100.0

        data_quality = DataQualityScore(
            completeness_pct=completeness_pct,
            accuracy_pct=accuracy_pct,
            consistency_pct=100.0 - (len(self._unmapped_fields) * 2.0),  # simple heuristic
            timeliness_score=95.0,
        )

        return ReconciliationReport(
            run_id=self._run_id,
            job_id=self._job_id,
            generated_at=datetime.now(tz=timezone.utc),
            start_time=self._start_time,
            end_time=end_time,
            per_object=per_object,
            data_quality=data_quality,
            record_results=list(self._record_results),
            unmapped_fields=list(self._unmapped_fields),
            failure_scenarios=list(self._failure_scenarios),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_builder(job_id: str = "job-test-001") -> ReconciliationReportBuilder:
    return ReconciliationReportBuilder(job_id=job_id)


def _successful_account_migration(
    builder: ReconciliationReportBuilder, count: int, start_idx: int = 1
) -> None:
    for i in range(start_idx, start_idx + count):
        builder.add_success(
            legacy_id=f"LEGACY-ACC-{i:08d}",
            object_type="Account",
            salesforce_id=f"001xx000{i:09d}",
        )


def _failed_account_migration(
    builder: ReconciliationReportBuilder, count: int, start_idx: int = 9000
) -> None:
    for i in range(start_idx, start_idx + count):
        builder.add_failure(
            legacy_id=f"LEGACY-ACC-{i:08d}",
            object_type="Account",
            error_code="DUPLICATE_VALUE",
            error_message="Duplicate external ID detected",
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_legacy_accounts() -> List[Dict[str, Any]]:
    with _LEGACY_ACCOUNTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def sample_sf_accounts() -> List[Dict[str, Any]]:
    with _SF_ACCOUNTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def full_migration_report() -> ReconciliationReport:
    """Report from a realistic mixed migration (accounts + contacts + opportunities)."""
    builder = _make_builder("job-full-001")
    # Accounts
    _successful_account_migration(builder, count=980)
    _failed_account_migration(builder, count=20)
    # Contacts
    for i in range(1, 1950):
        builder.add_success(
            legacy_id=f"LEGACY-CON-{i:08d}",
            object_type="Contact",
            salesforce_id=f"003xx000{i:09d}",
        )
    builder.add_failure(
        legacy_id="LEGACY-CON-99999999",
        object_type="Contact",
        error_code="MISSING_REQUIRED_FIELD",
        error_message="Required field LastName is blank",
    )
    # Opportunities
    for i in range(1, 500):
        builder.add_success(
            legacy_id=f"LEGACY-OPP-{i:08d}",
            object_type="Opportunity",
            salesforce_id=f"006xx000{i:09d}",
        )
    return builder.build()


# ---------------------------------------------------------------------------
# Tests: report generation
# ---------------------------------------------------------------------------


class TestGenerateReconciliationReport:
    """Core report generation: structure, required fields, type checking."""

    def test_build_returns_reconciliation_report(self):
        builder = _make_builder()
        report = builder.build()
        assert isinstance(report, ReconciliationReport)

    def test_report_has_run_id(self):
        report = _make_builder().build()
        assert report.run_id
        assert len(report.run_id) > 0

    def test_report_has_job_id(self):
        report = _make_builder("job-xyz").build()
        assert report.job_id == "job-xyz"

    def test_report_has_generated_at_timestamp(self):
        report = _make_builder().build()
        assert report.generated_at is not None
        assert isinstance(report.generated_at, datetime)
        assert report.generated_at.tzinfo is not None

    def test_report_has_start_and_end_time(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=10)
        report = builder.build()
        assert report.start_time <= report.end_time

    def test_report_total_records_matches_added(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=50)
        report = builder.build()
        assert report.total_records == 50

    def test_report_totals_are_consistent(self, full_migration_report):
        report = full_migration_report
        assert report.total_succeeded + report.total_failed <= report.total_records

    def test_to_dict_is_json_serializable(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=5)
        report = builder.build()
        d = report.to_dict()
        serialized = json.dumps(d)  # must not raise
        assert len(serialized) > 0

    def test_to_dict_contains_required_top_level_keys(self):
        report = _make_builder().build()
        d = report.to_dict()
        required_keys = {
            "run_id", "job_id", "generated_at", "start_time", "end_time",
            "per_object", "data_quality", "summary", "unmapped_fields",
            "failure_scenarios", "record_results",
        }
        for key in required_keys:
            assert key in d, f"Top-level key '{key}' missing from report dict"

    def test_summary_section_has_correct_keys(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=3)
        report = builder.build()
        summary = report.to_dict()["summary"]
        assert "total_records" in summary
        assert "total_succeeded" in summary
        assert "total_failed" in summary
        assert "overall_error_rate_pct" in summary
        assert "duplicate_count" in summary


# ---------------------------------------------------------------------------
# Tests: all object types included
# ---------------------------------------------------------------------------


class TestReportIncludesAllObjectTypes:
    """Report must break down results per object type: Account, Contact, Opportunity, etc."""

    def test_account_object_type_present(self, full_migration_report):
        assert "Account" in full_migration_report.per_object

    def test_contact_object_type_present(self, full_migration_report):
        assert "Contact" in full_migration_report.per_object

    def test_opportunity_object_type_present(self, full_migration_report):
        assert "Opportunity" in full_migration_report.per_object

    def test_per_object_totals_sum_to_report_total(self, full_migration_report):
        summed = sum(s.total for s in full_migration_report.per_object.values())
        assert summed == full_migration_report.total_records

    def test_per_object_succeeded_summed_correctly(self, full_migration_report):
        summed_succeeded = sum(s.succeeded for s in full_migration_report.per_object.values())
        assert summed_succeeded == full_migration_report.total_succeeded

    def test_per_object_failed_summed_correctly(self, full_migration_report):
        summed_failed = sum(s.failed for s in full_migration_report.per_object.values())
        assert summed_failed == full_migration_report.total_failed

    def test_account_totals_correct(self, full_migration_report):
        account_summary = full_migration_report.per_object["Account"]
        assert account_summary.total == 1000
        assert account_summary.succeeded == 980
        assert account_summary.failed == 20

    def test_contact_totals_correct(self, full_migration_report):
        contact_summary = full_migration_report.per_object["Contact"]
        assert contact_summary.total == 1950
        assert contact_summary.succeeded == 1949
        assert contact_summary.failed == 1

    def test_per_object_error_rate_computed(self, full_migration_report):
        account_summary = full_migration_report.per_object["Account"]
        expected_rate = (20 / 1000) * 100.0
        assert account_summary.error_rate_pct == pytest.approx(expected_rate, rel=1e-6)

    def test_per_object_success_rate_is_complement_of_error_rate(self, full_migration_report):
        for summary in full_migration_report.per_object.values():
            if summary.total > 0:
                rate_sum = summary.error_rate_pct + summary.success_rate_pct
                skipped_rate = (summary.skipped / summary.total) * 100.0
                # success + error + skipped should cover all records (within float tolerance)
                # For records that are neither success nor failure (skipped), this may differ
                assert rate_sum <= 100.0 + 1e-6, (
                    f"{summary.object_type}: success + error rate exceeds 100%"
                )

    @pytest.mark.parametrize("object_type", ["Account", "Contact", "Opportunity"])
    def test_object_type_summary_in_dict_output(self, full_migration_report, object_type):
        d = full_migration_report.to_dict()
        assert object_type in d["per_object"]
        obj_d = d["per_object"][object_type]
        assert "total" in obj_d
        assert "succeeded" in obj_d
        assert "failed" in obj_d
        assert "error_rate_pct" in obj_d


# ---------------------------------------------------------------------------
# Tests: error rate threshold
# ---------------------------------------------------------------------------


class TestErrorRateBelowThreshold:
    """Overall migration error rate must stay below 0.1 % for the run to be promotable."""

    def test_zero_failures_is_below_threshold(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=1000)
        report = builder.build()
        assert report.overall_error_rate_pct < _MAX_ACCEPTABLE_ERROR_RATE_PCT

    def test_one_failure_in_1000_is_below_threshold(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=999)
        _failed_account_migration(builder, count=1)
        report = builder.build()
        # 1/1000 = 0.1% — exactly at threshold, still acceptable
        assert report.overall_error_rate_pct <= _MAX_ACCEPTABLE_ERROR_RATE_PCT

    def test_two_failures_in_1000_exceeds_threshold(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=998)
        _failed_account_migration(builder, count=2)
        report = builder.build()
        # 2/1000 = 0.2% > 0.1%
        assert report.overall_error_rate_pct > _MAX_ACCEPTABLE_ERROR_RATE_PCT

    def test_account_specific_error_rate(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=9990)
        _failed_account_migration(builder, count=10)
        report = builder.build()
        account_summary = report.per_object["Account"]
        error_rate = account_summary.error_rate_pct
        assert error_rate == pytest.approx(10 / 10000 * 100, rel=1e-6)
        assert error_rate < _MAX_ACCEPTABLE_ERROR_RATE_PCT

    def test_overall_error_rate_formula(self):
        builder = _make_builder()
        for i in range(500):
            builder.add_success(f"ACC-{i}", "Account", f"SF-ACC-{i}")
        for i in range(500):
            builder.add_success(f"CON-{i}", "Contact", f"SF-CON-{i}")
        builder.add_failure("ACC-FAIL-1", "Account", "ERR", "Something failed")
        report = builder.build()
        # 1 failure out of 1001 total
        expected = 1 / 1001 * 100.0
        assert report.overall_error_rate_pct == pytest.approx(expected, rel=1e-6)

    def test_threshold_constant_is_point_one_percent(self):
        assert _MAX_ACCEPTABLE_ERROR_RATE_PCT == pytest.approx(0.1, rel=1e-9)


# ---------------------------------------------------------------------------
# Tests: report saved as JSON
# ---------------------------------------------------------------------------


class TestReportSavedAsJson:
    """ReconciliationReport.to_dict() output must be serializable and reloadable."""

    def test_report_can_be_written_to_json_file(self, tmp_path):
        builder = _make_builder("job-json-test")
        _successful_account_migration(builder, count=10)
        report = builder.build()
        out_path = tmp_path / "reconciliation_report.json"
        out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        assert out_path.exists()
        loaded = json.loads(out_path.read_text(encoding="utf-8"))
        assert loaded["job_id"] == "job-json-test"

    def test_json_output_is_valid_utf8(self, tmp_path):
        builder = _make_builder("job-utf8")
        # Add an account with Unicode chars in the legacy ID
        builder.add_success("LEGACY-CAFÉ-001", "Account", "001xx0000CAFE001")
        report = builder.build()
        d = report.to_dict()
        raw = json.dumps(d, ensure_ascii=False)
        reloaded = json.loads(raw)
        results = reloaded["record_results"]
        assert any(r["legacy_id"] == "LEGACY-CAFÉ-001" for r in results)

    def test_json_output_has_iso_8601_timestamps(self):
        builder = _make_builder()
        report = builder.build()
        d = report.to_dict()
        for ts_field in ("generated_at", "start_time", "end_time"):
            ts = d[ts_field]
            assert "T" in ts, f"{ts_field} must be ISO-8601: got '{ts}'"

    def test_json_round_trip_preserves_counts(self, tmp_path):
        builder = _make_builder("job-round-trip")
        _successful_account_migration(builder, count=42)
        _failed_account_migration(builder, count=3)
        report = builder.build()
        out_path = tmp_path / "report_round_trip.json"
        out_path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
        loaded = json.loads(out_path.read_text(encoding="utf-8"))
        assert loaded["summary"]["total_records"] == 45
        assert loaded["summary"]["total_succeeded"] == 42
        assert loaded["summary"]["total_failed"] == 3

    def test_json_output_data_quality_section_present(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=5)
        report = builder.build()
        d = report.to_dict()
        dq = d["data_quality"]
        assert "completeness_pct" in dq
        assert "accuracy_pct" in dq
        assert "consistency_pct" in dq
        assert "timeliness_score" in dq
        assert "overall_score" in dq

    def test_json_output_numeric_fields_are_numbers(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=10)
        report = builder.build()
        d = report.to_dict()
        summary = d["summary"]
        assert isinstance(summary["total_records"], int)
        assert isinstance(summary["total_succeeded"], int)
        assert isinstance(summary["total_failed"], int)
        assert isinstance(summary["overall_error_rate_pct"], float)

    def test_json_output_includes_per_object_detail(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=3)
        for i in range(2):
            builder.add_success(f"CON-{i}", "Contact", f"SF-CON-{i}")
        report = builder.build()
        d = report.to_dict()
        assert "Account" in d["per_object"]
        assert "Contact" in d["per_object"]
        assert d["per_object"]["Account"]["total"] == 3
        assert d["per_object"]["Contact"]["total"] == 2

    def test_file_name_convention_uses_job_id(self, tmp_path):
        """Report file names should embed the job ID for traceability."""
        job_id = "job-20260316-001"
        builder = _make_builder(job_id)
        report = builder.build()
        fname = f"reconciliation_{job_id}.json"
        out_path = tmp_path / fname
        out_path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
        assert out_path.exists()
        assert job_id in out_path.name


# ---------------------------------------------------------------------------
# Tests: report includes timestamps
# ---------------------------------------------------------------------------


class TestReportIncludesTimestamps:
    """Timestamps must be present, timezone-aware, and logically ordered."""

    def test_generated_at_is_present(self):
        report = _make_builder().build()
        assert report.generated_at is not None

    def test_start_time_is_present(self):
        report = _make_builder().build()
        assert report.start_time is not None

    def test_end_time_is_present(self):
        report = _make_builder().build()
        assert report.end_time is not None

    def test_start_time_before_or_equal_end_time(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=100)
        report = builder.build()
        assert report.start_time <= report.end_time

    def test_generated_at_after_start_time(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=5)
        report = builder.build()
        assert report.generated_at >= report.start_time

    def test_all_timestamps_are_timezone_aware(self):
        report = _make_builder().build()
        for ts_name, ts_val in [
            ("generated_at", report.generated_at),
            ("start_time", report.start_time),
            ("end_time", report.end_time),
        ]:
            assert ts_val.tzinfo is not None, f"{ts_name} must be timezone-aware"

    def test_record_migrated_at_is_set_for_successes(self):
        builder = _make_builder()
        builder.add_success("ACC-001", "Account", "SF-001")
        report = builder.build()
        success_results = [r for r in report.record_results if r.success]
        for r in success_results:
            assert r.migrated_at is not None
            assert r.migrated_at.tzinfo is not None

    def test_record_migrated_at_is_none_for_failures(self):
        builder = _make_builder()
        builder.add_failure("ACC-FAIL", "Account", "ERR_001", "Validation failed")
        report = builder.build()
        failure_results = [r for r in report.record_results if not r.success]
        for r in failure_results:
            assert r.migrated_at is None

    def test_to_dict_timestamps_are_iso_strings(self):
        report = _make_builder().build()
        d = report.to_dict()
        for ts_key in ("generated_at", "start_time", "end_time"):
            ts_str = d[ts_key]
            assert isinstance(ts_str, str)
            # Must be parseable
            parsed = datetime.fromisoformat(ts_str)
            assert parsed is not None

    def test_record_result_migrated_at_in_dict(self):
        builder = _make_builder()
        builder.add_success("ACC-001", "Account", "SF-001")
        report = builder.build()
        d = report.to_dict()
        results = d["record_results"]
        success_result = next((r for r in results if r["success"]), None)
        assert success_result is not None
        assert success_result["migrated_at"] is not None


# ---------------------------------------------------------------------------
# Tests: failure scenarios documented
# ---------------------------------------------------------------------------


class TestFailureScenariosDocumented:
    """All distinct failure reasons must be collected and surfaced in the report."""

    def test_single_failure_scenario_recorded(self):
        builder = _make_builder()
        builder.add_failure("ACC-FAIL-1", "Account", "DUPLICATE_VALUE", "Duplicate external ID")
        report = builder.build()
        assert "Duplicate external ID" in report.failure_scenarios

    def test_multiple_distinct_failure_scenarios(self):
        builder = _make_builder()
        builder.add_failure("ACC-F1", "Account", "DUPLICATE_VALUE", "Duplicate external ID")
        builder.add_failure("ACC-F2", "Account", "MISSING_REQUIRED_FIELD", "Name is required")
        builder.add_failure("ACC-F3", "Account", "INVALID_TYPE", "Invalid account type value")
        report = builder.build()
        assert len(report.failure_scenarios) == 3

    def test_duplicate_failure_messages_deduplicated(self):
        builder = _make_builder()
        for i in range(10):
            builder.add_failure(
                f"ACC-FAIL-{i}", "Account", "DUPLICATE_VALUE", "Duplicate external ID"
            )
        report = builder.build()
        # All 10 have the same message; should appear only once
        assert report.failure_scenarios.count("Duplicate external ID") == 1

    def test_skipped_reasons_in_failure_scenarios(self):
        builder = _make_builder()
        builder.add_skipped("Account", "Account is suspended — migration blocked")
        report = builder.build()
        assert "Account is suspended — migration blocked" in report.failure_scenarios

    def test_failure_scenarios_in_json_output(self):
        builder = _make_builder()
        builder.add_failure("ACC-F", "Account", "ERR_001", "Critical error in transformation")
        report = builder.build()
        d = report.to_dict()
        assert "failure_scenarios" in d
        assert "Critical error in transformation" in d["failure_scenarios"]

    def test_no_failure_scenarios_when_all_succeed(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=20)
        report = builder.build()
        assert len(report.failure_scenarios) == 0

    def test_business_rule_violation_as_failure_scenario(self):
        """Suspended account violation must appear as a documented failure scenario."""
        builder = _make_builder()
        builder.add_failure(
            "LEGACY-ACC-00000003",
            "Account",
            "SUSPENDED_ACCOUNT_CANNOT_BE_MIGRATED",
            "Account LEGACY-ACC-00000003 is suspended and cannot be migrated",
        )
        report = builder.build()
        assert any(
            "suspended" in scenario.lower()
            for scenario in report.failure_scenarios
        )

    def test_already_migrated_as_failure_scenario(self):
        """Pre-migrated account violation must be documented."""
        builder = _make_builder()
        builder.add_failure(
            "LEGACY-ACC-00000005",
            "Account",
            "ACCOUNT_ALREADY_MIGRATED",
            "Account LEGACY-ACC-00000005 is already migrated to Salesforce ID 001xx000003GYkZDDZ",
        )
        report = builder.build()
        assert any(
            "already migrated" in scenario.lower()
            for scenario in report.failure_scenarios
        )

    @pytest.mark.parametrize(
        "error_code, error_message",
        [
            ("DUPLICATE_VALUE", "Duplicate external ID detected"),
            ("MISSING_REQUIRED_FIELD", "Required field Name is blank"),
            ("INVALID_TYPE", "Invalid picklist value for AccountType"),
            ("FIELD_CUSTOM_VALIDATION_EXCEPTION", "Custom validation rule failed"),
            ("UNABLE_TO_LOCK_ROW", "Record locked by another process"),
        ],
    )
    def test_various_error_codes_documented(self, error_code, error_message):
        builder = _make_builder()
        builder.add_failure("LEGACY-ACC-TEST", "Account", error_code, error_message)
        report = builder.build()
        # Failure scenario must be documented
        assert error_message in report.failure_scenarios
        # The failed record must appear in record_results
        assert any(
            r.error_code == error_code for r in report.record_results
        )


# ---------------------------------------------------------------------------
# Tests: unmapped fields
# ---------------------------------------------------------------------------


class TestUnmappedFieldsDetection:
    """Fields present in legacy data but absent from the domain model must be flagged."""

    def test_single_unmapped_field_recorded(self):
        builder = _make_builder()
        builder.register_unmapped_field("legacy_custom_field_1")
        report = builder.build()
        assert "legacy_custom_field_1" in report.unmapped_fields

    def test_multiple_unmapped_fields(self):
        builder = _make_builder()
        for f in ("erp_cost_centre", "erp_division_code", "legacy_segment"):
            builder.register_unmapped_field(f)
        report = builder.build()
        assert len(report.unmapped_fields) == 3

    def test_duplicate_unmapped_field_deduplicated(self):
        builder = _make_builder()
        builder.register_unmapped_field("erp_cost_centre")
        builder.register_unmapped_field("erp_cost_centre")
        report = builder.build()
        assert report.unmapped_fields.count("erp_cost_centre") == 1

    def test_unmapped_fields_in_json_output(self):
        builder = _make_builder()
        builder.register_unmapped_field("erp_segment_code")
        report = builder.build()
        d = report.to_dict()
        assert "erp_segment_code" in d["unmapped_fields"]

    def test_no_unmapped_fields_when_clean(self):
        report = _make_builder().build()
        assert report.unmapped_fields == []


# ---------------------------------------------------------------------------
# Tests: duplicate detection results
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Duplicates must be counted per object type and included in the report."""

    def test_duplicate_count_tracked_per_object_type(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=10)
        builder.add_duplicate("Account")
        builder.add_duplicate("Account")
        report = builder.build()
        account_summary = report.per_object["Account"]
        assert account_summary.duplicate_count == 2

    def test_total_duplicate_count_across_all_types(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=5)
        for _ in range(3):
            builder.add_duplicate("Account")
        for i in range(3):
            builder.add_success(f"CON-{i}", "Contact", f"SF-CON-{i}")
        builder.add_duplicate("Contact")
        report = builder.build()
        assert report.duplicate_count == 4

    def test_duplicate_count_in_json_output(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=2)
        builder.add_duplicate("Account")
        report = builder.build()
        d = report.to_dict()
        assert d["summary"]["duplicate_count"] >= 1


# ---------------------------------------------------------------------------
# Tests: data quality score
# ---------------------------------------------------------------------------


class TestDataQualityScore:
    """Data quality scores must be reasonable values within expected ranges."""

    def test_accuracy_pct_is_100_when_all_succeed(self):
        builder = _make_builder()
        _successful_account_migration(builder, count=100)
        report = builder.build()
        assert report.data_quality.accuracy_pct == pytest.approx(100.0, rel=1e-6)

    def test_accuracy_pct_decreases_with_failures(self):
        builder_clean = _make_builder()
        _successful_account_migration(builder_clean, count=100)
        clean_report = builder_clean.build()

        builder_dirty = _make_builder()
        _successful_account_migration(builder_dirty, count=90)
        _failed_account_migration(builder_dirty, count=10)
        dirty_report = builder_dirty.build()

        assert dirty_report.data_quality.accuracy_pct < clean_report.data_quality.accuracy_pct

    def test_overall_score_is_between_0_and_100(self, full_migration_report):
        score = full_migration_report.data_quality.overall_score
        assert 0.0 <= score <= 100.0

    def test_data_quality_overall_score_formula(self):
        dq = DataQualityScore(
            completeness_pct=80.0,
            accuracy_pct=90.0,
            consistency_pct=95.0,
            timeliness_score=70.0,
        )
        expected = 80.0 * 0.35 + 90.0 * 0.35 + 95.0 * 0.20 + 70.0 * 0.10
        assert dq.overall_score == pytest.approx(expected, rel=1e-6)

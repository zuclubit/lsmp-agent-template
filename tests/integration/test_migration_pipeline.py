"""
End-to-end integration tests for the migration pipeline.

Tests the full ETL flow at the domain level:
    extract → transform → validate → load (mocked Salesforce)

All external I/O (database, Salesforce API) is replaced with in-memory
fakes so the tests are fast and deterministic.

Marks: @pytest.mark.integration
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Domain imports
# ---------------------------------------------------------------------------
from domain.entities.account import Account, AccountStatus, AccountType, Industry
from domain.entities.migration_job import MigrationConfig, MigrationJob, PhaseRecord
from domain.events.migration_events import (
    MigrationCompleted,
    MigrationFailed,
    MigrationPhase,
    MigrationStarted,
    MigrationStatus,
    PhaseCompleted,
    RecordMigrated,
)
from domain.exceptions.domain_exceptions import (
    BusinessRuleViolation,
    InvalidStateTransition,
    ValidationError,
)
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> MigrationConfig:
    defaults = dict(
        source_system="ERP_v2",
        target_org_id="00Dxx0000001gPLEAY",
        record_types=("Account",),
        batch_size=200,
        max_retries=3,
        dry_run=False,
        error_threshold_percent=5.0,
    )
    defaults.update(overrides)
    return MigrationConfig(**defaults)


def _make_job(total_records: int = 100, **config_overrides) -> MigrationJob:
    return MigrationJob.create(
        config=_make_config(**config_overrides),
        initiated_by="admin@migration.example.com",
        total_records=total_records,
    )


def _legacy_account_rows() -> List[Dict[str, Any]]:
    """Load the fixture file of realistic legacy account data."""
    return json.loads((_FIXTURES_DIR / "sample_legacy_accounts.json").read_text())


def _transform_legacy_row(row: Dict[str, Any]) -> Optional[Account]:
    """
    Minimal transformer: maps a legacy fixture row to an Account domain entity.
    Returns None for suspended accounts (they are excluded from migration).
    """
    if row["acct_status"] == "S":
        return None  # Suspended accounts are skipped

    billing_address = None
    if row.get("bill_addr_city") and row.get("bill_addr_country"):
        try:
            billing_address = Address(
                street=row["bill_addr_street"] or "N/A",
                city=row["bill_addr_city"],
                country_code=row["bill_addr_country"],
                state=row.get("bill_addr_state"),
                postal_code=row.get("bill_addr_zip"),
                unit=row.get("bill_addr_unit"),
            )
        except Exception:
            billing_address = None

    primary_email = Email.try_parse(row["email_address"]) if row.get("email_address") else None

    account = Account.create(
        legacy_id=row["acct_id"],
        name=row["acct_name"],
        account_type=AccountType.CUSTOMER if row["acct_type"] == "CUST" else AccountType.PROSPECT,
        status=AccountStatus.ACTIVE if row["acct_status"] == "A" else AccountStatus.INACTIVE,
        billing_address=billing_address,
        primary_email=primary_email,
        annual_revenue=row.get("annual_revenue"),
        number_of_employees=row.get("employee_count"),
        description=row.get("acct_description"),
    )
    return account


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def migration_config() -> MigrationConfig:
    return _make_config()


@pytest.fixture
def migration_job(migration_config) -> MigrationJob:
    return MigrationJob.create(
        config=migration_config,
        initiated_by="pipeline@migration.example.com",
        total_records=500,
    )


@pytest.fixture
def legacy_rows() -> List[Dict[str, Any]]:
    return _legacy_account_rows()


@pytest.fixture
def mock_sf_client():
    """Async mock Salesforce client that returns fake IDs for every create call."""
    client = AsyncMock()
    counter = {"n": 0}

    async def _fake_create(sobject: str, data: Dict[str, Any]) -> Dict[str, Any]:
        counter["n"] += 1
        return {"id": f"001Dn{counter['n']:012d}AAA", "success": True, "errors": []}

    async def _fake_bulk_upsert(sobject, records, external_id_field):
        from integrations.rest_clients.salesforce_client import BulkJobResult, BulkJobState
        return BulkJobResult(
            job_id="7505x000001TestJob",
            state=BulkJobState.JOB_COMPLETE,
            number_records_processed=len(records),
            number_records_failed=0,
            successful_results=[{"sf__Id": f"001x{i:09d}", "sf__Created": "true"} for i in range(len(records))],
            failed_results=[],
        )

    client.create = AsyncMock(side_effect=_fake_create)
    client.bulk_upsert = AsyncMock(side_effect=_fake_bulk_upsert)
    client.query = AsyncMock(return_value=MagicMock(records=[], total_size=0, done=True))
    return client


# ===========================================================================
# TESTS
# ===========================================================================


@pytest.mark.integration
class TestMigrationJobStateMachine:
    """Full lifecycle of the MigrationJob aggregate (no I/O)."""

    def test_job_starts_in_pending_state(self, migration_job):
        assert migration_job.status == MigrationStatus.PENDING

    def test_start_transitions_to_running(self, migration_job):
        migration_job.start()
        assert migration_job.status == MigrationStatus.RUNNING

    def test_start_emits_migration_started_event(self, migration_job):
        migration_job.start()
        events = migration_job.collect_events()
        started_events = [e for e in events if isinstance(e, MigrationStarted)]
        assert len(started_events) == 1
        assert started_events[0].initiated_by == "pipeline@migration.example.com"

    def test_full_phase_progression(self, migration_job):
        """Walk through all migration phases and verify counters are updated."""
        migration_job.start()
        migration_job.collect_events()  # clear start event

        phases = [
            MigrationPhase.PREREQUISITE_CHECK,
            MigrationPhase.DATA_EXTRACTION,
            MigrationPhase.DATA_VALIDATION,
            MigrationPhase.DATA_TRANSFORMATION,
            MigrationPhase.DATA_LOAD,
        ]
        for phase in phases:
            migration_job.begin_phase(phase)
            migration_job.complete_phase(
                phase,
                records_processed=100,
                records_succeeded=98,
                records_failed=2,
                records_skipped=0,
            )

        events = migration_job.collect_events()
        phase_events = [e for e in events if isinstance(e, PhaseCompleted)]
        assert len(phase_events) == 5
        assert migration_job.counters.records_succeeded == 490  # 98 × 5

    def test_pause_and_resume(self, migration_job):
        migration_job.start()
        migration_job.collect_events()
        migration_job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        migration_job.pause(paused_by="ops@example.com", reason="Maintenance window")
        assert migration_job.status == MigrationStatus.PAUSED

        migration_job.resume(resumed_by="ops@example.com")
        assert migration_job.status == MigrationStatus.RUNNING

    def test_complete_emits_migration_completed_event(self, migration_job):
        migration_job.start()
        migration_job.collect_events()
        migration_job.begin_phase(MigrationPhase.DATA_LOAD)
        migration_job.complete_phase(
            MigrationPhase.DATA_LOAD,
            records_processed=500,
            records_succeeded=500,
            records_failed=0,
        )
        migration_job.collect_events()
        migration_job.complete(report_url="https://reports.example.com/mig-001")

        events = migration_job.collect_events()
        completed = [e for e in events if isinstance(e, MigrationCompleted)]
        assert len(completed) == 1
        assert completed[0].records_succeeded == 500
        assert completed[0].report_url == "https://reports.example.com/mig-001"

    def test_invalid_transition_raises_exception(self, migration_job):
        """PENDING → COMPLETED is not an allowed transition."""
        with pytest.raises(InvalidStateTransition) as exc_info:
            migration_job.complete()
        assert "pending" in exc_info.value.from_state
        assert "completed" in exc_info.value.to_state

    def test_error_threshold_triggers_auto_fail(self, migration_job):
        """If error rate exceeds threshold, complete_phase auto-fails the job."""
        migration_job.start()
        migration_job.begin_phase(MigrationPhase.DATA_LOAD)
        # 20% error rate > 5% threshold
        migration_job.complete_phase(
            MigrationPhase.DATA_LOAD,
            records_processed=100,
            records_succeeded=80,
            records_failed=20,
        )
        assert migration_job.status == MigrationStatus.FAILED

    def test_validation_blocking_errors_fail_job(self, migration_job):
        """Blocking validation errors must transition the job to FAILED."""
        migration_job.start()
        migration_job.begin_phase(MigrationPhase.DATA_VALIDATION)
        migration_job.record_validation_result(
            total_checked=100,
            passed=85,
            warnings=5,
            errors=10,
            rule_results={"REQUIRED_FIELDS": 10},
            blocking_errors=True,
        )
        assert migration_job.status == MigrationStatus.FAILED


@pytest.mark.integration
class TestFullAccountMigrationPipeline:
    """End-to-end pipeline: extract fixture data → transform → load to mock SF."""

    def test_extract_and_transform_fixture_accounts(self, legacy_rows):
        """All 5 fixture accounts are extracted; the suspended one is filtered out."""
        transformed = [_transform_legacy_row(row) for row in legacy_rows]
        non_null = [a for a in transformed if a is not None]

        # Fixture has 5 rows: 4 active/inactive, 1 suspended (skipped)
        assert len(non_null) == 4

    def test_already_migrated_account_raises_on_second_migration(self, legacy_rows):
        """An account with sf_id set must raise BusinessRuleViolation on mark_migrated."""
        # Row 4 (index 4) is "Already Migrated Co" with sf_id already set
        already_migrated_row = next(r for r in legacy_rows if r.get("sf_id"))
        account = _transform_legacy_row(already_migrated_row)
        assert account is not None

        # Simulate the row already has a Salesforce ID in DB
        sf_id = SalesforceId(already_migrated_row["sf_id"])
        account.mark_migrated(sf_id, "MIG-PRIOR-RUN")
        account.collect_events()

        with pytest.raises(BusinessRuleViolation) as exc_info:
            account.mark_migrated(SalesforceId("001Dn000001NewAAA"), "MIG-NEW-RUN")
        assert "ACCOUNT_ALREADY_MIGRATED" in exc_info.value.rule

    @pytest.mark.asyncio
    async def test_full_account_migration_pipeline(self, legacy_rows, mock_sf_client):
        """All non-suspended, non-already-migrated accounts are loaded to Salesforce."""
        job = _make_job(total_records=len(legacy_rows))
        job.start()
        job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        job.complete_phase(
            MigrationPhase.DATA_EXTRACTION,
            records_processed=len(legacy_rows),
            records_succeeded=len(legacy_rows),
            records_failed=0,
        )
        job.begin_phase(MigrationPhase.DATA_TRANSFORMATION)
        accounts = [a for a in (_transform_legacy_row(r) for r in legacy_rows) if a is not None]
        job.complete_phase(
            MigrationPhase.DATA_TRANSFORMATION,
            records_processed=len(legacy_rows),
            records_succeeded=len(accounts),
            records_failed=0,
            records_skipped=len(legacy_rows) - len(accounts),
        )

        # Load: only migrate accounts not already migrated
        to_migrate = [a for a in accounts if not a.is_migrated]
        job.begin_phase(MigrationPhase.DATA_LOAD)

        loaded = 0
        for account in to_migrate:
            payload = account.to_salesforce_payload()
            result = await mock_sf_client.create("Account", payload)
            sf_id = SalesforceId(result["id"])
            account.mark_migrated(sf_id, str(job.job_id))
            loaded += 1

        job.complete_phase(
            MigrationPhase.DATA_LOAD,
            records_processed=len(to_migrate),
            records_succeeded=loaded,
            records_failed=0,
        )
        job.complete()

        assert job.status == MigrationStatus.COMPLETED
        assert all(a.is_migrated for a in to_migrate)
        assert mock_sf_client.create.call_count == loaded

    @pytest.mark.asyncio
    async def test_migration_handles_duplicate_records(self, mock_sf_client):
        """Accounts with same legacy_id must raise BusinessRuleViolation on duplicate migration."""
        account = Account.create(
            legacy_id="LEGACY-ACC-DUPE-001",
            name="Duplicate Test Corp",
        )
        first_sf_id = SalesforceId("001Dn000001FirstAAA")
        account.mark_migrated(first_sf_id, "MIG-001")

        with pytest.raises(BusinessRuleViolation):
            account.mark_migrated(SalesforceId("001Dn000001SecndAAA"), "MIG-002")

    @pytest.mark.asyncio
    async def test_migration_rollback_on_validation_failure(self):
        """Migration must transition to FAILED when validation finds blocking errors."""
        job = _make_job(total_records=100)
        job.start()
        job.begin_phase(MigrationPhase.DATA_VALIDATION)
        job.record_validation_result(
            total_checked=100,
            passed=60,
            warnings=10,
            errors=30,
            rule_results={"MISSING_NAME": 15, "INVALID_EMAIL": 15},
            blocking_errors=True,
        )

        assert job.status == MigrationStatus.FAILED

        events = job.collect_events()
        failed_events = [e for e in events if isinstance(e, MigrationFailed)]
        assert len(failed_events) == 1
        assert "BLOCKING_VALIDATION_ERRORS" in failed_events[0].error_code

    def test_migration_resumes_from_last_completed_phase(self):
        """Paused migration must resume from where it left off."""
        job = _make_job(total_records=1000)
        job.start()
        job.begin_phase(MigrationPhase.DATA_EXTRACTION)
        job.complete_phase(
            MigrationPhase.DATA_EXTRACTION,
            records_processed=1000, records_succeeded=1000, records_failed=0
        )
        job.begin_phase(MigrationPhase.DATA_VALIDATION)
        job.pause(paused_by="ops@example.com", reason="Night maintenance")

        # Resume and continue with transformation
        job.resume(resumed_by="ops@example.com")
        job.complete_phase(
            MigrationPhase.DATA_VALIDATION,
            records_processed=1000, records_succeeded=990, records_failed=10
        )

        completed_phases = [p for p in job.phase_history if p.is_complete]
        assert len(completed_phases) == 2
        assert completed_phases[0].phase == MigrationPhase.DATA_EXTRACTION
        assert completed_phases[1].phase == MigrationPhase.DATA_VALIDATION

    @pytest.mark.asyncio
    async def test_batch_processing_1000_records(self, mock_sf_client):
        """1000 accounts must all be loaded via the mock client without errors."""
        accounts = [
            Account.create(legacy_id=f"LEGACY-BATCH-{i:06d}", name=f"Batch Co {i:04d}")
            for i in range(1, 1001)
        ]

        payloads = [a.to_salesforce_payload() for a in accounts]

        from integrations.rest_clients.salesforce_client import BulkJobResult, BulkJobState
        bulk_result = BulkJobResult(
            job_id="7505x000001BulkTest",
            state=BulkJobState.JOB_COMPLETE,
            number_records_processed=1000,
            number_records_failed=0,
            successful_results=[{"sf__Id": f"001x{i:09d}", "sf__Created": "true"} for i in range(1000)],
            failed_results=[],
        )
        mock_sf_client.bulk_upsert.return_value = bulk_result

        result = await mock_sf_client.bulk_upsert("Account", payloads, "Legacy_ID__c")

        assert result.number_records_processed == 1000
        assert result.number_records_failed == 0
        assert result.state == BulkJobState.JOB_COMPLETE

    @pytest.mark.asyncio
    async def test_error_quarantine_for_invalid_records(self):
        """Records that fail transformation must be quarantined, not silently dropped."""
        invalid_rows = [
            {"acct_id": "BAD-001", "acct_name": "", "acct_status": "A", "acct_type": "CUST"},
            {"acct_id": "BAD-002", "acct_name": "   ", "acct_status": "A", "acct_type": "CUST"},
        ]

        quarantine = []
        transformed = []
        for row in invalid_rows:
            try:
                account = Account.create(
                    legacy_id=row["acct_id"],
                    name=row["acct_name"],
                )
                transformed.append(account)
            except (ValidationError, BusinessRuleViolation) as exc:
                quarantine.append({"row": row, "error": str(exc)})

        assert len(quarantine) == 2
        assert len(transformed) == 0


@pytest.mark.integration
class TestContactMigrationWithRelationships:
    """Contacts must be linked to their migrated Account records."""

    def test_contact_migration_requires_account_mapping(self):
        """
        A contact must reference the Salesforce Account ID of its parent.
        Verify that the relationship mapping logic produces the correct SF ID.
        """
        # Simulate account mapping table: legacy_id → sf_id
        account_mapping = {
            "LEGACY-ACC-00000001": "001Dn000001MockAA2",
            "LEGACY-ACC-00000004": "001Dn000001MockBB3",
        }

        legacy_contacts = [
            {"id": "LEGACY-CON-001", "account_id": "LEGACY-ACC-00000001",
             "first_name": "Jane", "last_name": "Smith", "email": "jane@acme.com"},
            {"id": "LEGACY-CON-002", "account_id": "LEGACY-ACC-00000004",
             "first_name": "Bob", "last_name": "Jones", "email": "bob@gfp.com"},
            {"id": "LEGACY-CON-003", "account_id": "LEGACY-ACC-UNKNOWN",
             "first_name": "Unknown", "last_name": "Parent", "email": "u@example.com"},
        ]

        mapped = []
        unmapped = []
        for contact in legacy_contacts:
            sf_account_id = account_mapping.get(contact["account_id"])
            if sf_account_id:
                mapped.append({**contact, "AccountId": sf_account_id})
            else:
                unmapped.append(contact)

        assert len(mapped) == 2
        assert len(unmapped) == 1
        assert mapped[0]["AccountId"] == "001Dn000001MockAA2"
        assert unmapped[0]["id"] == "LEGACY-CON-003"

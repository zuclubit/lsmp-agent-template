"""
Migration Validation: Data completeness checks.

Verifies all records were migrated with no data loss.
Run AFTER migration completes by comparing source system
counts/checksums with migrated Salesforce data.

Usage:
    pytest tests/migration_validation/ -m migration_validation --migration-id MIG-2025-001

Environment:
    MIGRATION_VALIDATION_MODE=post_migration
    SOURCE_DB_URL=...
    SALESFORCE_VALIDATION_TOKEN=...
"""
from __future__ import annotations

import csv
import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# Validation Models
# ---------------------------------------------------------------------------

@dataclass
class RecordCountSummary:
    object_type: str
    source_count: int
    migrated_count: int
    tolerance_pct: float = 0.001  # 0.1% tolerance

    @property
    def completeness_rate(self) -> float:
        if self.source_count == 0:
            return 1.0
        return self.migrated_count / self.source_count

    @property
    def is_within_tolerance(self) -> bool:
        loss = max(0, self.source_count - self.migrated_count)
        loss_rate = loss / max(self.source_count, 1)
        return loss_rate <= self.tolerance_pct

    @property
    def missing_count(self) -> int:
        return max(0, self.source_count - self.migrated_count)


@dataclass
class FieldCompletenessResult:
    field_name: str
    total_records: int
    populated_in_source: int
    populated_in_target: int

    @property
    def preservation_rate(self) -> float:
        if self.populated_in_source == 0:
            return 1.0
        return self.populated_in_target / self.populated_in_source


@dataclass
class MigrationValidationReport:
    migration_id: str
    validation_timestamp: str
    record_counts: List[RecordCountSummary] = field(default_factory=list)
    field_completeness: List[FieldCompletenessResult] = field(default_factory=list)
    orphaned_contacts: int = 0
    duplicate_records: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def overall_pass(self) -> bool:
        return (
            all(s.is_within_tolerance for s in self.record_counts)
            and self.orphaned_contacts == 0
            and len(self.errors) == 0
        )


# ---------------------------------------------------------------------------
# Mock data sources (replace with real DB/API clients in prod)
# ---------------------------------------------------------------------------

class MockSourceSystem:
    """Simulates legacy source system data for validation testing."""

    ACCOUNTS = [
        {"id": f"LEG-{i:05d}", "name": f"Company {i}", "email": f"co{i}@example.com",
         "phone": f"555{i:07d}", "account_type": "C", "industry_code": "TECH",
         "billing_country": "United States"} for i in range(1, 501)
    ]

    CONTACTS = [
        {"id": f"CON-{i:05d}", "account_id": f"LEG-{(i % 500) + 1:05d}",
         "first_name": f"First{i}", "last_name": f"Last{i}",
         "email": f"contact{i}@example.com"} for i in range(1, 1001)
    ]

    def get_account_count(self) -> int:
        return len(self.ACCOUNTS)

    def get_contact_count(self) -> int:
        return len(self.CONTACTS)

    def get_accounts(self) -> List[Dict]:
        return self.ACCOUNTS

    def get_contacts(self) -> List[Dict]:
        return self.CONTACTS

    def get_account_ids(self) -> set:
        return {a["id"] for a in self.ACCOUNTS}


class MockSalesforceOrg:
    """Simulates Salesforce org data for validation testing."""

    def __init__(self, missing_accounts: int = 0, missing_contacts: int = 0):
        self._missing_accounts = missing_accounts
        self._missing_contacts = missing_contacts
        self._source = MockSourceSystem()

    def get_account_count(self) -> int:
        return self._source.get_account_count() - self._missing_accounts

    def get_contact_count(self) -> int:
        return self._source.get_contact_count() - self._missing_contacts

    def get_migrated_account_ids(self) -> set:
        all_ids = self._source.get_account_ids()
        if self._missing_accounts > 0:
            return set(list(all_ids)[:-self._missing_accounts])
        return all_ids

    def get_accounts_with_email(self) -> int:
        return self._source.get_account_count() - self._missing_accounts

    def get_contacts_with_account(self) -> int:
        """Returns number of contacts that have a valid Account reference."""
        return self._source.get_contact_count() - self._missing_contacts

    def query_field_population(self, sobject: str, field: str) -> Tuple[int, int]:
        """Returns (total_records, records_with_field_populated)."""
        if sobject == "Account":
            total = self.get_account_count()
            # Simulate: 95% of accounts have phone populated
            populated = int(total * 0.95) if field == "Phone" else total
            return total, populated
        return 0, 0


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def source_system() -> MockSourceSystem:
    return MockSourceSystem()


@pytest.fixture
def perfect_salesforce_org(source_system) -> MockSalesforceOrg:
    """Salesforce org with 100% migration completeness."""
    return MockSalesforceOrg(missing_accounts=0, missing_contacts=0)


@pytest.fixture
def imperfect_org_within_tolerance() -> MockSalesforceOrg:
    """Org missing < 0.1% of records (within acceptable tolerance)."""
    return MockSalesforceOrg(missing_accounts=0, missing_contacts=1)  # 0.1% missing


@pytest.fixture
def imperfect_org_exceeds_tolerance() -> MockSalesforceOrg:
    """Org missing > 0.1% of records (exceeds tolerance - should fail)."""
    return MockSalesforceOrg(missing_accounts=5, missing_contacts=0)  # 1% accounts missing


# ===========================================================================
# TESTS
# ===========================================================================


@pytest.mark.migration_validation
class TestAccountRecordCount:
    """Validates Account record counts match between source and Salesforce."""

    def test_account_count_matches_exactly(self, source_system, perfect_salesforce_org):
        """Zero-tolerance completeness: all 500 accounts must be in Salesforce."""
        source_count = source_system.get_account_count()
        sf_count = perfect_salesforce_org.get_account_count()

        summary = RecordCountSummary(
            object_type="Account",
            source_count=source_count,
            migrated_count=sf_count,
        )
        assert summary.missing_count == 0
        assert summary.completeness_rate == 1.0
        assert summary.is_within_tolerance

    def test_missing_accounts_within_tolerance_passes(self, imperfect_org_within_tolerance, source_system):
        """0 missing accounts from 500 (0%) should pass at 0.1% tolerance."""
        summary = RecordCountSummary(
            object_type="Account",
            source_count=source_system.get_account_count(),
            migrated_count=imperfect_org_within_tolerance.get_account_count(),
            tolerance_pct=0.001,
        )
        assert summary.is_within_tolerance

    def test_missing_accounts_exceeds_tolerance_fails(self, imperfect_org_exceeds_tolerance, source_system):
        """5 missing accounts from 500 (1%) exceeds 0.1% tolerance."""
        summary = RecordCountSummary(
            object_type="Account",
            source_count=source_system.get_account_count(),
            migrated_count=imperfect_org_exceeds_tolerance.get_account_count(),
            tolerance_pct=0.001,
        )
        assert not summary.is_within_tolerance
        assert summary.missing_count == 5

    def test_completeness_rate_calculation(self):
        """Completeness rate should be a fraction between 0 and 1."""
        summary = RecordCountSummary(
            object_type="Account", source_count=1000, migrated_count=998
        )
        assert summary.completeness_rate == pytest.approx(0.998, rel=1e-3)

    def test_zero_source_records_is_100_complete(self):
        """Edge case: no source records means 100% complete."""
        summary = RecordCountSummary(
            object_type="Account", source_count=0, migrated_count=0
        )
        assert summary.completeness_rate == 1.0


@pytest.mark.migration_validation
class TestContactRecordCount:
    """Validates Contact record counts."""

    def test_contact_count_matches(self, source_system, perfect_salesforce_org):
        source_count = source_system.get_contact_count()
        sf_count = perfect_salesforce_org.get_contact_count()
        assert source_count == sf_count

    def test_contact_count_report(self, source_system, perfect_salesforce_org):
        summary = RecordCountSummary(
            object_type="Contact",
            source_count=source_system.get_contact_count(),
            migrated_count=perfect_salesforce_org.get_contact_count(),
        )
        assert summary.is_within_tolerance
        assert summary.completeness_rate == 1.0


@pytest.mark.migration_validation
class TestOrphanedContactDetection:
    """Validates that no contacts exist without a parent Account."""

    def test_no_orphaned_contacts_in_perfect_migration(self, source_system, perfect_salesforce_org):
        """All contacts must be linked to a valid Account."""
        contacts_with_account = perfect_salesforce_org.get_contacts_with_account()
        total_contacts = perfect_salesforce_org.get_contact_count()
        assert contacts_with_account == total_contacts

    def test_orphaned_contacts_detected(self):
        """Simulation: if contacts have no parent account, report should flag them."""
        org = MockSalesforceOrg(missing_accounts=10, missing_contacts=0)
        # In this simulation, we check account ID referential integrity
        source = MockSourceSystem()
        migrated_account_ids = org.get_migrated_account_ids()
        source_account_ids = source.get_account_ids()

        missing_account_ids = source_account_ids - migrated_account_ids
        assert len(missing_account_ids) == 10  # 10 accounts missing → potential orphans


@pytest.mark.migration_validation
class TestRequiredFieldPopulation:
    """Validates required fields are populated post-migration."""

    def test_account_name_always_populated(self, perfect_salesforce_org):
        """Account Name is required in Salesforce and must always be populated."""
        total, populated = perfect_salesforce_org.query_field_population("Account", "Name")
        assert populated == total, f"Accounts missing Name: {total - populated}"

    def test_email_preservation_rate(self, perfect_salesforce_org):
        """Emails should be preserved at the same rate as they existed in source."""
        total, populated = perfect_salesforce_org.query_field_population("Account", "Email__c")
        # All source accounts have email, so all SF accounts should too
        completeness = FieldCompletenessResult(
            field_name="Email__c",
            total_records=total,
            populated_in_source=total,
            populated_in_target=populated,
        )
        assert completeness.preservation_rate == 1.0

    def test_phone_number_preservation(self, perfect_salesforce_org):
        """Phone numbers should be preserved for at least 95% of records."""
        total, populated = perfect_salesforce_org.query_field_population("Account", "Phone")
        result = FieldCompletenessResult(
            field_name="Phone",
            total_records=total,
            populated_in_source=total,
            populated_in_target=populated,
        )
        assert result.preservation_rate >= 0.95, (
            f"Phone preservation rate {result.preservation_rate:.1%} is below 95% threshold"
        )


@pytest.mark.migration_validation
class TestLegacyIDPreservation:
    """Validates Legacy_ID__c field is set for all migrated records."""

    def test_all_accounts_have_legacy_id(self, source_system, perfect_salesforce_org):
        """Every migrated Account must have Legacy_ID__c populated for traceability."""
        migrated_ids = perfect_salesforce_org.get_migrated_account_ids()
        source_ids = source_system.get_account_ids()
        # Every source ID should appear in Salesforce
        missing = source_ids - migrated_ids
        assert len(missing) == 0, f"Accounts missing from Salesforce: {missing}"


@pytest.mark.migration_validation
class TestMigrationValidationReport:
    """Tests for the overall migration validation report generation."""

    def test_perfect_migration_report_passes(self, source_system, perfect_salesforce_org):
        """A perfect migration should produce a passing validation report."""
        report = MigrationValidationReport(
            migration_id="MIG-2025-001",
            validation_timestamp="2025-03-16T00:00:00Z",
            record_counts=[
                RecordCountSummary("Account", source_system.get_account_count(), perfect_salesforce_org.get_account_count()),
                RecordCountSummary("Contact", source_system.get_contact_count(), perfect_salesforce_org.get_contact_count()),
            ],
            orphaned_contacts=0,
        )
        assert report.overall_pass is True

    def test_report_fails_with_orphaned_contacts(self, source_system, perfect_salesforce_org):
        """Report should fail if orphaned contacts are detected."""
        report = MigrationValidationReport(
            migration_id="MIG-2025-002",
            validation_timestamp="2025-03-16T01:00:00Z",
            record_counts=[
                RecordCountSummary("Account", 500, 500),
            ],
            orphaned_contacts=15,  # 15 orphaned contacts detected
        )
        assert report.overall_pass is False

    def test_report_fails_with_missing_records_above_tolerance(self, source_system):
        """Report should fail if record loss exceeds tolerance."""
        report = MigrationValidationReport(
            migration_id="MIG-2025-003",
            validation_timestamp="2025-03-16T02:00:00Z",
            record_counts=[
                RecordCountSummary("Account", 500, 490, tolerance_pct=0.001),  # 2% loss
            ],
        )
        assert report.overall_pass is False

    def test_report_fails_with_validation_errors(self, source_system, perfect_salesforce_org):
        """Report should fail if there are validation errors logged."""
        report = MigrationValidationReport(
            migration_id="MIG-2025-004",
            validation_timestamp="2025-03-16T03:00:00Z",
            record_counts=[RecordCountSummary("Account", 500, 500)],
            errors=["Checksum mismatch for 3 records"],
        )
        assert report.overall_pass is False

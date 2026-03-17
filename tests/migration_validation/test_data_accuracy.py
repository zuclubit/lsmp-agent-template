"""
Migration validation: Data accuracy checks.

Spot-checks specific record values for correctness by comparing legacy source
data (erp.accounts rows from fixtures) with migrated Salesforce data (from
sample_salesforce_accounts.json). Uses the real domain mapper
(PostgresAccountRepository._to_domain) and Account.to_salesforce_payload()
as the authoritative transformation chain.

All tests are synchronous — no I/O, no database, no HTTP. Fixtures are loaded
from JSON files in tests/fixtures/ so that any fixture change causes test drift
to surface immediately.

Marks:
  pytest.mark.migration_validation – used by CI to gate promotion to prod.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

import pytest

from adapters.outbound.postgres_account_repository import (
    PostgresAccountRepository,
    _INDUSTRY_MAP,
    _STATUS_MAP,
    _TYPE_MAP,
)
from domain.entities.account import (
    Account,
    AccountStatus,
    AccountType,
    ContactInfo,
    Industry,
)
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId

pytestmark = pytest.mark.migration_validation

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_LEGACY_ACCOUNTS_PATH = _FIXTURES_DIR / "sample_legacy_accounts.json"
_SF_ACCOUNTS_PATH = _FIXTURES_DIR / "sample_salesforce_accounts.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_legacy_accounts() -> List[Dict[str, Any]]:
    """Load raw legacy account rows from the JSON fixture."""
    with _LEGACY_ACCOUNTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_sf_accounts() -> List[Dict[str, Any]]:
    """Load Salesforce account records from the JSON fixture."""
    with _SF_ACCOUNTS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _legacy_row_to_domain(row: Dict[str, Any]) -> Account:
    """Convert a legacy fixture row to an Account via the real mapper."""
    return PostgresAccountRepository._to_domain(row)


def _sf_by_legacy_id(sf_accounts: List[Dict[str, Any]], legacy_id: str) -> Optional[Dict[str, Any]]:
    """Return the Salesforce record whose Legacy_ID__c matches legacy_id."""
    for rec in sf_accounts:
        if rec.get("Legacy_ID__c") == legacy_id:
            return rec
    return None


def _domain_to_sf_payload(account: Account) -> Dict[str, Any]:
    """Produce Salesforce payload via the domain entity's own method."""
    return account.to_salesforce_payload()


def _parse_sf_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse Salesforce ISO-8601 datetime string to timezone-aware datetime."""
    if not raw:
        return None
    # Salesforce format: "2018-03-15T08:30:00.000+0000"
    cleaned = re.sub(r"\.(\d+)\+0000$", "+00:00", raw)
    cleaned = re.sub(r"\+0000$", "+00:00", cleaned)
    return datetime.fromisoformat(cleaned)


# ---------------------------------------------------------------------------
# Fixtures (pytest)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def legacy_rows() -> List[Dict[str, Any]]:
    return _load_legacy_accounts()


@pytest.fixture(scope="module")
def sf_accounts() -> List[Dict[str, Any]]:
    return _load_sf_accounts()


@pytest.fixture(scope="module")
def domain_accounts(legacy_rows: List[Dict[str, Any]]) -> List[Account]:
    return [_legacy_row_to_domain(row) for row in legacy_rows]


@pytest.fixture(scope="module")
def sf_payloads(domain_accounts: List[Account]) -> List[Dict[str, Any]]:
    """Salesforce payloads produced by the domain entities."""
    return [acc.to_salesforce_payload() for acc in domain_accounts]


# ---------------------------------------------------------------------------
# Test class: account name accuracy
# ---------------------------------------------------------------------------


class TestAccountNameAccuracy:
    """Verify that account names are preserved exactly, including Unicode."""

    def test_account_name_preserved_exactly_ascii(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        assert sf_rec is not None, "Salesforce record for LEGACY-ACC-00000001 must exist"
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.name == row["acct_name"], "Domain name must equal raw legacy name"
        assert sf_rec["Name"] == domain_acc.name, "Salesforce Name must equal domain name"

    def test_account_name_preserved_exactly_unicode(self, legacy_rows, sf_accounts):
        """Name with umlauts and special Unicode chars must survive the transform."""
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000002")
        assert sf_rec is not None
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.name == "Café Münchener GmbH"
        assert sf_rec["Name"] == "Café Münchener GmbH"

    def test_account_name_not_truncated(self, domain_accounts):
        for acc in domain_accounts:
            assert len(acc.name) <= 255, f"Account {acc.legacy_id} name exceeds 255 chars"

    def test_domain_payload_name_matches_entity(self, domain_accounts, sf_payloads):
        for acc, payload in zip(domain_accounts, sf_payloads):
            assert payload["Name"] == acc.name, (
                f"Payload Name for {acc.legacy_id} must equal entity name"
            )


# ---------------------------------------------------------------------------
# Test class: address normalization
# ---------------------------------------------------------------------------


class TestAddressNormalizationAccuracy:
    """Billing address fields must be mapped correctly; shipping null when absent."""

    def test_billing_street_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        assert sf_rec is not None
        # BillingStreet in SF may concatenate street + unit
        assert "1 Infinite Loop" in sf_rec["BillingStreet"]

    def test_billing_city_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address is not None
        assert domain_acc.billing_address.city == "Cupertino"
        assert sf_rec["BillingCity"] == "Cupertino"

    def test_billing_state_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address.state == "CA"
        assert sf_rec["BillingState"] == "CA"

    def test_billing_postal_code_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address.postal_code == "95014"
        assert sf_rec["BillingPostalCode"] == "95014"

    def test_billing_country_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address.country_code == "US"
        assert sf_rec["BillingCountry"] == "US"

    def test_international_address_no_state(self, legacy_rows, sf_accounts):
        """German address has no state; both domain and SF record must reflect that."""
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000002")
        assert sf_rec is not None
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address is not None
        assert domain_acc.billing_address.state is None
        assert sf_rec["BillingState"] is None

    def test_uk_postcode_with_space_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000004")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000004")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address.postal_code == "EC3A 8EP"
        assert sf_rec["BillingPostalCode"] == "EC3A 8EP"

    def test_null_shipping_address_handled(self, legacy_rows):
        """Account with no shipping address columns must not raise, domain value is None."""
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        domain_acc = _legacy_row_to_domain(row)
        # The repository currently maps shipping to None; payload should not include shipping keys
        payload = domain_acc.to_salesforce_payload()
        assert "ShippingStreet" not in payload or payload.get("ShippingStreet") is None

    def test_unicode_city_name_preserved(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000002")
        domain_acc = _legacy_row_to_domain(row)
        assert domain_acc.billing_address.city == "München"
        assert sf_rec["BillingCity"] == "München"


# ---------------------------------------------------------------------------
# Test class: date/datetime conversion accuracy
# ---------------------------------------------------------------------------


class TestDateConversionAccuracy:
    """Timestamps must be preserved with UTC timezone and millisecond precision."""

    def test_created_at_is_timezone_aware(self, domain_accounts):
        for acc in domain_accounts:
            assert acc.created_at.tzinfo is not None, (
                f"Account {acc.legacy_id} created_at must be timezone-aware"
            )

    def test_updated_at_is_timezone_aware(self, domain_accounts):
        for acc in domain_accounts:
            assert acc.updated_at.tzinfo is not None, (
                f"Account {acc.legacy_id} updated_at must be timezone-aware"
            )

    def test_created_at_utc_for_acme(self, legacy_rows, domain_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000001")
        # "2018-03-15T08:30:00Z" → year 2018, month 3, day 15, hour 8, minute 30
        assert acc.created_at.year == 2018
        assert acc.created_at.month == 3
        assert acc.created_at.day == 15
        assert acc.created_at.hour == 8
        assert acc.created_at.minute == 30
        assert acc.created_at.tzinfo == timezone.utc

    def test_sf_created_date_matches_legacy_created_ts(self, legacy_rows, sf_accounts):
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000001")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000001")
        domain_acc = _legacy_row_to_domain(row)
        sf_dt = _parse_sf_datetime(sf_rec.get("CreatedDate"))
        assert sf_dt is not None
        assert domain_acc.created_at.year == sf_dt.year
        assert domain_acc.created_at.month == sf_dt.month
        assert domain_acc.created_at.day == sf_dt.day
        assert domain_acc.created_at.hour == sf_dt.hour
        assert domain_acc.created_at.minute == sf_dt.minute

    def test_sf_last_modified_matches_legacy_modified_ts(self, legacy_rows, sf_accounts):
        for legacy_id in ["LEGACY-ACC-00000001", "LEGACY-ACC-00000004"]:
            row = next(r for r in legacy_rows if r["acct_id"] == legacy_id)
            sf_rec = _sf_by_legacy_id(sf_accounts, legacy_id)
            domain_acc = _legacy_row_to_domain(row)
            sf_dt = _parse_sf_datetime(sf_rec.get("LastModifiedDate"))
            assert sf_dt is not None
            assert domain_acc.updated_at.year == sf_dt.year
            assert domain_acc.updated_at.month == sf_dt.month
            assert domain_acc.updated_at.day == sf_dt.day

    def test_naive_datetime_gets_utc_assigned(self):
        """Rows with naive datetime objects (no tz) must be tagged UTC by the mapper."""
        row = {
            "acct_id": "TEST-NAIVE-DT",
            "acct_name": "Naive Datetime Test",
            "acct_type": "CUST",
            "acct_status": "A",
            "industry_code": None,
            "bill_addr_city": None,
            "bill_addr_country": None,
            "phone_number": None,
            "website_url": None,
            "email_address": None,
            "sf_id": None,
            "annual_revenue": None,
            "employee_count": None,
            "acct_description": None,
            # naive datetime (no tzinfo)
            "created_ts": datetime(2022, 6, 15, 10, 30, 0),
            "modified_ts": datetime(2022, 6, 15, 10, 30, 0),
            "row_version": 1,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.created_at.tzinfo is not None, "Mapper must attach UTC to naive datetimes"
        assert acc.updated_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Test class: currency / numeric field accuracy
# ---------------------------------------------------------------------------


class TestCurrencyFieldAccuracy:
    """Annual revenue and employee count must be preserved as exact numeric values."""

    def test_annual_revenue_preserved_exact(self, legacy_rows, domain_accounts):
        cases = [
            ("LEGACY-ACC-00000001", 12_500_000.00),
            ("LEGACY-ACC-00000002", 875_000.00),
            ("LEGACY-ACC-00000004", 250_000_000.00),
            ("LEGACY-ACC-00000005", 4_200_000.00),
        ]
        for legacy_id, expected_rev in cases:
            acc = next(a for a in domain_accounts if a.legacy_id == legacy_id)
            assert acc.annual_revenue == pytest.approx(expected_rev, rel=1e-9), (
                f"{legacy_id}: annual_revenue mismatch"
            )

    def test_annual_revenue_null_when_absent(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        assert acc.annual_revenue is None

    def test_employee_count_preserved(self, domain_accounts):
        cases = [
            ("LEGACY-ACC-00000001", 250),
            ("LEGACY-ACC-00000002", 18),
            ("LEGACY-ACC-00000004", 1200),
        ]
        for legacy_id, expected_count in cases:
            acc = next(a for a in domain_accounts if a.legacy_id == legacy_id)
            assert acc.number_of_employees == expected_count

    def test_employee_count_null_when_absent(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        assert acc.number_of_employees is None

    def test_sf_annual_revenue_matches_domain(self, domain_accounts, sf_accounts):
        for acc in domain_accounts:
            sf_rec = _sf_by_legacy_id(sf_accounts, acc.legacy_id)
            if sf_rec is None:
                continue
            if acc.annual_revenue is None:
                assert sf_rec.get("AnnualRevenue") is None
            else:
                assert sf_rec["AnnualRevenue"] == pytest.approx(acc.annual_revenue, rel=1e-9)

    def test_large_revenue_no_precision_loss(self):
        """250_000_000 must survive round-trip without floating-point truncation."""
        row = {
            "acct_id": "LEGACY-ACC-00000004",
            "acct_name": "Global Finance Partners",
            "acct_type": "PART",
            "acct_status": "A",
            "industry_code": "FIN",
            "bill_addr_city": "London",
            "bill_addr_country": "GB",
            "bill_addr_street": "30 St Mary Axe",
            "bill_addr_state": None,
            "bill_addr_zip": "EC3A 8EP",
            "bill_addr_unit": "Floor 12",
            "phone_number": "+442071234567",
            "fax_number": None,
            "website_url": None,
            "email_address": None,
            "annual_revenue": 250_000_000.00,
            "employee_count": 1200,
            "acct_description": None,
            "sf_id": None,
            "created_ts": "2015-09-01T07:00:00Z",
            "modified_ts": "2026-02-28T16:45:00Z",
            "row_version": 14,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.annual_revenue == 250_000_000.00
        payload = acc.to_salesforce_payload()
        assert payload["AnnualRevenue"] == 250_000_000.00


# ---------------------------------------------------------------------------
# Test class: special characters
# ---------------------------------------------------------------------------


class TestSpecialCharacterHandling:
    """Unicode, HTML-reserved chars, and diacritics must survive the transform."""

    def test_description_with_html_chars(self, legacy_rows, sf_accounts):
        """Description containing <>&\"' must be preserved verbatim."""
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000002")
        domain_acc = _legacy_row_to_domain(row)
        assert "<>&\"'" in domain_acc.description
        assert sf_rec["Description"] == domain_acc.description

    def test_description_em_dash_preserved(self, legacy_rows, sf_accounts):
        """Description with em-dash (—) must not be stripped or replaced."""
        row = next(r for r in legacy_rows if r["acct_id"] == "LEGACY-ACC-00000002")
        sf_rec = _sf_by_legacy_id(sf_accounts, "LEGACY-ACC-00000002")
        domain_acc = _legacy_row_to_domain(row)
        assert "—" in domain_acc.description
        assert "—" in sf_rec["Description"]

    def test_umlaut_in_city_preserved(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000002")
        assert "ü" in acc.billing_address.city  # München

    def test_umlaut_in_name_preserved(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000002")
        assert "ü" in acc.name  # Münchener

    def test_payload_description_matches_domain(self, domain_accounts, sf_payloads):
        """to_salesforce_payload must not mutate the description."""
        for acc, payload in zip(domain_accounts, sf_payloads):
            if acc.description is not None:
                assert payload.get("Description") == acc.description


# ---------------------------------------------------------------------------
# Test class: null field handling
# ---------------------------------------------------------------------------


class TestNullFieldHandling:
    """Absent / null legacy fields must map to None in domain and be omitted from payload."""

    def test_null_industry_maps_to_none(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000002")
        assert acc.industry is None

    def test_null_email_maps_to_none(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        assert acc.primary_email is None

    def test_null_phone_maps_to_none(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        assert acc.contact_info is None or acc.contact_info.phone is None

    def test_null_annual_revenue_omitted_from_payload(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        payload = acc.to_salesforce_payload()
        # None values are stripped from payload by to_salesforce_payload
        assert "AnnualRevenue" not in payload

    def test_null_description_omitted_from_payload(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000003")
        assert acc.description is None
        payload = acc.to_salesforce_payload()
        assert "Description" not in payload

    def test_null_fax_omitted_from_payload(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000002")
        payload = acc.to_salesforce_payload()
        # Fax is null; ContactInfo.fax is None; payload should not include Fax key
        fax_val = payload.get("Fax")
        assert fax_val is None or "Fax" not in payload

    def test_null_sf_id_means_not_migrated(self, domain_accounts):
        for acc in domain_accounts:
            if acc.legacy_id != "LEGACY-ACC-00000005":
                assert not acc.is_migrated, f"{acc.legacy_id} should not be marked migrated"

    def test_all_payload_values_are_non_none(self, domain_accounts):
        """to_salesforce_payload must strip None values before returning."""
        for acc in domain_accounts:
            payload = acc.to_salesforce_payload()
            for key, val in payload.items():
                assert val is not None, (
                    f"Account {acc.legacy_id}: payload key '{key}' has None value (should be absent)"
                )


# ---------------------------------------------------------------------------
# Test class: picklist value mapping
# ---------------------------------------------------------------------------


class TestPicklistValueMapping:
    """Legacy codes must map to correct Salesforce picklist labels."""

    @pytest.mark.parametrize(
        "legacy_id, expected_type",
        [
            ("LEGACY-ACC-00000001", "Customer"),
            ("LEGACY-ACC-00000003", "Prospect"),
            ("LEGACY-ACC-00000004", "Partner"),
            ("LEGACY-ACC-00000005", "Customer"),
        ],
    )
    def test_account_type_mapping(self, legacy_id, expected_type, domain_accounts, sf_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == legacy_id)
        sf_rec = _sf_by_legacy_id(sf_accounts, legacy_id)
        payload = acc.to_salesforce_payload()
        assert payload["Type"] == expected_type, (
            f"{legacy_id}: expected Type={expected_type}, got {payload['Type']}"
        )
        if sf_rec:
            assert sf_rec["Type"] == expected_type

    @pytest.mark.parametrize(
        "legacy_id, expected_industry",
        [
            ("LEGACY-ACC-00000001", "Technology"),
            ("LEGACY-ACC-00000004", "Finance"),
            ("LEGACY-ACC-00000005", "Retail"),
        ],
    )
    def test_industry_mapping(self, legacy_id, expected_industry, domain_accounts, sf_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == legacy_id)
        sf_rec = _sf_by_legacy_id(sf_accounts, legacy_id)
        payload = acc.to_salesforce_payload()
        assert payload.get("Industry") == expected_industry, (
            f"{legacy_id}: expected Industry={expected_industry}, got {payload.get('Industry')}"
        )
        if sf_rec:
            assert sf_rec["Industry"] == expected_industry

    @pytest.mark.parametrize(
        "legacy_status_code, expected_domain_status",
        [
            ("A", AccountStatus.ACTIVE),
            ("I", AccountStatus.INACTIVE),
            ("S", AccountStatus.SUSPENDED),
            ("P", AccountStatus.PENDING_REVIEW),
        ],
    )
    def test_status_code_mapping(self, legacy_status_code, expected_domain_status):
        row = {
            "acct_id": f"TEST-STATUS-{legacy_status_code}",
            "acct_name": "Status Test Account",
            "acct_type": "CUST",
            "acct_status": legacy_status_code,
            "industry_code": None,
            "bill_addr_city": None,
            "bill_addr_country": None,
            "phone_number": None,
            "website_url": None,
            "email_address": None,
            "sf_id": None,
            "annual_revenue": None,
            "employee_count": None,
            "acct_description": None,
            "created_ts": None,
            "modified_ts": None,
            "row_version": 1,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.status == expected_domain_status, (
            f"Legacy status '{legacy_status_code}' should map to {expected_domain_status}"
        )

    def test_unknown_type_code_defaults_to_prospect(self):
        row = {
            "acct_id": "TEST-UNKNOWN-TYPE",
            "acct_name": "Unknown Type Account",
            "acct_type": "ZZZZZ",  # unknown code
            "acct_status": "A",
            "industry_code": None,
            "bill_addr_city": None,
            "bill_addr_country": None,
            "phone_number": None,
            "website_url": None,
            "email_address": None,
            "sf_id": None,
            "annual_revenue": None,
            "employee_count": None,
            "acct_description": None,
            "created_ts": None,
            "modified_ts": None,
            "row_version": 1,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.account_type == AccountType.PROSPECT

    def test_unknown_industry_code_maps_to_none(self):
        row = {
            "acct_id": "TEST-UNKNOWN-INDUSTRY",
            "acct_name": "Unknown Industry",
            "acct_type": "CUST",
            "acct_status": "A",
            "industry_code": "ZZZZ",  # unknown
            "bill_addr_city": None,
            "bill_addr_country": None,
            "phone_number": None,
            "website_url": None,
            "email_address": None,
            "sf_id": None,
            "annual_revenue": None,
            "employee_count": None,
            "acct_description": None,
            "created_ts": None,
            "modified_ts": None,
            "row_version": 1,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.industry is None

    def test_mfg_industry_maps_to_manufacturing(self):
        row = {
            "acct_id": "TEST-MFG",
            "acct_name": "Manufacturing Test",
            "acct_type": "PROS",
            "acct_status": "S",
            "industry_code": "MFG",
            "bill_addr_city": "Detroit",
            "bill_addr_country": "US",
            "bill_addr_street": "99 Industrial Ave",
            "bill_addr_state": "MI",
            "bill_addr_zip": "48201",
            "bill_addr_unit": None,
            "phone_number": None,
            "fax_number": None,
            "website_url": None,
            "email_address": None,
            "sf_id": None,
            "annual_revenue": None,
            "employee_count": None,
            "acct_description": None,
            "created_ts": None,
            "modified_ts": None,
            "row_version": 1,
        }
        acc = _legacy_row_to_domain(row)
        assert acc.industry == Industry.MANUFACTURING


# ---------------------------------------------------------------------------
# Test class: relationship integrity
# ---------------------------------------------------------------------------


class TestRelationshipIntegrity:
    """Legacy_ID__c and sf_id must be cross-referenced accurately."""

    def test_legacy_id_c_present_in_all_sf_records(self, sf_accounts):
        for rec in sf_accounts:
            assert "Legacy_ID__c" in rec, "Every SF record must carry Legacy_ID__c"
            assert rec["Legacy_ID__c"], f"Legacy_ID__c must not be blank: {rec}"

    def test_legacy_id_c_matches_acct_id(self, legacy_rows, sf_accounts):
        legacy_ids = {row["acct_id"] for row in legacy_rows}
        sf_legacy_ids = {rec["Legacy_ID__c"] for rec in sf_accounts}
        # Every SF record's Legacy_ID__c must correspond to a known legacy account
        for sf_lid in sf_legacy_ids:
            assert sf_lid in legacy_ids, f"SF Legacy_ID__c '{sf_lid}' not found in legacy rows"

    def test_already_migrated_account_has_sf_id(self, domain_accounts):
        acc = next(a for a in domain_accounts if a.legacy_id == "LEGACY-ACC-00000005")
        assert acc.is_migrated, "LEGACY-ACC-00000005 must be pre-migrated (sf_id set)"
        assert acc.salesforce_id is not None

    def test_unmigrated_accounts_have_no_sf_id(self, domain_accounts):
        unmigrated = [
            a for a in domain_accounts
            if a.legacy_id in {"LEGACY-ACC-00000001", "LEGACY-ACC-00000002",
                               "LEGACY-ACC-00000003", "LEGACY-ACC-00000004"}
        ]
        for acc in unmigrated:
            assert not acc.is_migrated, f"{acc.legacy_id} should NOT be migrated"

    def test_legacy_id_in_payload(self, domain_accounts):
        """Every account's Salesforce payload must include Legacy_ID__c."""
        for acc in domain_accounts:
            payload = acc.to_salesforce_payload()
            assert "Legacy_ID__c" in payload, f"Payload for {acc.legacy_id} missing Legacy_ID__c"
            assert payload["Legacy_ID__c"] == acc.legacy_id

    def test_sf_record_id_is_18_char_salesforce_id(self, sf_accounts):
        for rec in sf_accounts:
            sf_id = rec.get("Id")
            if sf_id:
                assert len(sf_id) in (15, 18), (
                    f"Salesforce Id '{sf_id}' must be 15 or 18 characters"
                )

    def test_no_duplicate_legacy_ids_in_sf_records(self, sf_accounts):
        legacy_ids = [rec["Legacy_ID__c"] for rec in sf_accounts]
        assert len(legacy_ids) == len(set(legacy_ids)), "Duplicate Legacy_ID__c found in SF records"

    def test_no_duplicate_sf_ids(self, sf_accounts):
        sf_ids = [rec["Id"] for rec in sf_accounts]
        assert len(sf_ids) == len(set(sf_ids)), "Duplicate Salesforce Id values found in SF records"

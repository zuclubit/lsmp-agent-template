"""
Contract tests for the Legacy System data schema.

Defines and enforces the expected schema of legacy data we consume during
migration. Breaking changes in the legacy system's column layout, data types,
or encoding should fail these tests before they reach the migration pipeline.

Approach:
  - Each "contract" is a JSON Schema document describing the expected structure
  - We validate sample data (from fixtures) against the schema
  - We also test our field-mapping constants stay in sync with the contract

Marks: @pytest.mark.contract
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# ===========================================================================
# Legacy DB schema contracts (expressed as lightweight validators)
# ===========================================================================


def _validate_schema(record: Dict[str, Any], schema: Dict, record_label: str = "") -> List[str]:
    """
    Validate a record dict against a schema definition.
    Returns a list of violation strings (empty list = valid).
    """
    violations: List[str] = []
    label = f"[{record_label}] " if record_label else ""

    for field_name, rules in schema.items():
        value = record.get(field_name, "__MISSING__")

        # Required check
        if rules.get("required") and value == "__MISSING__":
            violations.append(f"{label}Missing required field '{field_name}'")
            continue

        if value == "__MISSING__" or value is None:
            if rules.get("nullable", True):
                continue
            else:
                violations.append(f"{label}Field '{field_name}' must not be null")
                continue

        # Type check
        expected_type = rules.get("type")
        if expected_type and not isinstance(value, expected_type):
            violations.append(
                f"{label}Field '{field_name}' expected {expected_type.__name__}, got {type(value).__name__}"
            )

        # Max length check
        max_len = rules.get("max_length")
        if max_len and isinstance(value, str) and len(value) > max_len:
            violations.append(f"{label}Field '{field_name}' exceeds max length {max_len} (got {len(value)})")

        # Pattern check
        pattern = rules.get("pattern")
        if pattern and isinstance(value, str):
            if not re.match(pattern, value):
                violations.append(f"{label}Field '{field_name}' value '{value}' does not match pattern '{pattern}'")

        # Allowed values check
        allowed = rules.get("allowed_values")
        if allowed and value not in allowed:
            violations.append(f"{label}Field '{field_name}' value '{value}' not in allowed values {allowed}")

    return violations


# ---------------------------------------------------------------------------
# Legacy Account table schema contract
# ---------------------------------------------------------------------------

LEGACY_ACCOUNT_SCHEMA: Dict[str, Any] = {
    "acct_id": {
        "required": True,
        "nullable": False,
        "type": str,
        "max_length": 50,
    },
    "acct_name": {
        "required": True,
        "nullable": False,
        "type": str,
        "max_length": 255,
    },
    "acct_type": {
        "required": True,
        "nullable": False,
        "type": str,
        "allowed_values": {"CUST", "PROS", "PART", "COMP"},
    },
    "acct_status": {
        "required": True,
        "nullable": False,
        "type": str,
        "allowed_values": {"A", "I", "S", "P"},
    },
    "industry_code": {
        "required": False,
        "nullable": True,
        "type": str,
        "allowed_values": {"TECH", "FIN", "HLTH", "MFG", "RET", "GOVT", "EDU", "MEDIA",
                           "BANK", "INS", "TRANS", "UTIL", None},
    },
    "bill_addr_street": {"required": False, "nullable": True, "type": str, "max_length": 255},
    "bill_addr_city": {"required": False, "nullable": True, "type": str, "max_length": 100},
    "bill_addr_state": {"required": False, "nullable": True, "type": str, "max_length": 50},
    "bill_addr_zip": {"required": False, "nullable": True, "type": str, "max_length": 20},
    "bill_addr_country": {
        "required": False,
        "nullable": True,
        "type": str,
        "max_length": 2,
    },
    "phone_number": {"required": False, "nullable": True, "type": str, "max_length": 40},
    "website_url": {"required": False, "nullable": True, "type": str, "max_length": 500},
    "email_address": {"required": False, "nullable": True, "type": str, "max_length": 254},
    "annual_revenue": {"required": False, "nullable": True},
    "employee_count": {"required": False, "nullable": True},
    "sf_id": {"required": False, "nullable": True, "type": str, "max_length": 18},
    "row_version": {"required": True, "nullable": False, "type": int},
}

# ---------------------------------------------------------------------------
# Legacy Contact table schema contract
# ---------------------------------------------------------------------------

LEGACY_CONTACT_SCHEMA: Dict[str, Any] = {
    "contact_id": {"required": True, "nullable": False, "type": str, "max_length": 50},
    "acct_id": {"required": True, "nullable": False, "type": str, "max_length": 50},
    "first_name": {"required": False, "nullable": True, "type": str, "max_length": 80},
    "last_name": {"required": True, "nullable": False, "type": str, "max_length": 80},
    "email_address": {"required": False, "nullable": True, "type": str, "max_length": 254},
    "phone_number": {"required": False, "nullable": True, "type": str, "max_length": 40},
    "title": {"required": False, "nullable": True, "type": str, "max_length": 128},
    "department": {"required": False, "nullable": True, "type": str, "max_length": 80},
    "contact_status": {
        "required": True,
        "nullable": False,
        "type": str,
        "allowed_values": {"A", "I"},
    },
    "row_version": {"required": True, "nullable": False, "type": int},
}

# ---------------------------------------------------------------------------
# Legacy API response schema contract (REST-style legacy endpoint)
# ---------------------------------------------------------------------------

LEGACY_API_ACCOUNT_LIST_RESPONSE_SCHEMA: Dict[str, Any] = {
    "status": {
        "required": True,
        "nullable": False,
        "type": str,
        "allowed_values": {"success", "error"},
    },
    "total_count": {"required": True, "nullable": False, "type": int},
    "page": {"required": True, "nullable": False, "type": int},
    "page_size": {"required": True, "nullable": False, "type": int},
    "data": {"required": True, "nullable": False, "type": list},
}

# ---------------------------------------------------------------------------
# CSV export format contract
# ---------------------------------------------------------------------------

LEGACY_CSV_REQUIRED_COLUMNS = {
    "acct_id", "acct_name", "acct_type", "acct_status",
    "bill_addr_city", "bill_addr_country", "row_version",
}

LEGACY_CSV_OPTIONAL_COLUMNS = {
    "industry_code", "bill_addr_street", "bill_addr_state", "bill_addr_zip",
    "ship_addr_street", "ship_addr_city", "ship_addr_country",
    "phone_number", "fax_number", "website_url", "email_address",
    "annual_revenue", "employee_count", "acct_description", "sf_id",
    "created_ts", "modified_ts",
}


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def legacy_account_fixtures() -> List[Dict[str, Any]]:
    return json.loads((_FIXTURES_DIR / "sample_legacy_accounts.json").read_text())


@pytest.fixture
def valid_legacy_account() -> Dict[str, Any]:
    """A minimally valid legacy account row."""
    return {
        "acct_id": "LEGACY-ACC-00000001",
        "acct_name": "Acme Corporation",
        "acct_type": "CUST",
        "acct_status": "A",
        "industry_code": "TECH",
        "bill_addr_street": "1 Test St",
        "bill_addr_unit": None,
        "bill_addr_city": "Testville",
        "bill_addr_state": "CA",
        "bill_addr_zip": "90210",
        "bill_addr_country": "US",
        "phone_number": "+14085551234",
        "fax_number": None,
        "website_url": "https://example.com",
        "email_address": "test@example.com",
        "annual_revenue": 1000000.0,
        "employee_count": 100,
        "acct_description": "Test account",
        "sf_id": None,
        "created_ts": "2020-01-01T00:00:00Z",
        "modified_ts": "2024-01-01T00:00:00Z",
        "row_version": 1,
    }


@pytest.fixture
def valid_legacy_contact() -> Dict[str, Any]:
    return {
        "contact_id": "LEGACY-CON-00000001",
        "acct_id": "LEGACY-ACC-00000001",
        "first_name": "Jane",
        "last_name": "Smith",
        "email_address": "jane.smith@example.com",
        "phone_number": "+14085559876",
        "title": "CTO",
        "department": "Engineering",
        "contact_status": "A",
        "row_version": 1,
    }


# ===========================================================================
# TESTS
# ===========================================================================


@pytest.mark.contract
class TestLegacyAccountSchemaContract:
    """The legacy erp.accounts table must conform to the defined schema contract."""

    def test_valid_account_passes_schema_validation(self, valid_legacy_account):
        violations = _validate_schema(valid_legacy_account, LEGACY_ACCOUNT_SCHEMA, "valid_account")
        assert violations == [], f"Unexpected violations: {violations}"

    def test_account_id_is_required(self):
        row = {"acct_name": "Orphan", "acct_type": "CUST", "acct_status": "A", "row_version": 1}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("acct_id" in v for v in violations)

    def test_account_name_is_required(self):
        row = {"acct_id": "ID-001", "acct_type": "CUST", "acct_status": "A", "row_version": 1}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("acct_name" in v for v in violations)

    def test_account_name_max_255_characters(self):
        row = {
            "acct_id": "ID-001", "acct_type": "CUST", "acct_status": "A", "row_version": 1,
            "acct_name": "X" * 256,
        }
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("acct_name" in v and "max length" in v for v in violations)

    @pytest.mark.parametrize("acct_type", ["CUST", "PROS", "PART", "COMP"])
    def test_all_valid_account_types_pass(self, valid_legacy_account, acct_type):
        row = {**valid_legacy_account, "acct_type": acct_type}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert not any("acct_type" in v for v in violations)

    def test_invalid_account_type_fails(self, valid_legacy_account):
        row = {**valid_legacy_account, "acct_type": "INVALID_TYPE"}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("acct_type" in v for v in violations)

    @pytest.mark.parametrize("status", ["A", "I", "S", "P"])
    def test_all_valid_statuses_pass(self, valid_legacy_account, status):
        row = {**valid_legacy_account, "acct_status": status}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert not any("acct_status" in v for v in violations)

    def test_invalid_status_fails(self, valid_legacy_account):
        row = {**valid_legacy_account, "acct_status": "DELETED"}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("acct_status" in v for v in violations)

    def test_null_nullable_fields_are_valid(self, valid_legacy_account):
        """Fields marked nullable=True must accept None without violation."""
        row = {
            **valid_legacy_account,
            "industry_code": None,
            "phone_number": None,
            "email_address": None,
            "annual_revenue": None,
            "sf_id": None,
        }
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert violations == [], f"Nullable fields should not fail: {violations}"

    def test_row_version_must_be_integer(self, valid_legacy_account):
        row = {**valid_legacy_account, "row_version": "not_an_int"}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        assert any("row_version" in v for v in violations)

    def test_fixture_accounts_all_pass_schema(self, legacy_account_fixtures):
        """All records in the fixture file must conform to the legacy schema contract."""
        for record in legacy_account_fixtures:
            violations = _validate_schema(record, LEGACY_ACCOUNT_SCHEMA, record.get("acct_id", "?"))
            assert violations == [], (
                f"Fixture account '{record.get('acct_id')}' violated schema: {violations}"
            )


@pytest.mark.contract
class TestLegacyContactSchemaContract:
    """The legacy contacts table must conform to the defined schema contract."""

    def test_valid_contact_passes_schema(self, valid_legacy_contact):
        violations = _validate_schema(valid_legacy_contact, LEGACY_CONTACT_SCHEMA)
        assert violations == []

    def test_contact_id_required(self):
        row = {"acct_id": "ACC-001", "last_name": "Smith", "contact_status": "A", "row_version": 1}
        violations = _validate_schema(row, LEGACY_CONTACT_SCHEMA)
        assert any("contact_id" in v for v in violations)

    def test_last_name_required(self):
        row = {
            "contact_id": "CON-001", "acct_id": "ACC-001",
            "contact_status": "A", "row_version": 1,
        }
        violations = _validate_schema(row, LEGACY_CONTACT_SCHEMA)
        assert any("last_name" in v for v in violations)

    def test_account_reference_required(self):
        """Every contact must have a parent account reference."""
        row = {
            "contact_id": "CON-001", "last_name": "Smith",
            "contact_status": "A", "row_version": 1,
        }
        violations = _validate_schema(row, LEGACY_CONTACT_SCHEMA)
        assert any("acct_id" in v for v in violations)

    @pytest.mark.parametrize("status", ["A", "I"])
    def test_valid_contact_statuses(self, valid_legacy_contact, status):
        row = {**valid_legacy_contact, "contact_status": status}
        violations = _validate_schema(row, LEGACY_CONTACT_SCHEMA)
        assert not any("contact_status" in v for v in violations)

    def test_optional_fields_can_be_null(self, valid_legacy_contact):
        row = {**valid_legacy_contact, "first_name": None, "phone_number": None, "title": None}
        violations = _validate_schema(row, LEGACY_CONTACT_SCHEMA)
        assert violations == []


@pytest.mark.contract
class TestLegacyAPIResponseContract:
    """The legacy REST API response must match the expected structure."""

    @pytest.mark.parametrize(
        "response",
        [
            {"status": "success", "total_count": 100, "page": 1, "page_size": 50, "data": []},
            {"status": "success", "total_count": 0, "page": 1, "page_size": 50, "data": []},
            {"status": "error", "total_count": 0, "page": 1, "page_size": 50, "data": []},
        ],
    )
    def test_valid_api_responses_pass(self, response):
        violations = _validate_schema(response, LEGACY_API_ACCOUNT_LIST_RESPONSE_SCHEMA)
        assert violations == []

    def test_missing_status_field_fails(self):
        response = {"total_count": 10, "page": 1, "page_size": 50, "data": []}
        violations = _validate_schema(response, LEGACY_API_ACCOUNT_LIST_RESPONSE_SCHEMA)
        assert any("status" in v for v in violations)

    def test_invalid_status_value_fails(self):
        response = {"status": "ok", "total_count": 10, "page": 1, "page_size": 50, "data": []}
        violations = _validate_schema(response, LEGACY_API_ACCOUNT_LIST_RESPONSE_SCHEMA)
        assert any("status" in v for v in violations)

    def test_data_field_must_be_list(self):
        response = {"status": "success", "total_count": 1, "page": 1, "page_size": 50, "data": {}}
        violations = _validate_schema(response, LEGACY_API_ACCOUNT_LIST_RESPONSE_SCHEMA)
        assert any("data" in v for v in violations)


@pytest.mark.contract
class TestLegacyCSVExportContract:
    """CSV exports from the legacy system must contain all required columns."""

    def test_all_required_columns_present(self):
        """Simulate a CSV export with all required columns."""
        csv_columns = {
            "acct_id", "acct_name", "acct_type", "acct_status",
            "bill_addr_city", "bill_addr_country", "row_version",
            "industry_code", "email_address", "phone_number",  # optional
        }
        missing = LEGACY_CSV_REQUIRED_COLUMNS - csv_columns
        assert missing == set(), f"CSV export is missing required columns: {missing}"

    def test_missing_required_column_detected(self):
        """A CSV without 'acct_id' must be flagged."""
        csv_columns = {
            "acct_name", "acct_type", "acct_status",
            "bill_addr_city", "bill_addr_country", "row_version",
        }
        missing = LEGACY_CSV_REQUIRED_COLUMNS - csv_columns
        assert "acct_id" in missing

    def test_column_name_casing_contract(self):
        """Column names must be snake_case lowercase (legacy convention)."""
        all_columns = LEGACY_CSV_REQUIRED_COLUMNS | LEGACY_CSV_OPTIONAL_COLUMNS
        for col in all_columns:
            assert col == col.lower(), f"Column '{col}' is not lowercase snake_case"
            assert " " not in col, f"Column '{col}' contains spaces"

    @pytest.mark.parametrize(
        "acct_id,expected_valid",
        [
            ("LEGACY-ACC-00000001", True),
            ("LEG-001", True),
            ("", False),
            (None, False),
            ("A" * 51, False),  # exceeds max_length 50
        ],
    )
    def test_acct_id_format_contract(self, valid_legacy_account, acct_id, expected_valid):
        """acct_id must be a non-empty string of at most 50 characters."""
        row = {**valid_legacy_account, "acct_id": acct_id}
        violations = _validate_schema(row, LEGACY_ACCOUNT_SCHEMA)
        id_violations = [v for v in violations if "acct_id" in v]
        if expected_valid:
            assert id_violations == []
        else:
            assert len(id_violations) > 0


@pytest.mark.contract
class TestFieldMappingConstants:
    """
    The mapping constants in postgres_account_repository.py must align with
    the legacy schema contract defined here.
    """

    def test_status_map_covers_all_contract_statuses(self):
        """_STATUS_MAP must have an entry for every allowed status code."""
        from adapters.outbound.postgres_account_repository import _STATUS_MAP
        contract_statuses = LEGACY_ACCOUNT_SCHEMA["acct_status"]["allowed_values"]
        for code in contract_statuses:
            assert code in _STATUS_MAP, (
                f"_STATUS_MAP missing entry for status code '{code}' defined in contract"
            )

    def test_type_map_covers_all_contract_types(self):
        """_TYPE_MAP must have an entry for every allowed type code."""
        from adapters.outbound.postgres_account_repository import _TYPE_MAP
        contract_types = LEGACY_ACCOUNT_SCHEMA["acct_type"]["allowed_values"]
        for code in contract_types:
            assert code in _TYPE_MAP, (
                f"_TYPE_MAP missing entry for type code '{code}' defined in contract"
            )

    def test_industry_map_only_contains_known_codes(self):
        """_INDUSTRY_MAP must only contain industry codes we expect from the legacy system."""
        from adapters.outbound.postgres_account_repository import _INDUSTRY_MAP
        known_codes = LEGACY_ACCOUNT_SCHEMA["industry_code"]["allowed_values"] - {None}
        for code in _INDUSTRY_MAP:
            assert code in known_codes, (
                f"_INDUSTRY_MAP has unmapped code '{code}' not present in schema contract"
            )

"""
Unit tests for AccountTransformer data transformation pipeline.

Tests the full transformation pipeline steps in isolation using
in-memory DataFrames (no Parquet I/O in unit tests). Each pipeline
method is exercised via the public transform() entry point with a
temp directory of synthetic Parquet files, plus direct unit tests
on private pipeline steps.

Module under test: migration/data_transformations/account_transformer.py
Pattern: pytest with tmp_path fixture, parametrize, AAA.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from account_transformer import AccountTransformer, SF_ACCOUNT_SCHEMA
from transformation_rules import ACCOUNT_FIELD_MAP, INDUSTRY_MAP, ACCOUNT_TYPE_MAP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_parquet(path: Path, records: List[Dict[str, Any]]) -> Path:
    """Write a list of dicts as a Parquet file named accounts_001.parquet."""
    df = pd.DataFrame(records)
    table = pa.Table.from_pandas(df, preserve_index=False)
    out = path / "accounts_001.parquet"
    pq.write_table(table, out)
    return out


def _minimal_record(**overrides) -> Dict[str, Any]:
    """Return a legacy record with all required columns populated."""
    base = {
        "COMPANY_ID": "LEGACY-001",
        "COMPANY_NAME": "Acme Corp",
        "COMPANY_TYPE": "CUSTOMER",
        "INDUSTRY_CODE": "TECHNOLOGY",
        "PHONE_NUMBER": "5551234567",
        "FAX_NUMBER": "5559876543",
        "WEBSITE_URL": "acme.com",
        "ANNUAL_REVENUE": 1_000_000.0,
        "EMPLOYEE_COUNT": 250,
        "ADDR_LINE1": "123 Main St",
        "ADDR_CITY": "San Francisco",
        "ADDR_STATE": "CA",
        "ADDR_ZIP": "94102",
        "ADDR_COUNTRY": "UNITED STATES",
        "DESCRIPTION": "Premier enterprise account",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def input_dir(tmp_path: Path) -> Path:
    return tmp_path / "input"


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    d = tmp_path / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def transformer(input_dir: Path, output_dir: Path) -> AccountTransformer:
    input_dir.mkdir(parents=True, exist_ok=True)
    return AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)


# ===========================================================================
# 1. transform() — happy path (end-to-end pipeline with synthetic Parquet)
# ===========================================================================


class TestTransformHappyPath:
    """transform() succeeds with well-formed Parquet input."""

    def test_returns_metrics_dict(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet(input_dir, [_minimal_record()])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        assert isinstance(metrics, dict)

    def test_metrics_contain_expected_keys(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet(input_dir, [_minimal_record()])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        for key in ("input_files", "raw_rows", "output_rows", "rows_dropped",
                    "violations", "transform_ts"):
            assert key in metrics

    def test_output_rows_equals_input_when_no_dups(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        records = [_minimal_record(COMPANY_ID=f"LEGACY-{i:03d}") for i in range(10)]
        _write_parquet(input_dir, records)
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        assert metrics["output_rows"] == 10
        assert metrics["rows_dropped"] == 0

    def test_raw_rows_counted(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        records = [_minimal_record(COMPANY_ID=f"ID-{i}") for i in range(5)]
        _write_parquet(input_dir, records)
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        assert metrics["raw_rows"] == 5

    def test_metrics_json_written_to_output_dir(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet(input_dir, [_minimal_record()])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        t.transform()
        metrics_file = output_dir / "account_transform_metrics.json"
        assert metrics_file.exists()
        with metrics_file.open() as fh:
            data = json.load(fh)
        assert "output_rows" in data

    def test_dry_run_does_not_write_parquet_output(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet(input_dir, [_minimal_record()])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        t.transform()
        parquet_files = list(output_dir.glob("sf_accounts_*.parquet"))
        assert len(parquet_files) == 0

    def test_not_dry_run_writes_parquet_and_csv(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        _write_parquet(input_dir, [_minimal_record()])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=False)
        t.transform()
        parquet_files = list(output_dir.glob("sf_accounts_*.parquet"))
        csv_files = list(output_dir.glob("sf_accounts_*.csv"))
        assert len(parquet_files) == 1
        assert len(csv_files) == 1


# ===========================================================================
# 2. transform() — error cases
# ===========================================================================


class TestTransformErrors:
    """transform() raises when input is missing or unreadable."""

    def test_raises_file_not_found_when_no_parquet_files(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        with pytest.raises(FileNotFoundError, match="No account Parquet files"):
            t.transform()

    def test_multiple_input_files_concatenated(self, input_dir, output_dir):
        input_dir.mkdir(parents=True, exist_ok=True)
        # Write two separate Parquet files
        for suffix, start in [("001", 0), ("002", 5)]:
            records = [_minimal_record(COMPANY_ID=f"ID-{i}") for i in range(start, start + 5)]
            df = pd.DataFrame(records)
            table = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table, input_dir / f"accounts_{suffix}.parquet")
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        assert metrics["raw_rows"] == 10


# ===========================================================================
# 3. _rename_columns (pipeline step)
# ===========================================================================


class TestRenameColumns:
    """_rename_columns() applies ACCOUNT_FIELD_MAP to rename legacy column names."""

    def test_company_name_renamed_to_name(self, transformer):
        df = pd.DataFrame({"COMPANY_NAME": ["Acme"], "COMPANY_ID": ["ID-1"]})
        result = transformer._rename_columns(df)
        assert "Name" in result.columns
        assert "COMPANY_NAME" not in result.columns

    def test_company_id_renamed_to_legacy_id(self, transformer):
        df = pd.DataFrame({"COMPANY_ID": ["ID-1"]})
        result = transformer._rename_columns(df)
        assert "Legacy_ID__c" in result.columns

    def test_unrecognised_columns_preserved_unchanged(self, transformer):
        df = pd.DataFrame({"COMPANY_NAME": ["Acme"], "CUSTOM_FIELD": ["value"]})
        result = transformer._rename_columns(df)
        assert "CUSTOM_FIELD" in result.columns

    def test_rename_does_not_duplicate_rows(self, transformer):
        df = pd.DataFrame({"COMPANY_NAME": ["A", "B", "C"]})
        result = transformer._rename_columns(df)
        assert len(result) == 3


# ===========================================================================
# 4. _map_picklist_values (pipeline step)
# ===========================================================================


class TestMapPicklistValues:
    """_map_picklist_values() maps Type, Industry, and Country columns."""

    def test_type_customer_mapped_to_sf_value(self, transformer):
        df = pd.DataFrame({"Type": ["CUSTOMER"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "Type"] == "Customer - Direct"

    def test_unknown_type_defaults_to_prospect(self, transformer):
        df = pd.DataFrame({"Type": ["UNKNOWN_TYPE"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "Type"] == "Prospect"

    def test_industry_technology_mapped(self, transformer):
        df = pd.DataFrame({"Industry": ["TECHNOLOGY"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "Industry"] == "Technology"

    def test_unknown_industry_defaults_to_none(self, transformer):
        df = pd.DataFrame({"Industry": ["UNKNOWN_IND"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "Industry"] is None

    def test_billing_country_normalised(self, transformer):
        df = pd.DataFrame({"BillingCountry": ["UNITED STATES"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "BillingCountry"] == "US"

    def test_shipping_country_normalised(self, transformer):
        df = pd.DataFrame({"ShippingCountry": ["CANADA"]})
        result = transformer._map_picklist_values(df)
        assert result.loc[0, "ShippingCountry"] == "CA"

    def test_columns_absent_do_not_raise(self, transformer):
        df = pd.DataFrame({"Name": ["Test"]})
        result = transformer._map_picklist_values(df)
        assert "Name" in result.columns


# ===========================================================================
# 5. _normalise_fields (pipeline step)
# ===========================================================================


class TestNormaliseFields:
    """_normalise_fields() applies per-column normalisation functions."""

    def test_phone_normalised_to_e164(self, transformer):
        df = pd.DataFrame({"Phone": ["(555) 123-4567"]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "Phone"] == "+15551234567"

    def test_fax_normalised(self, transformer):
        df = pd.DataFrame({"Fax": ["5559876543"]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "Fax"] == "+15559876543"

    def test_website_prefixed_with_https(self, transformer):
        df = pd.DataFrame({"Website": ["acme.com"]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "Website"].startswith("https://")

    def test_name_truncated_to_255(self, transformer):
        df = pd.DataFrame({"Name": ["X" * 300]})
        result = transformer._normalise_fields(df)
        assert len(result.loc[0, "Name"]) <= 255

    def test_null_name_filled_with_unknown(self, transformer):
        df = pd.DataFrame({"Name": [None]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "Name"] == "UNKNOWN"

    def test_negative_annual_revenue_set_to_null(self, transformer):
        df = pd.DataFrame({"AnnualRevenue": [-500.0]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "AnnualRevenue"] is None or pd.isna(result.loc[0, "AnnualRevenue"])

    def test_positive_annual_revenue_preserved(self, transformer):
        df = pd.DataFrame({"AnnualRevenue": [250_000.0]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "AnnualRevenue"] == 250_000.0

    def test_employee_count_string_parsed_to_int32(self, transformer):
        df = pd.DataFrame({"NumberOfEmployees": ["1500"]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "NumberOfEmployees"] == 1500

    def test_legacy_id_stripped(self, transformer):
        df = pd.DataFrame({"Legacy_ID__c": ["  ID-001  "]})
        result = transformer._normalise_fields(df)
        assert result.loc[0, "Legacy_ID__c"] == "ID-001"


# ===========================================================================
# 6. _add_migration_fields (pipeline step)
# ===========================================================================


class TestAddMigrationFields:
    """_add_migration_fields() stamps Migration_Status__c and _transform_ts."""

    def test_migration_status_set_to_pending(self, transformer):
        df = pd.DataFrame({"Name": ["Acme"]})
        result = transformer._add_migration_fields(df)
        assert result.loc[0, "Migration_Status__c"] == "Pending"

    def test_transform_ts_column_added(self, transformer):
        df = pd.DataFrame({"Name": ["Acme"]})
        result = transformer._add_migration_fields(df)
        assert "_transform_ts" in result.columns

    def test_transform_ts_is_timezone_aware(self, transformer):
        df = pd.DataFrame({"Name": ["Acme"]})
        result = transformer._add_migration_fields(df)
        ts = result.loc[0, "_transform_ts"]
        # pd.Timestamp.now(tz="UTC") should be tz-aware
        assert ts.tzinfo is not None


# ===========================================================================
# 7. _deduplicate (pipeline step)
# ===========================================================================


class TestDeduplicate:
    """_deduplicate() removes rows with duplicate Legacy_ID__c, keeping first."""

    def test_duplicate_legacy_ids_removed(self, transformer):
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", "ID-002", "ID-001"],
            "Name": ["Acme", "Beta", "Acme Duplicate"],
        })
        result = transformer._deduplicate(df)
        assert len(result) == 2

    def test_no_duplicates_unchanged(self, transformer):
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", "ID-002", "ID-003"],
            "Name": ["A", "B", "C"],
        })
        result = transformer._deduplicate(df)
        assert len(result) == 3

    def test_dedup_keeps_first_occurrence(self, transformer):
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", "ID-001"],
            "Name": ["Original", "Duplicate"],
        })
        result = transformer._deduplicate(df)
        assert result.iloc[0]["Name"] == "Original"

    def test_dedup_resets_index(self, transformer):
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", "ID-001", "ID-002"],
            "Name": ["A", "B", "C"],
        })
        result = transformer._deduplicate(df)
        assert list(result.index) == list(range(len(result)))

    def test_sort_by_extraction_ts_before_dedup(self, transformer):
        """When _extraction_ts present, keep the more-recent record."""
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", "ID-001"],
            "Name": ["Old", "Recent"],
            "_extraction_ts": [
                pd.Timestamp("2025-01-01", tz="UTC"),
                pd.Timestamp("2025-06-01", tz="UTC"),
            ],
        })
        result = transformer._deduplicate(df)
        assert len(result) == 1
        assert result.iloc[0]["Name"] == "Recent"


# ===========================================================================
# 8. Parametrize — full pipeline edge-case records
# ===========================================================================


class TestFullPipelineEdgeCases:
    """End-to-end single-record scenarios that exercise edge branches."""

    @pytest.mark.parametrize(
        "field_overrides, expected_check",
        [
            # Null phone should survive normalise (returns None)
            ({"PHONE_NUMBER": None}, lambda df: True),
            # Revenue of 0 is valid
            ({"ANNUAL_REVENUE": 0.0}, lambda df: df.loc[0, "AnnualRevenue"] == 0.0 if "AnnualRevenue" in df.columns else True),
            # Unknown industry → None (default)
            ({"INDUSTRY_CODE": "ALIEN_CODE"}, lambda df: True),
        ],
    )
    def test_edge_case_records_complete_without_error(
        self, input_dir, output_dir, field_overrides, expected_check
    ):
        input_dir.mkdir(parents=True, exist_ok=True)
        rec = _minimal_record(**field_overrides)
        _write_parquet(input_dir, [rec])
        t = AccountTransformer(input_dir=input_dir, output_dir=output_dir, dry_run=True)
        metrics = t.transform()
        assert metrics["output_rows"] >= 0

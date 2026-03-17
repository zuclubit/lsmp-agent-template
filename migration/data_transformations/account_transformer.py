"""
account_transformer.py
─────────────────────────────────────────────────────────────────────────────
Transforms legacy Account/Company Parquet files into Salesforce-ready
Account records (CSV / Parquet).

Pipeline:
  1. Read raw Parquet files from extract stage
  2. Rename columns via ACCOUNT_FIELD_MAP
  3. Map picklist values (Type, Industry, Country)
  4. Normalise phones, URLs, strings
  5. Apply validation rules and collect metrics
  6. Deduplication by Legacy_ID__c
  7. Write transformed output as Parquet + CSV

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from transformation_rules import (
    ACCOUNT_FIELD_MAP,
    ACCOUNT_TYPE_MAP,
    ACCOUNT_VALIDATION_RULES,
    INDUSTRY_MAP,
    RulesEngine,
    map_picklist,
    normalise_country,
    normalise_phone,
    normalise_string,
    normalise_url,
)

logger = logging.getLogger(__name__)

SF_ACCOUNT_SCHEMA = pa.schema([
    pa.field("Legacy_ID__c",        pa.string(),  nullable=False),
    pa.field("Name",                pa.string(),  nullable=False),
    pa.field("AccountNumber",       pa.string(),  nullable=True),
    pa.field("Type",                pa.string(),  nullable=True),
    pa.field("Industry",            pa.string(),  nullable=True),
    pa.field("AnnualRevenue",       pa.float64(), nullable=True),
    pa.field("NumberOfEmployees",   pa.int32(),   nullable=True),
    pa.field("Phone",               pa.string(),  nullable=True),
    pa.field("Fax",                 pa.string(),  nullable=True),
    pa.field("Website",             pa.string(),  nullable=True),
    pa.field("Description",         pa.string(),  nullable=True),
    pa.field("BillingStreet",       pa.string(),  nullable=True),
    pa.field("BillingCity",         pa.string(),  nullable=True),
    pa.field("BillingState",        pa.string(),  nullable=True),
    pa.field("BillingPostalCode",   pa.string(),  nullable=True),
    pa.field("BillingCountry",      pa.string(),  nullable=True),
    pa.field("ShippingStreet",      pa.string(),  nullable=True),
    pa.field("ShippingCity",        pa.string(),  nullable=True),
    pa.field("ShippingState",       pa.string(),  nullable=True),
    pa.field("ShippingPostalCode",  pa.string(),  nullable=True),
    pa.field("ShippingCountry",     pa.string(),  nullable=True),
    pa.field("DunsNumber",          pa.string(),  nullable=True),
    pa.field("Sic",                 pa.string(),  nullable=True),
    pa.field("NaicsCode__c",        pa.string(),  nullable=True),
    pa.field("Segment__c",          pa.string(),  nullable=True),
    pa.field("Region__c",           pa.string(),  nullable=True),
    pa.field("Territory__c",        pa.string(),  nullable=True),
    pa.field("Credit_Rating__c",    pa.string(),  nullable=True),
    pa.field("Credit_Limit__c",     pa.float64(), nullable=True),
    pa.field("Source_System__c",    pa.string(),  nullable=True),
    pa.field("Migration_Status__c", pa.string(),  nullable=True),
    pa.field("_transform_ts",       pa.timestamp("us", tz="UTC"), nullable=False),
])


class AccountTransformer:
    """
    Reads raw Account Parquet files, applies field mapping, value mapping,
    normalisation, deduplication, and validation, then writes
    Salesforce-ready output files.
    """

    def __init__(
        self,
        input_dir:    Path,
        output_dir:   Path,
        country_code: str = "1",
        dry_run:      bool = False,
    ) -> None:
        self.input_dir    = Path(input_dir)
        self.output_dir   = Path(output_dir)
        self.country_code = country_code
        self.dry_run      = dry_run
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._rules_engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        self._metrics: Dict = {}

        logger.info("[AccountTransformer] input=%s output=%s dry_run=%s",
                    input_dir, output_dir, dry_run)

    # ─── Public API ───────────────────────────────────────────────────────────

    def transform(self) -> Dict:
        """Execute the full transformation pipeline. Returns metrics dict."""
        parquet_files = sorted(self.input_dir.glob("accounts_*.parquet"))
        if not parquet_files:
            raise FileNotFoundError(
                f"No account Parquet files found in {self.input_dir}")

        logger.info("[AccountTransformer] Found %d input files.", len(parquet_files))

        frames: List[pd.DataFrame] = []
        for f in parquet_files:
            try:
                df = pq.read_table(f).to_pandas()
                logger.debug("[AccountTransformer] Read %d rows from %s", len(df), f.name)
                frames.append(df)
            except Exception as exc:
                logger.error("[AccountTransformer] Failed to read %s: %s", f, exc)

        if not frames:
            raise ValueError("All input files failed to load.")

        raw_df = pd.concat(frames, ignore_index=True)
        logger.info("[AccountTransformer] Total raw rows loaded: %d", len(raw_df))

        # Pipeline steps
        df = self._rename_columns(raw_df)
        df = self._map_picklist_values(df)
        df = self._normalise_fields(df)
        df = self._add_migration_fields(df)
        df, rules_result = self._rules_engine.apply(df)
        df = self._deduplicate(df)

        self._metrics = {
            "input_files":      len(parquet_files),
            "raw_rows":         len(raw_df),
            "output_rows":      len(df),
            "rows_dropped":     len(raw_df) - len(df),
            "violations":       rules_result.total_violations,
            "violations_detail":rules_result.violations_by_rule,
            "transform_ts":     datetime.now(timezone.utc).isoformat(),
        }

        if not self.dry_run:
            self._write_outputs(df)

        self._write_metrics()
        logger.info("[AccountTransformer] Complete. %d → %d rows.", len(raw_df), len(df))
        return self._metrics

    # ─── Pipeline Steps ───────────────────────────────────────────────────────

    def _rename_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply ACCOUNT_FIELD_MAP to rename legacy columns to SF field names."""
        rename_map = {k: v for k, v in ACCOUNT_FIELD_MAP.items() if k in df.columns}
        df = df.rename(columns=rename_map)
        logger.debug("[AccountTransformer] Renamed %d columns.", len(rename_map))
        return df

    def _map_picklist_values(self, df: pd.DataFrame) -> pd.DataFrame:
        """Map legacy picklist codes to Salesforce picklist values."""
        if "Type" in df.columns:
            df["Type"] = df["Type"].apply(
                lambda v: map_picklist(v, ACCOUNT_TYPE_MAP, default="Prospect"))

        if "Industry" in df.columns:
            df["Industry"] = df["Industry"].apply(
                lambda v: map_picklist(v, INDUSTRY_MAP, default=None))

        if "BillingCountry" in df.columns:
            df["BillingCountry"] = df["BillingCountry"].apply(normalise_country)

        if "ShippingCountry" in df.columns:
            df["ShippingCountry"] = df["ShippingCountry"].apply(normalise_country)

        return df

    def _normalise_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply field-level normalisation functions."""
        # Phone fields
        for phone_col in ["Phone", "Fax"]:
            if phone_col in df.columns:
                df[phone_col] = df[phone_col].apply(
                    lambda v: normalise_phone(v, self.country_code))

        # URL
        if "Website" in df.columns:
            df["Website"] = df["Website"].apply(normalise_url)

        # Name truncation
        if "Name" in df.columns:
            df["Name"] = df["Name"].apply(lambda v: normalise_string(v, 255))
            df["Name"] = df["Name"].fillna("UNKNOWN")

        # Address fields
        for addr_col in ["BillingStreet", "BillingCity", "ShippingStreet", "ShippingCity"]:
            if addr_col in df.columns:
                df[addr_col] = df[addr_col].apply(lambda v: normalise_string(v, 255))

        for short_col in ["BillingState", "BillingPostalCode",
                           "ShippingState", "ShippingPostalCode"]:
            if short_col in df.columns:
                df[short_col] = df[short_col].apply(lambda v: normalise_string(v, 80))

        # Description (long text)
        if "Description" in df.columns:
            df["Description"] = df["Description"].apply(
                lambda v: normalise_string(v, 32000))

        # Revenue / credit must be float
        for num_col in ["AnnualRevenue", "Credit_Limit__c"]:
            if num_col in df.columns:
                df[num_col] = pd.to_numeric(df[num_col], errors="coerce")
                df[num_col] = df[num_col].where(df[num_col] >= 0, None)

        # Employees
        if "NumberOfEmployees" in df.columns:
            df["NumberOfEmployees"] = pd.to_numeric(
                df["NumberOfEmployees"], errors="coerce").astype("Int32")

        # Legacy_ID__c must be string
        if "Legacy_ID__c" in df.columns:
            df["Legacy_ID__c"] = df["Legacy_ID__c"].astype(str).str.strip()

        return df

    def _add_migration_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        df["Migration_Status__c"] = "Pending"
        df["_transform_ts"]       = pd.Timestamp.now(tz="UTC")
        return df

    def _deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Remove duplicate Legacy_ID__c rows, keeping the most recent."""
        pre = len(df)
        if "_extraction_ts" in df.columns:
            df = df.sort_values("_extraction_ts", ascending=False)
        df = df.drop_duplicates(subset=["Legacy_ID__c"], keep="first")
        dupes = pre - len(df)
        if dupes > 0:
            logger.warning("[AccountTransformer] Dropped %d duplicate Legacy_ID__c rows.", dupes)
        return df.reset_index(drop=True)

    def _write_outputs(self, df: pd.DataFrame) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Parquet
        parquet_path = self.output_dir / f"sf_accounts_{ts}.parquet"
        cols = [f.name for f in SF_ACCOUNT_SCHEMA if f.name in df.columns]
        table = pa.Table.from_pandas(df[cols], preserve_index=False)
        pq.write_table(table, parquet_path, compression="snappy")
        logger.info("[AccountTransformer] Parquet written: %s (%d rows)", parquet_path, len(df))

        # CSV (for Salesforce Data Loader compatibility)
        csv_path = self.output_dir / f"sf_accounts_{ts}.csv"
        df[cols].to_csv(csv_path, index=False, encoding="utf-8")
        logger.info("[AccountTransformer] CSV written: %s", csv_path)

        self._metrics["parquet_output"] = str(parquet_path)
        self._metrics["csv_output"]     = str(csv_path)

    def _write_metrics(self) -> None:
        metrics_path = self.output_dir / "account_transform_metrics.json"
        with metrics_path.open("w") as fh:
            json.dump(self._metrics, fh, indent=2)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="Transform legacy Account Parquet files.")
    parser.add_argument("--input-dir",  required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--log-level",  default="INFO")
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)

    transformer = AccountTransformer(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    )
    metrics = transformer.transform()
    logger.info("Transform metrics: %s", json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

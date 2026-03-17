"""
contact_transformer.py
─────────────────────────────────────────────────────────────────────────────
Transforms legacy Contact/Person Parquet files into Salesforce-ready
Contact records.

Pipeline:
  1. Read Parquet files from extraction stage
  2. Rename legacy columns to SF field names via CONTACT_FIELD_MAP
  3. Map salutation, lead source picklists
  4. Normalise phones, emails, country codes, strings
  5. Resolve AccountId foreign key via Legacy_Account_ID__c mapping table
  6. Apply validation rules
  7. Deduplication by Legacy_ID__c
  8. Write Parquet + CSV output

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from transformation_rules import (
    CONTACT_FIELD_MAP,
    CONTACT_VALIDATION_RULES,
    LEAD_SOURCE_MAP,
    SALUTATION_MAP,
    RulesEngine,
    map_picklist,
    normalise_country,
    normalise_email,
    normalise_phone,
    normalise_string,
)

logger = logging.getLogger(__name__)

SF_CONTACT_SCHEMA = pa.schema([
    pa.field("Legacy_ID__c",           pa.string(),  nullable=False),
    pa.field("Legacy_Account_ID__c",   pa.string(),  nullable=True),
    pa.field("AccountId",              pa.string(),  nullable=True),
    pa.field("FirstName",              pa.string(),  nullable=True),
    pa.field("LastName",               pa.string(),  nullable=False),
    pa.field("MiddleName",             pa.string(),  nullable=True),
    pa.field("Salutation",             pa.string(),  nullable=True),
    pa.field("Suffix",                 pa.string(),  nullable=True),
    pa.field("Title",                  pa.string(),  nullable=True),
    pa.field("Department",             pa.string(),  nullable=True),
    pa.field("Phone",                  pa.string(),  nullable=True),
    pa.field("MobilePhone",            pa.string(),  nullable=True),
    pa.field("HomePhone",              pa.string(),  nullable=True),
    pa.field("Email",                  pa.string(),  nullable=True),
    pa.field("AssistantName",          pa.string(),  nullable=True),
    pa.field("AssistantPhone",         pa.string(),  nullable=True),
    pa.field("MailingStreet",          pa.string(),  nullable=True),
    pa.field("MailingCity",            pa.string(),  nullable=True),
    pa.field("MailingState",           pa.string(),  nullable=True),
    pa.field("MailingPostalCode",      pa.string(),  nullable=True),
    pa.field("MailingCountry",         pa.string(),  nullable=True),
    pa.field("Birthdate",              pa.date32(),  nullable=True),
    pa.field("Description",            pa.string(),  nullable=True),
    pa.field("DoNotCall",              pa.bool_(),   nullable=True),
    pa.field("HasOptedOutOfEmail",     pa.bool_(),   nullable=True),
    pa.field("HasOptedOutOfFax",       pa.bool_(),   nullable=True),
    pa.field("LeadSource",             pa.string(),  nullable=True),
    pa.field("LinkedIn_URL__c",        pa.string(),  nullable=True),
    pa.field("Twitter_Handle__c",      pa.string(),  nullable=True),
    pa.field("Source_System__c",       pa.string(),  nullable=True),
    pa.field("Migration_Status__c",    pa.string(),  nullable=True),
    pa.field("_transform_ts",          pa.timestamp("us", tz="UTC"), nullable=False),
])


class ContactTransformer:
    """
    Reads raw Contact Parquet files, applies mapping/normalisation/validation,
    resolves AccountId foreign keys, then writes Salesforce-ready outputs.
    """

    def __init__(
        self,
        input_dir:           Path,
        output_dir:          Path,
        account_mapping_csv: Optional[Path] = None,
        country_code:        str = "1",
        dry_run:             bool = False,
    ) -> None:
        self.input_dir           = Path(input_dir)
        self.output_dir          = Path(output_dir)
        self.account_mapping_csv = Path(account_mapping_csv) if account_mapping_csv else None
        self.country_code        = country_code
        self.dry_run             = dry_run
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._rules_engine = RulesEngine(CONTACT_VALIDATION_RULES)
        self._account_map: Dict[str, str] = {}
        self._metrics: Dict = {}

        if self.account_mapping_csv and self.account_mapping_csv.exists():
            self._account_map = self._load_account_map(self.account_mapping_csv)
            logger.info("[ContactTransformer] Loaded %d account ID mappings.",
                        len(self._account_map))

    # ─── Public API ───────────────────────────────────────────────────────────

    def transform(self) -> Dict:
        parquet_files = sorted(self.input_dir.glob("contacts_*.parquet"))
        if not parquet_files:
            raise FileNotFoundError(
                f"No contact Parquet files found in {self.input_dir}")

        logger.info("[ContactTransformer] Found %d input files.", len(parquet_files))
        frames: List[pd.DataFrame] = []
        for f in parquet_files:
            try:
                df = pq.read_table(f).to_pandas()
                frames.append(df)
            except Exception as exc:
                logger.error("[ContactTransformer] Failed to read %s: %s", f, exc)

        if not frames:
            raise ValueError("All input files failed to load.")

        raw_df = pd.concat(frames, ignore_index=True)
        logger.info("[ContactTransformer] Total raw rows: %d", len(raw_df))

        df = self._rename_columns(raw_df)
        df = self._map_picklist_values(df)
        df = self._normalise_fields(df)
        df = self._resolve_account_ids(df)
        df = self._add_migration_fields(df)
        df, rules_result = self._rules_engine.apply(df)
        df = self._deduplicate(df)

        self._metrics = {
            "input_files":          len(parquet_files),
            "raw_rows":             len(raw_df),
            "output_rows":          len(df),
            "rows_dropped":         len(raw_df) - len(df),
            "violations":           rules_result.total_violations,
            "violations_detail":    rules_result.violations_by_rule,
            "account_ids_resolved": int((df["AccountId"].notna()).sum()),
            "account_ids_missing":  int((df["AccountId"].isna()).sum()),
            "transform_ts":         datetime.now(timezone.utc).isoformat(),
        }

        if not self.dry_run:
            self._write_outputs(df)

        self._write_metrics()
        logger.info("[ContactTransformer] Complete. %d → %d rows.", len(raw_df), len(df))
        return self._metrics

    # ─── Pipeline Steps ───────────────────────────────────────────────────────

    def _rename_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        rename_map = {k: v for k, v in CONTACT_FIELD_MAP.items() if k in df.columns}
        return df.rename(columns=rename_map)

    def _map_picklist_values(self, df: pd.DataFrame) -> pd.DataFrame:
        if "Salutation" in df.columns:
            df["Salutation"] = df["Salutation"].apply(
                lambda v: map_picklist(v, SALUTATION_MAP, default=None))

        if "LeadSource" in df.columns:
            df["LeadSource"] = df["LeadSource"].apply(
                lambda v: map_picklist(v, LEAD_SOURCE_MAP, default="Other"))

        if "MailingCountry" in df.columns:
            df["MailingCountry"] = df["MailingCountry"].apply(normalise_country)

        return df

    def _normalise_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        # Email
        if "Email" in df.columns:
            df["Email"] = df["Email"].apply(normalise_email)

        # Phones
        for phone_col in ["Phone", "MobilePhone", "HomePhone", "AssistantPhone"]:
            if phone_col in df.columns:
                df[phone_col] = df[phone_col].apply(
                    lambda v: normalise_phone(v, self.country_code))

        # Names
        for name_col, max_len in [("FirstName", 40), ("LastName", 80),
                                   ("MiddleName", 40), ("Suffix", 40)]:
            if name_col in df.columns:
                df[name_col] = df[name_col].apply(
                    lambda v: normalise_string(v, max_len))

        # Title / Department
        if "Title" in df.columns:
            df["Title"] = df["Title"].apply(lambda v: normalise_string(v, 128))
        if "Department" in df.columns:
            df["Department"] = df["Department"].apply(lambda v: normalise_string(v, 80))

        # Address
        for addr_col in ["MailingStreet", "MailingCity"]:
            if addr_col in df.columns:
                df[addr_col] = df[addr_col].apply(lambda v: normalise_string(v, 255))
        for short_addr in ["MailingState", "MailingPostalCode"]:
            if short_addr in df.columns:
                df[short_addr] = df[short_addr].apply(lambda v: normalise_string(v, 80))

        # Social URLs
        if "LinkedIn_URL__c" in df.columns:
            df["LinkedIn_URL__c"] = df["LinkedIn_URL__c"].apply(
                lambda v: normalise_string(v, 255))
        if "Twitter_Handle__c" in df.columns:
            df["Twitter_Handle__c"] = df["Twitter_Handle__c"].apply(
                lambda v: normalise_string(v, 80))

        # Booleans — convert int/string to Python bool
        for bool_col in ["DoNotCall", "HasOptedOutOfEmail", "HasOptedOutOfFax"]:
            if bool_col in df.columns:
                df[bool_col] = df[bool_col].map(
                    {1: True, 0: False, "1": True, "0": False,
                     True: True, False: False, "Y": True, "N": False}
                ).fillna(False).astype(bool)

        # Birthdate — ensure date type
        if "Birthdate" in df.columns:
            df["Birthdate"] = pd.to_datetime(df["Birthdate"], errors="coerce").dt.date

        # Legacy ID
        if "Legacy_ID__c" in df.columns:
            df["Legacy_ID__c"] = df["Legacy_ID__c"].astype(str).str.strip()

        if "Legacy_Account_ID__c" in df.columns:
            df["Legacy_Account_ID__c"] = df["Legacy_Account_ID__c"].astype(str).str.strip()
            df["Legacy_Account_ID__c"] = df["Legacy_Account_ID__c"].replace(
                {"nan": None, "None": None, "": None})

        return df

    def _resolve_account_ids(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Replace Legacy_Account_ID__c with the real Salesforce AccountId
        from the account mapping table. Contacts without a mapping will have
        AccountId = None (they will be created without an account link).
        """
        if not self._account_map or "Legacy_Account_ID__c" not in df.columns:
            df["AccountId"] = None
            return df

        df["AccountId"] = df["Legacy_Account_ID__c"].map(self._account_map)
        unresolved = df["AccountId"].isna().sum()
        if unresolved > 0:
            logger.warning(
                "[ContactTransformer] %d contacts could not be linked to an Account.",
                unresolved,
            )
        return df

    def _add_migration_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        df["Migration_Status__c"] = "Pending"
        df["_transform_ts"]       = pd.Timestamp.now(tz="UTC")
        return df

    def _deduplicate(self, df: pd.DataFrame) -> pd.DataFrame:
        pre = len(df)
        if "_extraction_ts" in df.columns:
            df = df.sort_values("_extraction_ts", ascending=False)
        df = df.drop_duplicates(subset=["Legacy_ID__c"], keep="first")
        dupes = pre - len(df)
        if dupes:
            logger.warning("[ContactTransformer] Dropped %d duplicate Legacy_ID__c rows.", dupes)
        return df.reset_index(drop=True)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_account_map(csv_path: Path) -> Dict[str, str]:
        """
        Load a mapping CSV with columns: legacy_id, salesforce_id
        Returns dict: legacy_id -> salesforce_account_id
        """
        mapping_df = pd.read_csv(csv_path, dtype=str)
        mapping_df.columns = mapping_df.columns.str.strip().str.lower()
        if "legacy_id" not in mapping_df.columns or "salesforce_id" not in mapping_df.columns:
            raise ValueError(
                "Account mapping CSV must have 'legacy_id' and 'salesforce_id' columns.")
        mapping_df = mapping_df.dropna(subset=["legacy_id", "salesforce_id"])
        return dict(zip(mapping_df["legacy_id"], mapping_df["salesforce_id"]))

    def _write_outputs(self, df: pd.DataFrame) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        cols = [f.name for f in SF_CONTACT_SCHEMA if f.name in df.columns]

        parquet_path = self.output_dir / f"sf_contacts_{ts}.parquet"
        table = pa.Table.from_pandas(df[cols], preserve_index=False)
        pq.write_table(table, parquet_path, compression="snappy")
        logger.info("[ContactTransformer] Parquet written: %s (%d rows)", parquet_path, len(df))

        csv_path = self.output_dir / f"sf_contacts_{ts}.csv"
        df[cols].to_csv(csv_path, index=False, encoding="utf-8")
        logger.info("[ContactTransformer] CSV written: %s", csv_path)

        self._metrics["parquet_output"] = str(parquet_path)
        self._metrics["csv_output"]     = str(csv_path)

    def _write_metrics(self) -> None:
        metrics_path = self.output_dir / "contact_transform_metrics.json"
        with metrics_path.open("w") as fh:
            json.dump(self._metrics, fh, indent=2)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="Transform legacy Contact Parquet files.")
    parser.add_argument("--input-dir",           required=True)
    parser.add_argument("--output-dir",          required=True)
    parser.add_argument("--account-mapping-csv", default=None)
    parser.add_argument("--dry-run",             action="store_true")
    parser.add_argument("--log-level",           default="INFO")
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)

    transformer = ContactTransformer(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        account_mapping_csv=Path(args.account_mapping_csv) if args.account_mapping_csv else None,
        dry_run=args.dry_run,
    )
    metrics = transformer.transform()
    logger.info("Transform metrics: %s", json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

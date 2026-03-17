"""
account_extractor.py
─────────────────────────────────────────────────────────────────────────────
Extracts Account/Company records from the legacy SQL database.

Features:
  - Full pagination with configurable page size
  - Checkpoint support (resume on failure)
  - Incremental extraction via last_modified_date filter
  - Parquet output with strict PyArrow schema validation
  - Post-processing: normalise nulls, cast numeric types, sanitise strings
  - Extraction metrics written to JSON alongside Parquet files

Usage:
    python account_extractor.py \
        --db-url "mssql+pyodbc://user:pass@server/LegacyDB?driver=ODBC+Driver+18+for+SQL+Server" \
        --output-dir /data/migration/raw/accounts \
        --page-size 5000 \
        --incremental 2026-01-01

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa

# Local
from base_extractor import BaseExtractor, ExtractionMetrics

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AccountExtractor")


# ─── Constants ────────────────────────────────────────────────────────────────

LEGACY_ACCOUNT_TABLE = "dbo.COMPANY_MASTER"


class AccountExtractor(BaseExtractor):
    """
    Extracts company/account records from the legacy MSSQL database.

    Legacy table: dbo.COMPANY_MASTER
    Maps to Salesforce: Account
    """

    EXTRACTOR_NAME = "AccountExtractor"
    OBJECT_NAME    = "accounts"

    def __init__(
        self,
        db_url:             str,
        output_dir:         Path,
        page_size:          int = 5000,
        incremental_since:  Optional[date] = None,
        active_only:        bool = True,
        checkpoint_dir:     Optional[Path] = None,
        max_retries:        int = 3,
    ) -> None:
        super().__init__(
            db_url=db_url,
            output_dir=output_dir,
            page_size=page_size,
            checkpoint_dir=checkpoint_dir,
            max_retries=max_retries,
        )
        self.incremental_since = incremental_since
        self.active_only       = active_only

        logger.info(
            "[AccountExtractor] Config: page_size=%d active_only=%s incremental_since=%s",
            page_size, active_only, incremental_since,
        )

    # ─── SQL builders ─────────────────────────────────────────────────────────

    def _build_where_clause(self) -> str:
        conditions: list[str] = []
        if self.active_only:
            conditions.append("STATUS_CODE NOT IN ('DELETED', 'INACTIVE', 'MERGED')")
        if self.incremental_since:
            ts = self.incremental_since.strftime("%Y-%m-%d")
            conditions.append(f"MODIFIED_DATE >= '{ts}'")
        return ("WHERE " + " AND ".join(conditions)) if conditions else ""

    def _build_count_query(self) -> str:
        where = self._build_where_clause()
        return f"SELECT COUNT(*) FROM {LEGACY_ACCOUNT_TABLE} {where}"

    def _build_page_query(self, offset: int, limit: int) -> str:
        where = self._build_where_clause()
        return f"""
            SELECT
                COMPANY_ID,
                COMPANY_NAME,
                COMPANY_CODE,
                STATUS_CODE,
                COMPANY_TYPE,
                INDUSTRY_CODE,
                SIC_CODE,
                NAICS_CODE,
                ANNUAL_REVENUE,
                EMPLOYEE_COUNT,
                PHONE_NUMBER,
                FAX_NUMBER,
                WEBSITE_URL,
                EMAIL_ADDRESS,
                ADDR_LINE1,
                ADDR_LINE2,
                ADDR_CITY,
                ADDR_STATE,
                ADDR_ZIP,
                ADDR_COUNTRY,
                BILLING_ADDR_LINE1,
                BILLING_ADDR_CITY,
                BILLING_ADDR_STATE,
                BILLING_ADDR_ZIP,
                BILLING_ADDR_COUNTRY,
                PARENT_COMPANY_ID,
                OWNER_USER_ID,
                DESCRIPTION,
                CREATED_DATE,
                MODIFIED_DATE,
                CREATED_BY,
                MODIFIED_BY,
                TAX_ID,
                DUNS_NUMBER,
                CREDIT_LIMIT,
                CREDIT_RATING,
                ACCOUNT_MANAGER_ID,
                SEGMENT_CODE,
                REGION_CODE,
                TERRITORY_CODE,
                IS_PARTNER,
                IS_COMPETITOR,
                IS_CUSTOMER,
                SOURCE_SYSTEM
            FROM {LEGACY_ACCOUNT_TABLE}
            {where}
            ORDER BY COMPANY_ID ASC
            OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY
        """

    # ─── Schema ───────────────────────────────────────────────────────────────

    def _get_expected_schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("COMPANY_ID",           pa.int64(),      nullable=False),
            pa.field("COMPANY_NAME",          pa.string(),     nullable=False),
            pa.field("COMPANY_CODE",          pa.string(),     nullable=True),
            pa.field("STATUS_CODE",           pa.string(),     nullable=True),
            pa.field("COMPANY_TYPE",          pa.string(),     nullable=True),
            pa.field("INDUSTRY_CODE",         pa.string(),     nullable=True),
            pa.field("SIC_CODE",              pa.string(),     nullable=True),
            pa.field("NAICS_CODE",            pa.string(),     nullable=True),
            pa.field("ANNUAL_REVENUE",        pa.float64(),    nullable=True),
            pa.field("EMPLOYEE_COUNT",        pa.int32(),      nullable=True),
            pa.field("PHONE_NUMBER",          pa.string(),     nullable=True),
            pa.field("FAX_NUMBER",            pa.string(),     nullable=True),
            pa.field("WEBSITE_URL",           pa.string(),     nullable=True),
            pa.field("EMAIL_ADDRESS",         pa.string(),     nullable=True),
            pa.field("ADDR_LINE1",            pa.string(),     nullable=True),
            pa.field("ADDR_LINE2",            pa.string(),     nullable=True),
            pa.field("ADDR_CITY",             pa.string(),     nullable=True),
            pa.field("ADDR_STATE",            pa.string(),     nullable=True),
            pa.field("ADDR_ZIP",              pa.string(),     nullable=True),
            pa.field("ADDR_COUNTRY",          pa.string(),     nullable=True),
            pa.field("BILLING_ADDR_LINE1",    pa.string(),     nullable=True),
            pa.field("BILLING_ADDR_CITY",     pa.string(),     nullable=True),
            pa.field("BILLING_ADDR_STATE",    pa.string(),     nullable=True),
            pa.field("BILLING_ADDR_ZIP",      pa.string(),     nullable=True),
            pa.field("BILLING_ADDR_COUNTRY",  pa.string(),     nullable=True),
            pa.field("PARENT_COMPANY_ID",     pa.int64(),      nullable=True),
            pa.field("OWNER_USER_ID",         pa.int64(),      nullable=True),
            pa.field("DESCRIPTION",           pa.string(),     nullable=True),
            pa.field("CREATED_DATE",          pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("MODIFIED_DATE",         pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("CREATED_BY",            pa.string(),     nullable=True),
            pa.field("MODIFIED_BY",           pa.string(),     nullable=True),
            pa.field("TAX_ID",                pa.string(),     nullable=True),
            pa.field("DUNS_NUMBER",           pa.string(),     nullable=True),
            pa.field("CREDIT_LIMIT",          pa.float64(),    nullable=True),
            pa.field("CREDIT_RATING",         pa.string(),     nullable=True),
            pa.field("ACCOUNT_MANAGER_ID",    pa.int64(),      nullable=True),
            pa.field("SEGMENT_CODE",          pa.string(),     nullable=True),
            pa.field("REGION_CODE",           pa.string(),     nullable=True),
            pa.field("TERRITORY_CODE",        pa.string(),     nullable=True),
            pa.field("IS_PARTNER",            pa.bool_(),      nullable=True),
            pa.field("IS_COMPETITOR",         pa.bool_(),      nullable=True),
            pa.field("IS_CUSTOMER",           pa.bool_(),      nullable=True),
            pa.field("SOURCE_SYSTEM",         pa.string(),     nullable=True),
            # Extraction metadata columns (added in _post_process_df)
            pa.field("_extraction_ts",        pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("_extractor_version",    pa.string(),     nullable=False),
        ])

    # ─── Post-processing ──────────────────────────────────────────────────────

    def _post_process_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Cleans and standardises the raw DataFrame:
          - Trim string whitespace
          - Normalise boolean integer columns
          - Cast numeric columns
          - Handle NULL equivalents
          - Add extraction metadata
        """
        df = df.copy()

        # ── String cleanup ─────────────────────────────────────────────────
        str_cols = [
            "COMPANY_NAME", "COMPANY_CODE", "STATUS_CODE", "COMPANY_TYPE",
            "INDUSTRY_CODE", "PHONE_NUMBER", "FAX_NUMBER", "WEBSITE_URL",
            "EMAIL_ADDRESS", "ADDR_LINE1", "ADDR_LINE2", "ADDR_CITY",
            "ADDR_STATE", "ADDR_ZIP", "ADDR_COUNTRY", "BILLING_ADDR_LINE1",
            "BILLING_ADDR_CITY", "BILLING_ADDR_STATE", "BILLING_ADDR_ZIP",
            "BILLING_ADDR_COUNTRY", "DESCRIPTION", "CREDIT_RATING",
            "SEGMENT_CODE", "REGION_CODE", "TERRITORY_CODE",
            "TAX_ID", "DUNS_NUMBER", "SOURCE_SYSTEM",
        ]
        for col in str_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip()
                df[col] = df[col].replace({"nan": None, "None": None, "": None, "NULL": None})

        # ── Null sentinel cleanup ──────────────────────────────────────────
        df["COMPANY_NAME"] = df["COMPANY_NAME"].fillna("UNKNOWN")
        df["STATUS_CODE"]  = df["STATUS_CODE"].fillna("UNKNOWN")

        # ── Boolean normalisation ─────────────────────────────────────────
        for bool_col in ["IS_PARTNER", "IS_COMPETITOR", "IS_CUSTOMER"]:
            if bool_col in df.columns:
                df[bool_col] = df[bool_col].map(
                    {1: True, 0: False, "1": True, "0": False,
                     "Y": True, "N": False, "YES": True, "NO": False}
                ).astype("boolean")

        # ── Numeric casting ───────────────────────────────────────────────
        df["ANNUAL_REVENUE"] = pd.to_numeric(df.get("ANNUAL_REVENUE"), errors="coerce")
        df["CREDIT_LIMIT"]   = pd.to_numeric(df.get("CREDIT_LIMIT"),   errors="coerce")
        df["EMPLOYEE_COUNT"] = pd.to_numeric(df.get("EMPLOYEE_COUNT"), errors="coerce").astype("Int32")

        # ── Timestamp normalisation ───────────────────────────────────────
        for ts_col in ["CREATED_DATE", "MODIFIED_DATE"]:
            if ts_col in df.columns:
                df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)

        # ── Drop duplicate rows (by COMPANY_ID) ──────────────────────────
        pre_len = len(df)
        df = df.drop_duplicates(subset=["COMPANY_ID"], keep="last")
        dupes = pre_len - len(df)
        if dupes > 0:
            logger.warning("[AccountExtractor] Dropped %d duplicate COMPANY_ID rows.", dupes)
            self.metrics.skipped_rows += dupes

        # ── Extraction metadata ───────────────────────────────────────────
        now = pd.Timestamp.now(tz="UTC")
        df["_extraction_ts"]      = now
        df["_extractor_version"]  = "1.0.0"

        return df


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Account/Company records from legacy SQL database."
    )
    parser.add_argument("--db-url",       required=True,
                        help="SQLAlchemy database URL")
    parser.add_argument("--output-dir",   required=True,
                        help="Directory to write Parquet files")
    parser.add_argument("--page-size",    type=int, default=5000,
                        help="Records per page (default: 5000)")
    parser.add_argument("--incremental",  type=date.fromisoformat, default=None,
                        metavar="YYYY-MM-DD",
                        help="Only extract records modified since this date")
    parser.add_argument("--all-statuses", action="store_true",
                        help="Include inactive/deleted accounts")
    parser.add_argument("--no-resume",    action="store_true",
                        help="Ignore checkpoint and start fresh")
    parser.add_argument("--log-level",    default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.getLogger().setLevel(args.log_level)

    extractor = AccountExtractor(
        db_url=args.db_url,
        output_dir=Path(args.output_dir),
        page_size=args.page_size,
        incremental_since=args.incremental,
        active_only=not args.all_statuses,
    )

    with extractor:
        metrics = extractor.extract(resume=not args.no_resume)

    logger.info("─── Extraction Summary ───────────────────────────────")
    for key, val in metrics.to_dict().items():
        logger.info("  %-25s %s", key + ":", val)

    if metrics.pages_failed > 0:
        logger.error("[AccountExtractor] %d page(s) failed. Check metrics.json for details.",
                     metrics.pages_failed)
        sys.exit(1)


if __name__ == "__main__":
    main()

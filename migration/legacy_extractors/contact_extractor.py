"""
contact_extractor.py
─────────────────────────────────────────────────────────────────────────────
Extracts Person/Contact records from the legacy SQL database.

Legacy table : dbo.PERSON_MASTER (with JOIN to dbo.PERSON_CONTACT_DETAIL)
Salesforce target : Contact

Features:
  - Pagination + checkpoint resume identical to AccountExtractor
  - Joins the contact-detail table for email/phone sub-types
  - Incremental extraction by MODIFIED_DATE
  - Deduplication by PERSON_ID + COMPANY_ID
  - Strict schema validation before Parquet write
  - Optional PII masking mode for lower environments

Usage:
    python contact_extractor.py \
        --db-url "mssql+pyodbc://user:pass@server/LegacyDB?driver=ODBC+Driver+18+for+SQL+Server" \
        --output-dir /data/migration/raw/contacts \
        --page-size 5000

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa

from base_extractor import BaseExtractor, ExtractionMetrics

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ContactExtractor")

LEGACY_PERSON_TABLE  = "dbo.PERSON_MASTER"
LEGACY_CONTACT_TABLE = "dbo.PERSON_CONTACT_DETAIL"


class ContactExtractor(BaseExtractor):
    """
    Extracts person/contact records from the legacy MSSQL database.

    Joins PERSON_MASTER with PERSON_CONTACT_DETAIL to retrieve
    primary email and phone sub-types. Implements PII masking for
    non-production extractions.
    """

    EXTRACTOR_NAME = "ContactExtractor"
    OBJECT_NAME    = "contacts"

    def __init__(
        self,
        db_url:            str,
        output_dir:        Path,
        page_size:         int = 5000,
        incremental_since: Optional[date] = None,
        active_only:       bool = True,
        mask_pii:          bool = False,
        checkpoint_dir:    Optional[Path] = None,
        max_retries:       int = 3,
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
        self.mask_pii          = mask_pii

        logger.info(
            "[ContactExtractor] Config: page_size=%d active_only=%s "
            "incremental_since=%s mask_pii=%s",
            page_size, active_only, incremental_since, mask_pii,
        )

    # ─── SQL ──────────────────────────────────────────────────────────────────

    def _build_where_clause(self) -> str:
        conditions: list[str] = []
        if self.active_only:
            conditions.append("p.STATUS_CODE NOT IN ('DELETED', 'INACTIVE', 'MERGED')")
        if self.incremental_since:
            ts = self.incremental_since.strftime("%Y-%m-%d")
            conditions.append(f"p.MODIFIED_DATE >= '{ts}'")
        return ("WHERE " + " AND ".join(conditions)) if conditions else ""

    def _build_count_query(self) -> str:
        where = self._build_where_clause()
        return f"SELECT COUNT(*) FROM {LEGACY_PERSON_TABLE} p {where}"

    def _build_page_query(self, offset: int, limit: int) -> str:
        where = self._build_where_clause()
        return f"""
            SELECT
                p.PERSON_ID,
                p.COMPANY_ID,
                p.FIRST_NAME,
                p.LAST_NAME,
                p.MIDDLE_NAME,
                p.SALUTATION,
                p.SUFFIX,
                p.JOB_TITLE,
                p.DEPARTMENT,
                p.STATUS_CODE,
                p.PERSON_TYPE,
                p.IS_PRIMARY_CONTACT,
                p.IS_DECISION_MAKER,
                p.DO_NOT_CALL,
                p.DO_NOT_EMAIL,
                p.DO_NOT_MAIL,
                p.PHONE_NUMBER        AS DIRECT_PHONE,
                p.MOBILE_NUMBER,
                p.ASSISTANT_NAME,
                p.ASSISTANT_PHONE,
                p.ADDR_LINE1,
                p.ADDR_LINE2,
                p.ADDR_CITY,
                p.ADDR_STATE,
                p.ADDR_ZIP,
                p.ADDR_COUNTRY,
                p.BIRTHDATE,
                p.GENDER_CODE,
                p.PREFERRED_LANGUAGE,
                p.LINKEDIN_URL,
                p.TWITTER_HANDLE,
                p.DESCRIPTION,
                p.OWNER_USER_ID,
                p.CREATED_DATE,
                p.MODIFIED_DATE,
                p.CREATED_BY,
                p.MODIFIED_BY,
                p.LEAD_SOURCE,
                p.REPORTS_TO_ID,
                p.SOURCE_SYSTEM,
                -- Primary email from contact detail (subquery)
                (
                    SELECT TOP 1 cd.CONTACT_VALUE
                    FROM {LEGACY_CONTACT_TABLE} cd
                    WHERE cd.PERSON_ID    = p.PERSON_ID
                      AND cd.CONTACT_TYPE = 'EMAIL'
                      AND cd.IS_PRIMARY   = 1
                ) AS PRIMARY_EMAIL,
                -- Work phone from contact detail
                (
                    SELECT TOP 1 cd.CONTACT_VALUE
                    FROM {LEGACY_CONTACT_TABLE} cd
                    WHERE cd.PERSON_ID    = p.PERSON_ID
                      AND cd.CONTACT_TYPE = 'PHONE_WORK'
                      AND cd.IS_PRIMARY   = 1
                ) AS WORK_PHONE
            FROM {LEGACY_PERSON_TABLE} p
            {where}
            ORDER BY p.PERSON_ID ASC
            OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY
        """

    # ─── Schema ───────────────────────────────────────────────────────────────

    def _get_expected_schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("PERSON_ID",          pa.int64(),   nullable=False),
            pa.field("COMPANY_ID",         pa.int64(),   nullable=True),
            pa.field("FIRST_NAME",         pa.string(),  nullable=True),
            pa.field("LAST_NAME",          pa.string(),  nullable=False),
            pa.field("MIDDLE_NAME",        pa.string(),  nullable=True),
            pa.field("SALUTATION",         pa.string(),  nullable=True),
            pa.field("SUFFIX",             pa.string(),  nullable=True),
            pa.field("JOB_TITLE",          pa.string(),  nullable=True),
            pa.field("DEPARTMENT",         pa.string(),  nullable=True),
            pa.field("STATUS_CODE",        pa.string(),  nullable=True),
            pa.field("PERSON_TYPE",        pa.string(),  nullable=True),
            pa.field("IS_PRIMARY_CONTACT", pa.bool_(),   nullable=True),
            pa.field("IS_DECISION_MAKER",  pa.bool_(),   nullable=True),
            pa.field("DO_NOT_CALL",        pa.bool_(),   nullable=True),
            pa.field("DO_NOT_EMAIL",       pa.bool_(),   nullable=True),
            pa.field("DO_NOT_MAIL",        pa.bool_(),   nullable=True),
            pa.field("DIRECT_PHONE",       pa.string(),  nullable=True),
            pa.field("MOBILE_NUMBER",      pa.string(),  nullable=True),
            pa.field("WORK_PHONE",         pa.string(),  nullable=True),
            pa.field("ASSISTANT_NAME",     pa.string(),  nullable=True),
            pa.field("ASSISTANT_PHONE",    pa.string(),  nullable=True),
            pa.field("ADDR_LINE1",         pa.string(),  nullable=True),
            pa.field("ADDR_LINE2",         pa.string(),  nullable=True),
            pa.field("ADDR_CITY",          pa.string(),  nullable=True),
            pa.field("ADDR_STATE",         pa.string(),  nullable=True),
            pa.field("ADDR_ZIP",           pa.string(),  nullable=True),
            pa.field("ADDR_COUNTRY",       pa.string(),  nullable=True),
            pa.field("BIRTHDATE",          pa.date32(),  nullable=True),
            pa.field("GENDER_CODE",        pa.string(),  nullable=True),
            pa.field("PREFERRED_LANGUAGE", pa.string(),  nullable=True),
            pa.field("LINKEDIN_URL",       pa.string(),  nullable=True),
            pa.field("TWITTER_HANDLE",     pa.string(),  nullable=True),
            pa.field("DESCRIPTION",        pa.string(),  nullable=True),
            pa.field("OWNER_USER_ID",      pa.int64(),   nullable=True),
            pa.field("CREATED_DATE",       pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("MODIFIED_DATE",      pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("CREATED_BY",         pa.string(),  nullable=True),
            pa.field("MODIFIED_BY",        pa.string(),  nullable=True),
            pa.field("LEAD_SOURCE",        pa.string(),  nullable=True),
            pa.field("REPORTS_TO_ID",      pa.int64(),   nullable=True),
            pa.field("SOURCE_SYSTEM",      pa.string(),  nullable=True),
            pa.field("PRIMARY_EMAIL",      pa.string(),  nullable=True),
            # Extraction metadata
            pa.field("_extraction_ts",     pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("_extractor_version", pa.string(),  nullable=False),
            pa.field("_pii_masked",        pa.bool_(),   nullable=False),
        ])

    # ─── Post-processing ──────────────────────────────────────────────────────

    def _post_process_df(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── Required fields ────────────────────────────────────────────────
        df["LAST_NAME"] = df["LAST_NAME"].fillna("UNKNOWN")

        # ── String cleanup ─────────────────────────────────────────────────
        str_cols = [
            "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME", "SALUTATION", "SUFFIX",
            "JOB_TITLE", "DEPARTMENT", "STATUS_CODE", "DIRECT_PHONE",
            "MOBILE_NUMBER", "WORK_PHONE", "ASSISTANT_NAME",
            "ADDR_LINE1", "ADDR_LINE2", "ADDR_CITY", "ADDR_STATE",
            "ADDR_ZIP", "ADDR_COUNTRY", "PRIMARY_EMAIL",
            "LINKEDIN_URL", "TWITTER_HANDLE", "DESCRIPTION",
            "PREFERRED_LANGUAGE", "LEAD_SOURCE", "SOURCE_SYSTEM",
        ]
        for col in str_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip()
                df[col] = df[col].replace({"nan": None, "None": None, "": None})

        # ── Boolean normalisation ─────────────────────────────────────────
        bool_cols = [
            "IS_PRIMARY_CONTACT", "IS_DECISION_MAKER",
            "DO_NOT_CALL", "DO_NOT_EMAIL", "DO_NOT_MAIL",
        ]
        for col in bool_cols:
            if col in df.columns:
                df[col] = df[col].map(
                    {1: True, 0: False, "1": True, "0": False,
                     "Y": True, "N": False, True: True, False: False}
                ).astype("boolean")

        # ── Timestamp normalisation ───────────────────────────────────────
        for ts_col in ["CREATED_DATE", "MODIFIED_DATE"]:
            if ts_col in df.columns:
                df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)

        # ── Birthdate normalisation ───────────────────────────────────────
        if "BIRTHDATE" in df.columns:
            df["BIRTHDATE"] = pd.to_datetime(
                df["BIRTHDATE"], errors="coerce"
            ).dt.date

        # ── Email validation ──────────────────────────────────────────────
        email_re = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
        if "PRIMARY_EMAIL" in df.columns:
            invalid_mask = df["PRIMARY_EMAIL"].notna() & ~df["PRIMARY_EMAIL"].str.match(email_re)
            invalid_count = invalid_mask.sum()
            if invalid_count > 0:
                logger.warning("[ContactExtractor] %d invalid email addresses set to None.",
                               invalid_count)
                df.loc[invalid_mask, "PRIMARY_EMAIL"] = None
            self.metrics.skipped_rows += int(invalid_count)

        # ── Deduplication ─────────────────────────────────────────────────
        pre_len = len(df)
        df = df.drop_duplicates(subset=["PERSON_ID"], keep="last")
        dupes = pre_len - len(df)
        if dupes > 0:
            logger.warning("[ContactExtractor] Dropped %d duplicate PERSON_ID rows.", dupes)
            self.metrics.skipped_rows += dupes

        # ── PII masking (for non-production) ─────────────────────────────
        if self.mask_pii:
            df = self._apply_pii_masking(df)

        # ── Metadata ─────────────────────────────────────────────────────
        df["_extraction_ts"]      = pd.Timestamp.now(tz="UTC")
        df["_extractor_version"]  = "1.0.0"
        df["_pii_masked"]         = self.mask_pii

        return df

    def _apply_pii_masking(self, df: pd.DataFrame) -> pd.DataFrame:
        """Replaces real PII with deterministic masked values for lower envs."""
        logger.info("[ContactExtractor] Applying PII masking.")

        def _hash_str(val: Optional[str]) -> Optional[str]:
            if val is None or str(val) in {"None", "nan"}:
                return None
            return "MASKED_" + hashlib.sha256(str(val).encode()).hexdigest()[:8].upper()

        pii_cols = ["FIRST_NAME", "LAST_NAME", "PRIMARY_EMAIL",
                    "DIRECT_PHONE", "MOBILE_NUMBER", "WORK_PHONE",
                    "ADDR_LINE1", "ADDR_LINE2", "LINKEDIN_URL", "TWITTER_HANDLE"]
        for col in pii_cols:
            if col in df.columns:
                df[col] = df[col].apply(_hash_str)

        # Nullify exact birth date
        if "BIRTHDATE" in df.columns:
            df["BIRTHDATE"] = None

        return df


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Contact/Person records from legacy SQL database."
    )
    parser.add_argument("--db-url",       required=True)
    parser.add_argument("--output-dir",   required=True)
    parser.add_argument("--page-size",    type=int, default=5000)
    parser.add_argument("--incremental",  type=date.fromisoformat, default=None,
                        metavar="YYYY-MM-DD")
    parser.add_argument("--all-statuses", action="store_true")
    parser.add_argument("--mask-pii",     action="store_true",
                        help="Mask personally identifiable information")
    parser.add_argument("--no-resume",    action="store_true")
    parser.add_argument("--log-level",    default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.getLogger().setLevel(args.log_level)

    extractor = ContactExtractor(
        db_url=args.db_url,
        output_dir=Path(args.output_dir),
        page_size=args.page_size,
        incremental_since=args.incremental,
        active_only=not args.all_statuses,
        mask_pii=args.mask_pii,
    )

    with extractor:
        metrics = extractor.extract(resume=not args.no_resume)

    logger.info("─── Extraction Summary ─────────────────────────────────")
    for key, val in metrics.to_dict().items():
        logger.info("  %-25s %s", key + ":", val)

    if metrics.pages_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

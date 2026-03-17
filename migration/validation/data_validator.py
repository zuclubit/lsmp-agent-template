"""
data_validator.py
─────────────────────────────────────────────────────────────────────────────
Post-migration data validation framework.

Runs the following checks after records are loaded into Salesforce:
  1. Record count verification (source vs. Salesforce)
  2. Field checksum comparison (hash-based spot checks)
  3. Referential integrity checks (Contacts → Accounts)
  4. Required field completeness rates
  5. Duplicate detection
  6. Orphaned record detection

Produces a structured ValidationReport with pass/fail per check.

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pyarrow.parquet as pq
from simple_salesforce import Salesforce, SalesforceLogin

logger = logging.getLogger(__name__)


# ─── Result models ────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of a single validation check."""
    check_name:   str
    passed:       bool
    severity:     str  # "critical" | "warning" | "info"
    message:      str
    details:      Optional[Dict[str, Any]] = None
    timestamp:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed":     self.passed,
            "severity":   self.severity,
            "message":    self.message,
            "details":    self.details,
            "timestamp":  self.timestamp.isoformat(),
        }


@dataclass
class ValidationReport:
    """Aggregated results of all validation checks."""
    object_name:     str
    run_id:          str
    start_time:      datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time:        Optional[datetime] = None
    checks:          List[CheckResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def critical_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == "critical"]

    @property
    def is_passing(self) -> bool:
        """Report is passing if there are zero critical failures."""
        return len(self.critical_failures) == 0

    def finish(self) -> None:
        self.end_time = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_name":       self.object_name,
            "run_id":            self.run_id,
            "start_time":        self.start_time.isoformat(),
            "end_time":          self.end_time.isoformat() if self.end_time else None,
            "total_checks":      len(self.checks),
            "passed":            self.passed_count,
            "failed":            self.failed_count,
            "critical_failures": len(self.critical_failures),
            "is_passing":        self.is_passing,
            "checks":            [c.to_dict() for c in self.checks],
        }


# ─── Validator ────────────────────────────────────────────────────────────────

class DataValidator:
    """
    Orchestrates post-migration validation checks between
    the source Parquet files and the Salesforce target org.
    """

    def __init__(
        self,
        sf_username:          str,
        sf_password:          str,
        sf_security_token:    str = "",
        use_sandbox:          bool = True,
        api_version:          str = "59.0",
        output_dir:           Optional[Path] = None,
        count_tolerance_pct:  float = 0.5,
        checksum_sample_size: int = 500,
    ) -> None:
        self.sf_username          = sf_username
        self.sf_password          = sf_password
        self.sf_security_token    = sf_security_token
        self.use_sandbox          = use_sandbox
        self.api_version          = api_version
        self.output_dir           = Path(output_dir) if output_dir else Path(".")
        self.count_tolerance_pct  = count_tolerance_pct
        self.checksum_sample_size = checksum_sample_size
        self._sf: Optional[Salesforce] = None
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> None:
        session_id, instance = SalesforceLogin(
            username=self.sf_username,
            password=self.sf_password,
            security_token=self.sf_security_token,
            sandbox=self.use_sandbox,
            sf_version=self.api_version,
        )
        self._sf = Salesforce(instance=instance, session_id=session_id,
                              version=self.api_version)
        logger.info("[DataValidator] Connected to Salesforce.")

    # ─── Public API ───────────────────────────────────────────────────────────

    def validate_accounts(
        self,
        source_parquet_dir: Path,
        run_id:             str,
    ) -> ValidationReport:
        """Run full account validation suite."""
        if self._sf is None:
            self.connect()

        report = ValidationReport(object_name="Account", run_id=run_id)
        source_df = self._load_parquet_dir(source_parquet_dir, "sf_accounts_*.parquet")

        report.checks.append(self._check_record_count(source_df, "Account",
                                                        "Legacy_ID__c"))
        report.checks.append(self._check_required_field_completeness(
            source_df, "Name", "Account"))
        report.checks.append(self._check_required_field_completeness(
            source_df, "Legacy_ID__c", "Account"))
        report.checks.append(self._check_duplicates_in_source(
            source_df, "Legacy_ID__c", "Account"))
        report.checks.append(self._check_sf_duplicates(
            "Account", "Legacy_ID__c"))
        report.checks.append(self._check_field_checksums(
            source_df, "Account", "Name", "Legacy_ID__c"))
        report.checks.append(self._check_migration_status_distribution("Account"))

        report.finish()
        self._save_report(report)
        return report

    def validate_contacts(
        self,
        source_parquet_dir: Path,
        run_id:             str,
    ) -> ValidationReport:
        """Run full contact validation suite."""
        if self._sf is None:
            self.connect()

        report    = ValidationReport(object_name="Contact", run_id=run_id)
        source_df = self._load_parquet_dir(source_parquet_dir, "sf_contacts_*.parquet")

        report.checks.append(self._check_record_count(source_df, "Contact",
                                                        "Legacy_ID__c"))
        report.checks.append(self._check_required_field_completeness(
            source_df, "LastName", "Contact"))
        report.checks.append(self._check_required_field_completeness(
            source_df, "Legacy_ID__c", "Contact"))
        report.checks.append(self._check_referential_integrity(
            source_df, "Contact", "AccountId", "Account"))
        report.checks.append(self._check_sf_duplicates("Contact", "Legacy_ID__c"))
        report.checks.append(self._check_field_checksums(
            source_df, "Contact", "Email", "Legacy_ID__c"))
        report.checks.append(self._check_migration_status_distribution("Contact"))

        report.finish()
        self._save_report(report)
        return report

    # ─── Individual Checks ────────────────────────────────────────────────────

    def _check_record_count(
        self,
        source_df:   pd.DataFrame,
        object_name: str,
        id_field:    str,
    ) -> CheckResult:
        """Verify SF record count is within tolerance of source count."""
        source_count = source_df[id_field].nunique()
        try:
            result = self._sf.query(
                f"SELECT COUNT() FROM {object_name} "
                f"WHERE {id_field} != null AND Migration_Status__c != 'Failed'"
            )
            sf_count = result.get("totalSize", 0)
        except Exception as exc:
            return CheckResult(
                check_name="record_count",
                passed=False,
                severity="critical",
                message=f"Could not query {object_name} count: {exc}",
            )

        diff_pct = abs(source_count - sf_count) / max(source_count, 1) * 100
        passed   = diff_pct <= self.count_tolerance_pct

        return CheckResult(
            check_name="record_count",
            passed=passed,
            severity="critical" if not passed else "info",
            message=(
                f"{object_name} count: source={source_count}, sf={sf_count}, "
                f"diff={diff_pct:.2f}% ({'PASS' if passed else 'FAIL'})"
            ),
            details={
                "source_count":    source_count,
                "sf_count":        sf_count,
                "diff_pct":        round(diff_pct, 2),
                "tolerance_pct":   self.count_tolerance_pct,
            },
        )

    def _check_required_field_completeness(
        self,
        source_df:   pd.DataFrame,
        field_name:  str,
        object_name: str,
    ) -> CheckResult:
        """Check that a required field has no nulls in the source data."""
        if field_name not in source_df.columns:
            return CheckResult(
                check_name=f"required_field_{field_name}",
                passed=False,
                severity="warning",
                message=f"Column {field_name} not found in source DataFrame.",
            )

        null_count   = source_df[field_name].isna().sum()
        blank_count  = (source_df[field_name].astype(str).str.strip() == "").sum()
        total_null   = int(null_count + blank_count)
        total_rows   = len(source_df)
        completeness = (total_rows - total_null) / total_rows * 100 if total_rows > 0 else 0

        passed = completeness >= 99.0

        return CheckResult(
            check_name=f"required_field_{field_name}",
            passed=passed,
            severity="critical" if field_name in ("Name", "LastName", "Legacy_ID__c")
                     else "warning",
            message=(
                f"{object_name}.{field_name}: "
                f"completeness={completeness:.2f}% "
                f"({total_null} null/blank out of {total_rows})"
            ),
            details={"null_count": total_null, "total_rows": total_rows,
                     "completeness_pct": round(completeness, 2)},
        )

    def _check_duplicates_in_source(
        self,
        source_df:   pd.DataFrame,
        id_field:    str,
        object_name: str,
    ) -> CheckResult:
        """Detect duplicate external IDs in the source data."""
        dupes     = source_df[id_field].duplicated().sum()
        passed    = dupes == 0
        return CheckResult(
            check_name=f"source_duplicates_{id_field}",
            passed=passed,
            severity="critical" if not passed else "info",
            message=f"{object_name} source: {dupes} duplicate {id_field} values found.",
            details={"duplicate_count": int(dupes)},
        )

    def _check_sf_duplicates(
        self,
        object_name: str,
        ext_id_field: str,
    ) -> CheckResult:
        """Detect duplicate external IDs in Salesforce."""
        try:
            result = self._sf.query_all(
                f"SELECT {ext_id_field}, COUNT(Id) cnt "
                f"FROM {object_name} "
                f"WHERE {ext_id_field} != null "
                f"GROUP BY {ext_id_field} "
                f"HAVING COUNT(Id) > 1"
            )
            dup_count = result.get("totalSize", 0)
        except Exception as exc:
            return CheckResult(
                check_name=f"sf_duplicates_{ext_id_field}",
                passed=False,
                severity="warning",
                message=f"Could not check SF duplicates: {exc}",
            )

        passed = dup_count == 0
        return CheckResult(
            check_name=f"sf_duplicates_{ext_id_field}",
            passed=passed,
            severity="critical" if not passed else "info",
            message=f"Salesforce {object_name}: {dup_count} duplicate {ext_id_field} groups.",
            details={"duplicate_groups": dup_count},
        )

    def _check_field_checksums(
        self,
        source_df:    pd.DataFrame,
        object_name:  str,
        check_field:  str,
        id_field:     str,
    ) -> CheckResult:
        """
        Sample N records from source and verify their check_field value
        matches what's in Salesforce using a hash comparison.
        """
        if check_field not in source_df.columns or id_field not in source_df.columns:
            return CheckResult(
                check_name=f"checksum_{check_field}",
                passed=False,
                severity="warning",
                message=f"Column {check_field} or {id_field} not in source.",
            )

        sample = source_df[[id_field, check_field]].dropna(subset=[id_field]).head(
            self.checksum_sample_size)

        # Query SF for the same records
        id_list = "', '".join(sample[id_field].astype(str).tolist())
        try:
            result = self._sf.query_all(
                f"SELECT {id_field}, {check_field} FROM {object_name} "
                f"WHERE {id_field} IN ('{id_list}')"
            )
            sf_records = result.get("records", [])
        except Exception as exc:
            return CheckResult(
                check_name=f"checksum_{check_field}",
                passed=False,
                severity="warning",
                message=f"Could not query SF for checksum: {exc}",
            )

        sf_map = {r[id_field]: r.get(check_field, "") for r in sf_records}
        mismatches = 0
        mismatch_examples = []

        for _, row in sample.iterrows():
            legacy_id = str(row[id_field])
            src_val   = str(row.get(check_field, "") or "").strip()
            sf_val    = str(sf_map.get(legacy_id, "") or "").strip()

            src_hash = hashlib.md5(src_val.encode()).hexdigest()
            sf_hash  = hashlib.md5(sf_val.encode()).hexdigest()

            if src_hash != sf_hash:
                mismatches += 1
                if len(mismatch_examples) < 5:
                    mismatch_examples.append({
                        "legacy_id": legacy_id,
                        "src":       src_val[:100],
                        "sf":        sf_val[:100],
                    })

        match_rate = (len(sample) - mismatches) / len(sample) * 100 if sample.size > 0 else 0
        passed     = match_rate >= 98.0

        return CheckResult(
            check_name=f"checksum_{check_field}",
            passed=passed,
            severity="warning" if not passed else "info",
            message=(
                f"{object_name}.{check_field} checksum: "
                f"{mismatches}/{len(sample)} mismatches ({match_rate:.1f}% match)"
            ),
            details={
                "sampled":          len(sample),
                "mismatches":       mismatches,
                "match_rate_pct":   round(match_rate, 2),
                "examples":         mismatch_examples,
            },
        )

    def _check_referential_integrity(
        self,
        source_df:      pd.DataFrame,
        child_object:   str,
        fk_field:       str,
        parent_object:  str,
    ) -> CheckResult:
        """Check that FK references resolve in Salesforce."""
        if fk_field not in source_df.columns:
            return CheckResult(
                check_name=f"ref_integrity_{fk_field}",
                passed=True,
                severity="info",
                message=f"FK field {fk_field} not in source — skipping.",
            )

        orphan_query = (
            f"SELECT COUNT() FROM {child_object} "
            f"WHERE {fk_field} = null "
            f"AND Legacy_ID__c != null"
        )
        try:
            result     = self._sf.query(orphan_query)
            orphan_cnt = result.get("totalSize", 0)
        except Exception as exc:
            return CheckResult(
                check_name=f"ref_integrity_{fk_field}",
                passed=False,
                severity="warning",
                message=f"Could not check referential integrity: {exc}",
            )

        total_result = self._sf.query(
            f"SELECT COUNT() FROM {child_object} WHERE Legacy_ID__c != null")
        total_cnt = total_result.get("totalSize", 0)
        orphan_pct = (orphan_cnt / max(total_cnt, 1)) * 100
        passed     = orphan_pct <= 2.0

        return CheckResult(
            check_name=f"ref_integrity_{fk_field}",
            passed=passed,
            severity="warning" if not passed else "info",
            message=(
                f"{child_object} orphaned (no {parent_object} link): "
                f"{orphan_cnt}/{total_cnt} ({orphan_pct:.1f}%)"
            ),
            details={
                "orphan_count": orphan_cnt,
                "total_count":  total_cnt,
                "orphan_pct":   round(orphan_pct, 2),
            },
        )

    def _check_migration_status_distribution(
        self, object_name: str
    ) -> CheckResult:
        """Check the distribution of Migration_Status__c values in SF."""
        try:
            result = self._sf.query_all(
                f"SELECT Migration_Status__c, COUNT(Id) cnt "
                f"FROM {object_name} "
                f"WHERE Legacy_ID__c != null "
                f"GROUP BY Migration_Status__c"
            )
            dist = {r["Migration_Status__c"]: r["cnt"]
                    for r in result.get("records", [])}
        except Exception as exc:
            return CheckResult(
                check_name="migration_status_distribution",
                passed=False,
                severity="warning",
                message=f"Could not query status distribution: {exc}",
            )

        failed_count = dist.get("Failed", 0)
        total_count  = sum(dist.values())
        fail_pct     = (failed_count / max(total_count, 1)) * 100
        passed       = fail_pct <= 5.0

        return CheckResult(
            check_name="migration_status_distribution",
            passed=passed,
            severity="critical" if fail_pct > 20 else ("warning" if not passed else "info"),
            message=(
                f"{object_name} status distribution: {dist}. "
                f"Failed={fail_pct:.1f}%"
            ),
            details={"distribution": dist, "failed_pct": round(fail_pct, 2)},
        )

    # ─── Utility ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_parquet_dir(directory: Path, pattern: str) -> pd.DataFrame:
        files  = sorted(Path(directory).glob(pattern))
        frames = [pq.read_table(f).to_pandas() for f in files]
        if not frames:
            raise FileNotFoundError(f"No files matching {pattern} in {directory}")
        return pd.concat(frames, ignore_index=True)

    def _save_report(self, report: ValidationReport) -> None:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"validation_report_{report.object_name}_{ts}.json"
        with path.open("w") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        logger.info("[DataValidator] Report written: %s (%s)",
                    path, "PASS" if report.is_passing else "FAIL")

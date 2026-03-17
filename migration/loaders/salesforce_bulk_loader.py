"""
salesforce_bulk_loader.py
─────────────────────────────────────────────────────────────────────────────
Salesforce Bulk API 2.0 loader.

Uses the `simple_salesforce` library for SOAP/REST authentication and
the Bulk API 2.0 endpoints for high-throughput DML operations.

Features:
  - Supports insert, update, upsert, delete operations
  - Bulk API 2.0 job management (create → upload → close → poll → results)
  - Automatic CSV batching with configurable chunk sizes
  - Rate-limit awareness (429 / Salesforce API limit headers)
  - Retry logic inherited from BaseLoader
  - Success/error result CSV parsing
  - Account mapping file output (for Contact FK resolution)

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from simple_salesforce import Salesforce, SalesforceLogin
from simple_salesforce.exceptions import (
    SalesforceExpiredSession,
    SalesforceMalformedRequest,
    SalesforceMoreThanOneRecord,
    SalesforceResourceNotFound,
)

from base_loader import BaseLoader, LoadMetrics, RetryPolicy

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
BULK_API_VERSION     = "59.0"
BULK_MAX_ROWS        = 150_000_000   # Salesforce Bulk API 2.0 limit
POLL_INTERVAL_SEC    = 5
MAX_POLL_ATTEMPTS    = 360           # 30 minutes max wait
SF_DATE_FORMAT       = "%Y-%m-%dT%H:%M:%S.000+0000"


class SalesforceBulkLoader(BaseLoader):
    """
    Bulk API 2.0 loader for high-volume Salesforce DML.

    Splits large DataFrames into jobs, manages job lifecycle,
    polls for completion, and parses success/error CSVs.
    """

    LOADER_NAME = "SalesforceBulkLoader"

    def __init__(
        self,
        sf_instance_url:    str,
        sf_username:        str,
        sf_password:        str,
        sf_security_token:  str = "",
        sf_object_name:     str = "Account",
        batch_size:         int = 10_000,
        job_size:           int = 50_000,
        operation:          str = "upsert",
        external_id_field:  str = "Legacy_ID__c",
        use_sandbox:        bool = False,
        api_version:        str = BULK_API_VERSION,
        retry_policy:       Optional[RetryPolicy] = None,
        output_dir:         Optional[Path] = None,
        max_failures_pct:   float = 5.0,
        write_account_map:  bool = False,
    ) -> None:
        super().__init__(
            sf_instance_url=sf_instance_url,
            sf_username=sf_username,
            sf_password=sf_password,
            sf_security_token=sf_security_token,
            batch_size=batch_size,
            operation=operation,
            external_id_field=external_id_field,
            retry_policy=retry_policy,
            output_dir=output_dir,
            max_failures_pct=max_failures_pct,
        )
        self._sf_object_name  = sf_object_name
        self.job_size         = job_size
        self.use_sandbox      = use_sandbox
        self.api_version      = api_version
        self.write_account_map = write_account_map
        self._sf:       Optional[Salesforce] = None
        self._session_id: Optional[str]     = None
        self._base_url:   Optional[str]     = None

    @property
    def object_name(self) -> str:
        return self._sf_object_name

    # ─── Authentication ───────────────────────────────────────────────────────

    def _authenticate(self) -> None:
        """Authenticate via SOAP login and initialise Bulk API headers."""
        logger.info("[SalesforceBulkLoader] Authenticating as %s", self.sf_username)
        try:
            session_id, instance = SalesforceLogin(
                username=self.sf_username,
                password=self.sf_password,
                security_token=self.sf_security_token,
                sandbox=self.use_sandbox,
                sf_version=self.api_version,
            )
            self._sf         = Salesforce(instance=instance, session_id=session_id,
                                          version=self.api_version)
            self._session_id = session_id
            self._base_url   = f"https://{instance}/services/data/v{self.api_version}"
            logger.info("[SalesforceBulkLoader] Authenticated. Instance: %s", instance)
        except Exception as exc:
            raise ConnectionError(
                f"Salesforce authentication failed: {exc}") from exc

    # ─── Batch / Job execution ────────────────────────────────────────────────

    def _load_batch(
        self,
        records:           List[Dict],
        operation:         str,
        external_id_field: str,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Execute a Bulk API 2.0 job for this batch of records.

        Returns:
            (successes, failures)
        """
        csv_body = self._records_to_csv(records)
        job_id   = self._create_job(operation, external_id_field)

        try:
            self._upload_data(job_id, csv_body)
            self._close_job(job_id)
            self._poll_job(job_id)
            successes, failures = self._fetch_results(job_id, records)
        except Exception as exc:
            self._abort_job(job_id)
            raise

        return successes, failures

    def _create_job(self, operation: str, external_id_field: str) -> str:
        """Create a Bulk API 2.0 ingest job. Returns job ID."""
        url = f"{self._base_url}/jobs/ingest"
        payload = {
            "object":      self._sf_object_name,
            "operation":   operation,
            "contentType": "CSV",
            "lineEnding":  "LF",
        }
        if operation == "upsert":
            payload["externalIdFieldName"] = external_id_field

        resp = self._bulk_request("POST", url, json=payload)
        job_id = resp["id"]
        logger.debug("[SalesforceBulkLoader] Created Bulk job %s (%s %s)",
                     job_id, operation, self._sf_object_name)
        return job_id

    def _upload_data(self, job_id: str, csv_data: str) -> None:
        """Upload CSV data to an open Bulk API 2.0 job."""
        url = f"{self._base_url}/jobs/ingest/{job_id}/batches"
        headers = {
            "Authorization": f"Bearer {self._session_id}",
            "Content-Type":  "text/csv",
            "Accept":        "application/json",
        }
        resp = requests.put(url, data=csv_data.encode("utf-8"), headers=headers)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to upload data to job {job_id}: "
                f"HTTP {resp.status_code} {resp.text[:500]}")
        logger.debug("[SalesforceBulkLoader] Data uploaded to job %s.", job_id)

    def _close_job(self, job_id: str) -> None:
        """Signal that all data has been uploaded."""
        url = f"{self._base_url}/jobs/ingest/{job_id}"
        self._bulk_request("PATCH", url, json={"state": "UploadComplete"})
        logger.debug("[SalesforceBulkLoader] Job %s marked UploadComplete.", job_id)

    def _abort_job(self, job_id: str) -> None:
        try:
            url = f"{self._base_url}/jobs/ingest/{job_id}"
            self._bulk_request("PATCH", url, json={"state": "Aborted"})
            logger.warning("[SalesforceBulkLoader] Job %s aborted.", job_id)
        except Exception:
            pass

    def _poll_job(self, job_id: str) -> None:
        """Poll until the job reaches a terminal state."""
        url        = f"{self._base_url}/jobs/ingest/{job_id}"
        terminal   = {"JobComplete", "Failed", "Aborted"}
        retries    = 0

        while retries < MAX_POLL_ATTEMPTS:
            state_info = self._bulk_request("GET", url)
            state      = state_info.get("state", "")
            processed  = state_info.get("numberRecordsProcessed", 0)
            failed     = state_info.get("numberRecordsFailed",    0)
            logger.debug("[SalesforceBulkLoader] Job %s: state=%s processed=%d failed=%d",
                         job_id, state, processed, failed)

            if state in terminal:
                if state == "Failed":
                    raise RuntimeError(f"Bulk job {job_id} failed: {state_info}")
                if state == "Aborted":
                    raise RuntimeError(f"Bulk job {job_id} was aborted.")
                return  # JobComplete

            time.sleep(POLL_INTERVAL_SEC)
            retries += 1

        raise TimeoutError(
            f"Bulk job {job_id} did not complete within "
            f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_SEC} seconds.")

    def _fetch_results(
        self,
        job_id:  str,
        records: List[Dict],
    ) -> Tuple[List[Dict], List[Dict]]:
        """Fetch and parse success and error result CSVs."""
        successes: List[Dict] = []
        failures:  List[Dict] = []

        # Success results
        try:
            success_url = f"{self._base_url}/jobs/ingest/{job_id}/successfulResults"
            headers     = {"Authorization": f"Bearer {self._session_id}",
                           "Accept": "text/csv"}
            resp = requests.get(success_url, headers=headers)
            if resp.status_code == 200 and resp.text.strip():
                reader = csv.DictReader(io.StringIO(resp.text))
                for row in reader:
                    successes.append({"id": row.get("sf__Id", ""), "created": row.get("sf__Created")})
        except Exception as exc:
            logger.warning("[SalesforceBulkLoader] Could not fetch success results: %s", exc)

        # Failed results
        try:
            error_url = f"{self._base_url}/jobs/ingest/{job_id}/failedResults"
            headers   = {"Authorization": f"Bearer {self._session_id}",
                         "Accept": "text/csv"}
            resp = requests.get(error_url, headers=headers)
            if resp.status_code == 200 and resp.text.strip():
                reader = csv.DictReader(io.StringIO(resp.text))
                for row in reader:
                    failures.append({
                        "legacy_id":    row.get(self.external_id_field, ""),
                        "error":        row.get("sf__Error", ""),
                        "error_fields": row.get("sf__ErrorFields", ""),
                    })
        except Exception as exc:
            logger.warning("[SalesforceBulkLoader] Could not fetch error results: %s", exc)

        logger.info("[SalesforceBulkLoader] Job %s results: success=%d failed=%d",
                    job_id, len(successes), len(failures))
        return successes, failures

    # ─── HTTP helper ──────────────────────────────────────────────────────────

    def _bulk_request(self, method: str, url: str, **kwargs: Any) -> Dict:
        """Perform an authenticated Bulk API 2.0 request with retry on 429."""
        headers = kwargs.pop("headers", {})
        headers.update({
            "Authorization": f"Bearer {self._session_id}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })

        for attempt in range(1, self.retry_policy.max_retries + 1):
            resp = requests.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", self.retry_policy.base_delay_sec))
                logger.warning("[SalesforceBulkLoader] Rate limited. Waiting %ds.", wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Bulk API error {resp.status_code} on {method} {url}: {resp.text[:500]}")

            return resp.json() if resp.text else {}

        raise RuntimeError(f"All {self.retry_policy.max_retries} retry attempts exhausted.")

    # ─── CSV conversion ───────────────────────────────────────────────────────

    @staticmethod
    def _records_to_csv(records: List[Dict]) -> str:
        """Convert a list of dicts to a CSV string."""
        if not records:
            return ""
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=list(records[0].keys()),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for rec in records:
            clean_rec = {}
            for k, v in rec.items():
                if v is None or (isinstance(v, float) and v != v):  # NaN check
                    clean_rec[k] = ""
                elif isinstance(v, bool):
                    clean_rec[k] = str(v).lower()
                elif isinstance(v, datetime):
                    clean_rec[k] = v.strftime(SF_DATE_FORMAT)
                else:
                    clean_rec[k] = str(v)
            writer.writerow(clean_rec)
        return output.getvalue()

    # ─── Account mapping file ─────────────────────────────────────────────────

    def write_account_mapping_csv(self, metrics: LoadMetrics) -> Optional[Path]:
        """
        After a successful Account load, write a mapping CSV:
            legacy_id -> salesforce_id
        Used by ContactTransformer to resolve AccountId foreign keys.
        """
        if not self.write_account_map or not metrics.success_ids:
            return None

        # Re-query SF to get Legacy_ID__c -> Id mapping
        try:
            legacy_ids = list(metrics.success_ids)
            results = self._sf.query_all(
                f"SELECT Id, Legacy_ID__c FROM Account "
                f"WHERE Legacy_ID__c != null "
                f"ORDER BY CreatedDate DESC LIMIT 50000"
            )
            rows = [{"legacy_id": r["Legacy_ID__c"], "salesforce_id": r["Id"]}
                    for r in results.get("records", []) if r.get("Legacy_ID__c")]

            if rows:
                ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                path = self.output_dir / f"account_id_mapping_{ts}.csv"
                pd.DataFrame(rows).to_csv(path, index=False)
                logger.info("[SalesforceBulkLoader] Account mapping written: %s (%d rows)",
                            path, len(rows))
                return path
        except Exception as exc:
            logger.error("[SalesforceBulkLoader] Failed to write account mapping: %s", exc)
        return None

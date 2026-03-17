"""
Full-featured Salesforce REST + Bulk API 2.0 client.

Authentication: OAuth 2.0 JWT Bearer Token flow (server-to-server, no user
interaction required).  The client automatically refreshes the access token
before it expires and transparently retries the original request.

Supported operations
--------------------
REST API
  query          – SOQL query (handles query-more pagination automatically)
  create         – Single record insert
  update         – Single record update by Salesforce ID
  upsert         – Insert-or-update by external ID field
  delete         – Hard-delete by Salesforce ID

Bulk API 2.0
  bulk_insert    – Async bulk insert (CSV or list-of-dicts)
  bulk_update    – Async bulk update
  bulk_upsert    – Async bulk upsert by external ID field
  bulk_query     – Async bulk SOQL query

Dependencies: httpx, tenacity, cryptography, python-jose
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from jose import jwt

from .base_client import (
    AuthenticationError,
    BaseHTTPClient,
    ClientConfig,
    CircuitBreaker,
    RateLimitError,
    RetryConfig,
    ServerError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SF_API_VERSION = os.getenv("SF_API_VERSION", "v59.0")
SF_LOGIN_URL = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com")
_TOKEN_EXPIRY_BUFFER_SECONDS = 60  # refresh this many seconds before expiry


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class BulkJobState(str, Enum):
    UPLOAD_COMPLETE = "UploadComplete"
    IN_PROGRESS = "InProgress"
    ABORTED = "Aborted"
    JOB_COMPLETE = "JobComplete"
    FAILED = "Failed"


class BulkOperation(str, Enum):
    INSERT = "insert"
    UPDATE = "update"
    UPSERT = "upsert"
    DELETE = "delete"
    HARD_DELETE = "hardDelete"
    QUERY = "query"


@dataclass
class SalesforceConfig:
    """All credentials and connection settings for Salesforce."""

    client_id: str
    username: str
    private_key_pem: str  # PEM-encoded RSA private key (no passphrase)
    instance_url: str = ""  # populated after first auth
    api_version: str = SF_API_VERSION
    login_url: str = SF_LOGIN_URL
    bulk_poll_interval_seconds: float = 5.0
    bulk_poll_max_seconds: float = 600.0


@dataclass
class QueryResult:
    records: List[Dict[str, Any]]
    total_size: int
    done: bool
    next_records_url: Optional[str] = None


@dataclass
class BulkJobResult:
    job_id: str
    state: BulkJobState
    number_records_processed: int
    number_records_failed: int
    successful_results: List[Dict[str, Any]] = field(default_factory=list)
    failed_results: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass
class _TokenCache:
    access_token: str = ""
    instance_url: str = ""
    issued_at: float = 0.0
    expires_in: int = 3600

    def is_expired(self) -> bool:
        age = time.monotonic() - self.issued_at
        return age >= (self.expires_in - _TOKEN_EXPIRY_BUFFER_SECONDS)

    def clear(self) -> None:
        self.access_token = ""
        self.issued_at = 0.0


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class SalesforceClient(BaseHTTPClient):
    """
    Async Salesforce REST + Bulk API 2.0 client.

    Usage::

        config = SalesforceConfig(
            client_id=os.environ["SF_CLIENT_ID"],
            username=os.environ["SF_USERNAME"],
            private_key_pem=open("key.pem").read(),
        )
        async with SalesforceClient(config) as sf:
            result = await sf.query("SELECT Id, Name FROM Account LIMIT 10")
            for rec in result.records:
                print(rec)
    """

    def __init__(self, sf_config: SalesforceConfig) -> None:
        self._sf_config = sf_config
        self._token_cache = _TokenCache()
        self._auth_lock = asyncio.Lock()

        # We start with an empty base_url; it will be updated after first auth.
        client_config = ClientConfig(
            base_url=sf_config.instance_url or "https://placeholder.salesforce.com",
            timeout_seconds=60.0,
            retry=RetryConfig(max_attempts=3, wait_min_seconds=2.0, wait_max_seconds=30.0),
            circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30.0),
            default_headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        super().__init__(client_config)

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _build_jwt(self) -> str:
        """Construct and sign a JWT assertion for the Bearer flow."""
        now = int(time.time())
        payload = {
            "iss": self._sf_config.client_id,
            "sub": self._sf_config.username,
            "aud": self._sf_config.login_url,
            "exp": now + 300,
        }
        private_key = serialization.load_pem_private_key(
            self._sf_config.private_key_pem.encode(),
            password=None,
        )
        token = jwt.encode(payload, private_key, algorithm="RS256")
        return token

    async def _fetch_token(self) -> None:
        """Exchange a JWT assertion for an access token."""
        jwt_token = self._build_jwt()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._sf_config.login_url}/services/oauth2/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            raise AuthenticationError(
                f"JWT Bearer auth failed: {response.status_code} – {response.text}",
                status_code=response.status_code,
                response_body=response.text,
            )

        body = response.json()
        self._token_cache.access_token = body["access_token"]
        self._token_cache.instance_url = body["instance_url"]
        self._token_cache.issued_at = time.monotonic()
        self._token_cache.expires_in = int(body.get("expires_in", 3600))

        # Update the base_url of the underlying HTTP client
        if self._http:
            self._http.base_url = httpx.URL(
                f"{self._token_cache.instance_url}/services/data/{self._sf_config.api_version}"
            )
        self._sf_config.instance_url = self._token_cache.instance_url
        logger.info(
            "Salesforce access token obtained for user=%s instance=%s",
            self._sf_config.username,
            self._token_cache.instance_url,
        )

    async def _ensure_token(self) -> None:
        """Ensure a valid access token exists, refreshing if necessary."""
        async with self._auth_lock:
            if self._token_cache.is_expired():
                logger.debug("Salesforce token expired or missing – refreshing")
                await self._fetch_token()

    # ------------------------------------------------------------------
    # BaseHTTPClient abstract methods
    # ------------------------------------------------------------------

    async def _build_auth_headers(self) -> Dict[str, str]:
        await self._ensure_token()
        return {"Authorization": f"Bearer {self._token_cache.access_token}"}

    async def _on_auth_error(self, response: httpx.Response) -> bool:
        """On 401, invalidate the token and signal the caller to retry."""
        logger.warning("Salesforce 401 received – invalidating token cache")
        self._token_cache.clear()
        return True  # retry after re-auth

    # ------------------------------------------------------------------
    # REST API – Query
    # ------------------------------------------------------------------

    async def query(self, soql: str, *, include_deleted: bool = False) -> QueryResult:
        """
        Execute a SOQL query and return ALL records (handles queryMore).

        Args:
            soql:            SOQL statement.
            include_deleted: If True, use queryAll to include deleted records.

        Returns:
            :class:`QueryResult` with ``records`` containing all pages.
        """
        endpoint = "/queryAll" if include_deleted else "/query"
        response = await self.get(endpoint, params={"q": soql})
        response.raise_for_status()
        body = response.json()

        all_records: List[Dict[str, Any]] = list(body.get("records", []))
        next_url: Optional[str] = body.get("nextRecordsUrl")

        while next_url:
            page_resp = await self.get(next_url)
            page_resp.raise_for_status()
            page_body = page_resp.json()
            all_records.extend(page_body.get("records", []))
            next_url = page_body.get("nextRecordsUrl")

        logger.info(
            "SOQL query returned %d/%d records",
            len(all_records),
            body.get("totalSize", "?"),
        )
        return QueryResult(
            records=all_records,
            total_size=body.get("totalSize", len(all_records)),
            done=True,
        )

    # ------------------------------------------------------------------
    # REST API – DML
    # ------------------------------------------------------------------

    async def create(
        self, sobject: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Insert a single sObject record.

        Returns:
            dict with ``id``, ``success``, ``errors``.
        """
        response = await self.post(f"/sobjects/{sobject}/", json=data)
        if response.status_code not in (200, 201):
            raise ServerError(
                f"Create failed for {sobject}: {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )
        result = response.json()
        logger.info("Created %s id=%s", sobject, result.get("id"))
        return result

    async def update(
        self, sobject: str, record_id: str, data: Dict[str, Any]
    ) -> bool:
        """
        Update a sObject record by Salesforce ID.

        Returns:
            True if the update was accepted (HTTP 204).
        """
        response = await self.patch(f"/sobjects/{sobject}/{record_id}", json=data)
        if response.status_code != 204:
            raise ServerError(
                f"Update failed for {sobject}/{record_id}: {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )
        logger.info("Updated %s id=%s", sobject, record_id)
        return True

    async def upsert(
        self, sobject: str, external_id_field: str, external_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Upsert (insert-or-update) a sObject using an external ID field.

        Returns:
            dict with ``id``, ``created``.
        """
        response = await self.patch(
            f"/sobjects/{sobject}/{external_id_field}/{external_id}",
            json=data,
        )
        if response.status_code not in (200, 201, 204):
            raise ServerError(
                f"Upsert failed for {sobject}/{external_id_field}/{external_id}: "
                f"{response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )
        created = response.status_code == 201
        body = response.json() if response.content else {}
        logger.info(
            "%s %s %s/%s=%s",
            "Created" if created else "Updated",
            sobject,
            external_id_field,
            external_id,
            body.get("id", "—"),
        )
        return {**body, "created": created}

    async def delete(self, sobject: str, record_id: str) -> bool:
        """
        Hard-delete a sObject record.

        Returns:
            True if accepted (HTTP 204).
        """
        response = await self.request(
            method=__import__("integrations.rest_clients.base_client", fromlist=["HttpMethod"]).HttpMethod.DELETE,
            path=f"/sobjects/{sobject}/{record_id}",
        )
        if response.status_code != 204:
            raise ServerError(
                f"Delete failed for {sobject}/{record_id}: {response.status_code}",
                status_code=response.status_code,
                response_body=response.text,
            )
        logger.info("Deleted %s id=%s", sobject, record_id)
        return True

    # ------------------------------------------------------------------
    # Bulk API 2.0 – helpers
    # ------------------------------------------------------------------

    def _records_to_csv(self, records: List[Dict[str, Any]]) -> str:
        """Serialise a list of dicts to CSV (required by Bulk API 2.0)."""
        if not records:
            return ""
        buf = io.StringIO()
        fieldnames = list(records[0].keys())
        writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
        return buf.getvalue()

    async def _create_bulk_job(
        self,
        operation: BulkOperation,
        sobject: str,
        external_id_field: Optional[str] = None,
    ) -> str:
        """Create a Bulk API 2.0 ingest job and return its ID."""
        payload: Dict[str, Any] = {
            "operation": operation.value,
            "object": sobject,
            "contentType": "CSV",
            "lineEnding": "LF",
        }
        if external_id_field:
            payload["externalIdFieldName"] = external_id_field

        resp = await self.post("/jobs/ingest", json=payload)
        resp.raise_for_status()
        job_id = resp.json()["id"]
        logger.info("Bulk job created: id=%s op=%s object=%s", job_id, operation.value, sobject)
        return job_id

    async def _upload_bulk_data(self, job_id: str, csv_data: str) -> None:
        """Upload CSV data to an open bulk ingest job."""
        if self._http is None:
            raise RuntimeError("Client not started")
        auth_headers = await self._build_auth_headers()
        resp = await self._http.put(
            f"/jobs/ingest/{job_id}/batches",
            content=csv_data.encode("utf-8"),
            headers={**auth_headers, "Content-Type": "text/csv"},
        )
        resp.raise_for_status()
        logger.debug("Uploaded %d bytes to bulk job %s", len(csv_data), job_id)

    async def _close_bulk_job(self, job_id: str) -> None:
        """Signal that all data has been uploaded."""
        resp = await self.patch(
            f"/jobs/ingest/{job_id}",
            json={"state": BulkJobState.UPLOAD_COMPLETE.value},
        )
        resp.raise_for_status()

    async def _poll_bulk_job(self, job_id: str) -> BulkJobResult:
        """Poll until the bulk job reaches a terminal state."""
        deadline = time.monotonic() + self._sf_config.bulk_poll_max_seconds
        while time.monotonic() < deadline:
            resp = await self.get(f"/jobs/ingest/{job_id}")
            resp.raise_for_status()
            body = resp.json()
            state = BulkJobState(body["state"])
            logger.debug("Bulk job %s state=%s", job_id, state.value)

            if state in (BulkJobState.JOB_COMPLETE, BulkJobState.ABORTED, BulkJobState.FAILED):
                return await self._collect_bulk_results(job_id, body)

            await asyncio.sleep(self._sf_config.bulk_poll_interval_seconds)

        raise TimeoutError(
            f"Bulk job {job_id} did not complete within "
            f"{self._sf_config.bulk_poll_max_seconds}s"
        )

    async def _collect_bulk_results(
        self, job_id: str, status_body: Dict[str, Any]
    ) -> BulkJobResult:
        """Download successful and failed result CSVs."""
        successful: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for result_type, target in (("successfulResults", successful), ("failedResults", failed)):
            resp = await self.get(f"/jobs/ingest/{job_id}/{result_type}")
            if resp.status_code == 200 and resp.text:
                reader = csv.DictReader(io.StringIO(resp.text))
                target.extend(reader)

        return BulkJobResult(
            job_id=job_id,
            state=BulkJobState(status_body["state"]),
            number_records_processed=status_body.get("numberRecordsProcessed", 0),
            number_records_failed=status_body.get("numberRecordsFailed", 0),
            successful_results=successful,
            failed_results=failed,
        )

    # ------------------------------------------------------------------
    # Bulk API 2.0 – public interface
    # ------------------------------------------------------------------

    async def _run_bulk_job(
        self,
        operation: BulkOperation,
        sobject: str,
        records: List[Dict[str, Any]],
        external_id_field: Optional[str] = None,
    ) -> BulkJobResult:
        job_id = await self._create_bulk_job(operation, sobject, external_id_field)
        csv_data = self._records_to_csv(records)
        await self._upload_bulk_data(job_id, csv_data)
        await self._close_bulk_job(job_id)
        result = await self._poll_bulk_job(job_id)
        logger.info(
            "Bulk %s on %s complete: processed=%d failed=%d",
            operation.value,
            sobject,
            result.number_records_processed,
            result.number_records_failed,
        )
        return result

    async def bulk_insert(
        self, sobject: str, records: List[Dict[str, Any]]
    ) -> BulkJobResult:
        """Bulk insert records into a Salesforce object."""
        return await self._run_bulk_job(BulkOperation.INSERT, sobject, records)

    async def bulk_update(
        self, sobject: str, records: List[Dict[str, Any]]
    ) -> BulkJobResult:
        """Bulk update records (each record must include Salesforce 'Id')."""
        return await self._run_bulk_job(BulkOperation.UPDATE, sobject, records)

    async def bulk_upsert(
        self,
        sobject: str,
        records: List[Dict[str, Any]],
        external_id_field: str,
    ) -> BulkJobResult:
        """Bulk upsert using an external ID field."""
        return await self._run_bulk_job(
            BulkOperation.UPSERT, sobject, records, external_id_field
        )

    async def bulk_query(self, soql: str) -> List[Dict[str, Any]]:
        """
        Execute a SOQL query via Bulk API 2.0 (for large result sets).

        Returns a flat list of all result records.
        """
        payload = {"operation": BulkOperation.QUERY.value, "query": soql}
        resp = await self.post("/jobs/query", json=payload)
        resp.raise_for_status()
        job_id = resp.json()["id"]

        deadline = time.monotonic() + self._sf_config.bulk_poll_max_seconds
        while time.monotonic() < deadline:
            status_resp = await self.get(f"/jobs/query/{job_id}")
            status_resp.raise_for_status()
            body = status_resp.json()
            state = body["state"]
            if state == BulkJobState.JOB_COMPLETE.value:
                break
            if state in (BulkJobState.ABORTED.value, BulkJobState.FAILED.value):
                raise ServerError(f"Bulk query job {job_id} {state}")
            await asyncio.sleep(self._sf_config.bulk_poll_interval_seconds)
        else:
            raise TimeoutError(f"Bulk query job {job_id} timed out")

        records: List[Dict[str, Any]] = []
        locator = ""
        while True:
            params: Dict[str, Any] = {"maxRecords": 50000}
            if locator:
                params["locator"] = locator
            page_resp = await self.get(f"/jobs/query/{job_id}/results", params=params)
            page_resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(page_resp.text))
            records.extend(reader)
            locator = page_resp.headers.get("Sforce-Locator", "")
            if not locator or locator == "null":
                break

        logger.info("Bulk query returned %d records for job %s", len(records), job_id)
        return records

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def describe(self, sobject: str) -> Dict[str, Any]:
        """Return the metadata description for a sObject."""
        resp = await self.get(f"/sobjects/{sobject}/describe")
        resp.raise_for_status()
        return resp.json()

    async def limits(self) -> Dict[str, Any]:
        """Return current org API usage limits."""
        resp = await self.get("/limits")
        resp.raise_for_status()
        return resp.json()

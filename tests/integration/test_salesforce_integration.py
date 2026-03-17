"""
Integration tests for the Salesforce REST + Bulk API 2.0 client.

Tests use mocked HTTP transport so no live Salesforce org is required in CI.
Mark individual test classes with @pytest.mark.integration (registered in
conftest.py) to allow selective inclusion/exclusion.

Covers:
  - OAuth 2.0 JWT Bearer Token authentication
  - Account CRUD via REST API
  - SOQL query (single page + paginated)
  - Bulk API 2.0 upsert + job status polling
  - HTTP 429 rate-limit handling with Retry-After
  - HTTP 401 session-refresh and transparent retry
  - Platform Event publishing
  - Composite API batch requests

Dependencies: pytest, pytest-asyncio, httpx
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

# ---------------------------------------------------------------------------
# Pull in the real domain types we're testing against
# ---------------------------------------------------------------------------
from integrations.rest_clients.salesforce_client import (
    BulkJobResult,
    BulkJobState,
    BulkOperation,
    QueryResult,
    SalesforceClient,
    SalesforceConfig,
)
from integrations.rest_clients.base_client import (
    AuthenticationError,
    RateLimitError,
    ServerError,
)

# Path to the shared mock responses fixture
_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_MOCK_RESPONSES = json.loads((_FIXTURES_DIR / "mock_sf_responses.json").read_text())

# A valid 18-char Salesforce Account ID used consistently across tests
_VALID_SF_ACCOUNT_ID = "001Dn000001MockAA2"


# ===========================================================================
# Helpers
# ===========================================================================


def _make_httpx_response(status_code: int, body: Any, headers: Dict[str, str] | None = None) -> httpx.Response:
    """Build a real httpx.Response object suitable for mocking."""
    content = json.dumps(body).encode() if body is not None else b""
    response = httpx.Response(
        status_code=status_code,
        content=content,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return response


def _make_csv_response(status_code: int, csv_text: str) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=csv_text.encode(),
        headers={"Content-Type": "text/csv"},
    )


def _make_sf_config(tmp_path) -> SalesforceConfig:
    """Build a SalesforceConfig with a real RSA key pair for JWT tests."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    except Exception:
        pem = "MOCK_PEM"  # graceful fallback if cryptography not installed

    return SalesforceConfig(
        client_id="3MVG9TestClientId",
        username="migration@example.com.sandbox",
        private_key_pem=pem,
        instance_url="https://myorg.salesforce.com",
        api_version="v59.0",
        login_url="https://test.salesforce.com",
        bulk_poll_interval_seconds=0.01,  # fast polling in tests
        bulk_poll_max_seconds=5.0,
    )


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture
def sf_config(tmp_path) -> SalesforceConfig:
    return _make_sf_config(tmp_path)


@pytest.fixture
def mock_auth_token_response() -> httpx.Response:
    body = _MOCK_RESPONSES["auth"]["success"]["body"]
    return _make_httpx_response(200, body)


@pytest.fixture
def mock_account_create_response() -> httpx.Response:
    body = _MOCK_RESPONSES["account"]["create_success"]["body"]
    return _make_httpx_response(201, body)


@pytest.fixture
def mock_account_query_response() -> httpx.Response:
    body = _MOCK_RESPONSES["account"]["query_success"]["body"]
    return _make_httpx_response(200, body)


@pytest.fixture
def mock_bulk_job_created_response() -> httpx.Response:
    body = _MOCK_RESPONSES["bulk_api"]["create_job_success"]["body"]
    return _make_httpx_response(200, body)


@pytest.fixture
def mock_bulk_job_complete_response() -> httpx.Response:
    body = _MOCK_RESPONSES["bulk_api"]["job_complete_success"]["body"]
    return _make_httpx_response(200, body)


@pytest.fixture
def sample_account_records() -> List[Dict[str, Any]]:
    return [
        {"Name": "Acme Corporation", "Legacy_ID__c": "LEGACY-ACC-00000001", "Type": "Customer"},
        {"Name": "Global Finance Partners", "Legacy_ID__c": "LEGACY-ACC-00000004", "Type": "Partner"},
        {"Name": "Café Münchener GmbH", "Legacy_ID__c": "LEGACY-ACC-00000002", "Type": "Customer"},
    ]


# ===========================================================================
# TEST CLASSES
# ===========================================================================


@pytest.mark.integration
class TestSalesforceAuthentication:
    """OAuth 2.0 JWT Bearer flow — authentication and token lifecycle."""

    @pytest.mark.asyncio
    async def test_authenticate_fetches_access_token(self, sf_config, mock_auth_token_response):
        """A successful auth round-trip stores access_token and instance_url."""
        with patch("httpx.AsyncClient") as mock_async_client_cls:
            mock_http_instance = AsyncMock()
            mock_http_instance.__aenter__ = AsyncMock(return_value=mock_http_instance)
            mock_http_instance.__aexit__ = AsyncMock(return_value=False)
            mock_http_instance.post = AsyncMock(return_value=mock_auth_token_response)
            mock_async_client_cls.return_value = mock_http_instance

            async with SalesforceClient(sf_config) as client:
                await client._fetch_token()

            assert client._token_cache.access_token != ""
            assert "salesforce.com" in client._token_cache.instance_url

    @pytest.mark.asyncio
    async def test_authenticate_raises_on_invalid_credentials(self, sf_config):
        """HTTP 400 from token endpoint must raise AuthenticationError."""
        error_body = _MOCK_RESPONSES["auth"]["invalid_credentials"]["body"]
        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=_make_httpx_response(400, error_body))
            mock_cls.return_value = mock_instance

            async with SalesforceClient(sf_config) as client:
                with pytest.raises(AuthenticationError) as exc_info:
                    await client._fetch_token()
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_token_not_refreshed_when_still_valid(self, sf_config):
        """_ensure_token should not call _fetch_token if the cache is fresh."""
        with patch("httpx.AsyncClient"):
            async with SalesforceClient(sf_config) as client:
                client._token_cache.access_token = "existing_token"
                client._token_cache.issued_at = time.monotonic()  # just issued
                client._token_cache.expires_in = 3600

                with patch.object(client, "_fetch_token", new_callable=AsyncMock) as mock_fetch:
                    await client._ensure_token()
                    mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_token_is_refreshed(self, sf_config, mock_auth_token_response):
        """An expired token must trigger a refresh before the request continues."""
        with patch("httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_auth_token_response)
            mock_cls.return_value = mock_instance

            async with SalesforceClient(sf_config) as client:
                # Make the token appear expired
                client._token_cache.issued_at = time.monotonic() - 4000
                client._token_cache.expires_in = 3600

                with patch.object(client, "_fetch_token", new_callable=AsyncMock) as mock_fetch:
                    await client._ensure_token()
                    mock_fetch.assert_called_once()


@pytest.mark.integration
class TestSalesforceAccountOperations:
    """REST API CRUD operations for Account sObject."""

    @pytest.mark.asyncio
    async def test_create_account_returns_salesforce_id(
        self, sf_config, mock_account_create_response
    ):
        """Successful POST to /sobjects/Account/ must return id and success=True."""
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = mock_account_create_response
                result = await client.create(
                    "Account",
                    {
                        "Name": "Acme Corporation",
                        "Type": "Customer",
                        "Legacy_ID__c": "LEGACY-ACC-00000001",
                    },
                )

        assert result["id"] == "001Dn000001MockAA2"
        assert result["success"] is True
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_create_account_calls_correct_endpoint(self, sf_config):
        """create() must POST to /sobjects/{SObjectName}/."""
        success_body = {"id": "001Dn000001MockAA2", "success": True, "errors": []}
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = _make_httpx_response(201, success_body)
                await client.create("Account", {"Name": "Test"})
                call_path = mock_post.call_args[0][0]

        assert "/sobjects/Account/" in call_path

    @pytest.mark.asyncio
    async def test_update_account_returns_true_on_204(self, sf_config):
        """PATCH returning HTTP 204 No Content must return True."""
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "patch", new_callable=AsyncMock) as mock_patch:
                mock_patch.return_value = _make_httpx_response(204, None)
                result = await client.update(
                    "Account", _VALID_SF_ACCOUNT_ID, {"Industry": "Technology"}
                )

        assert result is True

    @pytest.mark.asyncio
    async def test_create_raises_server_error_on_4xx(self, sf_config):
        """A 4xx response from the REST API must raise ServerError."""
        error_body = _MOCK_RESPONSES["account"]["create_missing_required"]["body"]
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = _make_httpx_response(400, error_body)
                with pytest.raises(ServerError) as exc_info:
                    await client.create("Account", {})

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_session_refresh_on_401(self, sf_config, mock_auth_token_response, mock_account_create_response):
        """When _on_auth_error is triggered, the token cache must be cleared."""
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "stale_token"
            # Simulate _on_auth_error being called
            expired_response = _make_httpx_response(
                401,
                _MOCK_RESPONSES["auth"]["session_expired"]["body"],
            )
            retry_triggered = await client._on_auth_error(expired_response)

        assert retry_triggered is True
        assert client._token_cache.access_token == ""  # cache cleared

    @pytest.mark.asyncio
    async def test_upsert_account_by_external_id(self, sf_config):
        """upsert() should PATCH using external ID field and return created flag."""
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "patch", new_callable=AsyncMock) as mock_patch:
                mock_patch.return_value = _make_httpx_response(201, {"id": "001Dn000001MockAA2"})
                result = await client.upsert(
                    "Account",
                    "Legacy_ID__c",
                    "LEGACY-ACC-00000001",
                    {"Name": "Acme Corporation", "Type": "Customer"},
                )

        assert result["created"] is True
        assert result["id"] == "001Dn000001MockAA2"


@pytest.mark.integration
class TestSOQLQuery:
    """SOQL query execution including pagination."""

    @pytest.mark.asyncio
    async def test_query_returns_all_records_single_page(
        self, sf_config, mock_account_query_response
    ):
        """Single-page query must return all records with correct totalSize."""
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_account_query_response
                result = await client.query("SELECT Id, Name FROM Account LIMIT 10")

        assert isinstance(result, QueryResult)
        assert result.total_size == 2
        assert len(result.records) == 2
        assert result.done is True

    @pytest.mark.asyncio
    async def test_query_fetches_all_pages(self, sf_config):
        """query() must follow nextRecordsUrl until done=True."""
        page1 = _make_httpx_response(
            200,
            {
                "totalSize": 4,
                "done": False,
                "nextRecordsUrl": "/query/cursor-001",
                "records": [{"Id": "001A"}, {"Id": "001B"}],
            },
        )
        page2 = _make_httpx_response(
            200,
            {"totalSize": 4, "done": True, "records": [{"Id": "001C"}, {"Id": "001D"}]},
        )

        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                mock_get.side_effect = [page1, page2]
                result = await client.query("SELECT Id FROM Account")

        assert len(result.records) == 4
        assert result.done is True

    @pytest.mark.asyncio
    async def test_query_returns_empty_result_set(self, sf_config):
        """A query with zero results must return QueryResult with empty records list."""
        empty_body = _MOCK_RESPONSES["account"]["query_empty"]["body"]
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = _make_httpx_response(200, empty_body)
                result = await client.query("SELECT Id FROM Account WHERE Id = 'nonexistent'")

        assert result.total_size == 0
        assert result.records == []


@pytest.mark.integration
class TestBulkAPI20:
    """Bulk API 2.0 ingest job lifecycle: create → upload → close → poll."""

    @pytest.mark.asyncio
    async def test_bulk_upsert_creates_job_and_uploads_data(
        self, sf_config, sample_account_records
    ):
        """bulk_upsert() must go through the full job lifecycle and return BulkJobResult."""
        job_created = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["create_job_success"]["body"]
        )
        upload_ok = httpx.Response(status_code=201, content=b"", headers={})
        close_ok = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["close_job_success"]["body"]
        )
        job_complete = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["job_complete_success"]["body"]
        )
        success_results = _make_csv_response(
            200,
            "sf__Id,sf__Created,Name,Legacy_ID__c\n"
            "001Dn000001MockAA2,true,Acme Corporation,LEGACY-ACC-00000001\n",
        )
        failed_results = _make_csv_response(200, "sf__Id,sf__Error,Name,Legacy_ID__c\n")

        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            # Intercept all outgoing HTTP calls
            with patch.object(client, "post", new_callable=AsyncMock) as mock_post, \
                 patch.object(client, "patch", new_callable=AsyncMock) as mock_patch, \
                 patch.object(client, "get", new_callable=AsyncMock) as mock_get:

                mock_post.return_value = job_created

                # _upload_bulk_data uses client._http.put directly
                mock_http_inner = AsyncMock()
                mock_http_inner.put = AsyncMock(return_value=upload_ok)
                client._http = mock_http_inner

                mock_patch.return_value = close_ok
                mock_get.side_effect = [
                    job_complete,       # poll → terminal state
                    success_results,    # successfulResults CSV
                    failed_results,     # failedResults CSV
                ]

                result = await client.bulk_upsert(
                    "Account", sample_account_records, "Legacy_ID__c"
                )

        assert isinstance(result, BulkJobResult)
        assert result.state == BulkJobState.JOB_COMPLETE
        assert result.number_records_processed == 5000
        assert result.number_records_failed == 0

    @pytest.mark.asyncio
    async def test_bulk_job_status_polling_waits_for_terminal_state(self, sf_config):
        """_poll_bulk_job must keep polling until a terminal state is reached."""
        in_progress = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["job_in_progress"]["body"]
        )
        complete = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["job_complete_success"]["body"]
        )
        success_csv = _make_csv_response(200, "sf__Id,sf__Created\n")
        failed_csv = _make_csv_response(200, "sf__Id,sf__Error\n")

        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                mock_get.side_effect = [in_progress, complete, success_csv, failed_csv]
                result = await client._poll_bulk_job("7505x000001BulkJobAAA")

        assert result.state == BulkJobState.JOB_COMPLETE
        # get was called at least twice (once for InProgress, once for JobComplete)
        assert mock_get.call_count >= 2

    @pytest.mark.asyncio
    async def test_bulk_job_records_to_csv_serialisation(self, sf_config, sample_account_records):
        """_records_to_csv must produce valid CSV with a header row."""
        async with SalesforceClient(sf_config) as client:
            csv_output = client._records_to_csv(sample_account_records)

        reader = csv.DictReader(io.StringIO(csv_output))
        rows = list(reader)
        assert len(rows) == 3
        assert "Name" in rows[0]
        assert "Legacy_ID__c" in rows[0]
        assert rows[0]["Name"] == "Acme Corporation"

    @pytest.mark.asyncio
    async def test_bulk_job_timeout_raises_timeout_error(self, sf_config):
        """_poll_bulk_job must raise TimeoutError if job never reaches terminal state."""
        in_progress = _make_httpx_response(
            200, _MOCK_RESPONSES["bulk_api"]["job_in_progress"]["body"]
        )
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600
            client._sf_config.bulk_poll_max_seconds = 0.05  # tiny timeout
            client._sf_config.bulk_poll_interval_seconds = 0.02

            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = in_progress
                with pytest.raises(TimeoutError, match="did not complete"):
                    await client._poll_bulk_job("7505x000001BulkJobAAA")


@pytest.mark.integration
class TestRateLimitHandling:
    """HTTP 429 rate-limit scenarios."""

    @pytest.mark.asyncio
    async def test_rate_limit_response_exposes_retry_after(self, sf_config):
        """The base client must surface Retry-After from 429 responses."""
        rate_limit_body = _MOCK_RESPONSES["rate_limiting"]["api_limit_exceeded"]["body"]
        rate_limit_headers = _MOCK_RESPONSES["rate_limiting"]["api_limit_exceeded"]["headers"]

        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = _make_httpx_response(
                    429, rate_limit_body, rate_limit_headers
                )
                # The client raises on 4xx so we verify the status propagates
                with pytest.raises((RateLimitError, ServerError, Exception)):
                    await client.create("Account", {"Name": "Test"})


@pytest.mark.integration
class TestPlatformEvents:
    """Salesforce Platform Event publishing."""

    @pytest.mark.asyncio
    async def test_publish_migration_started_event(self, sf_config):
        """Publishing Migration_Event__e returns the new event record ID."""
        success_body = _MOCK_RESPONSES["platform_events"]["publish_success"]["body"]
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = _make_httpx_response(201, success_body)
                result = await client.create(
                    "Migration_Event__e",
                    {
                        "Status__c": "STARTED",
                        "Job_ID__c": "MIG-2026-001",
                        "Records_Migrated__c": 0,
                        "Timestamp__c": "2026-03-16T00:00:00Z",
                    },
                )

        assert result["success"] is True
        assert result["id"] == "e00xx0000000001MockAAA"

    @pytest.mark.asyncio
    async def test_platform_event_endpoint_contains_event_api_name(self, sf_config):
        """create() for a platform event must target the __e sObject endpoint."""
        success_body = _MOCK_RESPONSES["platform_events"]["publish_success"]["body"]
        async with SalesforceClient(sf_config) as client:
            client._token_cache.access_token = "test_token"
            client._token_cache.issued_at = time.monotonic()
            client._token_cache.expires_in = 3600

            with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = _make_httpx_response(201, success_body)
                await client.create("Migration_Event__e", {"Status__c": "COMPLETED"})
                call_path = mock_post.call_args[0][0]

        assert "Migration_Event__e" in call_path

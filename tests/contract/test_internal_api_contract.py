"""
Contract tests for the internal Migration REST API.

Ensures backward compatibility of the API across releases and validates
compliance with the OpenAPI specification defined in migration_routes.py.

Strategy:
  - Use FastAPI's TestClient to exercise the real route handlers
  - Assert response status codes, field names, and types match the contract
  - Test state transitions via HTTP (pause, resume, cancel)
  - Test backward-compat: v1 endpoints must keep working after any v2 changes
  - Validate pagination envelope shape is consistent across all list endpoints

Marks: @pytest.mark.contract
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Import the real FastAPI application factory and routes
# ---------------------------------------------------------------------------
from integrations.api_gateway.routes.migration_routes import (
    _RUNS_STORE,
    _BATCHES_STORE,
    _RECORDS_STORE,
    router as migration_router,
    RunStatus,
)
from integrations.api_gateway.routes.health_routes import router as health_router


# ---------------------------------------------------------------------------
# Minimal test app (strips JWT auth middleware for contract tests)
# ---------------------------------------------------------------------------

def _create_test_app() -> FastAPI:
    """
    Build a stripped-down FastAPI app with migration routes and no auth
    middleware, suitable for contract testing in isolation.
    """
    app = FastAPI(title="Migration API - Contract Test", version="1.0.0")
    app.include_router(health_router)
    app.include_router(migration_router, prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Response contract definitions
# ---------------------------------------------------------------------------

CONTRACT_MIGRATION_RUN_RESPONSE = {
    "required_fields": [
        "run_id", "status", "object_types", "dry_run",
        "batch_size", "processed_records", "successful_records",
        "failed_records", "skipped_records", "created_at",
    ],
    "field_types": {
        "run_id": str,
        "status": str,
        "object_types": list,
        "dry_run": bool,
        "batch_size": int,
        "processed_records": int,
        "successful_records": int,
        "failed_records": int,
        "skipped_records": int,
    },
    "valid_statuses": {"pending", "running", "paused", "completed", "cancelled", "failed"},
}

CONTRACT_PAGINATED_RESPONSE = {
    "required_fields": ["items", "total", "page", "page_size", "total_pages", "has_next", "has_prev"],
    "field_types": {
        "items": list,
        "total": int,
        "page": int,
        "page_size": int,
        "total_pages": int,
        "has_next": bool,
        "has_prev": bool,
    },
}

CONTRACT_HEALTH_RESPONSE = {
    "required_fields": ["status"],
    "valid_statuses": {"healthy", "degraded", "unhealthy"},
}

CONTRACT_ERROR_RESPONSE = {
    "required_fields": ["detail"],
}

CONTRACT_START_MIGRATION_REQUEST = {
    "required_fields": ["object_types"],
    "optional_fields": ["batch_size", "dry_run", "filter_query", "priority", "notify_on_completion", "schedule_at"],
    "batch_size_range": (1, 10000),
    "priority_range": (1, 10),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_contract_fields(body: Dict, contract: Dict, label: str = "") -> None:
    for field in contract.get("required_fields", []):
        assert field in body, f"[{label}] Required field '{field}' missing from response"
    for field, t in contract.get("field_types", {}).items():
        if field in body:
            assert isinstance(body[field], t), (
                f"[{label}] '{field}' expected {t.__name__}, got {type(body[field]).__name__}"
            )


def _seed_run(status: str = "running") -> str:
    """Directly inject a migration run into the in-memory store for state tests."""
    run_id = str(uuid.uuid4())
    _RUNS_STORE[run_id] = {
        "run_id": run_id,
        "status": status,
        "object_types": ["Account"],
        "dry_run": False,
        "batch_size": 200,
        "processed_records": 0,
        "successful_records": 0,
        "failed_records": 0,
        "skipped_records": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "priority": 5,
    }
    return run_id


# ===========================================================================
# FIXTURES
# ===========================================================================


@pytest.fixture(autouse=True)
def clear_stores():
    """Isolate each test by clearing the in-memory stores."""
    _RUNS_STORE.clear()
    _BATCHES_STORE.clear()
    _RECORDS_STORE.clear()
    yield
    _RUNS_STORE.clear()
    _BATCHES_STORE.clear()
    _RECORDS_STORE.clear()


@pytest.fixture
def client() -> TestClient:
    app = _create_test_app()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def running_run_id() -> str:
    return _seed_run("running")


@pytest.fixture
def paused_run_id() -> str:
    return _seed_run("paused")


@pytest.fixture
def pending_run_id() -> str:
    return _seed_run("pending")


@pytest.fixture
def completed_run_id() -> str:
    return _seed_run("completed")


# ===========================================================================
# CONTRACT TESTS
# ===========================================================================


@pytest.mark.contract
class TestCreateMigrationRunContract:
    """POST /api/v1/migrations/runs — create a new migration job."""

    def test_create_run_returns_202_accepted(self, client):
        resp = client.post(
            "/api/v1/migrations/runs",
            json={"object_types": ["Account"], "batch_size": 200, "dry_run": False},
        )
        assert resp.status_code == 202

    def test_create_run_response_shape_matches_contract(self, client):
        resp = client.post(
            "/api/v1/migrations/runs",
            json={"object_types": ["Account", "Contact"]},
        )
        body = resp.json()
        _assert_contract_fields(body, CONTRACT_MIGRATION_RUN_RESPONSE, "CreateRun")

    def test_create_run_id_is_uuid_format(self, client):
        resp = client.post("/api/v1/migrations/runs", json={"object_types": ["Account"]})
        run_id = resp.json()["run_id"]
        parsed = uuid.UUID(run_id)
        assert str(parsed) == run_id

    def test_create_run_status_is_pending(self, client):
        resp = client.post("/api/v1/migrations/runs", json={"object_types": ["Account"]})
        assert resp.json()["status"] == "pending"

    def test_create_run_preserves_object_types(self, client):
        types = ["Account", "Contact", "Opportunity"]
        resp = client.post("/api/v1/migrations/runs", json={"object_types": types})
        assert resp.json()["object_types"] == types

    def test_create_run_dry_run_flag_preserved(self, client):
        resp = client.post(
            "/api/v1/migrations/runs", json={"object_types": ["Account"], "dry_run": True}
        )
        assert resp.json()["dry_run"] is True

    def test_create_run_default_batch_size(self, client):
        resp = client.post("/api/v1/migrations/runs", json={"object_types": ["Account"]})
        assert resp.json()["batch_size"] == 200

    def test_create_run_missing_object_types_returns_422(self, client):
        resp = client.post("/api/v1/migrations/runs", json={"batch_size": 100})
        assert resp.status_code == 422

    def test_create_run_batch_size_too_large_returns_422(self, client):
        resp = client.post(
            "/api/v1/migrations/runs",
            json={"object_types": ["Account"], "batch_size": 99999},
        )
        assert resp.status_code == 422

    def test_create_run_batch_size_zero_returns_422(self, client):
        resp = client.post(
            "/api/v1/migrations/runs", json={"object_types": ["Account"], "batch_size": 0}
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("priority", [1, 5, 10])
    def test_create_run_valid_priorities(self, client, priority):
        resp = client.post(
            "/api/v1/migrations/runs",
            json={"object_types": ["Account"], "priority": priority},
        )
        assert resp.status_code == 202

    def test_create_run_priority_out_of_range_returns_422(self, client):
        resp = client.post(
            "/api/v1/migrations/runs",
            json={"object_types": ["Account"], "priority": 11},
        )
        assert resp.status_code == 422


@pytest.mark.contract
class TestGetMigrationRunContract:
    """GET /api/v1/migrations/runs/{run_id} — retrieve run details."""

    def test_get_run_returns_200(self, client, running_run_id):
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        assert resp.status_code == 200

    def test_get_run_response_matches_contract(self, client, running_run_id):
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        _assert_contract_fields(resp.json(), CONTRACT_MIGRATION_RUN_RESPONSE, "GetRun")

    def test_get_run_id_matches_requested_id(self, client, running_run_id):
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        assert resp.json()["run_id"] == running_run_id

    def test_get_nonexistent_run_returns_404(self, client):
        resp = client.get(f"/api/v1/migrations/runs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_404_response_contains_detail(self, client):
        resp = client.get(f"/api/v1/migrations/runs/{uuid.uuid4()}")
        body = resp.json()
        assert "detail" in body

    def test_get_run_status_is_valid_enum_value(self, client, running_run_id):
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        status = resp.json()["status"]
        assert status in CONTRACT_MIGRATION_RUN_RESPONSE["valid_statuses"]


@pytest.mark.contract
class TestListMigrationRunsContract:
    """GET /api/v1/migrations/runs — paginated list of migration runs."""

    def test_list_runs_returns_200(self, client):
        resp = client.get("/api/v1/migrations/runs")
        assert resp.status_code == 200

    def test_list_runs_response_is_paginated_envelope(self, client):
        resp = client.get("/api/v1/migrations/runs")
        _assert_contract_fields(resp.json(), CONTRACT_PAGINATED_RESPONSE, "ListRuns")

    def test_list_runs_empty_store_returns_zero_total(self, client):
        resp = client.get("/api/v1/migrations/runs")
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    def test_list_runs_has_prev_false_on_first_page(self, client):
        resp = client.get("/api/v1/migrations/runs?page=1")
        assert resp.json()["has_prev"] is False

    def test_list_runs_pagination_page_field(self, client):
        resp = client.get("/api/v1/migrations/runs?page=2&page_size=10")
        assert resp.json()["page"] == 2
        assert resp.json()["page_size"] == 10

    def test_list_runs_status_filter(self, client):
        _seed_run("running")
        _seed_run("paused")
        _seed_run("completed")
        resp = client.get("/api/v1/migrations/runs?status=running")
        body = resp.json()
        for item in body["items"]:
            assert item["status"] == "running"

    def test_list_runs_invalid_page_size_returns_422(self, client):
        resp = client.get("/api/v1/migrations/runs?page_size=0")
        assert resp.status_code == 422

    def test_list_runs_page_size_over_100_returns_422(self, client):
        resp = client.get("/api/v1/migrations/runs?page_size=101")
        assert resp.status_code == 422

    def test_list_runs_with_multiple_runs(self, client):
        for _ in range(5):
            _seed_run("running")
        resp = client.get("/api/v1/migrations/runs")
        assert resp.json()["total"] == 5
        assert len(resp.json()["items"]) == 5


@pytest.mark.contract
class TestPauseMigrationRunContract:
    """POST /api/v1/migrations/runs/{id}/pause — pause a running job."""

    def test_pause_running_run_returns_202(self, client, running_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{running_run_id}/pause")
        assert resp.status_code == 202

    def test_pause_updates_status_to_paused(self, client, running_run_id):
        client.post(f"/api/v1/migrations/runs/{running_run_id}/pause")
        status_resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        assert status_resp.json()["status"] == "paused"

    def test_pause_response_includes_run_id(self, client, running_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{running_run_id}/pause")
        assert resp.json()["run_id"] == running_run_id

    def test_pause_already_paused_returns_409(self, client, paused_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{paused_run_id}/pause")
        assert resp.status_code == 409

    def test_pause_nonexistent_run_returns_404(self, client):
        resp = client.post(f"/api/v1/migrations/runs/{uuid.uuid4()}/pause")
        assert resp.status_code == 404

    def test_pause_completed_run_returns_409(self, client, completed_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{completed_run_id}/pause")
        assert resp.status_code == 409


@pytest.mark.contract
class TestResumeMigrationRunContract:
    """POST /api/v1/migrations/runs/{id}/resume — resume a paused job."""

    def test_resume_paused_run_returns_202(self, client, paused_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{paused_run_id}/resume")
        assert resp.status_code == 202

    def test_resume_updates_status_to_running(self, client, paused_run_id):
        client.post(f"/api/v1/migrations/runs/{paused_run_id}/resume")
        status_resp = client.get(f"/api/v1/migrations/runs/{paused_run_id}")
        assert status_resp.json()["status"] == "running"

    def test_resume_running_run_returns_409(self, client, running_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{running_run_id}/resume")
        assert resp.status_code == 409

    def test_resume_nonexistent_run_returns_404(self, client):
        resp = client.post(f"/api/v1/migrations/runs/{uuid.uuid4()}/resume")
        assert resp.status_code == 404


@pytest.mark.contract
class TestCancelMigrationRunContract:
    """POST /api/v1/migrations/runs/{id}/cancel — cancel a job."""

    @pytest.mark.parametrize("seed_status", ["pending", "running", "paused"])
    def test_cancel_cancelable_run_returns_202(self, client, seed_status):
        run_id = _seed_run(seed_status)
        resp = client.post(f"/api/v1/migrations/runs/{run_id}/cancel")
        assert resp.status_code == 202

    def test_cancel_sets_status_to_cancelled(self, client, running_run_id):
        client.post(f"/api/v1/migrations/runs/{running_run_id}/cancel")
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        assert resp.json()["status"] == "cancelled"

    def test_cancel_already_completed_returns_409(self, client, completed_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{completed_run_id}/cancel")
        assert resp.status_code == 409

    def test_cancel_already_cancelled_returns_409(self, client):
        run_id = _seed_run("cancelled")
        resp = client.post(f"/api/v1/migrations/runs/{run_id}/cancel")
        assert resp.status_code == 409


@pytest.mark.contract
class TestMigrationStatsContract:
    """GET /api/v1/migrations/stats — organisation-wide statistics."""

    def test_stats_returns_200(self, client):
        resp = client.get("/api/v1/migrations/stats")
        assert resp.status_code == 200

    def test_stats_response_has_required_fields(self, client):
        resp = client.get("/api/v1/migrations/stats")
        body = resp.json()
        required = [
            "total_runs", "active_runs", "total_records_migrated",
            "total_records_failed", "overall_success_rate",
            "avg_throughput_per_hour", "top_error_categories",
            "by_object_type", "last_updated",
        ]
        for field in required:
            assert field in body, f"Stats field '{field}' missing"

    def test_stats_counts_active_runs(self, client):
        _seed_run("running")
        _seed_run("running")
        _seed_run("completed")
        resp = client.get("/api/v1/migrations/stats")
        assert resp.json()["active_runs"] == 2
        assert resp.json()["total_runs"] == 3


@pytest.mark.contract
class TestHealthEndpointContract:
    """GET /health — Kubernetes liveness/readiness probe endpoint."""

    def test_health_returns_200_or_503(self, client):
        resp = client.get("/health")
        assert resp.status_code in {200, 503}

    def test_health_response_has_status_field(self, client):
        resp = client.get("/health")
        assert "status" in resp.json()

    def test_liveness_probe_returns_200(self, client):
        """Liveness probe endpoint must always return 200 if the process is alive."""
        resp = client.get("/health/live")
        assert resp.status_code == 200

    def test_readiness_probe_structure(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code in {200, 503}


@pytest.mark.contract
class TestAPIBackwardCompatibility:
    """
    Backward compatibility tests: v1 contracts must remain stable.
    These tests encode the current API shape so that a refactor that breaks
    the contract is caught before deployment.
    """

    def test_create_run_endpoint_path_is_stable(self, client):
        """The v1 create run endpoint must remain at /api/v1/migrations/runs."""
        resp = client.post("/api/v1/migrations/runs", json={"object_types": ["Account"]})
        assert resp.status_code in {200, 201, 202}, "v1 create endpoint path has changed"

    def test_get_run_endpoint_path_is_stable(self, client, running_run_id):
        """The v1 get run endpoint must remain at /api/v1/migrations/runs/{id}."""
        resp = client.get(f"/api/v1/migrations/runs/{running_run_id}")
        assert resp.status_code == 200, "v1 get run endpoint path has changed"

    def test_run_id_field_name_is_stable(self, client):
        """The field 'run_id' in migration run responses must not be renamed."""
        resp = client.post("/api/v1/migrations/runs", json={"object_types": ["Account"]})
        assert "run_id" in resp.json(), "'run_id' field renamed — breaking change"

    def test_pagination_envelope_field_names_are_stable(self, client):
        """Pagination field names must remain stable across API versions."""
        resp = client.get("/api/v1/migrations/runs")
        body = resp.json()
        stable_fields = ["items", "total", "page", "page_size", "total_pages", "has_next", "has_prev"]
        for field in stable_fields:
            assert field in body, f"Pagination field '{field}' renamed — breaking change"

    def test_status_enum_values_are_stable(self, client):
        """Migration run status values are part of the public API contract."""
        stable_statuses = {"pending", "running", "paused", "completed", "cancelled", "failed"}
        actual_statuses = {v.value for v in RunStatus}
        missing = stable_statuses - actual_statuses
        assert missing == set(), f"Status enum values removed — breaking change: {missing}"

    def test_pause_endpoint_path_is_stable(self, client, running_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{running_run_id}/pause")
        assert resp.status_code in {200, 202}, "v1 pause endpoint path has changed"

    def test_resume_endpoint_path_is_stable(self, client, paused_run_id):
        resp = client.post(f"/api/v1/migrations/runs/{paused_run_id}/resume")
        assert resp.status_code in {200, 202}, "v1 resume endpoint path has changed"

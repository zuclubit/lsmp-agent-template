#!/usr/bin/env python3
"""
mock_sf_server.py — Local mock Salesforce API server (port 9001).

Implements the minimum subset of Salesforce Bulk API 2.0 and REST API
needed for the LSMP migration pipeline to run in dry-run / demo mode.

Endpoints:
  POST /services/oauth2/token         → OAuth2 token (always succeeds)
  GET  /services/data/v59.0/          → API version list
  GET  /services/data/v59.0/limits    → Org API limits
  POST /services/data/v59.0/jobs/ingest → Create Bulk API job
  GET  /services/data/v59.0/jobs/ingest/{job_id} → Job status
  PUT  /services/data/v59.0/jobs/ingest/{job_id}/batches → Upload CSV
  PATCH /services/data/v59.0/jobs/ingest/{job_id} → Close job
  GET  /services/data/v59.0/jobs/ingest/{job_id}/successfulResults → Success results
  GET  /services/data/v59.0/jobs/ingest/{job_id}/failedResults → Failed results
  GET  /api/v1/migrations/runs/{run_id} → Internal migration API
  GET  /api/v1/health                   → Health check

Usage:
    python3 demo/mock_sf_server.py
    # or
    make mock-sf
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Install fastapi/uvicorn if not available
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse, PlainTextResponse
    import uvicorn
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "fastapi", "uvicorn", "--quiet"])
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse, PlainTextResponse
    import uvicorn

# ---------------------------------------------------------------------------
# State (in-memory for demo)
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_job_results: Dict[str, List[Dict[str, Any]]] = {}

app = FastAPI(
    title="Mock Salesforce API",
    description="Local mock of Salesforce Bulk API 2.0 + REST API for LSMP demo",
    version="59.0",
)

MOCK_INSTANCE_URL = "http://localhost:9001"
MOCK_ACCESS_TOKEN = "mock-sf-access-token-00D000000000001AAA"

def _sf_id(prefix: str = "0") -> str:
    """Generate a fake 18-char Salesforce ID."""
    chars = string.ascii_letters + string.digits
    return prefix + "0D" + "".join(random.choices(chars, k=15))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _log(method: str, path: str, status: int = 200) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    color = "\033[32m" if status < 400 else "\033[31m"
    print(f"  [{ts}] {color}{method:6}\033[0m {path} → {status}")


# ---------------------------------------------------------------------------
# OAuth2
# ---------------------------------------------------------------------------

@app.post("/services/oauth2/token")
async def oauth_token(request: Request):
    _log("POST", "/services/oauth2/token")
    return JSONResponse({
        "access_token":  MOCK_ACCESS_TOKEN,
        "instance_url":  MOCK_INSTANCE_URL,
        "id":            f"{MOCK_INSTANCE_URL}/id/00D000000000001AAA/005000000000001AAA",
        "token_type":    "Bearer",
        "issued_at":     str(int(time.time() * 1000)),
        "signature":     "mock-signature",
    })


# ---------------------------------------------------------------------------
# REST API metadata
# ---------------------------------------------------------------------------

@app.get("/services/data/")
async def api_versions():
    _log("GET", "/services/data/")
    return JSONResponse([{"label": "Winter '25", "url": "/services/data/v59.0", "version": "59.0"}])


@app.get("/services/data/v59.0/")
async def api_root():
    _log("GET", "/services/data/v59.0/")
    return JSONResponse({
        "sobjects": f"{MOCK_INSTANCE_URL}/services/data/v59.0/sobjects",
        "limits":   f"{MOCK_INSTANCE_URL}/services/data/v59.0/limits",
        "jobs":     f"{MOCK_INSTANCE_URL}/services/data/v59.0/jobs",
    })


@app.get("/services/data/v59.0/limits")
async def api_limits():
    _log("GET", "/services/data/v59.0/limits")
    return JSONResponse({
        "DailyApiRequests":         {"Max": 15000, "Remaining": 14850},
        "DailyBulkApiRequests":     {"Max": 10000, "Remaining":  9980},
        "DailyBulkV2QueryFileStorageMB": {"Max": 10240, "Remaining": 10240},
        "ConcurrentAsyncGetReportInstances": {"Max": 200, "Remaining": 200},
        "HourlyTimeBasedWorkflow":  {"Max": 1000, "Remaining": 1000},
        "DataStorageMB":            {"Max": 5000, "Remaining": 4920},
    })


# ---------------------------------------------------------------------------
# Bulk API 2.0 — Ingest Jobs
# ---------------------------------------------------------------------------

@app.post("/services/data/v59.0/jobs/ingest")
async def create_job(request: Request):
    body = await request.json()
    job_id = "750" + "".join(random.choices(string.ascii_letters + string.digits, k=15))
    job = {
        "id":                  job_id,
        "state":               "Open",
        "object":              body.get("object", "Account"),
        "operation":           body.get("operation", "insert"),
        "contentType":         "CSV",
        "externalIdFieldName": body.get("externalIdFieldName", ""),
        "apiVersion":          59.0,
        "createdDate":         _now_iso(),
        "systemModstamp":      _now_iso(),
        "contentUrl":          f"/services/data/v59.0/jobs/ingest/{job_id}/batches",
    }
    _jobs[job_id] = job
    _job_results[job_id] = []
    _log("POST", "/services/data/v59.0/jobs/ingest")
    return JSONResponse(job, status_code=200)


@app.get("/services/data/v59.0/jobs/ingest/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        _log("GET", f"/services/data/v59.0/jobs/ingest/{job_id}", 404)
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(_jobs[job_id])
    # Simulate completion if closed
    if job["state"] == "UploadComplete":
        job["state"] = "JobComplete"
        job["numberRecordsProcessed"] = len(_job_results.get(job_id, []))
        job["numberRecordsFailed"] = 0
        _jobs[job_id] = job
    _log("GET", f"/services/data/v59.0/jobs/ingest/{job_id}")
    return JSONResponse(job)


@app.put("/services/data/v59.0/jobs/ingest/{job_id}/batches")
async def upload_batch(job_id: str, request: Request):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    body = await request.body()
    csv_text = body.decode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    # Simulate processing: 98% success rate
    results = []
    for row in rows:
        success = random.random() > 0.02
        record_id = _sf_id("001") if _jobs[job_id]["object"] == "Account" \
                    else _sf_id("003") if _jobs[job_id]["object"] == "Contact" \
                    else _sf_id("006")
        results.append({
            "sf_id":        record_id if success else "",
            "success":      success,
            "created":      success,
            "error":        "" if success else "FIELD_CUSTOM_VALIDATION_EXCEPTION: Demo error",
            "legacy_id":    row.get("Legacy_ID__c", row.get("acct_id", "")),
        })
    _job_results[job_id] = results
    _jobs[job_id]["state"] = "UploadComplete"
    _jobs[job_id]["numberRecordsProcessed"] = len(results)
    _jobs[job_id]["numberRecordsFailed"] = sum(1 for r in results if not r["success"])
    _log("PUT", f"/services/data/v59.0/jobs/ingest/{job_id}/batches")
    return Response(status_code=201)


@app.patch("/services/data/v59.0/jobs/ingest/{job_id}")
async def close_job(job_id: str, request: Request):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    body = await request.json()
    if body.get("state") == "UploadComplete":
        _jobs[job_id]["state"] = "UploadComplete"
    elif body.get("state") == "Aborted":
        _jobs[job_id]["state"] = "Aborted"
    _log("PATCH", f"/services/data/v59.0/jobs/ingest/{job_id}")
    return JSONResponse(_jobs[job_id])


@app.get("/services/data/v59.0/jobs/ingest/{job_id}/successfulResults")
async def successful_results(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    results = [r for r in _job_results.get(job_id, []) if r["success"]]
    output = io.StringIO()
    if results:
        writer = csv.DictWriter(output, fieldnames=["sf__Id", "sf__Created", "Legacy_ID__c"])
        writer.writeheader()
        for r in results:
            writer.writerow({"sf__Id": r["sf_id"], "sf__Created": "true", "Legacy_ID__c": r["legacy_id"]})
    _log("GET", f"/services/data/v59.0/jobs/ingest/{job_id}/successfulResults")
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.get("/services/data/v59.0/jobs/ingest/{job_id}/failedResults")
async def failed_results(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    results = [r for r in _job_results.get(job_id, []) if not r["success"]]
    output = io.StringIO()
    if results:
        writer = csv.DictWriter(output, fieldnames=["sf__Id", "sf__Error", "Legacy_ID__c"])
        writer.writeheader()
        for r in results:
            writer.writerow({"sf__Id": "", "sf__Error": r["error"], "Legacy_ID__c": r["legacy_id"]})
    _log("GET", f"/services/data/v59.0/jobs/ingest/{job_id}/failedResults")
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


# ---------------------------------------------------------------------------
# Internal Migration Control Plane API (mimics http://localhost:8000)
# ---------------------------------------------------------------------------

_migration_runs: Dict[str, Any] = {}


@app.get("/api/v1/health")
async def health():
    return JSONResponse({"status": "ok", "service": "mock-sf-server", "timestamp": _now_iso()})


@app.get("/api/v1/migrations/runs/{run_id}")
async def get_migration_run(run_id: str):
    run = _migration_runs.get(run_id, {
        "run_id":              run_id,
        "status":              "RUNNING",
        "total_records":       200,
        "processed_records":   150,
        "successful_records":  147,
        "failed_records":      3,
        "error_rate":          2.0,
        "started_at":          _now_iso(),
    })
    _log("GET", f"/api/v1/migrations/runs/{run_id}")
    return JSONResponse(run)


@app.post("/api/v1/migrations/runs/{run_id}/pause")
async def pause_run(run_id: str):
    _log("POST", f"/api/v1/migrations/runs/{run_id}/pause")
    return JSONResponse({"run_id": run_id, "status": "PAUSED", "timestamp": _now_iso()})


@app.post("/api/v1/migrations/runs/{run_id}/resume")
async def resume_run(run_id: str):
    _log("POST", f"/api/v1/migrations/runs/{run_id}/resume")
    return JSONResponse({"run_id": run_id, "status": "RUNNING", "timestamp": _now_iso()})


@app.get("/api/v1/migrations/errors")
async def get_errors(run_id: str = ""):
    _log("GET", "/api/v1/migrations/errors")
    return JSONResponse({"errors": [], "total": 0, "run_id": run_id})


@app.get("/api/v1/integrations/salesforce/limits")
async def sf_limits():
    _log("GET", "/api/v1/integrations/salesforce/limits")
    return JSONResponse({
        "dailyApiRequests":     {"max": 15000, "remaining": 14850},
        "dailyBulkRequests":    {"max": 10000, "remaining": 9980},
        "dataStorageMB":        {"max": 5000,  "remaining": 4920},
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("MOCK_SF_PORT", "9001"))
    print()
    print("━━━ Mock Salesforce API Server ━━━")
    print(f"  URL:          http://localhost:{port}")
    print(f"  OAuth2:       POST /services/oauth2/token")
    print(f"  Bulk API:     POST /services/data/v59.0/jobs/ingest")
    print(f"  Migration API:GET  /api/v1/migrations/runs/{{run_id}}")
    print(f"  Health:       GET  /api/v1/health")
    print()
    print("  Set SF_MOCK_MODE=true in .env to route agents here.")
    print("  Press Ctrl+C to stop.")
    print()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

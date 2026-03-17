# code-generation Skill — Usage Examples

## Example 1 — Valid Invocation

**Scenario:** Generate a Salesforce Bulk API 2.0 batch upload helper.

**Input:**
```json
{
  "task_description": "Generate a Python function that uploads a list of Account records to Salesforce using Bulk API 2.0. The function must accept a list of dicts, a job_id, and a session token injected from the environment. It must not accept credentials as parameters.",
  "target_language": "python",
  "output_path": "agents/migration-agent/tools.py"
}
```

**Expected Output:**
```python
import os
import httpx
from typing import List, Dict

SALESFORCE_BASE_URL = os.environ["SALESFORCE_INSTANCE_URL"]

async def upload_bulk_batch(
    job_id: str,
    records: List[Dict],
    session_token: str | None = None,
) -> dict:
    """Upload a batch of records to an open Bulk API 2.0 job."""
    token = session_token or os.environ["SALESFORCE_SESSION_TOKEN"]
    url = f"{SALESFORCE_BASE_URL}/services/data/v59.0/jobs/ingest/{job_id}/batches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/csv",
    }
    csv_body = _records_to_csv(records)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(url, content=csv_body, headers=headers)
        resp.raise_for_status()
    return {"job_id": job_id, "batch_size": len(records), "status": "uploaded"}

def _records_to_csv(records: List[Dict]) -> str:
    import csv, io
    if not records:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=records[0].keys())
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()
```

**Result:** `ALLOWED` — output is clean, credentials come from environment, no unsafe imports.

---

## Example 2 — Blocked by Validation (Credential in Input)

**Scenario:** Developer accidentally pastes a live API key into the task description.

**Input:**
```json
{
  "task_description": "Connect to Salesforce. Use api_key='sk-ant-api03-EXAMPLE_KEY_REDACTED' to authenticate.",
  "target_language": "python",
  "output_path": "src/sf_client.py"
}
```

**Validation rule triggered:** `no_credential_exposure`
Pattern match: `api_key='sk-ant-api03-...'`

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Input contains a credential pattern matching rule 'no_credential_exposure'. Remove the credential and reference the environment variable instead.",
  "code": "SKILL_INPUT_BLOCKED"
}
```

**Result:** `BLOCKED` — input is rejected before any generation occurs.

---

## Example 3 — Edge Case: Output Path Outside Approved Directories

**Scenario:** A request attempts to write generated code to a secrets directory.

**Input:**
```json
{
  "task_description": "Generate a Vault token renewal script.",
  "target_language": "bash",
  "output_path": "/var/secrets/renew_token.sh"
}
```

**Validation rule triggered:** `output_path_enforcement.write_forbidden_paths`
Path `/var/secrets/renew_token.sh` is not under `src/`, `migration/`, or `agents/`.

**Expected Response:**
```json
{
  "status": "BLOCKED",
  "reason": "Output path '/var/secrets/renew_token.sh' is outside the approved write paths (src/, migration/, agents/). Specify a path within an approved directory.",
  "code": "SKILL_PATH_FORBIDDEN"
}
```

**Result:** `BLOCKED` — the path check fires before generation begins, preventing writes to sensitive locations.

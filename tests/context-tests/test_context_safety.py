"""
Context Injection Safety Tests — Phase 9, Security Suite.

Verifies that context loading, injection, and retention mechanisms correctly:
  - Strip credentials and sensitive paths from project/runtime contexts
  - Redact PII from runtime context before it reaches the model
  - Enforce token budget limits on context payloads
  - Require human approval for restricted-context access
  - Default to session-only retention (no cross-session persistence)
  - Filter runtime context to the allowed field allowlist
  - Write an audit log entry on every context load
  - Partially mask Salesforce IDs in runtime context
  - Redact phone numbers from error record context

Compliance: FedRAMP AU-2, FedRAMP AC-3, GDPR Art. 5(2), CUI SP-1.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from validation.layer import (
    ContextValidationConfig,
    OutputSanitizer,
    ValidationLayer,
    ValidationResult,
    SecurityBlockedError,
)


# ---------------------------------------------------------------------------
# Context types and stubs
# ---------------------------------------------------------------------------


class ContextType(str, Enum):
    PROJECT = "project-context"
    RUNTIME = "runtime-context"
    RESTRICTED = "restricted-context"
    PRODUCTION_SECRETS = "production-secrets"
    VAULT_CREDENTIALS = "vault-credentials"


RESTRICTED_CONTEXT_TYPES = {
    ContextType.RESTRICTED,
    ContextType.PRODUCTION_SECRETS,
    ContextType.VAULT_CREDENTIALS,
}


@dataclass
class ContextAuditEntry:
    """Audit record generated on every context load."""
    agent_name: str
    context_type: str
    token_count: int
    timestamp_utc: str
    fields_loaded: list[str]
    redactions_applied: int


@dataclass
class ContextLoadResult:
    """Result of loading and sanitizing a context payload."""
    filtered_context: dict[str, Any]
    token_count: int
    redactions_applied: int
    audit_entry: ContextAuditEntry
    persist_across_sessions: bool = False
    blocked: bool = False
    block_reason: str = ""


# ---------------------------------------------------------------------------
# Context loader
# ---------------------------------------------------------------------------

# Fields allowed from migration API runtime context
_ALLOWED_RUNTIME_FIELDS = [
    "run_id", "object_type", "status", "record_count", "error_rate",
    "start_time", "end_time", "environment", "tenant_id", "job_id",
    "migration_phase", "batch_size", "source_system", "target_org",
]

# Credential and secret patterns to strip from context before injection
_CONTEXT_CREDENTIAL_PATTERNS = [
    (re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"), "[REDACTED:ANTHROPIC_KEY]"),
    (re.compile(r"hvs\.[a-zA-Z0-9_\-]{20,}"), "[REDACTED:VAULT_TOKEN]"),
    (re.compile(r"vault://[^\s\"']+"), "[FILTERED]"),
    (re.compile(r"password\s*[:=]\s*['\"]?\S+['\"]?", re.IGNORECASE), "[REDACTED:PASSWORD]"),
    (re.compile(r"secret\s*[:=]\s*['\"]?\S+['\"]?", re.IGNORECASE), "[REDACTED:SECRET]"),
]

# PII patterns
_CONTEXT_PII_PATTERNS = [
    (re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "[SSN_REDACTED]"),
]

# SF ID partial mask (first 14 chars → **, last 4 visible)
_SF_ID_PATTERN = re.compile(r"\b([a-zA-Z0-9]{14})([a-zA-Z0-9]{4})\b")


def _sanitize_context_string(text: str) -> tuple[str, int]:
    """Strip credentials and PII from a context string. Returns (sanitized_text, redaction_count)."""
    count = 0
    for pattern, replacement in _CONTEXT_CREDENTIAL_PATTERNS:
        new_text, n = pattern.subn(replacement, text)
        if n > 0:
            text = new_text
            count += n
    for pattern, replacement in _CONTEXT_PII_PATTERNS:
        new_text, n = pattern.subn(replacement, text)
        if n > 0:
            text = new_text
            count += n
    return text, count


def _mask_sf_ids(text: str) -> str:
    def _mask(m: re.Match) -> str:
        return "**************" + m.group(2)
    return _SF_ID_PATTERN.sub(_mask, text)


class ContextLoader:
    """
    Loads, filters, sanitizes, and audits context payloads before injection into agents.

    Enforces:
    - Allowlist filtering of runtime context fields
    - Credential/PII stripping
    - Token budget enforcement
    - Restricted context approval gate
    - Session-only persistence
    - Audit log on every load
    """

    def __init__(
        self,
        max_tokens: int = 200_000,
        persist_across_sessions: bool = False,
        audit_log: Optional[list[ContextAuditEntry]] = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._persist_across_sessions = persist_across_sessions
        self._audit_log: list[ContextAuditEntry] = audit_log if audit_log is not None else []

    def load(
        self,
        context_type: ContextType,
        raw_context: dict[str, Any],
        agent_name: str,
        human_approved: bool = False,
    ) -> ContextLoadResult:
        """Load and sanitize a context payload for injection."""
        from datetime import datetime, timezone

        # Gate: restricted contexts require human approval
        if context_type in RESTRICTED_CONTEXT_TYPES and not human_approved:
            audit = ContextAuditEntry(
                agent_name=agent_name,
                context_type=context_type.value,
                token_count=0,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                fields_loaded=[],
                redactions_applied=0,
            )
            self._audit_log.append(audit)
            return ContextLoadResult(
                filtered_context={},
                token_count=0,
                redactions_applied=0,
                audit_entry=audit,
                persist_across_sessions=False,
                blocked=True,
                block_reason=f"RESTRICTED_CONTEXT: '{context_type.value}' requires human approval",
            )

        # Step 1: Filter to allowed fields (runtime context only)
        if context_type == ContextType.RUNTIME:
            filtered = {k: v for k, v in raw_context.items() if k in _ALLOWED_RUNTIME_FIELDS}
        else:
            # For project context, sanitize the whole thing
            filtered = dict(raw_context)

        # Step 2: Sanitize all string values
        total_redactions = 0
        sanitized: dict[str, Any] = {}
        for key, value in filtered.items():
            if isinstance(value, str):
                cleaned, n = _sanitize_context_string(value)
                # Also mask SF IDs
                cleaned = _mask_sf_ids(cleaned)
                sanitized[key] = cleaned
                total_redactions += n
            else:
                sanitized[key] = value

        # Step 3: Token budget enforcement (rough: 4 chars ≈ 1 token)
        import json
        context_json = json.dumps(sanitized)
        approx_tokens = len(context_json) // 4

        if approx_tokens > self._max_tokens:
            # Truncate: keep as many fields as fit
            truncated: dict[str, Any] = {}
            running_tokens = 0
            for k, v in sanitized.items():
                field_str = json.dumps({k: v})
                field_tokens = len(field_str) // 4
                if running_tokens + field_tokens <= self._max_tokens:
                    truncated[k] = v
                    running_tokens += field_tokens
                else:
                    break
            sanitized = truncated
            approx_tokens = running_tokens

        # Step 4: Audit log
        audit = ContextAuditEntry(
            agent_name=agent_name,
            context_type=context_type.value,
            token_count=approx_tokens,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            fields_loaded=list(sanitized.keys()),
            redactions_applied=total_redactions,
        )
        self._audit_log.append(audit)

        return ContextLoadResult(
            filtered_context=sanitized,
            token_count=approx_tokens,
            redactions_applied=total_redactions,
            audit_entry=audit,
            persist_across_sessions=self._persist_across_sessions,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_log() -> list[ContextAuditEntry]:
    return []


@pytest.fixture
def loader(audit_log: list[ContextAuditEntry]) -> ContextLoader:
    return ContextLoader(max_tokens=200_000, persist_across_sessions=False, audit_log=audit_log)


@pytest.fixture
def layer() -> ValidationLayer:
    return ValidationLayer()


# ---------------------------------------------------------------------------
# Test 1: Credentials stripped from project context
# ---------------------------------------------------------------------------


def test_credentials_stripped_from_project_context(loader: ContextLoader) -> None:
    """Project context with embedded password must have the credential filtered before injection."""
    raw_context = {
        "project_name": "LSMP Migration Platform",
        "db_connection": "postgresql://admin:S3cr3tPassw0rd@db.internal/prod",
        "environment": "production",
        "version": "1.4.2",
    }
    result = loader.load(
        context_type=ContextType.PROJECT,
        raw_context=raw_context,
        agent_name="orchestrator-agent",
    )
    assert not result.blocked
    # Password must not appear in the filtered context
    context_str = str(result.filtered_context)
    assert "S3cr3tPassw0rd" not in context_str, (
        "Embedded password must be stripped from project context before injection"
    )
    assert result.redactions_applied > 0


# ---------------------------------------------------------------------------
# Test 2: Vault paths redacted in context
# ---------------------------------------------------------------------------


def test_vault_paths_redacted_in_context(loader: ContextLoader) -> None:
    """vault:// paths in context must be replaced with [FILTERED]."""
    raw_context = {
        "secret_path": "vault://secret/migration/prod/salesforce_token",
        "run_id": "run-001",
        "environment": "production",
    }
    result = loader.load(
        context_type=ContextType.PROJECT,
        raw_context=raw_context,
        agent_name="migration-agent",
    )
    assert not result.blocked
    context_str = str(result.filtered_context)
    assert "vault://" not in context_str, "vault:// path must be redacted"
    assert "[FILTERED]" in context_str


# ---------------------------------------------------------------------------
# Test 3: Raw PII stripped from runtime context error reports
# ---------------------------------------------------------------------------


def test_raw_pii_stripped_from_runtime_context(loader: ContextLoader) -> None:
    """Email address in error_report field of runtime context must be redacted."""
    raw_context = {
        "run_id": "run-pii-001",
        "object_type": "Contact",
        "status": "failed",
        "error_rate": 0.12,
        "environment": "production",
        "tenant_id": "tenant-001",
        # Non-allowlist field — will be stripped by runtime filter
        "error_details": "Record owner email: john.doe@agency.gov caused mapping failure",
    }
    result = loader.load(
        context_type=ContextType.RUNTIME,
        raw_context=raw_context,
        agent_name="validation-agent",
    )
    assert not result.blocked
    context_str = str(result.filtered_context)
    # email_details is not in allowlist — must be excluded
    assert "john.doe@agency.gov" not in context_str
    # run_id, status, etc. should be present (allowlisted)
    assert "run-pii-001" in context_str


# ---------------------------------------------------------------------------
# Test 4: Context token limit enforced
# ---------------------------------------------------------------------------


def test_context_token_limit_enforced() -> None:
    """Context exceeding max_tokens must be truncated, not silently passed in full."""
    small_loader = ContextLoader(max_tokens=10, persist_across_sessions=False)
    # Build a large context that far exceeds 10 tokens
    raw_context = {f"field_{i}": "x" * 200 for i in range(100)}  # ~50K chars
    result = small_loader.load(
        context_type=ContextType.PROJECT,
        raw_context=raw_context,
        agent_name="orchestrator-agent",
    )
    assert not result.blocked
    # Token count must be within the limit
    assert result.token_count <= 10, (
        f"Context must be truncated to max_tokens=10, got {result.token_count} tokens"
    )
    # The full 100-field context must not be present
    assert len(result.filtered_context) < 100, (
        "Context must be truncated — not all 100 fields should be present"
    )


# ---------------------------------------------------------------------------
# Test 5: Restricted context denied without approval
# ---------------------------------------------------------------------------


def test_restricted_context_denied_without_approval(loader: ContextLoader) -> None:
    """validation-agent requesting restricted-context without approval must be BLOCKED."""
    raw_context = {"classified_data": "TOP SECRET migration credentials"}
    result = loader.load(
        context_type=ContextType.RESTRICTED,
        raw_context=raw_context,
        agent_name="validation-agent",
        human_approved=False,
    )
    assert result.blocked, (
        "Expected blocked=True for restricted-context without human approval"
    )
    assert "RESTRICTED_CONTEXT" in result.block_reason
    assert result.filtered_context == {}


def test_restricted_context_allowed_with_approval(loader: ContextLoader) -> None:
    """Restricted context with human_approved=True must NOT be blocked."""
    raw_context = {"migration_plan": "approved execution steps"}
    result = loader.load(
        context_type=ContextType.RESTRICTED,
        raw_context=raw_context,
        agent_name="orchestrator-agent",
        human_approved=True,
    )
    assert not result.blocked, (
        f"Expected not blocked with human approval. Block reason: {result.block_reason}"
    )


# ---------------------------------------------------------------------------
# Test 6: Context retention is session-only by default
# ---------------------------------------------------------------------------


def test_context_retention_session_only(loader: ContextLoader) -> None:
    """Default context load must have persist_across_sessions=False."""
    result = loader.load(
        context_type=ContextType.RUNTIME,
        raw_context={"run_id": "run-001", "status": "running"},
        agent_name="migration-agent",
    )
    assert result.persist_across_sessions is False, (
        "Context must default to session-only retention (persist_across_sessions=False)"
    )


def test_cross_session_loader_persists(audit_log: list[ContextAuditEntry]) -> None:
    """When explicitly configured, persist_across_sessions should be True."""
    persistent_loader = ContextLoader(
        max_tokens=200_000,
        persist_across_sessions=True,
        audit_log=audit_log,
    )
    result = persistent_loader.load(
        context_type=ContextType.RUNTIME,
        raw_context={"run_id": "run-002", "status": "complete"},
        agent_name="documentation-agent",
    )
    # When explicitly set, it should persist
    assert result.persist_across_sessions is True


# ---------------------------------------------------------------------------
# Test 7: Runtime context fields allowlist enforced
# ---------------------------------------------------------------------------


def test_runtime_context_fields_allowlist(loader: ContextLoader) -> None:
    """Only whitelisted fields from migration API appear in runtime context."""
    raw_context = {
        # Allowlisted fields
        "run_id": "run-007",
        "object_type": "Account",
        "status": "running",
        "record_count": 100000,
        "error_rate": 0.02,
        "environment": "production",
        "tenant_id": "tenant-gov-001",
        # NON-allowlisted fields — must be stripped
        "internal_db_password": "s3cr3t!",
        "raw_salesforce_token": "00D0b000000AbCdE!SomeToken",
        "debug_stack_trace": "Exception in thread main...",
        "service_account_key": "super-secret-key-12345",
    }
    result = loader.load(
        context_type=ContextType.RUNTIME,
        raw_context=raw_context,
        agent_name="orchestrator-agent",
    )
    assert not result.blocked
    filtered = result.filtered_context

    # All allowlisted fields present
    for f in ["run_id", "object_type", "status", "record_count", "error_rate"]:
        assert f in filtered, f"Allowlisted field '{f}' must be present in filtered context"

    # Non-allowlisted fields must be absent
    for f in ["internal_db_password", "raw_salesforce_token", "debug_stack_trace", "service_account_key"]:
        assert f not in filtered, (
            f"Non-allowlisted field '{f}' must NOT appear in filtered runtime context"
        )


# ---------------------------------------------------------------------------
# Test 8: Context audit log written on every load
# ---------------------------------------------------------------------------


def test_context_audit_log_written(loader: ContextLoader, audit_log: list[ContextAuditEntry]) -> None:
    """Every context load must generate an audit entry with agent_name, context_type, token_count."""
    assert len(audit_log) == 0  # Start clean

    loader.load(
        context_type=ContextType.RUNTIME,
        raw_context={"run_id": "run-audit-001", "status": "complete"},
        agent_name="validation-agent",
    )

    assert len(audit_log) == 1, "Exactly one audit entry must be written per context load"
    entry = audit_log[0]
    assert entry.agent_name == "validation-agent"
    assert entry.context_type == ContextType.RUNTIME.value
    assert entry.token_count >= 0
    assert entry.timestamp_utc  # must be non-empty ISO-8601
    assert "run_id" in entry.fields_loaded


def test_blocked_load_still_writes_audit(loader: ContextLoader, audit_log: list[ContextAuditEntry]) -> None:
    """Even a blocked restricted-context load must still write an audit entry."""
    loader.load(
        context_type=ContextType.RESTRICTED,
        raw_context={"secret": "data"},
        agent_name="validation-agent",
        human_approved=False,
    )
    assert len(audit_log) == 1, "Blocked context load must still produce an audit entry"
    assert audit_log[0].agent_name == "validation-agent"


def test_multiple_loads_produce_multiple_audit_entries(
    loader: ContextLoader, audit_log: list[ContextAuditEntry]
) -> None:
    """Two context loads must produce two separate audit entries."""
    loader.load(ContextType.RUNTIME, {"run_id": "r1", "status": "ok"}, "agent-A")
    loader.load(ContextType.RUNTIME, {"run_id": "r2", "status": "ok"}, "agent-B")
    assert len(audit_log) == 2
    assert audit_log[0].agent_name == "agent-A"
    assert audit_log[1].agent_name == "agent-B"


# ---------------------------------------------------------------------------
# Test 9: Salesforce IDs partially masked in runtime context
# ---------------------------------------------------------------------------


def test_sf_id_partial_masked_in_context(loader: ContextLoader) -> None:
    """Salesforce IDs in runtime context must be partially masked (last 4 chars visible)."""
    sf_id = "001A000001LRlYZIA3"  # 18-char SF ID
    raw_context = {
        "run_id": "run-sf-001",
        "status": "complete",
        "environment": "production",
        "tenant_id": f"record-{sf_id}",  # embed SF ID in a string value
    }
    result = loader.load(
        context_type=ContextType.RUNTIME,
        raw_context=raw_context,
        agent_name="orchestrator-agent",
    )
    context_str = str(result.filtered_context)
    # Full SF ID must not be present
    assert sf_id not in context_str, (
        f"Full SF ID '{sf_id}' must be masked in runtime context"
    )
    # Last 4 chars must remain visible
    last_4 = sf_id[-4:]
    assert last_4 in context_str, (
        f"Last 4 chars of SF ID '{last_4}' must remain visible after masking"
    )


# ---------------------------------------------------------------------------
# Test 10: Phone number redacted from context
# ---------------------------------------------------------------------------


def test_phone_number_redacted_in_context(loader: ContextLoader) -> None:
    """Phone number in a project context error record must be redacted."""
    raw_context = {
        "project_name": "Federal Agency CRM Migration",
        "support_contact": "Migration hotline: (202) 555-0147, available 24/7",
        "environment": "production",
    }
    result = loader.load(
        context_type=ContextType.PROJECT,
        raw_context=raw_context,
        agent_name="documentation-agent",
    )
    context_str = str(result.filtered_context)
    assert "(202) 555-0147" not in context_str, (
        "Phone number must be redacted from project context"
    )
    assert "[PHONE_REDACTED]" in context_str
    assert result.redactions_applied > 0

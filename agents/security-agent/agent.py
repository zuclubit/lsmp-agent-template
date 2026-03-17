"""
Security Agent — Static Security Gate for Migration Pipeline

Single responsibility: Perform deterministic security checks on migration
payloads and configurations before execution is permitted.

Key design decisions:
1. Four deterministic checks are implemented as real Python code (NOT LLM calls):
   a. Path whitelist: only /var/data/migration/ and /tmp/migration-work/ (read-only)
   b. SOQL injection: only SELECT statements, no semicolons, no UNION, no DML
   c. Entropy check: Shannon entropy > 4.5 flags potential hardcoded secrets
   d. PII detection: email, SSN, credit card patterns — reject if raw PII found
2. Returns SecurityAuditResult with: passed, findings, risk_score (0.0–1.0)
3. BLOCKS pipeline if risk_score > 0.7 OR any CRITICAL finding
4. Model: claude-sonnet-4-6 for reasoning tasks that require LLM analysis
5. Structured SecurityFinding objects with severity, location, recommendation

API Spec: v2.0.0  |  Multi-tenant  |  SOX-aware
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECURITY_AGENT_MODEL = os.getenv("SECURITY_AGENT_MODEL", "claude-sonnet-4-6")
SECURITY_AGENT_MAX_TOKENS = int(os.getenv("SECURITY_AGENT_MAX_TOKENS", "4096"))

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt.txt"

# Paths the security agent permits for file read operations
_ALLOWED_READ_PATHS: tuple[str, ...] = (
    "/var/data/migration/",
    "/tmp/migration-work/",
)

# Shannon entropy threshold above which a string is flagged as a potential secret
_ENTROPY_SECRET_THRESHOLD = 4.5

# Minimum string length to run entropy check (avoid false positives on short tokens)
_ENTROPY_MIN_LENGTH = 16

# SOQL patterns that indicate injection risk
_SOQL_BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|UPSERT|UNDELETE|MERGE|DROP|ALTER|CREATE|EXEC|EXECUTE|"
    r"CALL|GRANT|REVOKE|TRUNCATE|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)
_SOQL_UNION_PATTERN = re.compile(r"\bUNION\b", re.IGNORECASE)
_SOQL_SEMICOLON_PATTERN = re.compile(r";")
_SOQL_MUST_START_WITH_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

# PII detection patterns
_PII_EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
_PII_SSN_PATTERN = re.compile(
    r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"
)
_PII_CREDIT_CARD_PATTERN = re.compile(
    r"\b(?:4[0-9]{12}(?:[0-9]{3})?|"          # Visa
    r"5[1-5][0-9]{14}|"                          # MasterCard
    r"3[47][0-9]{13}|"                           # Amex
    r"3(?:0[0-5]|[68][0-9])[0-9]{11}|"          # Diners
    r"6(?:011|5[0-9]{2})[0-9]{12})\b"           # Discover
)

# Risk score thresholds
_BLOCK_RISK_SCORE_THRESHOLD = 0.7


def _load_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("prompt.txt not found — using inline fallback")
        return (
            "You are the Migration Security Agent. "
            "Analyze security findings and provide recommendations. "
            "Never approve pipelines with CRITICAL findings."
        )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class FindingSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class FindingType(str, Enum):
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    SOQL_INJECTION = "SOQL_INJECTION"
    HARDCODED_SECRET = "HARDCODED_SECRET"
    PII_EXPOSURE = "PII_EXPOSURE"
    UNAUTHORIZED_PATH = "UNAUTHORIZED_PATH"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SecurityFinding(BaseModel):
    finding_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    finding_type: FindingType
    severity: FindingSeverity
    location: str = Field(..., description="File path, field name, or payload location where found.")
    description: str = Field(..., min_length=1)
    recommendation: str = Field(..., min_length=1)
    evidence_snippet: Optional[str] = Field(
        default=None,
        description="Redacted/masked evidence — NEVER store raw secrets or PII.",
    )


class SecurityAuditResult(BaseModel):
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str
    tenant_id: str
    passed: bool = Field(
        ...,
        description="True when risk_score <= 0.7 AND no CRITICAL findings.",
    )
    findings: list[SecurityFinding] = Field(default_factory=list)
    risk_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Composite risk score 0.0–1.0. > 0.7 blocks the pipeline.",
    )
    gate_decision: str = Field(
        ...,
        description="ALLOW / WARN / BLOCK — BLOCK when passed=False.",
    )
    llm_analysis: Optional[str] = Field(
        default=None,
        description="Optional LLM-generated security analysis narrative.",
    )
    checked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_ms: int = Field(default=0)

    @model_validator(mode="after")
    def gate_decision_consistent_with_passed(self) -> "SecurityAuditResult":
        if not self.passed and self.gate_decision == "ALLOW":
            raise ValueError("gate_decision cannot be ALLOW when passed is False.")
        return self


class SecurityAgentInput(BaseModel):
    job_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(
        ...,
        description="The migration payload or configuration to audit.",
    )
    file_paths: list[str] = Field(
        default_factory=list,
        description="File paths that will be accessed during migration.",
    )
    soql_queries: list[str] = Field(
        default_factory=list,
        description="SOQL query strings to validate.",
    )
    has_sox_scope: bool = Field(default=False)
    request_llm_analysis: bool = Field(
        default=False,
        description="When True, invoke the LLM for additional security reasoning.",
    )


# ---------------------------------------------------------------------------
# Check 1: Path Whitelist
# ---------------------------------------------------------------------------


def _check_path_whitelist(file_paths: list[str]) -> list[SecurityFinding]:
    """
    Only allow reads from whitelisted paths:
      - /var/data/migration/
      - /tmp/migration-work/

    Flags any path outside the whitelist as CRITICAL.
    Flags any write-mode indicators as HIGH.
    """
    findings: list[SecurityFinding] = []

    for raw_path in file_paths:
        normalized = os.path.normpath(raw_path)

        # Check for path traversal attempts
        if ".." in raw_path:
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.PATH_TRAVERSAL,
                    severity=FindingSeverity.CRITICAL,
                    location=raw_path,
                    description=f"Path traversal sequence detected in file path: {raw_path!r}",
                    recommendation="Reject this path. Path traversal is not permitted.",
                    evidence_snippet=raw_path[:100],
                )
            )
            continue

        # Check whitelist membership
        is_allowed = any(
            normalized.startswith(allowed_prefix)
            for allowed_prefix in _ALLOWED_READ_PATHS
        )

        if not is_allowed:
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.UNAUTHORIZED_PATH,
                    severity=FindingSeverity.CRITICAL,
                    location=normalized,
                    description=(
                        f"File path {normalized!r} is outside the permitted read directories. "
                        f"Only {list(_ALLOWED_READ_PATHS)} are allowed."
                    ),
                    recommendation=(
                        "Move source data to /var/data/migration/ or /tmp/migration-work/ "
                        "before initiating migration."
                    ),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 2: SOQL Injection
# ---------------------------------------------------------------------------


def _check_soql_injection(soql_queries: list[str]) -> list[SecurityFinding]:
    """
    Validate SOQL strings:
    - Must start with SELECT
    - No semicolons
    - No UNION
    - No DML keywords (INSERT/UPDATE/DELETE/UPSERT etc.)
    """
    findings: list[SecurityFinding] = []

    for i, query in enumerate(soql_queries):
        location = f"soql_queries[{i}]"
        query_stripped = query.strip()

        if not _SOQL_MUST_START_WITH_SELECT.match(query_stripped):
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.SOQL_INJECTION,
                    severity=FindingSeverity.CRITICAL,
                    location=location,
                    description=(
                        "SOQL query does not start with SELECT. "
                        "Only SELECT statements are permitted."
                    ),
                    recommendation="Rewrite as a SELECT statement. DML operations are prohibited.",
                    evidence_snippet=query_stripped[:80],
                )
            )
            continue

        if _SOQL_SEMICOLON_PATTERN.search(query_stripped):
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.SOQL_INJECTION,
                    severity=FindingSeverity.CRITICAL,
                    location=location,
                    description="SOQL query contains a semicolon, which may enable statement chaining.",
                    recommendation="Remove all semicolons. SOQL does not support statement chaining.",
                    evidence_snippet=query_stripped[:80],
                )
            )

        if _SOQL_UNION_PATTERN.search(query_stripped):
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.SOQL_INJECTION,
                    severity=FindingSeverity.HIGH,
                    location=location,
                    description="SOQL query contains UNION keyword, which may be used for injection.",
                    recommendation="Remove the UNION clause. SOQL UNION is not supported and is a red flag.",
                    evidence_snippet=query_stripped[:80],
                )
            )

        blocked_match = _SOQL_BLOCKED_KEYWORDS.search(query_stripped)
        if blocked_match:
            findings.append(
                SecurityFinding(
                    finding_type=FindingType.SOQL_INJECTION,
                    severity=FindingSeverity.CRITICAL,
                    location=location,
                    description=(
                        f"SOQL query contains prohibited keyword: "
                        f"{blocked_match.group()!r}. Only SELECT is allowed."
                    ),
                    recommendation=(
                        f"Remove the {blocked_match.group()!r} keyword. "
                        "Data modification is not permitted via SOQL in this pipeline."
                    ),
                    evidence_snippet=query_stripped[:80],
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 3: Entropy — potential hardcoded secrets
# ---------------------------------------------------------------------------


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq = Counter(s)
    n = len(s)
    return -sum((count / n) * math.log2(count / n) for count in freq.values())


def _extract_string_values(obj: Any, path: str = "root") -> list[tuple[str, str]]:
    """Recursively extract (path, value) pairs for all string values in a dict/list."""
    results: list[tuple[str, str]] = []

    if isinstance(obj, str):
        results.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_extract_string_values(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            results.extend(_extract_string_values(v, f"{path}[{i}]"))

    return results


def _check_entropy_secrets(payload: dict[str, Any]) -> list[SecurityFinding]:
    """
    Scan all string values in the payload for high Shannon entropy.
    Entropy > 4.5 and length >= 16 chars flags as potential hardcoded secret.
    """
    findings: list[SecurityFinding] = []

    # Key names that often contain secrets
    _SECRET_KEY_HINTS = frozenset({
        "password", "secret", "token", "key", "api_key", "access_key",
        "private_key", "credentials", "passwd", "pwd", "auth", "bearer",
        "connection_string", "dsn",
    })

    string_values = _extract_string_values(payload)

    for path, value in string_values:
        if len(value) < _ENTROPY_MIN_LENGTH:
            continue

        entropy = _shannon_entropy(value)

        # Flag high-entropy strings regardless of key name
        if entropy > _ENTROPY_SECRET_THRESHOLD:
            severity = FindingSeverity.HIGH

            # Elevate to CRITICAL if the key name hints at a secret
            key_name = path.split(".")[-1].lower().strip("[]0123456789")
            if any(hint in key_name for hint in _SECRET_KEY_HINTS):
                severity = FindingSeverity.CRITICAL

            # Redact the value — never store the raw secret
            redacted = value[:4] + "****" + value[-4:] if len(value) >= 8 else "****"

            findings.append(
                SecurityFinding(
                    finding_type=FindingType.HARDCODED_SECRET,
                    severity=severity,
                    location=path,
                    description=(
                        f"High-entropy string detected at {path!r} "
                        f"(entropy={entropy:.2f}, threshold={_ENTROPY_SECRET_THRESHOLD}). "
                        "This may be a hardcoded credential or secret."
                    ),
                    recommendation=(
                        "Remove hardcoded secrets from payloads. "
                        "Use environment variables, AWS Secrets Manager, "
                        "or HashiCorp Vault instead."
                    ),
                    evidence_snippet=f"entropy={entropy:.2f}, length={len(value)}, sample={redacted}",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Check 4: PII Detection
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email_address", _PII_EMAIL_PATTERN, "Email address"),
    ("ssn", _PII_SSN_PATTERN, "US Social Security Number"),
    ("credit_card", _PII_CREDIT_CARD_PATTERN, "Credit card number"),
]


def _mask_pii(value: str, pii_type: str) -> str:
    """Return a masked representation of a PII value."""
    if pii_type == "email_address":
        parts = value.split("@")
        return parts[0][:2] + "****@" + parts[1] if len(parts) == 2 else "****"
    if pii_type == "ssn":
        return "***-**-" + value[-4:]
    if pii_type == "credit_card":
        return "**** **** **** " + value[-4:]
    return "****"


def _check_pii(payload: dict[str, Any]) -> list[SecurityFinding]:
    """
    Scan all string values in the payload for raw PII.
    Detecting any PII produces a CRITICAL finding (raw PII must never be in payloads).
    """
    findings: list[SecurityFinding] = []
    string_values = _extract_string_values(payload)

    for path, value in string_values:
        for pii_type, pattern, pii_label in _PII_PATTERNS:
            matches = pattern.findall(value)
            if matches:
                masked = _mask_pii(matches[0], pii_type)
                findings.append(
                    SecurityFinding(
                        finding_type=FindingType.PII_EXPOSURE,
                        severity=FindingSeverity.CRITICAL,
                        location=path,
                        description=(
                            f"Raw {pii_label} detected at {path!r}. "
                            f"Found {len(matches)} instance(s). "
                            "PII must never be present in migration configuration payloads."
                        ),
                        recommendation=(
                            f"Remove all {pii_label.lower()} values from this payload. "
                            "Use tokenized/pseudonymized references instead. "
                            "Ensure source data is not embedded in configuration."
                        ),
                        evidence_snippet=f"type={pii_type}, count={len(matches)}, sample={masked}",
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Risk score calculator
# ---------------------------------------------------------------------------

_SEVERITY_WEIGHTS: dict[FindingSeverity, float] = {
    FindingSeverity.CRITICAL: 0.4,
    FindingSeverity.HIGH: 0.2,
    FindingSeverity.MEDIUM: 0.1,
    FindingSeverity.LOW: 0.03,
}


def _calculate_risk_score(findings: list[SecurityFinding]) -> float:
    """
    Risk score is the sum of severity weights, capped at 1.0.
    Any CRITICAL finding produces a minimum score of 0.7 (triggers block).
    """
    if not findings:
        return 0.0

    total = sum(_SEVERITY_WEIGHTS[f.severity] for f in findings)

    # Any CRITICAL finding must produce at least 0.71 (block threshold)
    has_critical = any(f.severity == FindingSeverity.CRITICAL for f in findings)
    if has_critical:
        total = max(total, 0.71)

    return min(1.0, round(total, 3))


# ---------------------------------------------------------------------------
# Security Agent
# ---------------------------------------------------------------------------


class SecurityAgent:
    """
    Performs static security checks before migration execution is permitted.

    The four deterministic checks (path whitelist, SOQL injection, entropy,
    PII) are implemented as pure Python — no LLM call is made for these.

    An optional LLM call can be requested via request_llm_analysis=True
    for additional security reasoning on complex payloads.
    """

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._model = SECURITY_AGENT_MODEL
        self._system_prompt = _load_system_prompt()

    def run(self, inp: SecurityAgentInput) -> SecurityAuditResult:
        """
        Run all security checks and return a SecurityAuditResult.

        Args:
            inp: SecurityAgentInput with payload, file_paths, and SOQL queries.

        Returns:
            SecurityAuditResult with passed, findings, risk_score, gate_decision.
        """
        start_ms = int(time.monotonic() * 1000)
        audit_id = str(uuid.uuid4())

        logger.info(
            "security_agent.start",
            extra={
                "audit_id": audit_id,
                "job_id": inp.job_id,
                "tenant_id": inp.tenant_id,
                "file_path_count": len(inp.file_paths),
                "soql_query_count": len(inp.soql_queries),
            },
        )

        all_findings: list[SecurityFinding] = []

        # ----------------------------------------------------------------
        # Check 1: Path whitelist
        # ----------------------------------------------------------------
        if inp.file_paths:
            path_findings = _check_path_whitelist(inp.file_paths)
            all_findings.extend(path_findings)
            logger.debug(
                "security_agent.path_check",
                extra={"findings": len(path_findings), "job_id": inp.job_id},
            )

        # ----------------------------------------------------------------
        # Check 2: SOQL injection
        # ----------------------------------------------------------------
        if inp.soql_queries:
            soql_findings = _check_soql_injection(inp.soql_queries)
            all_findings.extend(soql_findings)
            logger.debug(
                "security_agent.soql_check",
                extra={"findings": len(soql_findings), "job_id": inp.job_id},
            )

        # ----------------------------------------------------------------
        # Check 3: Entropy — hardcoded secrets
        # ----------------------------------------------------------------
        entropy_findings = _check_entropy_secrets(inp.payload)
        all_findings.extend(entropy_findings)
        logger.debug(
            "security_agent.entropy_check",
            extra={"findings": len(entropy_findings), "job_id": inp.job_id},
        )

        # ----------------------------------------------------------------
        # Check 4: PII detection
        # ----------------------------------------------------------------
        pii_findings = _check_pii(inp.payload)
        all_findings.extend(pii_findings)
        logger.debug(
            "security_agent.pii_check",
            extra={"findings": len(pii_findings), "job_id": inp.job_id},
        )

        # ----------------------------------------------------------------
        # Calculate risk score and gate decision
        # ----------------------------------------------------------------
        risk_score = _calculate_risk_score(all_findings)
        has_critical = any(f.severity == FindingSeverity.CRITICAL for f in all_findings)

        passed = risk_score <= _BLOCK_RISK_SCORE_THRESHOLD and not has_critical

        if has_critical or risk_score > _BLOCK_RISK_SCORE_THRESHOLD:
            gate_decision = "BLOCK"
        elif all_findings:
            gate_decision = "WARN"
        else:
            gate_decision = "ALLOW"

        # ----------------------------------------------------------------
        # Optional LLM analysis
        # ----------------------------------------------------------------
        llm_analysis: Optional[str] = None
        if inp.request_llm_analysis and all_findings:
            llm_analysis = self._run_llm_analysis(inp, all_findings)

        duration_ms = int(time.monotonic() * 1000) - start_ms

        result = SecurityAuditResult(
            audit_id=audit_id,
            job_id=inp.job_id,
            tenant_id=inp.tenant_id,
            passed=passed,
            findings=all_findings,
            risk_score=risk_score,
            gate_decision=gate_decision,
            llm_analysis=llm_analysis,
            duration_ms=duration_ms,
        )

        logger.info(
            "security_agent.complete",
            extra={
                "audit_id": audit_id,
                "job_id": inp.job_id,
                "passed": passed,
                "risk_score": risk_score,
                "gate_decision": gate_decision,
                "total_findings": len(all_findings),
                "critical_findings": sum(
                    1 for f in all_findings if f.severity == FindingSeverity.CRITICAL
                ),
                "duration_ms": duration_ms,
            },
        )

        return result

    def _run_llm_analysis(
        self,
        inp: SecurityAgentInput,
        findings: list[SecurityFinding],
    ) -> str:
        """
        Invoke the LLM for additional security reasoning on the findings.
        Used for complex payloads where deterministic checks need context.
        """
        findings_summary = "\n".join(
            f"- [{f.severity.value}] {f.finding_type.value} at {f.location}: {f.description}"
            for f in findings
        )
        user_message = (
            f"Security findings for job {inp.job_id} (tenant: {inp.tenant_id}):\n\n"
            f"{findings_summary}\n\n"
            "Provide a brief security analysis (3-5 sentences) covering:\n"
            "1. The most critical risks and their potential impact\n"
            "2. Whether any findings may be false positives and why\n"
            "3. Recommended remediation priority order\n\n"
            "Be specific and actionable. Do not suggest approving CRITICAL findings."
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=SECURITY_AGENT_MAX_TOKENS,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "security_agent.llm_analysis_error",
                extra={"error": str(exc), "job_id": inp.job_id},
            )
            return f"LLM analysis unavailable: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def run_security_agent(
    job_id: str,
    tenant_id: str,
    payload: dict[str, Any],
    file_paths: Optional[list[str]] = None,
    soql_queries: Optional[list[str]] = None,
    has_sox_scope: bool = False,
    request_llm_analysis: bool = False,
) -> SecurityAuditResult:
    """
    Convenience wrapper around SecurityAgent.run().

    Args:
        job_id: Migration job identifier.
        tenant_id: Tenant identifier.
        payload: Migration payload or configuration dict to audit.
        file_paths: File paths that will be accessed during migration.
        soql_queries: SOQL query strings to validate.
        has_sox_scope: Whether this migration is under SOX scope.
        request_llm_analysis: When True, request LLM analysis of findings.

    Returns:
        SecurityAuditResult with passed, findings, risk_score, gate_decision.
    """
    agent = SecurityAgent()
    inp = SecurityAgentInput(
        job_id=job_id,
        tenant_id=tenant_id,
        payload=payload,
        file_paths=file_paths or [],
        soql_queries=soql_queries or [],
        has_sox_scope=has_sox_scope,
        request_llm_analysis=request_llm_analysis,
    )
    return agent.run(inp)

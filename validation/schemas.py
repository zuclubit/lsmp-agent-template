"""
Pydantic v2 schema models for the Validation Layer.

All models use strict=True where applicable to prevent silent type coercion.
SHA-256 hashes are typed as constr(pattern=...) to enforce format.

Exported:
  - ValidationResult         — PASS / WARN / BLOCK enum
  - RedactionRecord          — records a single redaction event
  - ValidationDecision       — unified decision object returned by ValidationLayer methods
  - ToolCallValidation       — result of validating a tool call's name + arguments
  - ContextSafetyCheck       — result of classifying a context dict against allowed level
  - AgentInputSchema         — expected schema for all agent task inputs
  - AgentOutputSchema        — expected schema for all agent task outputs
  - SOQLQueryInput           — validated SOQL query wrapper
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ValidationResult(str, Enum):
    """Three-tier validation outcome."""
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class DataClassification(str, Enum):
    """Data sensitivity classification levels (FedRAMP / CUI aligned)."""
    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"
    CUI = "CUI"           # Controlled Unclassified Information
    PII = "PII"           # Personally Identifiable Information
    PHI = "PHI"           # Protected Health Information


class RuleAction(str, Enum):
    BLOCK = "BLOCK"
    REDACT = "REDACT"
    PARTIAL_MASK = "PARTIAL_MASK"
    WARN = "WARN"


# ---------------------------------------------------------------------------
# RedactionRecord
# ---------------------------------------------------------------------------


class RedactionRecord(BaseModel):
    """
    Records a single redaction event applied during output sanitization.

    The original_hash field is a SHA-256 hex digest of the original matched
    content. The plaintext match is never stored.
    """
    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(
        ...,
        description="Identifier of the rule that triggered the redaction (e.g., 'RR-001', 'BUILTIN-003')",
        min_length=1,
        max_length=64,
    )
    original_hash: str = Field(
        ...,
        description="SHA-256 hex digest of the original (unredacted) matched string",
        pattern=r"^[0-9a-f]{64}$",
    )
    replacement: str = Field(
        ...,
        description="The replacement string that was substituted (e.g., '[REDACTED:ANTHROPIC_KEY]')",
        max_length=256,
    )
    position: Optional[int] = Field(
        default=None,
        description="Character offset of the match in the original string (for audit tracing)",
        ge=0,
    )
    match_count: int = Field(
        default=1,
        description="Number of times this pattern was matched and replaced in the text",
        ge=1,
    )
    should_alert: bool = Field(
        default=False,
        description="Whether this redaction triggered an external alert (Splunk/PagerDuty)",
    )

    @classmethod
    def from_match(cls, rule_id: str, matched_text: str, replacement: str, position: int | None = None, alert: bool = False) -> "RedactionRecord":
        """
        Construct a RedactionRecord from a regex match.

        The matched_text is hashed immediately; the plaintext is discarded.
        """
        original_hash = hashlib.sha256(matched_text.encode("utf-8", errors="replace")).hexdigest()
        return cls(
            rule_id=rule_id,
            original_hash=original_hash,
            replacement=replacement,
            position=position,
            should_alert=alert,
        )


# ---------------------------------------------------------------------------
# ValidationDecision
# ---------------------------------------------------------------------------


class ValidationDecision(BaseModel):
    """
    Unified decision object returned by all ValidationLayer methods.

    If result == BLOCK, sanitized_value is None and blocked_hash contains
    the SHA-256 of the content that was blocked (for audit log correlation).
    """
    model_config = ConfigDict(frozen=True)

    result: ValidationResult = Field(
        ...,
        description="Overall validation outcome: PASS, WARN, or BLOCK",
    )
    rule_id: str = Field(
        default="",
        description="ID of the first rule that caused a non-PASS result (empty if PASS)",
        max_length=64,
    )
    message: str = Field(
        default="",
        description="Human-readable description of the decision reason",
        max_length=1024,
    )
    sanitized_value: Optional[Any] = Field(
        default=None,
        description=(
            "The sanitized value after redaction. "
            "None if result == BLOCK (original value must not be used)."
        ),
    )
    redactions_applied: list[RedactionRecord] = Field(
        default_factory=list,
        description="Ordered list of redactions applied during sanitization",
    )
    blocked_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the blocked content (for audit log correlation). "
            "Empty unless result == BLOCK."
        ),
        max_length=64,
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking warnings (e.g., high-entropy token detected but not confirmed secret)",
    )

    @property
    def is_blocked(self) -> bool:
        return self.result == ValidationResult.BLOCK

    @property
    def is_clean(self) -> bool:
        return self.result == ValidationResult.PASS and not self.redactions_applied

    @classmethod
    def pass_decision(cls, value: Any, redactions: list[RedactionRecord] | None = None) -> "ValidationDecision":
        """Construct a PASS decision."""
        return cls(
            result=ValidationResult.PASS,
            sanitized_value=value,
            redactions_applied=redactions or [],
        )

    @classmethod
    def warn_decision(cls, value: Any, rule_id: str, message: str, redactions: list[RedactionRecord] | None = None) -> "ValidationDecision":
        """Construct a WARN decision (non-blocking but flagged)."""
        return cls(
            result=ValidationResult.WARN,
            rule_id=rule_id,
            message=message,
            sanitized_value=value,
            redactions_applied=redactions or [],
        )

    @classmethod
    def block_decision(cls, rule_id: str, message: str, blocked_content: str = "") -> "ValidationDecision":
        """
        Construct a BLOCK decision. Hashes the blocked content immediately.
        Original content is never stored in the model.
        """
        blocked_hash = ""
        if blocked_content:
            blocked_hash = hashlib.sha256(blocked_content.encode("utf-8", errors="replace")).hexdigest()
        return cls(
            result=ValidationResult.BLOCK,
            rule_id=rule_id,
            message=message,
            sanitized_value=None,
            blocked_hash=blocked_hash,
        )


# ---------------------------------------------------------------------------
# ToolCallValidation
# ---------------------------------------------------------------------------


# Tools that require additional checks
_HIGH_RISK_TOOLS = frozenset({
    "cancel_migration",
    "pause_migration",
    "resume_migration",
    "retry_failed_records",
    "scale_batch_size",
    "run_custom_soql_check",
    "read_file",
})

_FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "private_key", "access_key", "secret_key", "client_secret",
    "auth_token", "bearer", "jwt",
})


class ToolCallValidation(BaseModel):
    """
    Result of validating a tool call's name and arguments before dispatch.

    Validates:
    - Tool name is a known tool (if known_tools list is provided)
    - Arguments do not contain forbidden field names (credentials)
    - cancel_migration requires arguments.confirm == True
    - run_custom_soql_check arguments.query must start with SELECT
    - Argument values do not contain injection patterns (checked by ValidationLayer)
    """
    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(
        ...,
        description="Name of the tool being called",
        min_length=1,
        max_length=128,
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments (sanitized — never contain raw credentials)",
    )
    validation_result: ValidationResult = Field(
        ...,
        description="Overall validation outcome for this tool call",
    )
    blocked_reason: Optional[str] = Field(
        default=None,
        description="Reason the tool call was blocked (None if not blocked)",
        max_length=512,
    )
    blocking_rule_id: str = Field(
        default="",
        description="Rule ID that caused the block (empty if not blocked)",
    )
    is_high_risk: bool = Field(
        default=False,
        description="True if this tool is in the high-risk tools set",
    )
    redactions_applied: list[RedactionRecord] = Field(
        default_factory=list,
        description="Redactions applied to argument values before this record was created",
    )

    @property
    def is_blocked(self) -> bool:
        return self.validation_result == ValidationResult.BLOCK

    @classmethod
    def for_blocked_call(cls, tool_name: str, arguments: dict, reason: str, rule_id: str = "") -> "ToolCallValidation":
        return cls(
            tool_name=tool_name,
            arguments={},  # never persist arguments that caused a block
            validation_result=ValidationResult.BLOCK,
            blocked_reason=reason,
            blocking_rule_id=rule_id,
            is_high_risk=tool_name in _HIGH_RISK_TOOLS,
        )

    @classmethod
    def for_valid_call(cls, tool_name: str, arguments: dict, redactions: list[RedactionRecord] | None = None) -> "ToolCallValidation":
        return cls(
            tool_name=tool_name,
            arguments=arguments,
            validation_result=ValidationResult.PASS,
            is_high_risk=tool_name in _HIGH_RISK_TOOLS,
            redactions_applied=redactions or [],
        )


# ---------------------------------------------------------------------------
# ContextSafetyCheck
# ---------------------------------------------------------------------------

# Classification hierarchy: higher index = more sensitive
_CLASSIFICATION_ORDER = [
    DataClassification.PUBLIC,
    DataClassification.INTERNAL,
    DataClassification.CONFIDENTIAL,
    DataClassification.RESTRICTED,
    DataClassification.CUI,
    DataClassification.PII,
    DataClassification.PHI,
]

_CLASSIFICATION_RANK: dict[str, int] = {c.value: i for i, c in enumerate(_CLASSIFICATION_ORDER)}


class ContextSafetyCheck(BaseModel):
    """
    Result of checking whether a context dict's data classification is within
    the allowed level for the requesting agent.

    A context is safe if classification_requested rank <= classification_allowed rank.
    """
    model_config = ConfigDict(frozen=True)

    context_name: str = Field(
        ...,
        description="Name or identifier of the context being checked",
        max_length=256,
    )
    classification_requested: DataClassification = Field(
        ...,
        description="The data classification level of the requested context data",
    )
    classification_allowed: DataClassification = Field(
        ...,
        description="The maximum data classification level allowed for the requesting agent",
    )
    safe: bool = Field(
        ...,
        description="True if classification_requested <= classification_allowed",
    )
    filtered_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Keys that were removed from the context because their classification "
            "exceeded classification_allowed"
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable explanation of the safety decision",
        max_length=512,
    )

    @model_validator(mode="after")
    def validate_safe_flag(self) -> "ContextSafetyCheck":
        """Enforce that the safe flag is consistent with the classification comparison."""
        requested_rank = _CLASSIFICATION_RANK.get(self.classification_requested.value, 0)
        allowed_rank = _CLASSIFICATION_RANK.get(self.classification_allowed.value, 0)
        expected_safe = requested_rank <= allowed_rank
        if self.safe != expected_safe:
            raise ValueError(
                f"safe={self.safe} is inconsistent with classification check: "
                f"{self.classification_requested.value} (rank {requested_rank}) vs "
                f"{self.classification_allowed.value} (rank {allowed_rank})"
            )
        return self

    @classmethod
    def check(
        cls,
        context_name: str,
        classification_requested: DataClassification,
        classification_allowed: DataClassification,
        filtered_keys: list[str] | None = None,
    ) -> "ContextSafetyCheck":
        """
        Factory method that computes the safe flag from the classification ranks.
        """
        requested_rank = _CLASSIFICATION_RANK.get(classification_requested.value, 0)
        allowed_rank = _CLASSIFICATION_RANK.get(classification_allowed.value, 0)
        safe = requested_rank <= allowed_rank

        if safe:
            reason = (
                f"Context '{context_name}' classified as {classification_requested.value} "
                f"is within allowed level {classification_allowed.value}."
            )
        else:
            reason = (
                f"Context '{context_name}' requires {classification_requested.value} access "
                f"but agent is only permitted {classification_allowed.value}. "
                f"Access denied."
            )

        return cls(
            context_name=context_name,
            classification_requested=classification_requested,
            classification_allowed=classification_allowed,
            safe=safe,
            filtered_keys=filtered_keys or [],
            reason=reason,
        )


# ---------------------------------------------------------------------------
# AgentInputSchema — validates all agent task inputs
# ---------------------------------------------------------------------------


class AgentInputSchema(BaseModel):
    """
    Standard schema for all agent task inputs.
    All fields must be sanitized by InputValidator before this model is populated.
    """
    model_config = ConfigDict(
        str_strip_whitespace=True,
        frozen=False,  # inputs may be enriched during orchestration
    )

    task: str = Field(
        ...,
        description="The task description or prompt for the agent",
        min_length=1,
        max_length=8192,
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Migration run identifier (e.g., 'run-abc-123')",
        max_length=128,
        pattern=r"^[a-zA-Z0-9\-_]+$",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant identifier for multi-tenant isolation",
        max_length=128,
        pattern=r"^[a-zA-Z0-9\-_]+$",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Orchestration session ID",
        max_length=64,
    )
    trace_id: Optional[str] = Field(
        default=None,
        description="OpenTelemetry trace ID for distributed tracing",
        max_length=64,
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context key-value pairs (pre-filtered to allowed keys)",
    )
    data_classification: DataClassification = Field(
        default=DataClassification.INTERNAL,
        description="Classification level of this input",
    )

    @field_validator("task")
    @classmethod
    def task_must_not_be_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("task must not be empty or whitespace only")
        return stripped

    @field_validator("context")
    @classmethod
    def context_must_not_contain_credentials(cls, v: dict) -> dict:
        """Reject context dicts that contain credential-named keys."""
        credential_keys = {
            "password", "passwd", "secret", "token", "api_key", "apikey",
            "private_key", "access_key", "secret_key", "credentials",
            "client_secret", "auth_token", "bearer", "jwt",
        }
        found = [k for k in v.keys() if k.lower() in credential_keys]
        if found:
            raise ValueError(
                f"Context contains forbidden credential keys: {found}. "
                "Use Vault secret references instead."
            )
        return v


# ---------------------------------------------------------------------------
# AgentOutputSchema — validates agent outputs before they leave the system
# ---------------------------------------------------------------------------


class AgentOutputSchema(BaseModel):
    """
    Standard schema for all agent task outputs.
    All outputs must be sanitized by OutputSanitizer before this model is populated.
    """
    model_config = ConfigDict(frozen=True)

    agent_name: str = Field(
        ...,
        description="Name of the agent that produced this output",
        min_length=1,
        max_length=128,
    )
    session_id: str = Field(
        ...,
        description="Orchestration session ID",
        max_length=64,
    )
    result_summary: str = Field(
        ...,
        description="Short, sanitized summary of the agent result (max 500 chars)",
        max_length=500,
    )
    grade: Optional[str] = Field(
        default=None,
        description="Quality grade (A/B/C/D/F) — only for validation agent outputs",
        pattern=r"^[ABCDF]$",
    )
    overall_score: Optional[float] = Field(
        default=None,
        description="Quality score 0.0–1.0 — only for validation agent outputs",
        ge=0.0,
        le=1.0,
    )
    pass_gate: Optional[bool] = Field(
        default=None,
        description="Security gate pass/fail — only for security agent outputs",
    )
    tool_call_count: int = Field(
        default=0,
        description="Total number of tool calls made during this session",
        ge=0,
    )
    tokens_used: int = Field(
        default=0,
        description="Total tokens consumed (input + output)",
        ge=0,
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message if agent failed (sanitized — no raw tracebacks with PII)",
        max_length=1024,
    )
    is_error: bool = Field(
        default=False,
        description="True if agent returned an error. Must be True when error is set.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific metadata (sanitized)",
    )

    @model_validator(mode="after")
    def error_and_is_error_consistent(self) -> "AgentOutputSchema":
        if self.error and not self.is_error:
            raise ValueError("is_error must be True when error field is set")
        return self


# ---------------------------------------------------------------------------
# SOQLQueryInput
# ---------------------------------------------------------------------------


class SOQLQueryInput(BaseModel):
    """
    Validated SOQL query wrapper. Enforces SELECT-only at the schema level.
    """
    model_config = ConfigDict(frozen=True)

    query: str = Field(
        ...,
        description="SOQL SELECT query string",
        min_length=7,      # "SELECT " minimum
        max_length=32768,  # Salesforce SOQL max length
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Associated migration run ID",
        max_length=128,
    )
    object_type: Optional[str] = Field(
        default=None,
        description="Salesforce object type being queried",
        max_length=64,
    )
    limit: Optional[int] = Field(
        default=None,
        description="LIMIT clause value (for validation — actual LIMIT must be in query string)",
        ge=1,
        le=10000,
    )

    @field_validator("query")
    @classmethod
    def must_be_select_only(cls, v: str) -> str:
        import re
        stripped = v.strip()
        if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
            raise ValueError("SOQL query must start with SELECT. DML/DDL statements are forbidden.")
        dml_pattern = re.compile(
            r"(?i)\b(DELETE\s+FROM|UPDATE\s+\w+\s+SET|INSERT\s+INTO|DROP\s+TABLE|"
            r"CREATE\s+TABLE|MERGE\s+INTO|GRANT\s+|TRUNCATE\s+TABLE|TRUNCATE\s+OBJECT|REVOKE\s+)\b"
        )
        if dml_pattern.search(stripped):
            raise ValueError("SOQL query contains forbidden DML/DDL keywords.")
        return stripped

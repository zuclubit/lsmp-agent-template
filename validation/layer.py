"""
Validation Layer — security, SOQL, and output sanitization for the agent system.

Provides:
  - PromptInjectionDetector: detects and blocks prompt injection attempts
  - SOQLValidator: enforces SOQL allowlist (SELECT-only)
  - OutputSanitizer: redacts credentials, PII, and high-entropy secrets from output
  - ValidationLayer: unified entry point composing all validators
  - ValidationResult: enum for PASS / BLOCK outcomes
  - SecurityBlockedError: raised when an input is blocked

Satisfies: FedRAMP SI-10, OWASP Prompt Injection (LLM01), CUI data handling.
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class ValidationResult(str, Enum):
    """Result of a validation or sanitization check."""

    PASS = "PASS"
    BLOCK = "BLOCK"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SecurityBlockedError(RuntimeError):
    """Raised when a security gate blocks an input or action."""

    def __init__(self, reason: str, rule_id: str = "", blocked_value_hash: str = "") -> None:
        super().__init__(f"SECURITY BLOCKED [{rule_id}]: {reason}")
        self.reason = reason
        self.rule_id = rule_id
        self.blocked_value_hash = blocked_value_hash


# ---------------------------------------------------------------------------
# Prompt Injection Detector
# ---------------------------------------------------------------------------

# Canonical injection patterns (pre-compiled for speed)
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("IGNORE_PREVIOUS", re.compile(r"ignore\s+(previous|all|prior|all\s+previous)\s+(instructions?|prompts?|context|directives?)", re.IGNORECASE)),
    ("IGNORE_ALL_PREVIOUS", re.compile(r"ignore\s+all\s+previous\s+instructions?", re.IGNORECASE)),
    ("SYSTEM_TAG", re.compile(r"\[SYSTEM\]", re.IGNORECASE)),
    ("IM_START_TAG", re.compile(r"<\|im_start\|>", re.IGNORECASE)),
    ("IM_END_TAG", re.compile(r"<\|im_end\|>", re.IGNORECASE)),
    ("FORGET_EVERYTHING", re.compile(r"forget\s+(everything|all|your\s+instructions?|previous|context)", re.IGNORECASE)),
    ("NEW_INSTRUCTIONS", re.compile(r"(new|updated?)\s+instructions?(\s+follow|\s+are|\s*:)", re.IGNORECASE)),
    ("DISREGARD_ABOVE", re.compile(r"disregard\s+(the\s+)?(above|previous|prior)\s+(instructions?|text|context)", re.IGNORECASE)),
    ("JAILBREAK_DAN", re.compile(r"\bDAN\b|\bjailbreak\b", re.IGNORECASE)),
    ("OVERRIDE_INSTRUCTIONS", re.compile(r"override\s+(all\s+)?(previous\s+)?(instructions?|safety|constraints?)", re.IGNORECASE)),
    ("ACT_AS_DIFFERENT", re.compile(r"(act|behave|pretend|roleplay)\s+as\s+(a\s+)?(different|new|evil|unrestricted|another)\s+(ai|agent|assistant|model)", re.IGNORECASE)),
    ("SUDO_MODE", re.compile(r"sudo\s+mode|developer\s+mode|god\s+mode|unrestricted\s+mode", re.IGNORECASE)),
    ("TRANSLATE_IGNORE", re.compile(r"translate\s+the\s+following\s+.*ignore", re.IGNORECASE)),
    ("REPEAT_AFTER_ME", re.compile(r"repeat\s+(after\s+me|the\s+following)\s*:", re.IGNORECASE)),
    ("YOU_ARE_NOW", re.compile(r"you\s+are\s+now\s+(a\s+)?(different|new|evil|unrestricted)", re.IGNORECASE)),
    ("STOP_BEING", re.compile(r"stop\s+being\s+(an?\s+)?(ai|assistant|claude|helpful)", re.IGNORECASE)),
    ("SOQL_CHAINING", re.compile(r";\s*(DELETE|DROP|UPDATE|INSERT|MERGE|GRANT)\s", re.IGNORECASE)),
    ("SQL_INJECTION_CLASSIC", re.compile(r"';\s*(DELETE|DROP|UPDATE|INSERT|MERGE|GRANT)\s", re.IGNORECASE)),
    ("ANGLE_BRACKET_SYSTEM", re.compile(r"<(system|assistant|user)\s*>", re.IGNORECASE)),
    ("HASH_SYSTEM", re.compile(r"#+\s*(SYSTEM|ASSISTANT|OVERRIDE)\s*#+", re.IGNORECASE)),
    ("TRIPLE_BACKTICK_SYSTEM", re.compile(r"```\s*system\s*\n", re.IGNORECASE)),
]

# Unicode homoglyph mappings for obfuscation detection (common lookalikes → ASCII)
_UNICODE_NORMALIZE_TABLE: dict[str, str] = {
    "\u0456": "i",   # Cyrillic і → i
    "\u04CF": "i",   # Cyrillic ӏ → i
    "\u0261": "g",   # ɡ → g
    "\u0430": "a",   # Cyrillic а → a
    "\u0435": "e",   # Cyrillic е → e
    "\u043E": "o",   # Cyrillic о → o
    "\u0440": "r",   # Cyrillic р → r
    "\u0441": "c",   # Cyrillic с → c
    "\u0445": "x",   # Cyrillic х → x
    "\u0443": "y",   # Cyrillic у → y
    "\u0570": "h",   # Armenian հ → h
    "\u0578": "o",   # Armenian ո → o
    "\u0581": "g",   # Armenian փ → g (approximate)
    "\u03BF": "o",   # Greek ο → o
    "\u03B5": "e",   # Greek ε → e
    "\u1D0F": "o",   # Latin small capital O → o
    "\u1D00": "a",   # Latin small capital A → a
    "\uFF29": "I",   # Fullwidth I
    "\uFF4F": "o",   # Fullwidth o
    "\u2147": "e",   # Euler's number e (ℇ) → e
    "\u2148": "i",   # Imaginary i (ⅈ) → i
    "\u2110": "I",   # Script capital I → I
}


def _normalize_unicode(text: str) -> str:
    """Normalize unicode lookalikes to their ASCII equivalents for injection detection."""
    # First apply NFKC normalization (decomposes compatibility characters)
    normalized = unicodedata.normalize("NFKC", text)
    # Then apply homoglyph substitution table
    result = []
    for ch in normalized:
        result.append(_UNICODE_NORMALIZE_TABLE.get(ch, ch))
    return "".join(result)


def _extract_all_strings(obj: Any, depth: int = 0, max_depth: int = 8) -> list[str]:
    """Recursively extract all string values from a nested dict/list structure."""
    if depth > max_depth:
        return []
    strings: list[str] = []
    if isinstance(obj, str):
        strings.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            strings.extend(_extract_all_strings(v, depth + 1, max_depth))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            strings.extend(_extract_all_strings(item, depth + 1, max_depth))
    return strings


class PromptInjectionDetector:
    """
    Detects prompt injection attempts in free-form text, structured dicts, and
    tool results. Returns a (ValidationResult, matched_rule_id) tuple.

    Detection covers:
    - Direct instruction override patterns
    - System-tag injections ([SYSTEM], <|im_start|>, etc.)
    - Nested injections in dict values (e.g., job_id containing injection)
    - Unicode homoglyph obfuscation
    - SOQL chaining attempts
    """

    def scan(self, value: Any) -> tuple[ValidationResult, str]:
        """
        Scan a value (str, dict, or list) for injection patterns.

        Returns:
            (ValidationResult.PASS, "") if clean
            (ValidationResult.BLOCK, rule_id) if injection detected
        """
        strings = _extract_all_strings(value)
        for text in strings:
            result, rule_id = self._scan_string(text)
            if result == ValidationResult.BLOCK:
                return ValidationResult.BLOCK, rule_id
        return ValidationResult.PASS, ""

    def _scan_string(self, text: str) -> tuple[ValidationResult, str]:
        """Scan a single string. Applies unicode normalization first."""
        if not text or not isinstance(text, str):
            return ValidationResult.PASS, ""

        # Check raw text
        for rule_id, pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                return ValidationResult.BLOCK, rule_id

        # Check unicode-normalized text (catches obfuscation)
        normalized = _normalize_unicode(text)
        if normalized != text:
            for rule_id, pattern in _INJECTION_PATTERNS:
                if pattern.search(normalized):
                    return ValidationResult.BLOCK, f"{rule_id}_UNICODE"

        return ValidationResult.PASS, ""


# ---------------------------------------------------------------------------
# SOQL Validator
# ---------------------------------------------------------------------------

# Blocked DML/DDL keywords — these must never appear in agent-submitted SOQL
_SOQL_BLOCKED_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    # Compound patterns checked FIRST (before individual keywords)
    ("SEMICOLON_CHAIN", re.compile(r";\s*\b(SELECT|DELETE|UPDATE|INSERT|DROP|CREATE|MERGE|GRANT)\b", re.IGNORECASE)),
    ("UNION_INJECTION", re.compile(r"\bUNION\b\s+\bSELECT\b", re.IGNORECASE)),
    # Individual DML/DDL keywords
    ("DELETE", re.compile(r"\bDELETE\b", re.IGNORECASE)),
    ("UPDATE", re.compile(r"\bUPDATE\b", re.IGNORECASE)),
    ("INSERT", re.compile(r"\bINSERT\b", re.IGNORECASE)),
    ("DROP", re.compile(r"\bDROP\b", re.IGNORECASE)),
    ("MERGE", re.compile(r"\bMERGE\b", re.IGNORECASE)),
    ("GRANT", re.compile(r"\bGRANT\b", re.IGNORECASE)),
    ("REVOKE", re.compile(r"\bREVOKE\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b", re.IGNORECASE)),
    ("ALTER", re.compile(r"\bALTER\b", re.IGNORECASE)),
    ("CREATE", re.compile(r"\bCREATE\b", re.IGNORECASE)),
    ("EXEC", re.compile(r"\bEXEC(UTE)?\b", re.IGNORECASE)),
]

_SOQL_MUST_START_WITH_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class SOQLValidator:
    """
    Validates SOQL queries to ensure they are safe SELECT-only statements.

    Rules enforced:
    1. Query must start with SELECT
    2. No DML/DDL keywords: DELETE, UPDATE, INSERT, DROP, MERGE, GRANT, etc.
    3. No UNION SELECT (injection chaining)
    4. No semicolon-chained statements

    Note: Subqueries with SELECT are allowed (e.g., WHERE Id IN (SELECT ...)).
    UNION SELECT is still blocked as it is the classic injection vector.
    """

    def validate(self, soql: str) -> tuple[ValidationResult, str]:
        """
        Validate a SOQL query.

        Returns:
            (ValidationResult.PASS, "") if safe
            (ValidationResult.BLOCK, rule_id) if blocked
        """
        if not soql or not isinstance(soql, str):
            return ValidationResult.BLOCK, "EMPTY_QUERY"

        # Check blocked DML/DDL keywords FIRST — regardless of whether the
        # statement starts with SELECT. This ensures DELETE, INSERT, etc. are
        # detected by their specific rule_id rather than NOT_SELECT_STATEMENT.
        for rule_id, pattern in _SOQL_BLOCKED_KEYWORDS:
            if pattern.search(soql):
                return ValidationResult.BLOCK, rule_id

        # After DML check: must start with SELECT
        if not _SOQL_MUST_START_WITH_SELECT.match(soql):
            return ValidationResult.BLOCK, "NOT_SELECT_STATEMENT"

        return ValidationResult.PASS, ""

    def validate_or_raise(self, soql: str) -> None:
        """Validate SOQL and raise SecurityBlockedError if blocked."""
        result, rule_id = self.validate(soql)
        if result == ValidationResult.BLOCK:
            content_hash = hashlib.sha256(soql.encode()).hexdigest()[:16]
            raise SecurityBlockedError(
                reason=f"SOQL query blocked by rule {rule_id}",
                rule_id=rule_id,
                blocked_value_hash=content_hash,
            )


# ---------------------------------------------------------------------------
# Output Sanitizer
# ---------------------------------------------------------------------------

@dataclass
class RedactionRecord:
    """Records a single redaction event for audit purposes."""
    rule_id: str
    replacement: str
    count: int


@dataclass
class SanitizationResult:
    """Result of sanitizing output text."""
    sanitized_text: str
    redactions: list[RedactionRecord] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def was_modified(self) -> bool:
        return len(self.redactions) > 0


# Ordered list of (rule_id, pattern, replacement) tuples.
# Order matters: more specific patterns are checked before generic ones.
_REDACTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # API Keys and tokens
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}", re.IGNORECASE), "[REDACTED:ANTHROPIC_KEY]"),
    ("SF_BEARER_TOKEN", re.compile(r"00D[a-zA-Z0-9]{12,15}![a-zA-Z0-9_.]{40,}", re.IGNORECASE), "[REDACTED:SF_TOKEN]"),
    ("SF_SESSION_TOKEN", re.compile(r"Bearer\s+00[a-zA-Z0-9]{40,}", re.IGNORECASE), "[REDACTED:SF_TOKEN]"),
    ("VAULT_TOKEN", re.compile(r"hvs\.[a-zA-Z0-9_\-]{20,}", re.IGNORECASE), "[REDACTED:VAULT_TOKEN]"),
    ("VAULT_TOKEN_LEGACY", re.compile(r"hvb\.[a-zA-Z0-9_\-]{20,}", re.IGNORECASE), "[REDACTED:VAULT_TOKEN]"),
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE), "[REDACTED:AWS_KEY]"),
    ("AWS_SECRET_KEY", re.compile(r"aws_secret_access_key\s*[:=]\s*[a-zA-Z0-9/+=]{40}", re.IGNORECASE), "[REDACTED:AWS_SECRET]"),
    ("GENERIC_API_KEY", re.compile(r"(api[_\-]?key|apikey)\s*[:=]\s*['\"]?[a-zA-Z0-9\-_]{32,}['\"]?", re.IGNORECASE), "[REDACTED:API_KEY]"),
    ("GENERIC_TOKEN", re.compile(r"(access[_\-]?token|auth[_\-]?token|service[_\-]?token)\s*[:=]\s*['\"]?[a-zA-Z0-9\-_.]{32,}['\"]?", re.IGNORECASE), "[REDACTED:TOKEN]"),
    # Private keys / certificates
    ("PRIVATE_KEY_PEM", re.compile(r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----[\s\S]*?-----END\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----", re.IGNORECASE), "[REDACTED:PRIVATE_KEY]"),
    ("CERTIFICATE_PEM", re.compile(r"-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----", re.IGNORECASE), "[REDACTED:CERTIFICATE]"),
    # DB connection strings
    ("DB_CONN_STRING", re.compile(r"(postgresql|postgres|mysql|mongodb|redis|oracle|mssql)\s*://[^\s\"']+", re.IGNORECASE), "[REDACTED:DB_CONNECTION_STRING]"),
    # PII
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), "[SSN_REDACTED]"),
    ("CREDIT_CARD", re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12})\b"), "[CREDIT_CARD_REDACTED]"),
    ("EMAIL", re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"), "[EMAIL_REDACTED]"),
    ("PHONE_US", re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE_REDACTED]"),
]

# High-entropy detection parameters
_HIGH_ENTROPY_MIN_LENGTH = 32
_HIGH_ENTROPY_THRESHOLD = 4.5  # Shannon entropy bits/char
_HIGH_ENTROPY_RULE_ID = "HIGH_ENTROPY_SECRET"

# Salesforce 18-character ID pattern — partial mask: show last 4
_SF_ID_PATTERN = re.compile(r"\b([a-zA-Z0-9]{14})([a-zA-Z0-9]{4})\b")


def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy (bits per character) of a string."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    entropy = 0.0
    for count in freq.values():
        p = count / n
        entropy -= p * math.log2(p)
    return entropy


def _is_high_entropy_secret(token: str) -> bool:
    """Return True if token looks like a high-entropy secret (base64/hex credential)."""
    if len(token) < _HIGH_ENTROPY_MIN_LENGTH:
        return False
    # Only flag tokens that look like encoded data (base64/hex charset)
    if not re.match(r"^[a-zA-Z0-9+/=_\-]+$", token):
        return False
    entropy = _shannon_entropy(token)
    return entropy >= _HIGH_ENTROPY_THRESHOLD


class OutputSanitizer:
    """
    Sanitizes agent output by redacting credentials, PII, and high-entropy secrets.

    All redactions are logged as RedactionRecord entries in the SanitizationResult.
    SSN detections also generate alerts (for compliance reporting).
    """

    def sanitize(self, text: str) -> SanitizationResult:
        """
        Apply all redaction rules to text.

        Returns SanitizationResult with sanitized text and audit trail.
        """
        if not text or not isinstance(text, str):
            return SanitizationResult(sanitized_text=text or "", redactions=[], alerts=[])

        result_text = text
        redactions: list[RedactionRecord] = []
        alerts: list[str] = []

        # Apply ordered redaction rules
        for rule_id, pattern, replacement in _REDACTION_RULES:
            new_text, count = pattern.subn(replacement, result_text)
            if count > 0:
                redactions.append(RedactionRecord(rule_id=rule_id, replacement=replacement, count=count))
                result_text = new_text
                # SSN requires a compliance alert
                if rule_id == "SSN":
                    alerts.append(f"PII_ALERT: SSN pattern detected and redacted ({count} occurrence(s))")

        # High-entropy token scanning — tokenize on whitespace/quotes/common delimiters
        tokens = re.split(r'[\s"\'`=:,\[\]{}()\n\r\t]+', result_text)
        for token in tokens:
            if _is_high_entropy_secret(token) and token not in ("[REDACTED:ANTHROPIC_KEY]", "[REDACTED:SF_TOKEN]",
                                                                  "[REDACTED:VAULT_TOKEN]", "[REDACTED:PRIVATE_KEY]"):
                result_text = result_text.replace(token, "[REDACTED:HIGH_ENTROPY_SECRET]", 1)
                redactions.append(RedactionRecord(
                    rule_id=_HIGH_ENTROPY_RULE_ID,
                    replacement="[REDACTED:HIGH_ENTROPY_SECRET]",
                    count=1,
                ))

        return SanitizationResult(
            sanitized_text=result_text,
            redactions=redactions,
            alerts=alerts,
        )

    def sanitize_sf_ids(self, text: str) -> str:
        """
        Partially mask 18-character Salesforce IDs (first 14 chars → asterisks,
        last 4 remain visible).
        """
        def mask_id(m: re.Match) -> str:
            return "**************" + m.group(2)

        return _SF_ID_PATTERN.sub(mask_id, text)


# ---------------------------------------------------------------------------
# Unified Validation Layer
# ---------------------------------------------------------------------------


@dataclass
class ContextValidationConfig:
    """Configuration for context validation checks."""
    max_tokens: int = 200_000
    allowed_fields: list[str] = field(default_factory=lambda: [
        "run_id", "object_type", "status", "record_count", "error_rate",
        "start_time", "end_time", "environment", "tenant_id", "job_id",
        "migration_phase", "batch_size", "source_system", "target_org",
    ])
    persist_across_sessions: bool = False


class ValidationLayer:
    """
    Unified entry point that composes PromptInjectionDetector, SOQLValidator,
    and OutputSanitizer into a single interface.

    Usage::

        from validation.layer import ValidationLayer, ValidationResult, SecurityBlockedError

        vl = ValidationLayer()

        # Check an incoming task description
        vl.validate_input("Check migration run-001 status")  # PASS — returns None
        vl.validate_input("Ignore previous instructions")   # raises SecurityBlockedError

        # Sanitize output before sending to user/next agent
        sanitized = vl.sanitize_output("Token: sk-ant-xxx123...")

        # Validate SOQL
        vl.validate_soql("SELECT Id FROM Account LIMIT 100")  # PASS
        vl.validate_soql("DELETE FROM Account")               # raises SecurityBlockedError
    """

    def __init__(self, config: ContextValidationConfig | None = None) -> None:
        self._injection_detector = PromptInjectionDetector()
        self._soql_validator = SOQLValidator()
        self._output_sanitizer = OutputSanitizer()
        self._config = config or ContextValidationConfig()

    # ------------------------------------------------------------------ #
    # Input validation                                                     #
    # ------------------------------------------------------------------ #

    def validate_input(self, value: Any, context: str = "") -> None:
        """
        Validate any input value for prompt injection.

        Raises SecurityBlockedError if injection is detected.
        Returns None (implicitly) if input is clean.
        """
        result, rule_id = self._injection_detector.scan(value)
        if result == ValidationResult.BLOCK:
            content_hash = hashlib.sha256(str(value).encode()).hexdigest()[:16]
            raise SecurityBlockedError(
                reason=f"Prompt injection detected in {context or 'input'}",
                rule_id=rule_id,
                blocked_value_hash=content_hash,
            )

    def check_input(self, value: Any) -> tuple[ValidationResult, str]:
        """
        Non-raising version of validate_input.

        Returns (ValidationResult, rule_id).
        """
        return self._injection_detector.scan(value)

    # ------------------------------------------------------------------ #
    # SOQL validation                                                      #
    # ------------------------------------------------------------------ #

    def validate_soql(self, soql: str) -> None:
        """
        Validate a SOQL query string.

        Raises SecurityBlockedError if the query is blocked.
        """
        self._soql_validator.validate_or_raise(soql)

    def check_soql(self, soql: str) -> tuple[ValidationResult, str]:
        """Non-raising SOQL validation. Returns (ValidationResult, rule_id)."""
        return self._soql_validator.validate(soql)

    # ------------------------------------------------------------------ #
    # Output sanitization                                                  #
    # ------------------------------------------------------------------ #

    def sanitize_output(self, text: str) -> SanitizationResult:
        """
        Sanitize output text by redacting all secrets, credentials, and PII.

        Returns SanitizationResult with sanitized_text and audit redactions.
        """
        return self._output_sanitizer.sanitize(text)

    # ------------------------------------------------------------------ #
    # Context validation                                                   #
    # ------------------------------------------------------------------ #

    def validate_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Filter a runtime context dict to only allowed fields, sanitize values,
        and enforce token limits.

        Returns the filtered, sanitized context.
        """
        filtered: dict[str, Any] = {}
        for key in self._config.allowed_fields:
            if key in context:
                value = context[key]
                # Sanitize string values
                if isinstance(value, str):
                    sanitized = self._output_sanitizer.sanitize(value)
                    filtered[key] = sanitized.sanitized_text
                else:
                    filtered[key] = value
        return filtered

    def check_context_token_limit(self, context_str: str) -> bool:
        """
        Return True if context is within token budget, False if it exceeds max_tokens.
        Uses a rough 4-chars-per-token approximation.
        """
        approx_tokens = len(context_str) // 4
        return approx_tokens <= self._config.max_tokens

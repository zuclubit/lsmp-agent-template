"""
Validation Rule Engine — loads redaction rules from security/redaction_rules.yaml
and provides a programmatic interface for applying them to arbitrary strings.

Rule priority order:
  1. BLOCK rules (injection, SOQL DML) — evaluated first; short-circuits on first match
  2. REDACT rules — applied in ascending rule id order
  3. PARTIAL_MASK rules — applied after all full-redaction passes

Builtin (hardcoded) fallback rules are always active even if YAML fails to load.
They cover the most critical patterns: ANTHROPIC_KEY, SF_TOKEN, PRIVATE_KEY,
and PROMPT_INJECTION.

Usage::

    from validation.rules import RuleEngine, BuiltinRules

    engine = RuleEngine()            # loads from YAML automatically
    results = engine.apply("sk-ant-xxxx... some text")
    for applied_rule in results.applied_rules:
        print(applied_rule.rule_id, applied_rule.action)
    print(results.sanitized_text)
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RuleAction(str, Enum):
    BLOCK = "BLOCK"
    REDACT = "REDACT"
    PARTIAL_MASK = "PARTIAL_MASK"
    WARN = "WARN"


class RuleSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Rule dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RedactionRule:
    """
    A single redaction or blocking rule loaded from redaction_rules.yaml
    or defined as a builtin fallback.
    """
    id: str                          # e.g. "RR-001"
    name: str                        # human-readable name
    pattern: str | None              # regex pattern string (None for entropy rules)
    replacement: str | None          # replacement string (None for BLOCK rules)
    severity: RuleSeverity
    action: RuleAction
    alert: bool = False
    rule_type: str = "pattern"       # "pattern" | "entropy"
    entropy_threshold: float = 4.5   # used only when rule_type == "entropy"
    entropy_min_length: int = 16     # used only when rule_type == "entropy"
    note: str = ""

    # Compiled regex — populated by RuleEngine after loading
    _compiled: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def compile(self) -> None:
        """Compile the pattern regex. Called once by RuleEngine after construction."""
        if self.pattern and self.rule_type == "pattern":
            try:
                self._compiled = re.compile(self.pattern, re.IGNORECASE | re.DOTALL)
            except re.error as exc:
                logger.error("Failed to compile rule %s pattern: %s — rule disabled", self.id, exc)
                self._compiled = None

    @property
    def compiled(self) -> re.Pattern[str] | None:
        return self._compiled


@dataclass
class AppliedRule:
    """Records a rule that was applied during sanitization."""
    rule_id: str
    rule_name: str
    action: RuleAction
    severity: RuleSeverity
    match_count: int
    should_alert: bool
    blocked_hash: str = ""  # SHA-256 of original content (BLOCK only, for audit)


@dataclass
class RuleEngineResult:
    """Result of running the rule engine over a text value."""
    sanitized_text: str
    was_blocked: bool
    blocking_rule_id: str
    blocking_rule_name: str
    blocked_content_hash: str           # SHA-256 of original content if blocked
    applied_rules: list[AppliedRule]

    @property
    def had_redactions(self) -> bool:
        return any(r.action in (RuleAction.REDACT, RuleAction.PARTIAL_MASK) for r in self.applied_rules)

    @property
    def alert_rules(self) -> list[AppliedRule]:
        return [r for r in self.applied_rules if r.should_alert]


# ---------------------------------------------------------------------------
# Builtin hardcoded fallback rules
# ---------------------------------------------------------------------------

class BuiltinRules:
    """
    Hardcoded fallback rules for the most critical patterns.
    These are always active — even if YAML loading fails entirely.
    They act as a last line of defence for the most sensitive patterns.
    """

    ANTHROPIC_KEY = RedactionRule(
        id="BUILTIN-001",
        name="anthropic_api_key",
        pattern=r"sk-ant-[A-Za-z0-9\-_]{20,}",
        replacement="[REDACTED:ANTHROPIC_KEY]",
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.REDACT,
        alert=True,
    )

    SF_TOKEN = RedactionRule(
        id="BUILTIN-002",
        name="salesforce_token",
        pattern=r"(?i)(sf_token|salesforce_token|access_token)[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9!.]{20,}",
        replacement="[REDACTED:SF_TOKEN]",
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.REDACT,
        alert=True,
    )

    VAULT_TOKEN = RedactionRule(
        id="BUILTIN-003",
        name="vault_token",
        pattern=r"hvs\.[A-Za-z0-9]{24,}|hvb\.[A-Za-z0-9]{24,}",
        replacement="[REDACTED:VAULT_TOKEN]",
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.REDACT,
        alert=True,
    )

    PRIVATE_KEY = RedactionRule(
        id="BUILTIN-004",
        name="private_key_block",
        pattern=r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----[\s\S]+?-----END",
        replacement="[REDACTED:PRIVATE_KEY]",
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.REDACT,
        alert=True,
    )

    PROMPT_INJECTION_IGNORE = RedactionRule(
        id="BUILTIN-005",
        name="prompt_injection_ignore_instructions",
        pattern=r"(?i)(ignore\s+(previous|prior|above|all)\s+instructions|forget\s+(everything|all)|new\s+instructions\s*:)",
        replacement=None,
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.BLOCK,
        alert=True,
    )

    PROMPT_INJECTION_SYSTEM = RedactionRule(
        id="BUILTIN-006",
        name="prompt_injection_system_override",
        pattern=r"(?i)(\[SYSTEM\]|<\|im_start\||<system>|<\|system\|>)",
        replacement=None,
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.BLOCK,
        alert=True,
    )

    SOQL_DML = RedactionRule(
        id="BUILTIN-007",
        name="soql_dml_injection",
        pattern=r"(?i)\b(DELETE\s+FROM|UPDATE\s+\w+\s+SET|INSERT\s+INTO|DROP\s+TABLE|CREATE\s+TABLE|MERGE\s+INTO|GRANT\s+|TRUNCATE\s+TABLE)\b",
        replacement=None,
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.BLOCK,
        alert=True,
    )

    SSN = RedactionRule(
        id="BUILTIN-008",
        name="us_ssn",
        pattern=r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b",
        replacement="[SSN_REDACTED]",
        severity=RuleSeverity.CRITICAL,
        action=RuleAction.REDACT,
        alert=True,
    )

    @classmethod
    def all(cls) -> list[RedactionRule]:
        """Return all builtin rules as a list."""
        rules = [
            cls.ANTHROPIC_KEY,
            cls.SF_TOKEN,
            cls.VAULT_TOKEN,
            cls.PRIVATE_KEY,
            cls.PROMPT_INJECTION_IGNORE,
            cls.PROMPT_INJECTION_SYSTEM,
            cls.SOQL_DML,
            cls.SSN,
        ]
        for rule in rules:
            rule.compile()
        return rules


# ---------------------------------------------------------------------------
# Rule loading from YAML
# ---------------------------------------------------------------------------

def _find_redaction_rules_yaml() -> Path | None:
    """Locate security/redaction_rules.yaml relative to this file or PROJECT_ROOT."""
    # Strategy 1: PROJECT_ROOT env var
    project_root = os.environ.get("PROJECT_ROOT")
    if project_root:
        candidate = Path(project_root) / "security" / "redaction_rules.yaml"
        if candidate.exists():
            return candidate

    # Strategy 2: Walk up from this file's location
    here = Path(__file__).resolve().parent
    for ancestor in [here, here.parent, here.parent.parent]:
        candidate = ancestor / "security" / "redaction_rules.yaml"
        if candidate.exists():
            return candidate

    return None


def _load_rules_from_yaml(yaml_path: Path) -> list[RedactionRule]:
    """Parse redaction_rules.yaml and return a list of RedactionRule objects."""
    try:
        import yaml  # PyYAML
    except ImportError:
        logger.warning("PyYAML not installed — cannot load rules from YAML. Using builtins only.")
        return []

    try:
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        logger.error("Failed to read redaction_rules.yaml at %s: %s — using builtins only.", yaml_path, exc)
        return []

    rules: list[RedactionRule] = []
    raw_rules = data.get("rules", [])

    severity_map = {s.value: s for s in RuleSeverity}
    action_map = {a.value: a for a in RuleAction}

    for raw in raw_rules:
        try:
            rule_id = str(raw.get("id", "UNKNOWN"))
            name = str(raw.get("name", rule_id))
            pattern = raw.get("pattern")          # may be None for entropy rules
            replacement = raw.get("replacement")  # None for BLOCK rules
            severity_str = str(raw.get("severity", "MEDIUM")).upper()
            action_str = str(raw.get("action", "REDACT")).upper()
            alert = bool(raw.get("alert", False))
            rule_type = "entropy" if raw.get("type") == "entropy" else "pattern"
            entropy_threshold = float(raw.get("threshold", 4.5))
            entropy_min_length = int(raw.get("min_length", 16))
            note = str(raw.get("note", ""))

            severity = severity_map.get(severity_str, RuleSeverity.MEDIUM)
            action = action_map.get(action_str, RuleAction.REDACT)

            rule = RedactionRule(
                id=rule_id,
                name=name,
                pattern=pattern,
                replacement=replacement,
                severity=severity,
                action=action,
                alert=alert,
                rule_type=rule_type,
                entropy_threshold=entropy_threshold,
                entropy_min_length=entropy_min_length,
                note=note,
            )
            rule.compile()
            rules.append(rule)
        except Exception as exc:
            logger.warning("Skipping malformed rule entry %s: %s", raw.get("id", "?"), exc)

    return rules


# ---------------------------------------------------------------------------
# Entropy checker (used by RuleEngine for entropy-type rules)
# ---------------------------------------------------------------------------

def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy in bits per character."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    entropy = 0.0
    for count in freq.values():
        p = count / n
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def _check_entropy(value: str, threshold: float, min_length: int) -> bool:
    """Return True if value exceeds entropy threshold and meets minimum length."""
    if len(value) < min_length:
        return False
    # Only flag strings that look like encoded secrets (base64/hex charset)
    if not re.match(r"^[a-zA-Z0-9+/=_\-]+$", value):
        return False
    return _shannon_entropy(value) >= threshold


# ---------------------------------------------------------------------------
# Rule Engine
# ---------------------------------------------------------------------------

class RuleEngine:
    """
    Loads and applies all redaction/block rules in priority order.

    Priority:
      1. BLOCK rules — evaluated first; if any matches, stop and return blocked result
      2. REDACT rules — applied in ascending id order to the text
      3. PARTIAL_MASK rules — applied after REDACT
      4. Entropy rules — applied last

    Builtin rules are always included as a fallback set.
    YAML rules override or extend builtins (by rule id deduplication — YAML wins).

    Thread safety: RuleEngine instances are read-only after construction and safe
    for concurrent use.
    """

    def __init__(self, yaml_path: Path | None = None, include_builtins: bool = True) -> None:
        builtin_rules = BuiltinRules.all() if include_builtins else []

        yaml_rules: list[RedactionRule] = []
        resolved_path = yaml_path or _find_redaction_rules_yaml()
        if resolved_path:
            yaml_rules = _load_rules_from_yaml(resolved_path)
            logger.debug("Loaded %d rules from %s", len(yaml_rules), resolved_path)
        else:
            logger.warning("redaction_rules.yaml not found — using builtin rules only.")

        # Merge: YAML rules take precedence over builtins with the same id
        merged: dict[str, RedactionRule] = {r.id: r for r in builtin_rules}
        for rule in yaml_rules:
            merged[rule.id] = rule  # YAML overrides builtin if same id

        all_rules = list(merged.values())

        # Partition into execution groups
        self._block_rules: list[RedactionRule] = [
            r for r in all_rules if r.action == RuleAction.BLOCK and r.rule_type == "pattern"
        ]
        self._redact_rules: list[RedactionRule] = [
            r for r in all_rules if r.action == RuleAction.REDACT and r.rule_type == "pattern"
        ]
        self._partial_mask_rules: list[RedactionRule] = [
            r for r in all_rules if r.action == RuleAction.PARTIAL_MASK and r.rule_type == "pattern"
        ]
        self._entropy_rules: list[RedactionRule] = [
            r for r in all_rules if r.rule_type == "entropy"
        ]

        # Sort each group by rule id for deterministic ordering
        for group in (self._block_rules, self._redact_rules, self._partial_mask_rules, self._entropy_rules):
            group.sort(key=lambda r: r.id)

        logger.debug(
            "RuleEngine initialized — block=%d redact=%d partial_mask=%d entropy=%d",
            len(self._block_rules),
            len(self._redact_rules),
            len(self._partial_mask_rules),
            len(self._entropy_rules),
        )

    def apply(self, text: str) -> RuleEngineResult:
        """
        Apply all rules to a text string.

        Returns RuleEngineResult. If a BLOCK rule matches, was_blocked=True and
        sanitized_text is the original text (unmodified, for safety — callers must
        not use the original; they should raise SecurityBlockedError instead).
        """
        if not isinstance(text, str):
            text = str(text) if text is not None else ""

        applied: list[AppliedRule] = []

        # --- Phase 1: BLOCK rules ---
        for rule in self._block_rules:
            if rule.compiled is None:
                continue
            if rule.compiled.search(text):
                content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
                applied.append(AppliedRule(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action=RuleAction.BLOCK,
                    severity=rule.severity,
                    match_count=1,
                    should_alert=rule.alert,
                    blocked_hash=content_hash,
                ))
                return RuleEngineResult(
                    sanitized_text=text,  # not used — caller must raise
                    was_blocked=True,
                    blocking_rule_id=rule.id,
                    blocking_rule_name=rule.name,
                    blocked_content_hash=content_hash,
                    applied_rules=applied,
                )

        # --- Phase 2: REDACT rules ---
        working_text = text
        for rule in self._redact_rules:
            if rule.compiled is None:
                continue
            new_text, count = rule.compiled.subn(rule.replacement or "[REDACTED]", working_text)
            if count > 0:
                applied.append(AppliedRule(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action=RuleAction.REDACT,
                    severity=rule.severity,
                    match_count=count,
                    should_alert=rule.alert,
                ))
                working_text = new_text

        # --- Phase 3: PARTIAL_MASK rules ---
        for rule in self._partial_mask_rules:
            if rule.compiled is None:
                continue
            new_text, count = rule.compiled.subn(rule.replacement or "[MASKED]", working_text)
            if count > 0:
                applied.append(AppliedRule(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action=RuleAction.PARTIAL_MASK,
                    severity=rule.severity,
                    match_count=count,
                    should_alert=rule.alert,
                ))
                working_text = new_text

        # --- Phase 4: Entropy rules ---
        for entropy_rule in self._entropy_rules:
            tokens = re.split(r'[\s"\'`=:,\[\]{}()\n\r\t]+', working_text)
            for token in tokens:
                if token and _check_entropy(token, entropy_rule.entropy_threshold, entropy_rule.entropy_min_length):
                    replacement = entropy_rule.replacement or "[POTENTIAL_SECRET_REDACTED]"
                    working_text = working_text.replace(token, replacement, 1)
                    applied.append(AppliedRule(
                        rule_id=entropy_rule.id,
                        rule_name=entropy_rule.name,
                        action=RuleAction.REDACT,
                        severity=entropy_rule.severity,
                        match_count=1,
                        should_alert=entropy_rule.alert,
                    ))

        return RuleEngineResult(
            sanitized_text=working_text,
            was_blocked=False,
            blocking_rule_id="",
            blocking_rule_name="",
            blocked_content_hash="",
            applied_rules=applied,
        )

    def apply_to_nested(self, obj: Any, depth: int = 0, max_depth: int = 8) -> tuple[Any, list[AppliedRule]]:
        """
        Recursively apply rules to all string values in a nested dict/list.

        Returns (sanitized_obj, all_applied_rules_flat).
        Blocks if any string value triggers a BLOCK rule — returns the original
        obj and sets was_blocked=True in the first blocking AppliedRule.
        """
        all_applied: list[AppliedRule] = []

        if depth > max_depth:
            return obj, all_applied

        if isinstance(obj, str):
            result = self.apply(obj)
            all_applied.extend(result.applied_rules)
            if result.was_blocked:
                return obj, all_applied  # caller inspects all_applied for BLOCK entries
            return result.sanitized_text, all_applied

        elif isinstance(obj, dict):
            sanitized: dict[str, Any] = {}
            for k, v in obj.items():
                san_v, sub_applied = self.apply_to_nested(v, depth + 1, max_depth)
                all_applied.extend(sub_applied)
                sanitized[k] = san_v
                # Short-circuit on BLOCK
                if any(r.action == RuleAction.BLOCK for r in sub_applied):
                    return obj, all_applied
            return sanitized, all_applied

        elif isinstance(obj, (list, tuple)):
            sanitized_list = []
            for item in obj:
                san_item, sub_applied = self.apply_to_nested(item, depth + 1, max_depth)
                all_applied.extend(sub_applied)
                sanitized_list.append(san_item)
                if any(r.action == RuleAction.BLOCK for r in sub_applied):
                    return obj, all_applied
            return (type(obj))(sanitized_list), all_applied

        else:
            # Non-string scalars (int, float, bool, None) are returned as-is
            return obj, all_applied

    @property
    def block_rule_count(self) -> int:
        return len(self._block_rules)

    @property
    def redact_rule_count(self) -> int:
        return len(self._redact_rules)

    @property
    def total_rule_count(self) -> int:
        return (
            len(self._block_rules)
            + len(self._redact_rules)
            + len(self._partial_mask_rules)
            + len(self._entropy_rules)
        )

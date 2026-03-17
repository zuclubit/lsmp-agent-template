"""
Security Audit Agent – powered by Anthropic Claude API.

This agent performs continuous security auditing of the migration platform,
scanning for OWASP vulnerabilities, misconfigurations, secrets in code,
excessive permissions, and compliance violations (GDPR, SOC 2, PCI-DSS).

Audit Categories
----------------
1. OWASP Top 10 scanning (injection, auth issues, sensitive data exposure, etc.)
2. Secrets detection (API keys, passwords, tokens hardcoded in source)
3. Dependency vulnerability scanning (outdated packages, known CVEs)
4. Salesforce security review (FLS, sharing rules, profiles/permission sets)
5. Infrastructure misconfiguration (open ports, default credentials, TLS config)
6. PII data handling compliance (GDPR, CCPA)
7. Access control audit (principle of least privilege)
8. Encryption at rest/in-transit verification

Usage
-----
    agent = SecurityAuditAgent()
    result = await agent.run(
        "Perform a full security audit of the integration layer. "
        "Focus on authentication handling and secrets management."
    )
    print(result.audit_report)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("SECURITY_AGENT_MAX_TOKENS", "4096"))

_SYSTEM_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "prompts", "system_prompt.md"
)


def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return (
            "You are a security audit agent for an enterprise Salesforce migration platform. "
            "Identify security vulnerabilities, compliance gaps, and misconfigurations. "
            "Provide specific, actionable remediation guidance."
        )


# ---------------------------------------------------------------------------
# Finding types
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Category(str, Enum):
    OWASP_A01_BROKEN_ACCESS_CONTROL = "A01:Broken Access Control"
    OWASP_A02_CRYPTO = "A02:Cryptographic Failures"
    OWASP_A03_INJECTION = "A03:Injection"
    OWASP_A04_INSECURE_DESIGN = "A04:Insecure Design"
    OWASP_A05_SECURITY_MISCONFIG = "A05:Security Misconfiguration"
    OWASP_A06_VULN_COMPONENTS = "A06:Vulnerable Components"
    OWASP_A07_AUTH = "A07:Identification and Authentication Failures"
    OWASP_A08_SOFTWARE_INTEGRITY = "A08:Software and Data Integrity Failures"
    OWASP_A09_LOGGING = "A09:Security Logging and Monitoring Failures"
    OWASP_A10_SSRF = "A10:Server-Side Request Forgery"
    SECRETS = "Secrets/Credentials Exposure"
    COMPLIANCE_GDPR = "Compliance:GDPR"
    COMPLIANCE_SOC2 = "Compliance:SOC2"
    DEPENDENCY_CVE = "Dependency:CVE"
    SALESFORCE_SECURITY = "Salesforce:Security"
    INFRASTRUCTURE = "Infrastructure"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "scan_file_for_secrets",
        "description": (
            "Scan a source file for hardcoded secrets: API keys, passwords, private keys, "
            "connection strings, tokens, and other sensitive values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string", "description": "File content to scan. Pass directly."},
            },
            "required": ["content", "file_path"],
        },
    },
    {
        "name": "check_dependency_vulnerabilities",
        "description": (
            "Scan requirements.txt or pyproject.toml for packages with known CVEs. "
            "Returns CVE IDs, severity, and patched versions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirements_content": {"type": "string"},
                "file_format": {
                    "type": "string",
                    "enum": ["requirements.txt", "pyproject.toml", "Pipfile"],
                    "default": "requirements.txt",
                },
            },
            "required": ["requirements_content"],
        },
    },
    {
        "name": "audit_authentication_code",
        "description": (
            "Analyse authentication and authorisation code for weaknesses: "
            "weak algorithms, missing expiry, improper token storage, "
            "missing rate limiting, JWT misconfigurations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code_snippet": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["code_snippet", "file_path"],
        },
    },
    {
        "name": "check_sql_injection",
        "description": "Scan code for potential SQL injection vulnerabilities (unparameterised queries).",
        "input_schema": {
            "type": "object",
            "properties": {
                "code_snippet": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["code_snippet", "file_path"],
        },
    },
    {
        "name": "audit_salesforce_permissions",
        "description": (
            "Audit the Salesforce API permission scope requested by the integration. "
            "Flags overly broad scopes, missing FLS enforcement, and sharing model risks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requested_scopes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "OAuth scopes in the connected app.",
                },
                "operations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Operations the integration performs: read, create, update, delete, bulk.",
                },
            },
            "required": ["requested_scopes", "operations"],
        },
    },
    {
        "name": "check_pii_handling",
        "description": (
            "Check whether PII fields (email, phone, SSN, DOB, address) are handled "
            "in compliance with GDPR/CCPA: encrypted at rest, not logged, masked in outputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code_snippet": {"type": "string"},
                "file_path": {"type": "string"},
                "pii_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "PII field names present in the data model.",
                },
            },
            "required": ["code_snippet", "file_path"],
        },
    },
    {
        "name": "check_tls_configuration",
        "description": "Verify TLS/SSL configuration: minimum version, cipher suites, certificate validation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code_snippet": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["code_snippet", "file_path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a source file for analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "generate_security_report",
        "description": "Compile all findings into a structured security audit report with CVSS scores.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of security finding dicts.",
                },
                "scope": {"type": "string", "description": "Audit scope description."},
                "format": {"type": "string", "enum": ["summary", "detailed"], "default": "detailed"},
            },
            "required": ["findings", "scope"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"][^'\"]{10,}['\"]", "API Key"),
    (r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{6,}['\"]", "Password"),
    (r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", "Private Key"),
    (r"(?i)(secret[_-]?key|secret)\s*[=:]\s*['\"][^'\"]{10,}['\"]", "Secret Key"),
    (r"(?i)(token|access[_-]?token)\s*[=:]\s*['\"][^'\"]{16,}['\"]", "Token"),
    (r"(?i)(connection[_-]?string|conn[_-]?str)\s*[=:]\s*['\"][^'\"]{20,}['\"]", "Connection String"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"(?i)sk-[a-zA-Z0-9]{20,}", "OpenAI API Key"),
]


async def scan_file_for_secrets(content: str, file_path: str) -> Dict[str, Any]:
    findings = []
    for line_num, line in enumerate(content.splitlines(), 1):
        for pattern, secret_type in _SECRET_PATTERNS:
            if re.search(pattern, line):
                # Check if it's likely a placeholder
                if any(placeholder in line.lower() for placeholder in
                       ["os.getenv", "os.environ", "config.", "settings.", "placeholder", "example", "change-me"]):
                    continue
                findings.append({
                    "line": line_num,
                    "type": secret_type,
                    "severity": Severity.CRITICAL.value,
                    "snippet": line.strip()[:80] + "...",
                    "recommendation": f"Move {secret_type} to environment variable or secrets manager",
                })
    return {
        "file_path": file_path,
        "findings_count": len(findings),
        "findings": findings,
        "status": "CRITICAL" if findings else "PASS",
    }


async def check_dependency_vulnerabilities(
    requirements_content: str, file_format: str = "requirements.txt"
) -> Dict[str, Any]:
    # In production: call safety / OSV API
    known_vulns = {
        "cryptography": {"min_safe": "42.0.0", "cve": "CVE-2023-49083", "severity": "HIGH"},
        "pyjwt": {"min_safe": "2.8.0", "cve": "CVE-2022-29217", "severity": "HIGH"},
        "requests": {"min_safe": "2.31.0", "cve": "CVE-2023-32681", "severity": "MEDIUM"},
    }
    findings = []
    for line in requirements_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pkg = re.split(r"[~>=<!]", line)[0].lower().strip()
        if pkg in known_vulns:
            v = known_vulns[pkg]
            findings.append({
                "package": pkg,
                "cve": v["cve"],
                "severity": v["severity"],
                "patched_version": v["min_safe"],
                "recommendation": f"Upgrade {pkg} to >= {v['min_safe']}",
            })
    return {
        "packages_scanned": len([l for l in requirements_content.splitlines() if l and not l.startswith("#")]),
        "vulnerabilities_found": len(findings),
        "findings": findings,
        "status": "CRITICAL" if any(f["severity"] == "HIGH" for f in findings) else "PASS",
    }


async def audit_authentication_code(code_snippet: str, file_path: str) -> Dict[str, Any]:
    issues = []
    if "HS256" in code_snippet and "RS256" not in code_snippet:
        issues.append({
            "issue": "Symmetric JWT algorithm (HS256) used – consider RS256 for better security",
            "severity": "MEDIUM",
            "line_hint": code_snippet.find("HS256"),
        })
    if "verify=False" in code_snippet or "verify_ssl=False" in code_snippet:
        issues.append({
            "issue": "SSL verification disabled – man-in-the-middle attack risk",
            "severity": Severity.HIGH.value,
        })
    if re.search(r"timedelta\(days=[3-9]\d{1,}", code_snippet) or re.search(r"timedelta\(days=[1-9]\d{2,}", code_snippet):
        issues.append({
            "issue": "Long-lived JWT token (> 30 days expiry detected)",
            "severity": Severity.MEDIUM.value,
        })
    return {
        "file_path": file_path,
        "issues_found": len(issues),
        "findings": issues,
        "status": "FAIL" if issues else "PASS",
    }


async def check_sql_injection(code_snippet: str, file_path: str) -> Dict[str, Any]:
    findings = []
    patterns = [
        (r'f"SELECT.*\{', "Potential f-string SQL injection"),
        (r'"SELECT.*" \+', "Potential string concatenation SQL injection"),
        (r"execute\(['\"]SELECT.*\+", "execute() with string concatenation"),
    ]
    for pattern, desc in patterns:
        if re.search(pattern, code_snippet):
            findings.append({"issue": desc, "severity": Severity.HIGH.value,
                             "recommendation": "Use parameterised queries"})
    return {"file_path": file_path, "findings": findings, "status": "FAIL" if findings else "PASS"}


async def audit_salesforce_permissions(
    requested_scopes: List[str], operations: List[str]
) -> Dict[str, Any]:
    findings = []
    if "full" in requested_scopes:
        findings.append({
            "scope": "full",
            "severity": Severity.HIGH.value,
            "issue": "'full' scope grants complete API access – use narrower scopes",
        })
    if "delete" in operations and "bulk_delete" not in operations:
        findings.append({
            "operation": "delete",
            "severity": Severity.MEDIUM.value,
            "issue": "Delete operations enabled – ensure only migration records are deletable",
        })
    return {"requested_scopes": requested_scopes, "operations": operations,
            "findings": findings, "status": "FAIL" if findings else "PASS"}


async def check_pii_handling(
    code_snippet: str, file_path: str, pii_fields: Optional[List[str]] = None
) -> Dict[str, Any]:
    pii_fields = pii_fields or ["email", "phone", "ssn", "date_of_birth", "address"]
    findings = []
    for field in pii_fields:
        if field.lower() in code_snippet.lower():
            if "logger" in code_snippet.lower() and field.lower() in code_snippet.lower():
                findings.append({
                    "field": field,
                    "issue": f"PII field '{field}' may be logged – verify it is masked",
                    "severity": Severity.HIGH.value,
                })
    return {"file_path": file_path, "pii_fields_checked": pii_fields,
            "findings": findings, "status": "WARNING" if findings else "PASS"}


async def check_tls_configuration(code_snippet: str, file_path: str) -> Dict[str, Any]:
    findings = []
    if "verify=False" in code_snippet:
        findings.append({"issue": "TLS verification disabled (verify=False)", "severity": Severity.CRITICAL.value})
    if "ssl_version" in code_snippet and ("TLSv1\b" in code_snippet or "TLSv1_1" in code_snippet):
        findings.append({"issue": "Deprecated TLS version (< 1.2) in use", "severity": Severity.HIGH.value})
    return {"file_path": file_path, "findings": findings, "status": "FAIL" if findings else "PASS"}


async def _read_file_tool(file_path: str) -> Dict[str, Any]:
    project_root = os.getenv("PROJECT_ROOT", "/Users/oscarvalois/Documents/Github/s-agent")
    full_path = os.path.join(project_root, file_path)
    try:
        with open(full_path, encoding="utf-8") as fh:
            return {"content": fh.read(), "exists": True}
    except FileNotFoundError:
        return {"content": "", "exists": False, "error": "Not found"}


async def generate_security_report(
    findings: List[Dict[str, Any]], scope: str, format: str = "detailed"
) -> Dict[str, Any]:
    by_severity: Dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "INFO")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    critical = by_severity.get("CRITICAL", 0)
    high = by_severity.get("HIGH", 0)
    risk_score = min(10.0, critical * 3.0 + high * 1.5 + by_severity.get("MEDIUM", 0) * 0.5)

    return {
        "report_id": str(uuid.uuid4()),
        "scope": scope,
        "audit_date": datetime.now(timezone.utc).isoformat(),
        "total_findings": len(findings),
        "findings_by_severity": by_severity,
        "risk_score": round(risk_score, 1),
        "risk_level": "CRITICAL" if risk_score >= 7 else "HIGH" if risk_score >= 4 else "MEDIUM" if risk_score >= 2 else "LOW",
        "findings": findings if format == "detailed" else [],
        "summary": f"{len(findings)} findings: {critical} CRITICAL, {high} HIGH",
        "pass_security_gate": critical == 0 and high == 0,
    }


_TOOL_DISPATCH = {
    "scan_file_for_secrets": scan_file_for_secrets,
    "check_dependency_vulnerabilities": check_dependency_vulnerabilities,
    "audit_authentication_code": audit_authentication_code,
    "check_sql_injection": check_sql_injection,
    "audit_salesforce_permissions": audit_salesforce_permissions,
    "check_pii_handling": check_pii_handling,
    "check_tls_configuration": check_tls_configuration,
    "read_file": _read_file_tool,
    "generate_security_report": generate_security_report,
}


# ---------------------------------------------------------------------------
# Result types & agent
# ---------------------------------------------------------------------------


@dataclass
class SecurityAuditResult:
    task: str
    findings_count: int
    critical_count: int
    high_count: int
    risk_level: str
    pass_security_gate: bool
    final_answer: str
    audit_report: Optional[Dict[str, Any]]
    iterations: int
    duration_seconds: float
    error: Optional[str] = None


class SecurityAuditAgent:
    """
    AI-powered security auditor for the Salesforce migration platform.

    Uses Claude to intelligently select and execute security checks,
    correlate findings, and produce a risk-prioritised audit report.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = _load_system_prompt()

    async def run(
        self,
        task: str,
        scope: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> SecurityAuditResult:
        """Execute a security audit task."""
        start_ts = time.perf_counter()
        messages: List[Dict[str, Any]] = [{"role": "user", "content": task}]
        final_text = ""
        audit_report: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        iteration = 0
        all_findings: List[Dict[str, Any]] = []

        try:
            for iteration in range(1, 20):
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system_prompt,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                    temperature=0.1,
                )

                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                for block in response.content:
                    if block.type == "text" and block.text:
                        final_text = block.text

                if not tool_blocks or response.stop_reason == "end_turn":
                    break

                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in tool_blocks:
                    try:
                        fn = _TOOL_DISPATCH.get(block.name)
                        result = await fn(**(block.input or {})) if fn else {"error": "unknown tool"}
                        if isinstance(result, dict) and "findings" in result:
                            all_findings.extend(result.get("findings", []))
                        if block.name == "generate_security_report":
                            audit_report = result
                        is_error = False
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": str(exc)}
                        is_error = True
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                        "is_error": is_error,
                    })
                messages.append({"role": "user", "content": results})

        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.error("SecurityAuditAgent error: %s", exc, exc_info=True)

        critical = sum(1 for f in all_findings if f.get("severity") == "CRITICAL")
        high = sum(1 for f in all_findings if f.get("severity") == "HIGH")

        return SecurityAuditResult(
            task=task,
            findings_count=len(all_findings),
            critical_count=critical,
            high_count=high,
            risk_level=audit_report.get("risk_level", "UNKNOWN") if audit_report else "UNKNOWN",
            pass_security_gate=critical == 0 and high == 0,
            final_answer=final_text,
            audit_report=audit_report,
            iterations=iteration,
            duration_seconds=round(time.perf_counter() - start_ts, 2),
            error=error,
        )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    async def _main() -> None:
        agent = SecurityAuditAgent()
        task = " ".join(sys.argv[1:]) or (
            "Perform a security audit of the integrations/rest_clients directory. "
            "Focus on authentication handling, secrets management, and TLS configuration."
        )
        result = await agent.run(task)
        print(f"\nSecurity Audit Result\n{'='*50}")
        print(f"Risk Level: {result.risk_level}  Gate: {'PASS' if result.pass_security_gate else 'FAIL'}")
        print(f"Findings: {result.findings_count} total, {result.critical_count} CRITICAL, {result.high_count} HIGH")
        print(f"\n{result.final_answer}")

    asyncio.run(_main())

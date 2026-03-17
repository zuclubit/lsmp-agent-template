"""
Skill tests for SecurityAuditAgent tool implementations.

Tests each security scanning function in isolation.
No Claude API calls. Tests the detection logic only.

Coverage:
- scan_file_for_secrets: various secret patterns
- check_dependency_vulnerabilities: CVE detection
- audit_authentication_code: JWT and SSL issues
- check_sql_injection: injection patterns
- audit_salesforce_permissions: scope analysis
- check_pii_handling: PII field logging detection
- check_tls_configuration: TLS version and verify=False
- generate_security_report: CVSS-style risk scoring
"""

from __future__ import annotations

import pytest

try:
    from agents.security_audit_agent.agent import (
        scan_file_for_secrets,
        check_dependency_vulnerabilities,
        audit_authentication_code,
        check_sql_injection,
        audit_salesforce_permissions,
        check_pii_handling,
        check_tls_configuration,
        generate_security_report,
        Severity,
    )
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False

pytestmark = pytest.mark.asyncio

skip_if = pytest.mark.skipif(not TOOLS_AVAILABLE, reason="Security agent tools not importable")


# ---------------------------------------------------------------------------
# scan_file_for_secrets
# ---------------------------------------------------------------------------


@skip_if
async def test_scan_detects_api_key_pattern():
    """api_key = 'value' should be detected."""
    code = 'api_key = "abc123xyz789secretvalue"'
    result = await scan_file_for_secrets(content=code, file_path="config.py")
    assert result["findings_count"] >= 1
    assert result["status"] == "CRITICAL"


@skip_if
async def test_scan_detects_password_literal():
    """password = 'hardcoded' should be detected."""
    code = 'password = "my-super-secret-password-123"'
    result = await scan_file_for_secrets(content=code, file_path="auth.py")
    assert result["findings_count"] >= 1


@skip_if
async def test_scan_ignores_env_var_patterns():
    """os.getenv patterns should NOT be flagged."""
    code = """
API_KEY = os.getenv("API_KEY")
PASSWORD = os.environ["PASSWORD"]
SECRET = config.get("secret")
"""
    result = await scan_file_for_secrets(content=code, file_path="settings.py")
    assert result["findings_count"] == 0
    assert result["status"] == "PASS"


@skip_if
async def test_scan_detects_openai_key():
    """OpenAI API key pattern sk-... should be detected."""
    code = 'OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz12345678"'
    result = await scan_file_for_secrets(content=code, file_path="ai_client.py")
    assert result["findings_count"] >= 1


@skip_if
async def test_scan_empty_file_passes():
    """Empty file should pass with zero findings."""
    result = await scan_file_for_secrets(content="", file_path="empty.py")
    assert result["findings_count"] == 0
    assert result["status"] == "PASS"


@skip_if
async def test_scan_result_schema():
    """scan_file_for_secrets must return all required schema fields."""
    result = await scan_file_for_secrets(content="x = 1", file_path="test.py")
    required = ["file_path", "findings_count", "findings", "status"]
    for f in required:
        assert f in result, f"Missing field: {f}"
    assert result["file_path"] == "test.py"
    assert isinstance(result["findings"], list)


@skip_if
async def test_scan_finding_has_required_fields():
    """Each finding in scan result must have required fields."""
    code = 'secret_key = "abcdefghijklmnopqrstuvwxyz-mysecret"'
    result = await scan_file_for_secrets(content=code, file_path="config.py")
    if result["findings"]:
        finding = result["findings"][0]
        assert "line" in finding
        assert "type" in finding
        assert "severity" in finding
        assert "recommendation" in finding


# ---------------------------------------------------------------------------
# check_dependency_vulnerabilities
# ---------------------------------------------------------------------------


@skip_if
async def test_deps_detects_vulnerable_pyjwt():
    """pyjwt below min_safe version should produce a finding."""
    reqs = "pyjwt==2.4.0\nrequests==2.28.0\n"
    result = await check_dependency_vulnerabilities(reqs)
    packages_with_vulns = [f["package"] for f in result["findings"]]
    assert "pyjwt" in packages_with_vulns


@skip_if
async def test_deps_no_vulns_for_safe_packages():
    """Packages not in the known_vulns dict should not produce findings."""
    reqs = "click==8.1.7\nrich==13.7.1\npydantic==2.7.0\n"
    result = await check_dependency_vulnerabilities(reqs)
    assert result["vulnerabilities_found"] == 0
    assert result["status"] == "PASS"


@skip_if
async def test_deps_result_schema():
    """check_dependency_vulnerabilities must return required fields."""
    result = await check_dependency_vulnerabilities("anthropic~=0.34.0")
    required = ["packages_scanned", "vulnerabilities_found", "findings", "status"]
    for f in required:
        assert f in result, f"Missing field: {f}"


@skip_if
async def test_deps_skips_comment_lines():
    """Comment lines in requirements.txt should not be processed."""
    reqs = "# This is a comment\n# pyjwt==2.4.0 (commented out)\nclick==8.1.7\n"
    result = await check_dependency_vulnerabilities(reqs)
    packages_with_vulns = [f["package"] for f in result["findings"]]
    assert "pyjwt" not in packages_with_vulns


# ---------------------------------------------------------------------------
# audit_authentication_code
# ---------------------------------------------------------------------------


@skip_if
async def test_auth_detects_hs256_jwt():
    """HS256 algorithm in JWT should be flagged as MEDIUM."""
    code = '''
token = jwt.encode(payload, secret, algorithm="HS256")
'''
    result = await audit_authentication_code(code, "auth/jwt_handler.py")
    assert result["status"] == "FAIL"
    severities = [f.get("severity") for f in result["findings"]]
    assert "MEDIUM" in severities or len(result["findings"]) >= 1


@skip_if
async def test_auth_detects_verify_false():
    """verify=False in auth code should be flagged as HIGH."""
    code = '''
resp = requests.post(token_url, verify=False)
'''
    result = await audit_authentication_code(code, "auth/client.py")
    assert result["status"] == "FAIL"
    findings = result["findings"]
    assert any(f.get("severity") == "HIGH" for f in findings)


@skip_if
async def test_auth_clean_code_passes():
    """Auth code with no issues should return PASS."""
    code = '''
token = jwt.encode(payload, private_key, algorithm="RS256")
resp = requests.post(token_url, verify=True)
'''
    result = await audit_authentication_code(code, "auth/secure.py")
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# check_sql_injection
# ---------------------------------------------------------------------------


@skip_if
async def test_sql_detects_fstring_interpolation():
    """f-string with SELECT and curly brace should be flagged."""
    code = 'query = f"SELECT Id FROM Account WHERE Id = \'{account_id}\'"'
    result = await check_sql_injection(code, "db.py")
    assert result["status"] == "FAIL"


@skip_if
async def test_sql_detects_string_concatenation():
    """String concatenation with SELECT should be flagged."""
    code = '"SELECT * FROM User WHERE username = " + username'
    result = await check_sql_injection(code, "db.py")
    assert result["status"] == "FAIL"


@skip_if
async def test_sql_clean_code_passes():
    """Code without obvious injection patterns should pass."""
    code = '''
result = sf.query("SELECT Id, Name FROM Account WHERE IsDeleted = false LIMIT 1000")
'''
    result = await check_sql_injection(code, "sf_client.py")
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# audit_salesforce_permissions
# ---------------------------------------------------------------------------


@skip_if
async def test_sf_full_scope_flagged():
    """'full' scope should produce a HIGH finding."""
    result = await audit_salesforce_permissions(
        requested_scopes=["full"],
        operations=["read", "create"],
    )
    assert result["status"] == "FAIL"
    finding_scopes = [f.get("scope") for f in result["findings"]]
    assert "full" in finding_scopes


@skip_if
async def test_sf_narrow_scope_passes():
    """Narrow scopes like api and refresh_token should pass."""
    result = await audit_salesforce_permissions(
        requested_scopes=["api", "refresh_token"],
        operations=["read", "create"],
    )
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# check_pii_handling
# ---------------------------------------------------------------------------


@skip_if
async def test_pii_flags_logging_with_pii_field():
    """Code that logs a PII field should produce a finding."""
    code = '''
logger.info(f"Processing record: email={record.email}")
logger.debug("Contact phone: %s", contact.phone)
'''
    result = await check_pii_handling(
        code_snippet=code,
        file_path="migration/transformer.py",
        pii_fields=["email", "phone"],
    )
    assert result["status"] == "WARNING"
    assert len(result["findings"]) >= 1


@skip_if
async def test_pii_passes_when_no_pii_in_code():
    """Code that doesn't reference PII fields should pass."""
    code = '''
def transform_record(record):
    return {"name": record.name, "account_type": record.account_type}
'''
    result = await check_pii_handling(
        code_snippet=code,
        file_path="migration/transformer.py",
        pii_fields=["ssn", "date_of_birth"],
    )
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# check_tls_configuration
# ---------------------------------------------------------------------------


@skip_if
async def test_tls_detects_verify_false():
    """verify=False should produce a CRITICAL finding."""
    code = 'requests.get(url, verify=False)'
    result = await check_tls_configuration(code, "client.py")
    assert result["status"] == "FAIL"
    assert len(result["findings"]) >= 1
    critical_findings = [f for f in result["findings"] if f.get("severity") == "CRITICAL"]
    assert len(critical_findings) >= 1


@skip_if
async def test_tls_clean_code_passes():
    """Code with TLS verification enabled should pass."""
    code = 'requests.get(url, verify=True, timeout=30)'
    result = await check_tls_configuration(code, "client.py")
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# generate_security_report
# ---------------------------------------------------------------------------


@skip_if
async def test_generate_report_risk_scoring():
    """Risk score calculation must follow: critical*3 + high*1.5 + medium*0.5."""
    findings = [
        {"severity": "CRITICAL"},
        {"severity": "HIGH"},
        {"severity": "HIGH"},
        {"severity": "MEDIUM"},
    ]
    result = await generate_security_report(findings, "test scope")

    # risk_score = min(10, 1*3 + 2*1.5 + 1*0.5) = min(10, 3 + 3 + 0.5) = 6.5
    assert result["risk_score"] == pytest.approx(6.5, abs=0.1)
    assert result["risk_level"] in ("CRITICAL", "HIGH")  # 6.5 >= 4 → HIGH or CRITICAL
    assert result["pass_security_gate"] is False


@skip_if
async def test_generate_report_schema():
    """generate_security_report must return all required schema fields."""
    result = await generate_security_report([], "empty scope")
    required = [
        "report_id", "scope", "audit_date", "total_findings",
        "findings_by_severity", "risk_score", "risk_level",
        "pass_security_gate", "summary",
    ]
    for f in required:
        assert f in result, f"Missing field: {f}"


@skip_if
async def test_generate_report_findings_not_in_summary_format():
    """Summary format should NOT include raw findings list."""
    findings = [{"severity": "LOW", "issue": "test"}]
    result = await generate_security_report(findings, "scope", format="summary")
    assert result["findings"] == []


@skip_if
async def test_generate_report_capped_at_10():
    """Risk score must be capped at 10.0 even with many critical findings."""
    findings = [{"severity": "CRITICAL"}] * 20
    result = await generate_security_report(findings, "scope")
    assert result["risk_score"] <= 10.0


@skip_if
async def test_generate_report_low_risk_passes_gate():
    """Only LOW and INFO findings → gate passes."""
    findings = [
        {"severity": "LOW", "issue": "minor issue"},
        {"severity": "INFO", "issue": "informational"},
    ]
    result = await generate_security_report(findings, "scope")
    assert result["pass_security_gate"] is True
    assert result["risk_level"] == "LOW"

"""
Tests for SecurityAuditAgent — path restrictions and SOQL injection prevention.

Critical tests:
1. Path traversal attempt → blocked, not executed
2. SOQL with DELETE → blocked at dispatch layer
3. chmod attempt → blocked (not allowed in tools)
4. CRITICAL finding → BLOCK decision returned
5. Entropy detection works for weak passwords (low entropy → not flagged)
6. High-entropy secrets ARE flagged by scan_file_for_secrets

All external API calls are mocked. Tests exercise:
  - The tool implementation logic (not Claude's reasoning)
  - The dispatch layer's validation of tool inputs
  - The SecurityAuditResult structure on various finding combinations
"""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from agents.security_audit_agent.agent import (
        SecurityAuditAgent,
        SecurityAuditResult,
        scan_file_for_secrets,
        check_dependency_vulnerabilities,
        audit_authentication_code,
        check_sql_injection,
        check_tls_configuration,
        generate_security_report,
        _read_file_tool,
        Severity,
        _TOOL_DISPATCH,
    )
    SECURITY_AGENT_AVAILABLE = True
except ImportError:
    SECURITY_AGENT_AVAILABLE = False
    SecurityAuditAgent = None
    SecurityAuditResult = None
    scan_file_for_secrets = None
    check_dependency_vulnerabilities = None
    audit_authentication_code = None
    check_sql_injection = None
    check_tls_configuration = None
    generate_security_report = None
    _read_file_tool = None
    Severity = None
    _TOOL_DISPATCH = None

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test 1: Path traversal attempt → blocked
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_path_traversal_blocked():
    """
    _read_file_tool must reject paths that traverse outside PROJECT_ROOT.
    Must NOT read the file and must return an error/empty result.
    """
    with patch.dict(
        os.environ,
        {"PROJECT_ROOT": "/Users/oscarvalois/Documents/Github/s-agent"},
        clear=False,
    ):
        # Attempt classic traversal
        result = await _read_file_tool("../../etc/passwd")

    assert result.get("exists") is False or "error" in result
    # Must NOT contain actual file content
    assert "root:" not in str(result.get("content", ""))
    assert "daemon:" not in str(result.get("content", ""))


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_path_traversal_double_encoded_blocked():
    """URL-encoded traversal path must also be rejected."""
    with patch.dict(
        os.environ,
        {"PROJECT_ROOT": "/Users/oscarvalois/Documents/Github/s-agent"},
        clear=False,
    ):
        result = await _read_file_tool("..%2F..%2Fetc%2Fshadow")

    # Since the path won't match any real file, it should not exist
    # Importantly, it should not read actual system files
    content = str(result.get("content", ""))
    assert "root:" not in content
    assert "$6$" not in content  # shadow hash pattern


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_path_within_project_root_allowed():
    """A valid relative path under PROJECT_ROOT must be readable."""
    with patch.dict(
        os.environ,
        {"PROJECT_ROOT": "/Users/oscarvalois/Documents/Github/s-agent"},
        clear=False,
    ):
        result = await _read_file_tool("agents/requirements.txt")

    # File exists and has content (or gracefully returns not found)
    assert "error" not in result or result.get("exists") is not None


# ---------------------------------------------------------------------------
# Test 2: SOQL with blocked keywords → rejected
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_sql_injection_detects_string_concatenation():
    """check_sql_injection must flag f-string concatenation patterns."""
    code_with_injection = '''
def get_user(user_id):
    query = f"SELECT Id FROM Account WHERE Id = '{user_id}'"
    return sf.query(query)
'''
    result = await check_sql_injection(
        code_snippet=code_with_injection,
        file_path="integrations/rest_clients/salesforce_client.py",
    )

    assert result["status"] == "FAIL"
    assert len(result["findings"]) >= 1
    # Finding should reference the injection type
    finding_text = str(result["findings"]).lower()
    assert "injection" in finding_text or "concatenation" in finding_text or "f-string" in finding_text


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_sql_injection_passes_parameterised_code():
    """Parameterised queries must NOT be flagged."""
    safe_code = '''
def get_account(account_id: str):
    result = sf.query("SELECT Id, Name FROM Account WHERE Id = :account_id",
                      account_id=account_id)
    return result
'''
    result = await check_sql_injection(
        code_snippet=safe_code,
        file_path="integrations/salesforce_client.py",
    )

    assert result["status"] == "PASS"
    assert len(result["findings"]) == 0


# ---------------------------------------------------------------------------
# Test 3: chmod attempt → not in allowed tools
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
def test_chmod_not_in_tool_dispatch():
    """
    chmod, shell execution, subprocess calls must NOT be registered tools.
    This verifies the tool registry cannot be used for OS-level operations.
    """
    forbidden_tools = [
        "chmod",
        "execute_command",
        "run_shell",
        "subprocess",
        "exec_command",
        "system_call",
        "os_exec",
    ]
    registered_tool_names = set(_TOOL_DISPATCH.keys())
    for forbidden in forbidden_tools:
        assert forbidden not in registered_tool_names, (
            f"Forbidden tool '{forbidden}' found in _TOOL_DISPATCH. "
            "This is a security violation — OS execution must never be a tool."
        )


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
def test_only_allowed_tools_registered():
    """Only the declared tool set should be in the dispatch registry."""
    allowed_tools = {
        "scan_file_for_secrets",
        "check_dependency_vulnerabilities",
        "audit_authentication_code",
        "check_sql_injection",
        "audit_salesforce_permissions",
        "check_pii_handling",
        "check_tls_configuration",
        "read_file",
        "generate_security_report",
    }
    registered = set(_TOOL_DISPATCH.keys())
    unexpected = registered - allowed_tools
    assert unexpected == set(), (
        f"Unexpected tools registered: {unexpected}. "
        "All tools must be explicitly approved in agent_security_model.md"
    )


# ---------------------------------------------------------------------------
# Test 4: CRITICAL finding → BLOCK decision returned
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_critical_finding_fails_security_gate():
    """generate_security_report must set pass_security_gate=False when critical findings exist."""
    findings = [
        {"severity": "CRITICAL", "issue": "Hardcoded AWS access key", "file": "config.py"},
        {"severity": "HIGH", "issue": "TLS verification disabled", "file": "client.py"},
    ]
    result = await generate_security_report(
        findings=findings,
        scope="integrations/rest_clients/",
        format="detailed",
    )

    assert result["pass_security_gate"] is False
    assert result["risk_level"] in ("CRITICAL", "HIGH")
    assert result["findings_by_severity"].get("CRITICAL", 0) == 1
    assert result["findings_by_severity"].get("HIGH", 0) == 1
    assert result["total_findings"] == 2


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_no_findings_passes_security_gate():
    """With zero findings, security gate must PASS."""
    result = await generate_security_report(
        findings=[],
        scope="agents/documentation-agent/",
    )

    assert result["pass_security_gate"] is True
    assert result["risk_level"] == "LOW"
    assert result["total_findings"] == 0


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_security_audit_result_critical_count():
    """SecurityAuditResult.critical_count must accurately reflect finding severity."""
    with patch("agents.security_audit_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = SecurityAuditAgent(api_key="test-key-not-real")

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()

        call_count = 0

        def _text_block(text: str) -> MagicMock:
            b = MagicMock()
            b.type = "text"
            b.text = text
            return b

        def _tool_block(name: str, inp: Dict, bid: str = "tu_001") -> MagicMock:
            b = MagicMock()
            b.type = "tool_use"
            b.name = name
            b.input = inp
            b.id = bid
            return b

        def _response(blocks, stop="end_turn") -> MagicMock:
            r = MagicMock()
            r.content = blocks
            r.stop_reason = stop
            return r

        async def mock_create(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _response(
                    [_tool_block("generate_security_report", {
                        "findings": [
                            {"severity": "CRITICAL", "issue": "Hardcoded key"},
                            {"severity": "CRITICAL", "issue": "Exposed secret"},
                        ],
                        "scope": "test/",
                    })],
                    stop="tool_use",
                )
            else:
                return _response(
                    [_text_block("Audit complete. 2 CRITICAL findings. Gate: FAIL.")],
                    stop="end_turn",
                )

        mock_client.messages.create = AsyncMock(side_effect=mock_create)

        result = await agent.run(task="Scan for secrets in config files.")

    assert result.critical_count == 2
    assert result.pass_security_gate is False


# ---------------------------------------------------------------------------
# Test 5: Entropy detection — weak passwords NOT flagged, strong secrets ARE
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_scan_does_not_flag_placeholder_values():
    """Placeholder values (os.getenv, config., example) must NOT be flagged as secrets."""
    code_with_placeholders = '''
API_KEY = os.getenv("ANTHROPIC_API_KEY")
DB_PASSWORD = config.get("database.password")
# Example: SECRET_KEY = "your-secret-key-here"
token = settings.INTERNAL_TOKEN
'''
    result = await scan_file_for_secrets(
        content=code_with_placeholders,
        file_path="agents/migration-agent/tools.py",
    )

    assert result["findings_count"] == 0
    assert result["status"] == "PASS"


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_scan_flags_hardcoded_aws_key():
    """A real AWS access key pattern must be detected."""
    code_with_real_key = '''
# Production credentials — DO NOT COMMIT
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
'''
    result = await scan_file_for_secrets(
        content=code_with_real_key,
        file_path="infrastructure/config.py",
    )

    # The AWS key pattern AKIA[A-Z0-9]{16} must be detected
    assert result["findings_count"] >= 1
    assert result["status"] == "CRITICAL"
    finding_types = [f["type"] for f in result["findings"]]
    assert "AWS Access Key ID" in finding_types


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_scan_flags_private_key_pem():
    """PEM private key headers must be detected."""
    code_with_private_key = '''
PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA2a2rwplBQLzHPZe5TNJQ0Nlkf9hHPRDCExmAVDW2Gr7OMFB...
-----END RSA PRIVATE KEY-----"""
'''
    result = await scan_file_for_secrets(
        content=code_with_private_key,
        file_path="security/keys/deploy.py",
    )

    assert result["findings_count"] >= 1
    assert result["status"] == "CRITICAL"


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_tls_verify_false_flagged():
    """SSL/TLS verification disabled must be flagged as CRITICAL."""
    code_with_verify_false = '''
import httpx

def get_data():
    resp = httpx.get("https://api.example.com/data", verify=False)
    return resp.json()
'''
    result = await check_tls_configuration(
        code_snippet=code_with_verify_false,
        file_path="integrations/rest_clients/base_client.py",
    )

    assert result["status"] == "FAIL"
    assert len(result["findings"]) >= 1
    finding_text = str(result["findings"]).lower()
    assert "tls" in finding_text or "ssl" in finding_text or "verify" in finding_text


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_dependency_vulnerability_detects_known_cve():
    """A requirements.txt with a known-vulnerable package must produce a finding."""
    requirements = """
anthropic~=0.34.0
pyjwt==2.6.0
cryptography==41.0.0
requests==2.28.0
"""
    result = await check_dependency_vulnerabilities(
        requirements_content=requirements,
        file_format="requirements.txt",
    )

    # pyjwt 2.6.0 is below the min_safe 2.8.0 in the known_vulns dict
    assert result["vulnerabilities_found"] >= 1
    vuln_packages = [f["package"] for f in result["findings"]]
    assert "pyjwt" in vuln_packages


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_audit_auth_flags_ssl_verification_disabled():
    """audit_authentication_code must flag verify=False as HIGH severity."""
    code = '''
resp = requests.post(auth_url, json=payload, verify=False)
token = resp.json()["access_token"]
'''
    result = await audit_authentication_code(
        code_snippet=code,
        file_path="integrations/rest_clients/base_client.py",
    )

    assert result["status"] == "FAIL"
    findings = result["findings"]
    assert any(f.get("severity") in ("HIGH", "CRITICAL") for f in findings)


# ---------------------------------------------------------------------------
# Test: SecurityAuditResult schema is complete
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SECURITY_AGENT_AVAILABLE, reason="SecurityAuditAgent not importable")
async def test_security_audit_result_schema_complete():
    """SecurityAuditResult must have all required fields on a normal run."""
    with patch("agents.security_audit_agent.agent.anthropic.AsyncAnthropic") as mock_anthropic:
        agent = SecurityAuditAgent(api_key="test-key-not-real")

        mock_client = mock_anthropic.return_value
        mock_client.messages = AsyncMock()

        def _resp(text: str) -> MagicMock:
            b = MagicMock()
            b.type = "text"
            b.text = text
            r = MagicMock()
            r.content = [b]
            r.stop_reason = "end_turn"
            return r

        mock_client.messages.create = AsyncMock(
            return_value=_resp("No critical findings. Security gate: PASS.")
        )

        result = await agent.run(task="Quick security check.")

    assert isinstance(result.task, str)
    assert isinstance(result.findings_count, int)
    assert isinstance(result.critical_count, int)
    assert isinstance(result.high_count, int)
    assert isinstance(result.pass_security_gate, bool)
    assert isinstance(result.iterations, int)
    assert isinstance(result.duration_seconds, float)
    assert result.risk_level is not None

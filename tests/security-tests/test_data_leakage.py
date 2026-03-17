"""
Data Leakage Prevention Tests — Phase 9, Security Suite.

Verifies that OutputSanitizer and ValidationLayer.sanitize_output correctly
redact all credential types, PII, high-entropy secrets, and partial-mask
Salesforce IDs before agent output reaches downstream consumers.

Compliance: FedRAMP SC-28, GDPR Art. 25 (data minimisation), CUI handling.
"""
from __future__ import annotations

import re
import string

import pytest

from validation.layer import (
    OutputSanitizer,
    SanitizationResult,
    ValidationLayer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sanitizer() -> OutputSanitizer:
    return OutputSanitizer()


@pytest.fixture
def layer() -> ValidationLayer:
    return ValidationLayer()


# ---------------------------------------------------------------------------
# Test 1: Anthropic API key stripped from output
# ---------------------------------------------------------------------------


def test_api_key_stripped_from_output(layer: ValidationLayer) -> None:
    """Output containing 'sk-ant-xxx' must have the key redacted to [REDACTED:ANTHROPIC_KEY]."""
    output = "The agent used API key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890 to connect."
    result = layer.sanitize_output(output)
    assert "sk-ant-" not in result.sanitized_text, "Anthropic API key must not appear in sanitized output"
    assert "[REDACTED:ANTHROPIC_KEY]" in result.sanitized_text
    assert result.was_modified
    rule_ids = [r.rule_id for r in result.redactions]
    assert "ANTHROPIC_KEY" in rule_ids


# ---------------------------------------------------------------------------
# Test 2: Salesforce bearer token stripped
# ---------------------------------------------------------------------------


def test_sf_token_stripped_from_output(layer: ValidationLayer) -> None:
    """Salesforce token in output must be redacted."""
    # Standard Salesforce instance token format: 00D + org ID + ! + session
    sf_token = "00D0b000000AbCdE!AQEAQLkZfO9Y8N2SomeRandomSalesforceToken1234567890xyz"
    output = f"Connected to Salesforce org with token: {sf_token}"
    result = layer.sanitize_output(output)
    assert sf_token not in result.sanitized_text, "SF token must not appear in sanitized output"
    assert "[REDACTED:SF_TOKEN]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 3: Vault token stripped
# ---------------------------------------------------------------------------


def test_vault_token_stripped_from_output(layer: ValidationLayer) -> None:
    """HashiCorp Vault token 'hvs.xxx' must be redacted."""
    vault_token = "hvs.CAESINm2hV8sL9x3tQ7KaGpDj1uFmNXoWeBRqYTvLwPzS4Op"
    output = f"Vault authentication successful. Token: {vault_token}"
    result = layer.sanitize_output(output)
    assert vault_token not in result.sanitized_text, "Vault token must not appear in sanitized output"
    assert "[REDACTED:VAULT_TOKEN]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 4: PEM private key stripped
# ---------------------------------------------------------------------------


def test_private_key_stripped_from_output(layer: ValidationLayer) -> None:
    """PEM-encoded private key block must be fully redacted."""
    pem_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA2a2rwplBQLF29amygykEMmYz0+Kcj3bKBp29Za3oFxWo\n"
        "MoreBase64EncodedKeyDataHereMIIEowIBAAKCAQEA2a2rwplBQL==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    output = f"Server certificate private key:\n{pem_key}\nEnd of key material."
    result = layer.sanitize_output(output)
    assert "BEGIN RSA PRIVATE KEY" not in result.sanitized_text
    assert "[REDACTED:PRIVATE_KEY]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 5: Email address redacted
# ---------------------------------------------------------------------------


def test_pii_email_redacted(layer: ValidationLayer) -> None:
    """Email address in output must be redacted to [EMAIL_REDACTED]."""
    output = "Contact the migration owner at john.doe@agency.gov for questions."
    result = layer.sanitize_output(output)
    assert "john.doe@agency.gov" not in result.sanitized_text
    assert "[EMAIL_REDACTED]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 6: SSN redacted and alert generated
# ---------------------------------------------------------------------------


def test_pii_ssn_redacted(layer: ValidationLayer) -> None:
    """SSN pattern must be redacted to [SSN_REDACTED] AND a compliance alert generated."""
    output = "Record ID 00123 has SSN 123-45-6789 flagged for review."
    result = layer.sanitize_output(output)
    assert "123-45-6789" not in result.sanitized_text
    assert "[SSN_REDACTED]" in result.sanitized_text
    # A compliance alert must be generated for SSN exposure
    assert len(result.alerts) > 0, "SSN detection must generate a compliance alert"
    assert any("SSN" in alert for alert in result.alerts)


# ---------------------------------------------------------------------------
# Test 7: Credit card number redacted
# ---------------------------------------------------------------------------


def test_pii_credit_card_redacted(layer: ValidationLayer) -> None:
    """Credit card number must be redacted to [CREDIT_CARD_REDACTED]."""
    output = "Payment record contains card number 4532015112830366 from 2019."
    result = layer.sanitize_output(output)
    assert "4532015112830366" not in result.sanitized_text
    assert "[CREDIT_CARD_REDACTED]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 8: Database connection string redacted
# ---------------------------------------------------------------------------


def test_db_connection_string_redacted(layer: ValidationLayer) -> None:
    """PostgreSQL connection string must be fully redacted."""
    conn_str = "postgresql://migration_user:S3cr3tP@ssw0rd!@db-prod.internal:5432/migration_db"
    output = f"Source database: {conn_str}"
    result = layer.sanitize_output(output)
    assert "S3cr3tP@ssw0rd!" not in result.sanitized_text
    assert "migration_user" not in result.sanitized_text
    assert "[REDACTED:DB_CONNECTION_STRING]" in result.sanitized_text
    assert result.was_modified


# ---------------------------------------------------------------------------
# Test 9: High-entropy string flagged as potential secret
# ---------------------------------------------------------------------------


def test_high_entropy_string_flagged(layer: ValidationLayer) -> None:
    """
    A 32+ character base64 string with high Shannon entropy must be flagged
    and redacted as a potential secret.
    """
    # This is a realistic-looking base64-encoded secret (high entropy)
    high_entropy_token = "Xk9mN3pQ7rL2vY8wZ1aB4cD6eF0gH5iJ"
    assert len(high_entropy_token) >= 32
    output = f"Internal token used for signing: {high_entropy_token}"
    result = layer.sanitize_output(output)
    # The high-entropy token must be redacted
    assert high_entropy_token not in result.sanitized_text, (
        f"High-entropy token '{high_entropy_token}' must be redacted"
    )
    assert result.was_modified
    rule_ids = [r.rule_id for r in result.redactions]
    assert "HIGH_ENTROPY_SECRET" in rule_ids


# ---------------------------------------------------------------------------
# Test 10: Multiple credentials in single output — all redacted
# ---------------------------------------------------------------------------


def test_multiple_secrets_in_output(layer: ValidationLayer) -> None:
    """Output containing 3 different credential types must have all redacted."""
    output = (
        "Debug dump from agent run:\n"
        "  anthropic_key = sk-ant-api03-Abc123DefGhi456JklMno789PqrStu012VwxYz\n"
        "  vault_token = hvs.CAESIB5jKmN2pQ9rStUvWxYz1234567890abcDEFghij\n"
        "  db_url = postgresql://admin:hunter2@db.internal/prod\n"
        "  status = complete\n"
    )
    result = layer.sanitize_output(output)
    # None of the raw credentials must be present
    assert "sk-ant-api03-" not in result.sanitized_text
    assert "hvs.CAES" not in result.sanitized_text
    assert "hunter2" not in result.sanitized_text
    # At least 3 distinct redaction rules must have fired
    assert len(result.redactions) >= 3, (
        f"Expected >= 3 redaction records, got {len(result.redactions)}: "
        f"{[r.rule_id for r in result.redactions]}"
    )


# ---------------------------------------------------------------------------
# Test 11: Clean output passes through unchanged
# ---------------------------------------------------------------------------


def test_clean_output_passes_unchanged(layer: ValidationLayer) -> None:
    """Output with no sensitive data must pass through unchanged."""
    clean_output = (
        "Migration run run-abc-123 completed successfully. "
        "4,200,000 Account records migrated with 99.97% success rate. "
        "Elapsed time: 14 hours 22 minutes. "
        "Validation grade: A (score: 0.973)."
    )
    result = layer.sanitize_output(clean_output)
    assert result.sanitized_text == clean_output, (
        f"Clean output should not be modified. "
        f"Redactions applied: {[r.rule_id for r in result.redactions]}"
    )
    assert not result.was_modified
    assert result.redactions == []
    assert result.alerts == []


# ---------------------------------------------------------------------------
# Test 12: Salesforce ID partial masked
# ---------------------------------------------------------------------------


def test_sf_id_partial_masked(sanitizer: OutputSanitizer) -> None:
    """18-character Salesforce ID must be partially masked — last 4 chars visible."""
    sf_id = "001A000001LRlYZIA3"  # 18-char SF ID
    output = f"Account record with SF ID: {sf_id} was migrated."
    masked_output = sanitizer.sanitize_sf_ids(output)
    # The full raw ID must not be present
    assert sf_id not in masked_output, "Full SF ID must not appear unmasked"
    # The last 4 characters must still be visible (IA3 is last 3, so check last 4 of the ID)
    last_4 = sf_id[-4:]
    assert last_4 in masked_output, f"Last 4 chars '{last_4}' must remain visible after masking"
    # Should contain asterisks for the masked portion
    assert "*" in masked_output


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_sanitizer_handles_empty_string(sanitizer: OutputSanitizer) -> None:
    """Empty string must return SanitizationResult with empty sanitized_text."""
    result = sanitizer.sanitize("")
    assert result.sanitized_text == ""
    assert not result.was_modified


def test_sanitizer_handles_none_gracefully(sanitizer: OutputSanitizer) -> None:
    """None input must not raise and must return SanitizationResult."""
    result = sanitizer.sanitize(None)
    assert isinstance(result, SanitizationResult)
    assert not result.was_modified


def test_vault_legacy_token_stripped(layer: ValidationLayer) -> None:
    """Legacy Vault token format 'hvb.xxx' must also be redacted."""
    output = "Old vault token: hvb.CAESIB5jKmN2pQ9rStUvWxYz1234567890"
    result = layer.sanitize_output(output)
    assert "hvb." not in result.sanitized_text


def test_aws_access_key_redacted(layer: ValidationLayer) -> None:
    """AWS access key (AKIA...) must be redacted."""
    output = "AWS access key: AKIAIOSFODNN7EXAMPLE used for S3 operations."
    result = layer.sanitize_output(output)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.sanitized_text
    assert result.was_modified


def test_phone_number_redacted(layer: ValidationLayer) -> None:
    """US phone number in output must be redacted."""
    output = "Customer contact: (202) 555-0147. Please follow up."
    result = layer.sanitize_output(output)
    assert "(202) 555-0147" not in result.sanitized_text
    assert "[PHONE_REDACTED]" in result.sanitized_text


def test_redaction_record_counts_are_accurate(layer: ValidationLayer) -> None:
    """RedactionRecord.count must reflect actual number of occurrences."""
    output = "Emails: alice@example.com and bob@example.com and carol@example.com"
    result = layer.sanitize_output(output)
    email_redactions = [r for r in result.redactions if r.rule_id == "EMAIL"]
    assert len(email_redactions) == 1  # One rule entry
    assert email_redactions[0].count == 3  # Three occurrences

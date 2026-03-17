"""
SOQL Injection Prevention Tests — Phase 9, Security Suite.

Verifies that SOQLValidator and ValidationLayer.validate_soql correctly block
all DML/DDL injection attempts while allowing legitimate SELECT queries through.

Rule coverage:
  - DELETE, UPDATE, INSERT, DROP, MERGE, GRANT (direct DML/DDL)
  - UNION SELECT injection chaining
  - Semicolon-chained statements
  - Case-insensitive matching
  - Valid SELECT queries including subqueries

Compliance: OWASP LLM03 (Training Data Poisoning via injection), Salesforce
SOQL security model, FedRAMP AC-3 (Access Enforcement).
"""
from __future__ import annotations

import pytest

from validation.layer import (
    SOQLValidator,
    SecurityBlockedError,
    ValidationLayer,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def validator() -> SOQLValidator:
    return SOQLValidator()


@pytest.fixture
def layer() -> ValidationLayer:
    return ValidationLayer()


# ---------------------------------------------------------------------------
# Test 1: DELETE FROM blocked
# ---------------------------------------------------------------------------


def test_delete_from_blocked(validator: SOQLValidator) -> None:
    """'DELETE FROM Account' must be BLOCKED."""
    soql = "DELETE FROM Account WHERE LastModifiedDate < LAST_YEAR"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for DELETE, got {result}"
    assert rule_id == "DELETE", f"Expected rule_id 'DELETE', got '{rule_id}'"


# ---------------------------------------------------------------------------
# Test 2: UPDATE SET blocked
# ---------------------------------------------------------------------------


def test_update_set_blocked(validator: SOQLValidator) -> None:
    """'UPDATE Account SET Name = ...' must be BLOCKED."""
    soql = "UPDATE Account SET Name = 'Hacked' WHERE Id = '001000000000001'"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for UPDATE, got {result}"
    assert rule_id == "UPDATE"


# ---------------------------------------------------------------------------
# Test 3: INSERT INTO blocked
# ---------------------------------------------------------------------------


def test_insert_into_blocked(validator: SOQLValidator) -> None:
    """'INSERT INTO Account (Name) VALUES (...)' must be BLOCKED."""
    soql = "INSERT INTO Account (Name, Industry) VALUES ('Evil Corp', 'Technology')"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for INSERT, got {result}"
    assert rule_id == "INSERT"


# ---------------------------------------------------------------------------
# Test 4: DROP TABLE blocked
# ---------------------------------------------------------------------------


def test_drop_table_blocked(validator: SOQLValidator) -> None:
    """'DROP TABLE accounts' must be BLOCKED."""
    soql = "DROP TABLE accounts"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for DROP, got {result}"
    assert rule_id == "DROP"


# ---------------------------------------------------------------------------
# Test 5: MERGE blocked
# ---------------------------------------------------------------------------


def test_merge_blocked(validator: SOQLValidator) -> None:
    """MERGE statement must be BLOCKED."""
    # Use a pure MERGE without embedded UPDATE to test MERGE rule specifically
    soql = "MERGE Account a USING staging__c s ON (a.Id = s.Id)"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for MERGE, got {result}"
    assert rule_id == "MERGE", f"Expected rule_id 'MERGE', got '{rule_id}'"


# ---------------------------------------------------------------------------
# Test 6: GRANT blocked
# ---------------------------------------------------------------------------


def test_grant_blocked(validator: SOQLValidator) -> None:
    """'GRANT ... ON Account TO user' must be BLOCKED."""
    soql = "GRANT SELECT ON Account TO 'integration_user'"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for GRANT, got {result}"
    assert rule_id == "GRANT", f"Expected rule_id 'GRANT', got '{rule_id}'"


# ---------------------------------------------------------------------------
# Test 7: UNION SELECT injection blocked
# ---------------------------------------------------------------------------


def test_union_injection_blocked(validator: SOQLValidator) -> None:
    """'SELECT ... UNION SELECT Password FROM Users__c' must be BLOCKED."""
    soql = "SELECT Id FROM Account UNION SELECT Password__c FROM AdminUsers__c LIMIT 100"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for UNION SELECT injection, got {result}"
    assert rule_id == "UNION_INJECTION"


# ---------------------------------------------------------------------------
# Test 8: Semicolon chaining blocked
# ---------------------------------------------------------------------------


def test_semicolon_chaining_blocked(validator: SOQLValidator) -> None:
    """'SELECT Id FROM Account; DELETE FROM Account' must be BLOCKED."""
    soql = "SELECT Id FROM Account WHERE IsActive__c = true; DELETE FROM Account"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, f"Expected BLOCK for semicolon chaining, got {result}"
    assert rule_id == "SEMICOLON_CHAIN"


# ---------------------------------------------------------------------------
# Test 9: Valid SELECT passes
# ---------------------------------------------------------------------------


def test_valid_select_passes(validator: SOQLValidator) -> None:
    """A well-formed SOQL SELECT with WHERE and LIMIT must PASS."""
    soql = (
        "SELECT Id, Name, BillingCity, BillingState, Industry, AnnualRevenue "
        "FROM Account "
        "WHERE Legacy_ID__c = 'LEG-001' "
        "AND IsActive__c = true "
        "LIMIT 100"
    )
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.PASS, (
        f"Expected PASS for valid SELECT, got {result} (rule: '{rule_id}')"
    )
    assert rule_id == ""


# ---------------------------------------------------------------------------
# Test 10: Valid SELECT with subquery passes
# ---------------------------------------------------------------------------


def test_valid_select_with_subquery(validator: SOQLValidator) -> None:
    """Legitimate SOQL subquery (IN clause) must PASS."""
    soql = (
        "SELECT Id, Name, AccountId "
        "FROM Contact "
        "WHERE AccountId IN (SELECT Id FROM Account WHERE Industry = 'Government') "
        "LIMIT 500"
    )
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.PASS, (
        f"Expected PASS for SELECT with subquery, got {result} (rule: '{rule_id}')"
    )


# ---------------------------------------------------------------------------
# Test 11: Case-insensitive DELETE detection
# ---------------------------------------------------------------------------


def test_case_insensitive_delete_blocked(validator: SOQLValidator) -> None:
    """Lowercase 'delete from account' must still be BLOCKED."""
    soql = "delete from account where createddate < last_year"
    result, rule_id = validator.validate(soql)
    assert result == ValidationResult.BLOCK, (
        f"Expected BLOCK for lowercase 'delete', got {result}. "
        "Case-insensitive matching is required."
    )


# ---------------------------------------------------------------------------
# Layer integration tests
# ---------------------------------------------------------------------------


def test_layer_validate_soql_raises_on_delete(layer: ValidationLayer) -> None:
    """ValidationLayer.validate_soql must raise SecurityBlockedError for DELETE."""
    with pytest.raises(SecurityBlockedError) as exc_info:
        layer.validate_soql("DELETE FROM Contact WHERE Email = null")
    assert exc_info.value.rule_id == "DELETE"
    assert exc_info.value.blocked_value_hash  # must carry a content hash


def test_layer_validate_soql_passes_for_valid_select(layer: ValidationLayer) -> None:
    """ValidationLayer.validate_soql must not raise for a valid SELECT."""
    # Should not raise
    layer.validate_soql("SELECT Id, Name, Email FROM Contact LIMIT 50")


def test_layer_check_soql_returns_block_tuple(layer: ValidationLayer) -> None:
    """ValidationLayer.check_soql must return (BLOCK, rule_id) for DML."""
    result, rule_id = layer.check_soql("INSERT INTO Account (Name) VALUES ('x')")
    assert result == ValidationResult.BLOCK
    assert rule_id == "INSERT"


def test_layer_check_soql_returns_pass_tuple(layer: ValidationLayer) -> None:
    """ValidationLayer.check_soql must return (PASS, '') for SELECT."""
    result, rule_id = layer.check_soql("SELECT Id FROM Opportunity LIMIT 10")
    assert result == ValidationResult.PASS
    assert rule_id == ""


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_mixed_case_update_blocked(validator: SOQLValidator) -> None:
    """Mixed-case 'UpDaTe' must be BLOCKED."""
    result, rule_id = validator.validate("UpDaTe Account SET Name = 'x'")
    assert result == ValidationResult.BLOCK


def test_empty_query_blocked(validator: SOQLValidator) -> None:
    """Empty query must be BLOCKED (not treated as SELECT)."""
    result, rule_id = validator.validate("")
    assert result == ValidationResult.BLOCK
    assert rule_id == "EMPTY_QUERY"


def test_none_query_blocked(validator: SOQLValidator) -> None:
    """None query must be BLOCKED."""
    result, rule_id = validator.validate(None)
    assert result == ValidationResult.BLOCK


def test_non_select_statement_blocked(validator: SOQLValidator) -> None:
    """A query not starting with SELECT must be BLOCKED."""
    result, rule_id = validator.validate("EXPLAIN SELECT Id FROM Account")
    assert result == ValidationResult.BLOCK
    assert rule_id == "NOT_SELECT_STATEMENT"


def test_revoke_blocked(validator: SOQLValidator) -> None:
    """REVOKE statement must be BLOCKED."""
    result, rule_id = validator.validate("REVOKE DELETE ON Account FROM 'user'")
    assert result == ValidationResult.BLOCK


def test_truncate_blocked(validator: SOQLValidator) -> None:
    """TRUNCATE statement must be BLOCKED."""
    result, rule_id = validator.validate("TRUNCATE TABLE Contact")
    assert result == ValidationResult.BLOCK


def test_create_blocked(validator: SOQLValidator) -> None:
    """CREATE TABLE statement must be BLOCKED."""
    result, rule_id = validator.validate("CREATE TABLE evil (id INT)")
    assert result == ValidationResult.BLOCK


def test_semicolon_chain_with_select_blocked(validator: SOQLValidator) -> None:
    """Two SELECTs separated by semicolon must be BLOCKED (chaining)."""
    result, rule_id = validator.validate(
        "SELECT Id FROM Account; SELECT Password__c FROM AdminConfig__c"
    )
    assert result == ValidationResult.BLOCK
    assert rule_id == "SEMICOLON_CHAIN"


def test_count_query_passes(validator: SOQLValidator) -> None:
    """SELECT COUNT() aggregate query must PASS."""
    result, rule_id = validator.validate(
        "SELECT COUNT(Id), StageName FROM Opportunity GROUP BY StageName LIMIT 20"
    )
    assert result == ValidationResult.PASS


def test_soql_with_like_passes(validator: SOQLValidator) -> None:
    """SELECT with LIKE clause must PASS (not confused with DML)."""
    result, rule_id = validator.validate(
        "SELECT Id, Name FROM Account WHERE Name LIKE 'Legacy%' LIMIT 100"
    )
    assert result == ValidationResult.PASS

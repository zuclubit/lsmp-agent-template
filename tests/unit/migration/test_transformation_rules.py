"""
Unit tests for transformation_rules module.

Covers all normalisation functions, picklist mapping helpers,
field-mapping dictionaries, individual ValidationRule apply() method,
and the RulesEngine aggregate runner.

Module under test: migration/data_transformations/transformation_rules.py
Pattern: pytest parametrize + AAA, no I/O dependencies.
"""

from __future__ import annotations

import pytest
import pandas as pd

from transformation_rules import (
    ACCOUNT_FIELD_MAP,
    ACCOUNT_TYPE_MAP,
    ACCOUNT_VALIDATION_RULES,
    CONTACT_FIELD_MAP,
    COUNTRY_CODE_MAP,
    INDUSTRY_MAP,
    LEAD_SOURCE_MAP,
    SALUTATION_MAP,
    RulesEngine,
    RulesEngineResult,
    ValidationRule,
    map_picklist,
    normalise_country,
    normalise_email,
    normalise_phone,
    normalise_string,
    normalise_url,
)


# ===========================================================================
# 1. normalise_phone
# ===========================================================================


class TestNormalisePhone:
    """normalise_phone() converts raw phone strings to E.164 format."""

    @pytest.mark.parametrize(
        "raw, country_code, expected",
        [
            # 10-digit US numbers — various separators
            ("5551234567", "1", "+15551234567"),
            ("(555) 123-4567", "1", "+15551234567"),
            ("555-123-4567", "1", "+15551234567"),
            ("555.123.4567", "1", "+15551234567"),
            # 11-digit with leading country code
            ("15551234567", "1", "+15551234567"),
            # Already E.164
            ("+44207946000", "1", "+44207946000"),
            # CA country code
            ("5552223333", "1", "+15552223333"),
        ],
    )
    def test_known_formats_produce_e164(self, raw, country_code, expected):
        assert normalise_phone(raw, country_code) == expected

    @pytest.mark.parametrize("blank", [None, "", "   ", "nan", "None"])
    def test_blank_inputs_return_none(self, blank):
        assert normalise_phone(blank) is None

    def test_short_random_string_returned_as_is_truncated(self):
        """Unparseable numbers are returned as-is (max 40 chars)."""
        raw = "12345"
        result = normalise_phone(raw)
        assert result is not None
        assert len(result) <= 40

    def test_truncates_to_40_chars(self):
        long_raw = "9" * 50
        result = normalise_phone(long_raw)
        assert len(result) <= 40


# ===========================================================================
# 2. normalise_email
# ===========================================================================


class TestNormaliseEmail:
    """normalise_email() lowercases and validates RFC-style patterns."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("User@Example.COM", "user@example.com"),
            ("plain@domain.org", "plain@domain.org"),
            ("a+b@sub.domain.io", "a+b@sub.domain.io"),
        ],
    )
    def test_valid_emails_lowercased(self, raw, expected):
        assert normalise_email(raw) == expected

    @pytest.mark.parametrize("invalid", ["not-an-email", "@nodomain", "user@", "user@@two.com"])
    def test_invalid_emails_return_none(self, invalid):
        assert normalise_email(invalid) is None

    @pytest.mark.parametrize("blank", [None, "", "nan", "None", "   "])
    def test_blank_inputs_return_none(self, blank):
        assert normalise_email(blank) is None


# ===========================================================================
# 3. normalise_url
# ===========================================================================


class TestNormaliseUrl:
    """normalise_url() ensures https:// prefix and respects 255-char limit."""

    def test_url_without_scheme_gets_https(self):
        assert normalise_url("acme.com") == "https://acme.com"

    def test_http_url_preserved(self):
        assert normalise_url("http://acme.com") == "http://acme.com"

    def test_https_url_preserved(self):
        assert normalise_url("https://acme.com/path") == "https://acme.com/path"

    def test_url_longer_than_255_returns_none(self):
        long_url = "https://example.com/" + "a" * 250
        assert normalise_url(long_url) is None

    def test_url_exactly_255_chars_is_accepted(self):
        url = "https://" + "a" * (255 - len("https://"))
        result = normalise_url(url)
        assert result is not None
        assert len(result) == 255

    @pytest.mark.parametrize("blank", [None, "", "nan", "None"])
    def test_blank_inputs_return_none(self, blank):
        assert normalise_url(blank) is None


# ===========================================================================
# 4. normalise_country
# ===========================================================================


class TestNormaliseCountry:
    """normalise_country() maps verbose names to ISO 3166-1 alpha-2 codes."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("United States", "US"),
            ("UNITED STATES OF AMERICA", "US"),
            ("USA", "US"),
            ("Canada", "CA"),
            ("United Kingdom", "GB"),
            ("UK", "GB"),
            ("Germany", "DE"),
            ("Australia", "AU"),
            ("Japan", "JP"),
            ("France", "FR"),
        ],
    )
    def test_known_country_names_mapped(self, raw, expected):
        assert normalise_country(raw) == expected

    def test_already_iso2_code_returned_uppercased(self):
        assert normalise_country("us") == "US"
        assert normalise_country("GB") == "GB"

    def test_unknown_country_returns_first_two_uppercase(self):
        result = normalise_country("Freedonia")
        assert result == "FR"  # First 2 chars uppercased

    @pytest.mark.parametrize("blank", [None, "", "nan", "None"])
    def test_blank_inputs_return_none(self, blank):
        assert normalise_country(blank) is None


# ===========================================================================
# 5. normalise_string
# ===========================================================================


class TestNormaliseString:
    """normalise_string() strips, collapses whitespace, and truncates."""

    def test_leading_trailing_whitespace_stripped(self):
        assert normalise_string("  hello  ") == "hello"

    def test_tabs_and_newlines_replaced_with_spaces(self):
        result = normalise_string("foo\tbar\nbaz")
        assert "\t" not in result
        assert "\n" not in result
        assert "foo bar baz" == result

    def test_multiple_spaces_collapsed(self):
        result = normalise_string("too   many   spaces")
        assert result == "too many spaces"

    def test_string_truncated_at_max_len(self):
        long = "a" * 300
        result = normalise_string(long, max_len=255)
        assert len(result) == 255

    def test_string_within_max_len_not_changed(self):
        short = "hello"
        assert normalise_string(short, max_len=255) == "hello"

    def test_custom_max_len_respected(self):
        result = normalise_string("abcdefghij", max_len=5)
        assert result == "abcde"

    @pytest.mark.parametrize("blank", [None, "", "nan", "None", "   "])
    def test_blank_inputs_return_none(self, blank):
        assert normalise_string(blank) is None


# ===========================================================================
# 6. map_picklist
# ===========================================================================


class TestMapPicklist:
    """map_picklist() resolves legacy codes against a mapping dict."""

    def test_known_key_returns_mapped_value(self):
        assert map_picklist("TECHNOLOGY", INDUSTRY_MAP) == "Technology"

    def test_lookup_is_case_insensitive(self):
        assert map_picklist("technology", INDUSTRY_MAP) == "Technology"
        assert map_picklist("Technology", INDUSTRY_MAP) == "Technology"

    def test_unknown_key_returns_default(self):
        assert map_picklist("UNKNOWN_CODE", INDUSTRY_MAP, default="Other") == "Other"
        assert map_picklist("UNKNOWN_CODE", INDUSTRY_MAP) is None

    @pytest.mark.parametrize("blank", [None, "", "nan", "None"])
    def test_blank_inputs_return_default(self, blank):
        assert map_picklist(blank, INDUSTRY_MAP, default="Fallback") == "Fallback"

    def test_account_type_map_customer_direct(self):
        assert map_picklist("CUSTOMER", ACCOUNT_TYPE_MAP) == "Customer - Direct"

    def test_account_type_partner_aliases(self):
        assert map_picklist("PARTNER", ACCOUNT_TYPE_MAP) == "Channel Partner / Reseller"
        assert map_picklist("RESELLER", ACCOUNT_TYPE_MAP) == "Channel Partner / Reseller"

    def test_lead_source_web_aliases(self):
        assert map_picklist("WEB", LEAD_SOURCE_MAP) == "Web"
        assert map_picklist("WEBSITE", LEAD_SOURCE_MAP) == "Web"

    def test_salutation_normalisation(self):
        assert map_picklist("MR", SALUTATION_MAP) == "Mr."
        assert map_picklist("DR", SALUTATION_MAP) == "Dr."


# ===========================================================================
# 7. Field mapping dictionaries
# ===========================================================================


class TestFieldMappingDictionaries:
    """Structural checks on ACCOUNT_FIELD_MAP and CONTACT_FIELD_MAP."""

    def test_account_field_map_contains_required_keys(self):
        required = {"COMPANY_ID", "COMPANY_NAME", "PHONE_NUMBER", "ADDR_COUNTRY"}
        assert required.issubset(ACCOUNT_FIELD_MAP.keys())

    def test_account_field_map_maps_company_id_to_legacy_id(self):
        assert ACCOUNT_FIELD_MAP["COMPANY_ID"] == "Legacy_ID__c"

    def test_account_field_map_maps_company_name_to_name(self):
        assert ACCOUNT_FIELD_MAP["COMPANY_NAME"] == "Name"

    def test_contact_field_map_contains_person_id(self):
        assert "PERSON_ID" in CONTACT_FIELD_MAP
        assert CONTACT_FIELD_MAP["PERSON_ID"] == "Legacy_ID__c"

    def test_country_code_map_has_common_entries(self):
        assert COUNTRY_CODE_MAP["UNITED STATES"] == "US"
        assert COUNTRY_CODE_MAP["CANADA"] == "CA"
        assert COUNTRY_CODE_MAP["UNITED KINGDOM"] == "GB"

    def test_industry_map_has_tech_aliases(self):
        assert INDUSTRY_MAP["TECHNOLOGY"] == "Technology"
        assert INDUSTRY_MAP["TECH"] == "Technology"
        assert INDUSTRY_MAP["SOFTWARE"] == "Technology"


# ===========================================================================
# 8. ValidationRule.apply()
# ===========================================================================


class TestValidationRuleApply:
    """Unit tests for ValidationRule.apply() method."""

    def test_warn_rule_returns_no_df_change_but_counts_violations(self):
        rule = ValidationRule(
            column="Name",
            description="Name not blank",
            check=lambda s: s.notna() & (s.str.strip() != ""),
            action="warn",
        )
        df = pd.DataFrame({"Name": ["Alice", "", "Bob", None]})
        result_df, violations = rule.apply(df)
        assert violations == 2  # "" and None
        assert len(result_df) == 4  # warn does not drop

    def test_drop_rule_removes_violating_rows(self):
        rule = ValidationRule(
            column="Legacy_ID__c",
            description="Legacy ID required",
            check=lambda s: s.notna() & (s.astype(str).str.strip() != ""),
            action="drop",
        )
        df = pd.DataFrame({"Legacy_ID__c": ["ID-001", None, "ID-003", ""]})
        result_df, violations = rule.apply(df)
        assert violations == 2
        assert len(result_df) == 2

    def test_fix_rule_corrects_violating_values(self):
        rule = ValidationRule(
            column="Name",
            description="Name max length 5",
            check=lambda s: s.str.len() <= 5,
            action="fix",
            fix_fn=lambda s: s.str[:5],
        )
        df = pd.DataFrame({"Name": ["Hi", "TooLongName", "OK"]})
        result_df, violations = rule.apply(df)
        assert violations == 1
        assert result_df.loc[1, "Name"] == "TooLo"
        assert result_df.loc[0, "Name"] == "Hi"

    def test_rule_on_missing_column_returns_zero_violations(self):
        rule = ValidationRule(
            column="NonExistentColumn",
            description="Column missing",
            check=lambda s: s.notna(),
            action="warn",
        )
        df = pd.DataFrame({"SomeOtherColumn": [1, 2, 3]})
        result_df, violations = rule.apply(df)
        assert violations == 0
        assert len(result_df) == 3

    def test_zero_violations_returns_unchanged_df(self):
        rule = ValidationRule(
            column="Phone",
            description="Phone max 40",
            check=lambda s: s.isna() | (s.str.len() <= 40),
            action="fix",
            fix_fn=lambda s: s.str[:40],
        )
        df = pd.DataFrame({"Phone": ["555-1234", "555-9876"]})
        result_df, violations = rule.apply(df)
        assert violations == 0
        pd.testing.assert_frame_equal(result_df, df)


# ===========================================================================
# 9. RulesEngine.apply()
# ===========================================================================


class TestRulesEngineApply:
    """Tests for RulesEngine applied to a full DataFrame."""

    def _make_valid_df(self, n: int = 5) -> pd.DataFrame:
        return pd.DataFrame({
            "Name": [f"Company {i}" for i in range(n)],
            "Legacy_ID__c": [f"ID-{i:04d}" for i in range(n)],
            "Phone": ["5551234567"] * n,
            "Website": ["https://example.com"] * n,
            "AnnualRevenue": [100_000.0] * n,
        })

    def test_clean_df_produces_zero_violations(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df()
        result_df, result = engine.apply(df)
        assert result.total_violations == 0
        assert result.total_errors == 0
        assert result.rows_dropped == 0

    def test_result_total_input_rows_set_correctly(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(10)
        _, result = engine.apply(df)
        assert result.total_input_rows == 10

    def test_blank_name_counted_as_error(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(3)
        df.loc[0, "Name"] = ""
        _, result = engine.apply(df)
        assert result.total_errors >= 1

    def test_negative_revenue_is_fixed(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(3)
        df.loc[1, "AnnualRevenue"] = -5000.0
        result_df, result = engine.apply(df)
        assert result.total_violations >= 1
        # fix_fn sets negative to None
        assert result_df.loc[1, "AnnualRevenue"] is None or pd.isna(result_df.loc[1, "AnnualRevenue"])

    def test_long_phone_is_fixed_to_40_chars(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(2)
        df.loc[0, "Phone"] = "9" * 45
        result_df, result = engine.apply(df)
        assert result.total_violations >= 1
        assert len(result_df.loc[0, "Phone"]) <= 40

    def test_violations_by_rule_populated(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(4)
        df.loc[0, "Name"] = ""
        df.loc[1, "Name"] = ""
        _, result = engine.apply(df)
        assert "Name must not be blank" in result.violations_by_rule

    def test_rows_dropped_counted_when_drop_action(self):
        drop_rule = ValidationRule(
            column="Legacy_ID__c",
            description="Legacy ID required",
            check=lambda s: s.notna() & (s.str.strip() != ""),
            action="drop",
        )
        engine = RulesEngine([drop_rule])
        df = pd.DataFrame({
            "Legacy_ID__c": ["ID-001", None, "ID-003", None]
        })
        result_df, result = engine.apply(df)
        assert result.rows_dropped == 2
        assert len(result_df) == 2

    def test_empty_rules_list_passes_df_through(self):
        engine = RulesEngine([])
        df = self._make_valid_df(5)
        result_df, result = engine.apply(df)
        assert len(result_df) == 5
        assert result.total_violations == 0

    def test_result_to_dict_contains_all_keys(self):
        engine = RulesEngine(ACCOUNT_VALIDATION_RULES)
        df = self._make_valid_df(2)
        _, result = engine.apply(df)
        d = result.to_dict()
        expected_keys = {
            "total_input_rows", "total_output_rows", "total_violations",
            "total_errors", "rows_dropped", "violations_by_rule",
        }
        assert expected_keys.issubset(d.keys())

    def test_multiple_rules_applied_sequentially(self):
        """Two rules both counting violations on the same column should sum correctly."""
        rule_blank = ValidationRule(
            column="Name",
            description="Name not blank",
            check=lambda s: s.notna() & (s.str.strip() != ""),
            action="warn",
        )
        rule_len = ValidationRule(
            column="Name",
            description="Name max length 10",
            check=lambda s: s.isna() | (s.str.len() <= 10),
            action="fix",
            fix_fn=lambda s: s.str[:10],
        )
        engine = RulesEngine([rule_blank, rule_len])
        df = pd.DataFrame({"Name": ["", "VeryLongNameExceedsLimit", "OK"]})
        result_df, result = engine.apply(df)
        assert result.total_violations == 2
        assert len(result_df.loc[1, "Name"]) <= 10


# ===========================================================================
# 10. RulesEngineResult
# ===========================================================================


class TestRulesEngineResult:
    """Tests for the RulesEngineResult summary dataclass."""

    def test_default_values_are_zero(self):
        r = RulesEngineResult()
        assert r.total_input_rows == 0
        assert r.total_output_rows == 0
        assert r.total_violations == 0
        assert r.total_errors == 0
        assert r.rows_dropped == 0
        assert r.violations_by_rule == {}

    def test_to_dict_serialises_all_fields(self):
        r = RulesEngineResult(
            total_input_rows=100,
            total_output_rows=95,
            total_violations=7,
            total_errors=2,
            rows_dropped=5,
            violations_by_rule={"rule_a": 5, "rule_b": 2},
        )
        d = r.to_dict()
        assert d["total_input_rows"] == 100
        assert d["rows_dropped"] == 5
        assert d["violations_by_rule"]["rule_a"] == 5

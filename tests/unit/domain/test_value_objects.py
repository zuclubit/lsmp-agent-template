"""
Unit tests for domain value objects.

Value objects are immutable, equality is based on value, and they enforce
their own validation invariants at construction time.

Modules under test:
  - domain/value_objects/email.py
  - domain/value_objects/address.py
  - domain/value_objects/salesforce_id.py

Pattern: AAA (Arrange – Act – Assert), parametrize for invalid inputs.
"""

from __future__ import annotations

import pytest

from domain.exceptions.domain_exceptions import MultipleValidationErrors, ValidationError
from domain.value_objects.address import Address
from domain.value_objects.email import Email
from domain.value_objects.salesforce_id import SalesforceId


# ===========================================================================
# Email value object
# ===========================================================================


class TestEmail:
    """Email: validated, normalised-to-lowercase, immutable, value-equality."""

    # --- happy path ---

    def test_valid_email_is_accepted(self) -> None:
        """Standard business email creates without error."""
        email = Email("user@example.com")
        assert email.value == "user@example.com"

    def test_email_is_canonicalised_to_lowercase(self) -> None:
        """Mixed-case input is canonicalised to lowercase."""
        email = Email("Admin@COMPANY.ORG")
        assert email.value == "admin@company.org"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        """Surrounding whitespace is stripped during normalisation."""
        email = Email("  user@example.com  ")
        assert email.value == "user@example.com"

    def test_local_part_property(self) -> None:
        """local_part returns the portion before '@'."""
        assert Email("finance@acme.example.com").local_part == "finance"

    def test_domain_property(self) -> None:
        """domain returns the portion after '@'."""
        assert Email("billing@acme.example.com").domain == "acme.example.com"

    def test_is_business_email_true_for_corporate_domain(self) -> None:
        """Corporate domains are classified as business emails."""
        email = Email("cfo@enterprise.io")
        assert email.is_business_email is True

    def test_is_business_email_false_for_gmail(self) -> None:
        """gmail.com is classified as a free-mail domain."""
        email = Email("user@gmail.com")
        assert email.is_business_email is False

    def test_str_representation_returns_value(self) -> None:
        """str(email) returns the normalised value."""
        email = Email("test@example.com")
        assert str(email) == "test@example.com"

    def test_repr_contains_value(self) -> None:
        """repr() includes the email address for debugging."""
        email = Email("debug@example.com")
        assert "debug@example.com" in repr(email)

    # --- equality and hashing ---

    def test_equal_to_same_address(self) -> None:
        """Two Email objects with the same address are equal."""
        assert Email("a@example.com") == Email("a@example.com")

    def test_equal_to_same_address_different_case(self) -> None:
        """Case difference is normalised away before comparison."""
        assert Email("A@EXAMPLE.COM") == Email("a@example.com")

    def test_not_equal_to_different_address(self) -> None:
        """Different addresses are not equal."""
        assert Email("a@example.com") != Email("b@example.com")

    def test_equal_to_plain_string(self) -> None:
        """Email compares equal to a plain normalised string."""
        email = Email("user@example.com")
        assert email == "user@example.com"

    def test_hashable_and_consistent(self) -> None:
        """hash(email) is stable and consistent with equality."""
        e1 = Email("hash@test.com")
        e2 = Email("hash@test.com")
        assert hash(e1) == hash(e2)
        assert {e1, e2} == {e1}  # deduplication in set

    # --- immutability ---

    def test_is_immutable(self) -> None:
        """Setting an attribute on Email must raise AttributeError."""
        email = Email("user@example.com")
        with pytest.raises(AttributeError):
            email.value = "hacked@evil.com"  # type: ignore[misc]

    # --- validation ---

    @pytest.mark.parametrize(
        "bad_email",
        [
            "",
            "   ",
            "notanemail",
            "@nodomain.com",
            "noat",
            "a" * 255 + "@x.com",
        ],
        ids=["empty", "whitespace", "no_at", "no_local", "no_dot_domain", "exceeds_max_len"],
    )
    def test_invalid_emails_raise_validation_error(self, bad_email: str) -> None:
        """All invalid email forms raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            Email(bad_email)
        assert exc_info.value.field == "email"

    def test_non_string_raises_validation_error(self) -> None:
        """Passing a non-string raises ValidationError."""
        with pytest.raises(ValidationError):
            Email(12345)  # type: ignore[arg-type]

    # --- factory helpers ---

    def test_from_string_alias_works(self) -> None:
        """Email.from_string() is equivalent to Email()."""
        assert Email.from_string("user@example.com") == Email("user@example.com")

    def test_try_parse_returns_email_for_valid(self) -> None:
        """try_parse() returns an Email for a valid address."""
        result = Email.try_parse("valid@example.com")
        assert result is not None
        assert result.value == "valid@example.com"

    def test_try_parse_returns_none_for_invalid(self) -> None:
        """try_parse() returns None instead of raising for invalid input."""
        assert Email.try_parse("not-an-email") is None
        assert Email.try_parse("") is None


# ===========================================================================
# Address value object
# ===========================================================================


class TestAddress:
    """Address: immutable postal address with per-country validation rules."""

    # --- happy path ---

    def test_valid_us_address_created(self) -> None:
        """Standard US address with all fields creates correctly."""
        addr = Address(
            street="100 Federal Ave",
            city="Austin",
            country_code="US",
            state="TX",
            postal_code="73301",
        )
        assert addr.street == "100 Federal Ave"
        assert addr.country_code == "US"
        assert addr.is_us_address is True

    def test_international_address_without_state(self) -> None:
        """Non-US address can omit state."""
        addr = Address(
            street="10 Downing Street",
            city="London",
            country_code="GB",
        )
        assert addr.country_code == "GB"
        assert addr.state is None

    def test_country_code_normalised_to_uppercase(self) -> None:
        """Lower-case country code is normalised to upper-case."""
        addr = Address(street="Main St", city="Berlin", country_code="de")
        assert addr.country_code == "DE"

    def test_unit_stored_stripped(self) -> None:
        """Unit/suite is stored stripped of surrounding whitespace."""
        addr = Address(
            street="500 Tech Park", city="Seattle", country_code="US",
            state="WA", postal_code="98101", unit="  Suite 200  "
        )
        assert addr.unit == "Suite 200"

    def test_single_line_string_representation(self) -> None:
        """str(address) produces a human-readable single-line form."""
        addr = Address(
            street="123 Main St", city="New York", country_code="US",
            state="NY", postal_code="10001"
        )
        text = str(addr)
        assert "123 Main St" in text
        assert "New York" in text

    # --- Salesforce dict mapping ---

    def test_to_salesforce_dict_keys(self) -> None:
        """to_salesforce_dict() returns SF-style suffix keys."""
        addr = Address(
            street="1 Infinite Loop", city="Cupertino", country_code="US",
            state="CA", postal_code="95014"
        )
        sf_dict = addr.to_salesforce_dict()
        assert "Street" in sf_dict
        assert "City" in sf_dict
        assert "State" in sf_dict
        assert "PostalCode" in sf_dict
        assert "Country" in sf_dict

    def test_to_salesforce_dict_includes_unit_in_street(self) -> None:
        """When unit is set, Street field includes 'street, unit'."""
        addr = Address(
            street="500 Tech Park", city="San Jose", country_code="US",
            state="CA", unit="Ste 100"
        )
        assert "Ste 100" in addr.to_salesforce_dict()["Street"]

    # --- from_dict factory ---

    def test_from_dict_creates_address(self) -> None:
        """from_dict() creates an Address from a plain dictionary."""
        addr = Address.from_dict({
            "street": "42 Test Blvd",
            "city": "Chicago",
            "country_code": "US",
            "state": "IL",
            "postal_code": "60601",
        })
        assert addr.city == "Chicago"
        assert addr.state == "IL"

    # --- equality and hashing ---

    def test_equal_to_address_with_same_values(self) -> None:
        """Two Address objects with identical data are equal."""
        a1 = Address(street="1 Main St", city="Denver", country_code="US", state="CO")
        a2 = Address(street="1 Main St", city="Denver", country_code="US", state="CO")
        assert a1 == a2

    def test_not_equal_when_city_differs(self) -> None:
        """Different city produces inequality."""
        a1 = Address(street="1 Main St", city="Denver", country_code="US", state="CO")
        a2 = Address(street="1 Main St", city="Boulder", country_code="US", state="CO")
        assert a1 != a2

    def test_hashable(self) -> None:
        """Address objects must be hashable for use in sets/dicts."""
        addr = Address(street="1 St", city="City", country_code="DE")
        assert isinstance(hash(addr), int)

    # --- immutability ---

    def test_is_immutable(self) -> None:
        """Setting an attribute on Address must raise AttributeError."""
        addr = Address(street="1 St", city="City", country_code="DE")
        with pytest.raises(AttributeError):
            addr.city = "Hacked"  # type: ignore[misc]

    # --- validation ---

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"street": "", "city": "NY", "country_code": "US", "state": "NY"},
            {"street": "1 St", "city": "", "country_code": "US", "state": "NY"},
            {"street": "1 St", "city": "NY", "country_code": ""},
        ],
        ids=["empty_street", "empty_city", "empty_country_code"],
    )
    def test_missing_required_fields_raise(self, kwargs: dict) -> None:
        """Missing street, city, or country_code raises a ValidationError."""
        with pytest.raises((ValidationError, MultipleValidationErrors)):
            Address(**kwargs)

    def test_unrecognised_country_code_raises(self) -> None:
        """An ISO code not in the recognised list raises ValidationError."""
        with pytest.raises((ValidationError, MultipleValidationErrors)):
            Address(street="St", city="City", country_code="ZZ")

    def test_us_address_requires_state(self) -> None:
        """US addresses without a state must raise ValidationError."""
        with pytest.raises((ValidationError, MultipleValidationErrors)):
            Address(street="1 Main St", city="Springfield", country_code="US")

    def test_us_invalid_state_code_raises(self) -> None:
        """US address with an invalid state code raises ValidationError."""
        with pytest.raises((ValidationError, MultipleValidationErrors)):
            Address(
                street="1 Main St", city="Springfield",
                country_code="US", state="XX"
            )

    def test_us_invalid_zip_format_raises(self) -> None:
        """US postal code not matching NNNNN or NNNNN-NNNN raises."""
        with pytest.raises((ValidationError, MultipleValidationErrors)):
            Address(
                street="1 St", city="NYC", country_code="US",
                state="NY", postal_code="ABCDE"
            )


# ===========================================================================
# SalesforceId value object
# ===========================================================================


class TestSalesforceId:
    """SalesforceId: validates 15/18-char IDs, normalises to 18-char form."""

    # --- happy path (18-char) ---

    def test_valid_18_char_account_id_accepted(self) -> None:
        """Valid 18-char Account ID (prefix 001) is accepted."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert len(str(sf_id)) == 18

    def test_valid_15_char_id_accepted_and_expanded(self) -> None:
        """15-char ID is accepted and expanded to 18 characters."""
        sf_id = SalesforceId("001B000000KmPzA")
        assert len(sf_id.id18) == 18
        assert sf_id.id15 == "001B000000KmPzA"

    def test_id18_property_returns_canonical_form(self) -> None:
        """id18 property returns the canonical 18-char form."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert sf_id.id18 == "001B000000KmPzAIAV"

    def test_str_returns_18_char_form(self) -> None:
        """str(sf_id) returns the 18-char canonical form."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert str(sf_id) == "001B000000KmPzAIAV"

    def test_repr_contains_id(self) -> None:
        """repr() includes the ID for debugging."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert "001B000000KmPzAIAV" in repr(sf_id)

    # --- object type detection ---

    def test_is_account_id_for_001_prefix(self) -> None:
        """IDs with 001 prefix are account IDs."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert sf_id.is_account_id is True
        assert sf_id.object_type == "Account"

    def test_key_prefix_returns_first_three_chars(self) -> None:
        """key_prefix returns the first 3 characters of the 18-char ID."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert sf_id.key_prefix == "001"

    # --- equality and hashing ---

    def test_equal_to_same_id(self) -> None:
        """Two SalesforceId objects wrapping the same ID are equal."""
        s1 = SalesforceId("001B000000KmPzAIAV")
        s2 = SalesforceId("001B000000KmPzAIAV")
        assert s1 == s2

    def test_equality_is_case_insensitive(self) -> None:
        """Equality comparison is case-insensitive on the 18-char form."""
        s1 = SalesforceId("001B000000KmPzAIAV")
        s2 = SalesforceId("001b000000kmpzaiav")
        assert s1 == s2

    def test_equal_to_plain_string(self) -> None:
        """SalesforceId compares equal to a matching plain string."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert sf_id == "001B000000KmPzAIAV"

    def test_not_equal_to_different_id(self) -> None:
        """Different IDs are not equal."""
        s1 = SalesforceId("001B000000KmPzAIAV")
        s2 = SalesforceId("003B000000KmPzAIAV")
        assert s1 != s2

    def test_hashable(self) -> None:
        """SalesforceId objects must be hashable."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        assert isinstance(hash(sf_id), int)

    def test_usable_in_set(self) -> None:
        """Two equal IDs produce one set entry."""
        s1 = SalesforceId("001B000000KmPzAIAV")
        s2 = SalesforceId("001B000000KmPzAIAV")
        assert len({s1, s2}) == 1

    # --- immutability ---

    def test_is_immutable(self) -> None:
        """Setting an attribute on SalesforceId raises AttributeError."""
        sf_id = SalesforceId("001B000000KmPzAIAV")
        with pytest.raises(AttributeError):
            sf_id.id18 = "tampered"  # type: ignore[misc]

    # --- validation ---

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "   ",
            "short",
            "toolongtoolongtoolong",
            "001!invalid@char#####",
        ],
        ids=["empty", "whitespace", "too_short", "too_long", "invalid_chars"],
    )
    def test_invalid_ids_raise_validation_error(self, bad_id: str) -> None:
        """Invalid ID forms raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            SalesforceId(bad_id)
        assert exc_info.value.field == "salesforce_id"

    def test_non_string_raises_validation_error(self) -> None:
        """Passing a non-string raises ValidationError."""
        with pytest.raises(ValidationError):
            SalesforceId(12345)  # type: ignore[arg-type]

    # --- factory helpers ---

    def test_from_string_alias_works(self) -> None:
        """from_string() is equivalent to the constructor."""
        assert SalesforceId.from_string("001B000000KmPzAIAV") == SalesforceId("001B000000KmPzAIAV")

    def test_try_parse_returns_instance_for_valid(self) -> None:
        """try_parse() returns a SalesforceId for a valid ID."""
        result = SalesforceId.try_parse("001B000000KmPzAIAV")
        assert result is not None

    def test_try_parse_returns_none_for_invalid(self) -> None:
        """try_parse() returns None instead of raising for invalid input."""
        assert SalesforceId.try_parse("bad") is None
        assert SalesforceId.try_parse("") is None

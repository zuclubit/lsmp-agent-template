"""
Address value object.

Immutable, validated, comparable by value.  No framework dependencies.
Covers both domestic (US) and international address formats with enough
structure to satisfy Salesforce's standard address compound fields.
"""

from __future__ import annotations

from typing import Optional
import re

from domain.exceptions.domain_exceptions import ValidationError, MultipleValidationErrors

# ISO 3166-1 alpha-2 country codes (subset sufficient for enterprise use)
_VALID_COUNTRY_CODES: frozenset[str] = frozenset({
    "US", "CA", "GB", "AU", "DE", "FR", "JP", "CN", "IN", "BR",
    "MX", "ES", "IT", "NL", "SE", "NO", "DK", "FI", "CH", "AT",
    "BE", "PL", "PT", "CZ", "HU", "RO", "GR", "IE", "NZ", "SG",
    "HK", "ZA", "AR", "CL", "CO", "PE", "VE", "EG", "NG", "KE",
    "AE", "SA", "IL", "TR", "PH", "TH", "VN", "MY", "ID", "PK",
})

_US_STATE_CODES: frozenset[str] = frozenset({
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID",
    "IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS",
    "MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
    "WI","WY","DC","PR","VI","GU","MP","AS",
})

_US_ZIP_PATTERN = re.compile(r"^\d{5}(-\d{4})?$")
_CA_POSTAL_PATTERN = re.compile(r"^[A-Z]\d[A-Z]\s?\d[A-Z]\d$", re.IGNORECASE)


class Address:
    """
    Immutable value object representing a postal address.

    Required fields: street, city, country_code.
    Optional fields: state/province, postal_code, unit/suite.

    Equality is value-based (normalised to uppercase for code fields,
    stripped whitespace for free-text fields).
    """

    __slots__ = (
        "_street",
        "_unit",
        "_city",
        "_state",
        "_postal_code",
        "_country_code",
    )

    def __init__(
        self,
        street: str,
        city: str,
        country_code: str,
        state: Optional[str] = None,
        postal_code: Optional[str] = None,
        unit: Optional[str] = None,
    ) -> None:
        errors: list[ValidationError] = []

        # --- street ---
        street = street.strip() if isinstance(street, str) else ""
        if not street:
            errors.append(ValidationError("street", street, "street cannot be blank"))
        elif len(street) > 255:
            errors.append(ValidationError("street", street, "street exceeds 255 characters"))

        # --- city ---
        city = city.strip() if isinstance(city, str) else ""
        if not city:
            errors.append(ValidationError("city", city, "city cannot be blank"))
        elif len(city) > 100:
            errors.append(ValidationError("city", city, "city exceeds 100 characters"))

        # --- country_code ---
        country_code = (country_code.strip().upper() if isinstance(country_code, str) else "")
        if not country_code:
            errors.append(ValidationError("country_code", country_code, "country_code cannot be blank"))
        elif country_code not in _VALID_COUNTRY_CODES:
            errors.append(ValidationError(
                "country_code", country_code,
                f"'{country_code}' is not a recognised ISO 3166-1 alpha-2 code"
            ))

        # --- state (required for US/CA) ---
        state_norm: Optional[str] = None
        if state is not None:
            state_norm = state.strip().upper()
        if country_code == "US" and not state_norm:
            errors.append(ValidationError("state", state, "state is required for US addresses"))
        elif country_code == "US" and state_norm and state_norm not in _US_STATE_CODES:
            errors.append(ValidationError("state", state_norm, f"'{state_norm}' is not a valid US state code"))

        # --- postal_code ---
        postal_code_norm: Optional[str] = None
        if postal_code is not None:
            postal_code_norm = postal_code.strip().upper()
        if country_code == "US" and postal_code_norm:
            if not _US_ZIP_PATTERN.match(postal_code_norm):
                errors.append(ValidationError(
                    "postal_code", postal_code_norm,
                    "US postal code must match NNNNN or NNNNN-NNNN"
                ))
        if country_code == "CA" and postal_code_norm:
            if not _CA_POSTAL_PATTERN.match(postal_code_norm):
                errors.append(ValidationError(
                    "postal_code", postal_code_norm,
                    "Canadian postal code must match ANA NAN format"
                ))

        # --- unit ---
        unit_norm: Optional[str] = None
        if unit is not None:
            unit_norm = unit.strip() or None

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise MultipleValidationErrors(errors)

        object.__setattr__(self, "_street", street)
        object.__setattr__(self, "_unit", unit_norm)
        object.__setattr__(self, "_city", city)
        object.__setattr__(self, "_state", state_norm)
        object.__setattr__(self, "_postal_code", postal_code_norm)
        object.__setattr__(self, "_country_code", country_code)

    # ------------------------------------------------------------------
    # Immutability
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise AttributeError("Address is immutable")

    # ------------------------------------------------------------------
    # Value object equality
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Address):
            return NotImplemented
        return (
            self._street == other._street
            and self._unit == other._unit
            and self._city == other._city
            and self._state == other._state
            and self._postal_code == other._postal_code
            and self._country_code == other._country_code
        )

    def __hash__(self) -> int:
        return hash((
            self._street,
            self._unit,
            self._city,
            self._state,
            self._postal_code,
            self._country_code,
        ))

    def __repr__(self) -> str:
        return (
            f"Address(street={self._street!r}, city={self._city!r}, "
            f"state={self._state!r}, postal_code={self._postal_code!r}, "
            f"country_code={self._country_code!r})"
        )

    def __str__(self) -> str:
        parts = [self._street]
        if self._unit:
            parts.append(self._unit)
        parts.append(self._city)
        if self._state:
            parts.append(self._state)
        if self._postal_code:
            parts.append(self._postal_code)
        parts.append(self._country_code)
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def street(self) -> str:
        return self._street

    @property
    def unit(self) -> Optional[str]:
        return self._unit

    @property
    def city(self) -> str:
        return self._city

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def postal_code(self) -> Optional[str]:
        return self._postal_code

    @property
    def country_code(self) -> str:
        return self._country_code

    @property
    def is_us_address(self) -> bool:
        return self._country_code == "US"

    @property
    def single_line(self) -> str:
        """One-line representation suitable for display."""
        return str(self)

    # ------------------------------------------------------------------
    # Salesforce mapping helper
    # ------------------------------------------------------------------

    def to_salesforce_dict(self) -> dict[str, Optional[str]]:
        """
        Returns a dict keyed by Salesforce standard address field suffixes.
        Prefix with 'Billing' or 'Shipping' as appropriate at the call site.
        """
        return {
            "Street": self._street if not self._unit else f"{self._street}, {self._unit}",
            "City": self._city,
            "State": self._state,
            "PostalCode": self._postal_code,
            "Country": self._country_code,
        }

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Address":
        """Create from a plain dictionary (e.g. from a database row or API payload)."""
        return cls(
            street=data.get("street", ""),
            city=data.get("city", ""),
            country_code=data.get("country_code", data.get("country", "")),
            state=data.get("state"),
            postal_code=data.get("postal_code", data.get("zip", None)),
            unit=data.get("unit", data.get("suite", None)),
        )

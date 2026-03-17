"""
Email value object.

Value objects are immutable, equality is based on value, and they carry their
own validation logic.  No framework or infrastructure imports are allowed here.
"""

from __future__ import annotations

import re
from typing import Final

from domain.exceptions.domain_exceptions import ValidationError

# RFC-5322 simplified regex – good enough for domain validation without a library.
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)

_MAX_LOCAL_PART_LENGTH: Final[int] = 64
_MAX_TOTAL_LENGTH: Final[int] = 254  # RFC 5321


class Email:
    """
    Immutable value object representing a validated email address.

    Usage::

        email = Email("user@example.com")
        same  = Email("User@Example.COM")   # canonicalised to lowercase
        assert email == same
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise ValidationError(
                field="email",
                value=value,
                reason="email must be a string",
            )

        normalised = value.strip().lower()

        if not normalised:
            raise ValidationError(field="email", value=value, reason="email cannot be blank")

        if len(normalised) > _MAX_TOTAL_LENGTH:
            raise ValidationError(
                field="email",
                value=value,
                reason=f"email exceeds maximum length of {_MAX_TOTAL_LENGTH} characters",
            )

        if "@" in normalised:
            local, _, domain = normalised.partition("@")
            if len(local) > _MAX_LOCAL_PART_LENGTH:
                raise ValidationError(
                    field="email",
                    value=value,
                    reason=f"local part exceeds {_MAX_LOCAL_PART_LENGTH} characters",
                )

        if not _EMAIL_PATTERN.match(normalised):
            raise ValidationError(
                field="email",
                value=value,
                reason="not a valid email format",
            )

        object.__setattr__(self, "_value", normalised)

    # ------------------------------------------------------------------
    # Value object protocol: immutability + equality based on value
    # ------------------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise AttributeError("Email is immutable")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Email):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other.strip().lower()
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Email({self._value!r})"

    def __str__(self) -> str:
        return self._value

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @property
    def value(self) -> str:
        """Return the normalised (lowercase) email address string."""
        return self._value

    @property
    def local_part(self) -> str:
        """Return the part before the '@'."""
        return self._value.split("@")[0]

    @property
    def domain(self) -> str:
        """Return the domain portion of the address."""
        return self._value.split("@")[1]

    @property
    def is_business_email(self) -> bool:
        """
        Heuristic check: returns False for well-known free-mail providers.
        This is a domain rule, not an infrastructure concern.
        """
        free_domains = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "live.com", "aol.com", "icloud.com", "protonmail.com",
            "mail.com", "zoho.com",
        }
        return self.domain not in free_domains

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_string(cls, value: str) -> "Email":
        """Alias for the constructor; useful for explicit intent in callers."""
        return cls(value)

    @classmethod
    def try_parse(cls, value: str) -> "Email | None":
        """
        Return an Email instance if *value* is valid, otherwise None.
        Never raises; intended for cases where a missing email is acceptable.
        """
        try:
            return cls(value)
        except ValidationError:
            return None

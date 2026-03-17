"""
transformation_rules.py
─────────────────────────────────────────────────────────────────────────────
Central rules engine for all legacy-to-Salesforce data transformations.

Provides:
  - Field mapping dictionaries (legacy column -> SF field)
  - Value mapping tables (legacy picklist codes -> SF picklist values)
  - Normalisation functions (phone, email, URL, address, name)
  - Validation rules (required fields, regex patterns, length limits)
  - A reusable RulesEngine class that applies all rules to a DataFrame

Author  : Migration Platform Team
Modified: 2026-03-16
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ─── Field Mapping Dictionaries ──────────────────────────────────────────────

ACCOUNT_FIELD_MAP: Dict[str, str] = {
    "COMPANY_ID":           "Legacy_ID__c",
    "COMPANY_NAME":         "Name",
    "COMPANY_CODE":         "AccountNumber",
    "COMPANY_TYPE":         "Type",
    "INDUSTRY_CODE":        "Industry",
    "ANNUAL_REVENUE":       "AnnualRevenue",
    "EMPLOYEE_COUNT":       "NumberOfEmployees",
    "PHONE_NUMBER":         "Phone",
    "FAX_NUMBER":           "Fax",
    "WEBSITE_URL":          "Website",
    "DESCRIPTION":          "Description",
    "ADDR_LINE1":           "BillingStreet",
    "ADDR_CITY":            "BillingCity",
    "ADDR_STATE":           "BillingState",
    "ADDR_ZIP":             "BillingPostalCode",
    "ADDR_COUNTRY":         "BillingCountry",
    "BILLING_ADDR_LINE1":   "ShippingStreet",
    "BILLING_ADDR_CITY":    "ShippingCity",
    "BILLING_ADDR_STATE":   "ShippingState",
    "BILLING_ADDR_ZIP":     "ShippingPostalCode",
    "BILLING_ADDR_COUNTRY": "ShippingCountry",
    "DUNS_NUMBER":          "DunsNumber",
    "SIC_CODE":             "Sic",
    "NAICS_CODE":           "NaicsCode__c",
    "SEGMENT_CODE":         "Segment__c",
    "REGION_CODE":          "Region__c",
    "TERRITORY_CODE":       "Territory__c",
    "CREDIT_RATING":        "Credit_Rating__c",
    "CREDIT_LIMIT":         "Credit_Limit__c",
    "SOURCE_SYSTEM":        "Source_System__c",
}

CONTACT_FIELD_MAP: Dict[str, str] = {
    "PERSON_ID":          "Legacy_ID__c",
    "COMPANY_ID":         "Legacy_Account_ID__c",
    "FIRST_NAME":         "FirstName",
    "LAST_NAME":          "LastName",
    "MIDDLE_NAME":        "MiddleName",
    "SALUTATION":         "Salutation",
    "SUFFIX":             "Suffix",
    "JOB_TITLE":          "Title",
    "DEPARTMENT":         "Department",
    "DIRECT_PHONE":       "Phone",
    "MOBILE_NUMBER":      "MobilePhone",
    "WORK_PHONE":         "HomePhone",
    "ASSISTANT_NAME":     "AssistantName",
    "ASSISTANT_PHONE":    "AssistantPhone",
    "PRIMARY_EMAIL":      "Email",
    "ADDR_LINE1":         "MailingStreet",
    "ADDR_CITY":          "MailingCity",
    "ADDR_STATE":         "MailingState",
    "ADDR_ZIP":           "MailingPostalCode",
    "ADDR_COUNTRY":       "MailingCountry",
    "BIRTHDATE":          "Birthdate",
    "DESCRIPTION":        "Description",
    "DO_NOT_CALL":        "DoNotCall",
    "DO_NOT_EMAIL":       "HasOptedOutOfEmail",
    "DO_NOT_MAIL":        "HasOptedOutOfFax",
    "LEAD_SOURCE":        "LeadSource",
    "LINKEDIN_URL":       "LinkedIn_URL__c",
    "TWITTER_HANDLE":     "Twitter_Handle__c",
    "SOURCE_SYSTEM":      "Source_System__c",
}

# ─── Value Mapping Tables ─────────────────────────────────────────────────────

ACCOUNT_TYPE_MAP: Dict[str, str] = {
    "CUSTOMER":            "Customer - Direct",
    "CHANNEL_CUSTOMER":    "Customer - Channel",
    "PARTNER":             "Channel Partner / Reseller",
    "RESELLER":            "Channel Partner / Reseller",
    "VENDOR":              "Technology Partner",
    "STRATEGIC_PARTNER":   "Strategic Partner",
    "PROSPECT":            "Prospect",
    "COMPETITOR":          "Competitor",
    "INTERNAL":            "Other",
    "GOVERNMENT":          "Other",
    "NON_PROFIT":          "Other",
    "UNKNOWN":             "Prospect",
}

INDUSTRY_MAP: Dict[str, str] = {
    "TECHNOLOGY":          "Technology",
    "TECH":                "Technology",
    "SOFTWARE":            "Technology",
    "HARDWARE":            "Technology",
    "FINTECH":             "Finance",
    "FINANCE":             "Finance",
    "BANKING":             "Finance",
    "INSURANCE":           "Insurance",
    "HEALTHCARE":          "Healthcare",
    "PHARMA":              "Healthcare",
    "PHARMACEUTICAL":      "Healthcare",
    "BIOTECH":             "Biotechnology",
    "RETAIL":              "Retail",
    "E_COMMERCE":          "Retail",
    "ECOMMERCE":           "Retail",
    "MANUFACTURING":       "Manufacturing",
    "AUTOMOTIVE":          "Transportation",
    "LOGISTICS":           "Transportation",
    "TRANSPORTATION":      "Transportation",
    "ENERGY":              "Energy",
    "OIL_GAS":             "Energy",
    "UTILITIES":           "Utilities",
    "EDUCATION":           "Education",
    "GOVERNMENT":          "Government",
    "NONPROFIT":           "Nonprofit",
    "MEDIA":               "Media",
    "ENTERTAINMENT":       "Entertainment",
    "TELECOM":             "Telecommunications",
    "TELECOMMUNICATIONS":  "Telecommunications",
    "CONSTRUCTION":        "Construction",
    "REAL_ESTATE":         "Real Estate",
    "HOSPITALITY":         "Hospitality",
    "FOOD_BEVERAGE":       "Food & Beverage",
    "CONSULTING":          "Consulting",
    "LEGAL":               "Legal",
}

SALUTATION_MAP: Dict[str, str] = {
    "MR":   "Mr.",  "MR.": "Mr.",
    "MRS":  "Mrs.", "MRS.":"Mrs.",
    "MS":   "Ms.",  "MS.": "Ms.",
    "DR":   "Dr.",  "DR.": "Dr.",
    "PROF": "Prof.","PROF.":"Prof.",
    "REV":  "Rev.", "REV.":"Rev.",
}

LEAD_SOURCE_MAP: Dict[str, str] = {
    "WEB":          "Web",
    "WEBSITE":      "Web",
    "PHONE":        "Phone Inquiry",
    "PHONE_INQUIRY":"Phone Inquiry",
    "PARTNER":      "Partner",
    "REFERRAL":     "Partner",
    "TRADE_SHOW":   "Trade Show",
    "TRADESHOW":    "Trade Show",
    "EMAIL":        "Email Campaign",
    "EMAIL_CAMPAIGN":"Email Campaign",
    "COLD_CALL":    "Cold Call",
    "PURCHASED":    "Purchased List",
    "INBOUND":      "Inbound",
    "ORGANIC":      "Web",
    "SOCIAL":       "Web",
    "OTHER":        "Other",
}

COUNTRY_CODE_MAP: Dict[str, str] = {
    "UNITED STATES":        "US",
    "UNITED STATES OF AMERICA": "US",
    "USA":                  "US",
    "U.S.A":                "US",
    "U.S.":                 "US",
    "CANADA":               "CA",
    "CAN":                  "CA",
    "UNITED KINGDOM":       "GB",
    "UK":                   "GB",
    "GREAT BRITAIN":        "GB",
    "GERMANY":              "DE",
    "DEUTSCHLAND":          "DE",
    "FRANCE":               "FR",
    "SPAIN":                "ES",
    "ESPANA":               "ES",
    "ITALY":                "IT",
    "ITALIA":               "IT",
    "NETHERLANDS":          "NL",
    "HOLLAND":              "NL",
    "AUSTRALIA":            "AU",
    "AUS":                  "AU",
    "JAPAN":                "JP",
    "CHINA":                "CN",
    "INDIA":                "IN",
    "BRAZIL":               "BR",
    "MEXICO":               "MX",
    "SINGAPORE":            "SG",
    "SWEDEN":               "SE",
    "NORWAY":               "NO",
    "DENMARK":              "DK",
    "FINLAND":              "FI",
}


# ─── Normalisation Functions ──────────────────────────────────────────────────

def normalise_phone(raw: Optional[str], country_code: str = "1") -> Optional[str]:
    """
    Normalise a phone number string to E.164 format (+15551234567).
    Falls back to cleaned string if not parseable.
    """
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    # US/CA 10-digit
    if len(digits) == 10:
        return f"+{country_code}{digits}"
    # Already E.164
    if digits.startswith("+") and 8 <= len(digits) <= 16:
        return digits
    # 11-digit with country code
    if len(digits) == 11 and digits.startswith(country_code):
        return f"+{digits}"
    # Return as-is (truncated to 40 chars per SF limit)
    return str(raw).strip()[:40]


def normalise_email(raw: Optional[str]) -> Optional[str]:
    """Lowercase and validate email address."""
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return None
    email = str(raw).strip().lower()
    pattern = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
    return email if pattern.match(email) else None


def normalise_url(raw: Optional[str]) -> Optional[str]:
    """Ensure URL has https:// prefix; return None if blank or too long."""
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return None
    url = str(raw).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url if len(url) <= 255 else None


def normalise_country(raw: Optional[str]) -> Optional[str]:
    """Map verbose country name to ISO 3166-1 alpha-2 code."""
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return None
    code = str(raw).strip().upper()
    if len(code) == 2:
        return code
    return COUNTRY_CODE_MAP.get(code, code[:2] if len(code) >= 2 else code)


def normalise_string(raw: Optional[str], max_len: int = 255) -> Optional[str]:
    """Strip whitespace, normalise whitespace chars, truncate to max_len."""
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return None
    cleaned = re.sub(r"[\r\n\t]+", " ", str(raw).strip())
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned[:max_len] if len(cleaned) > max_len else cleaned


def map_picklist(raw: Optional[str], mapping: Dict[str, str],
                 default: Optional[str] = None) -> Optional[str]:
    """Map a raw picklist code using the provided mapping dict."""
    if not raw or str(raw).strip() in {"nan", "None", ""}:
        return default
    return mapping.get(str(raw).strip().upper(), default)


# ─── Validation Rules ─────────────────────────────────────────────────────────

@dataclass
class ValidationRule:
    """Defines a single validation rule applied to a DataFrame column."""
    column:       str
    description:  str
    check:        Callable[[pd.Series], pd.Series]  # Returns bool mask (True = valid)
    action:       str = "warn"   # "warn" | "error" | "drop" | "fix"
    fix_fn:       Optional[Callable[[pd.Series], pd.Series]] = None

    def apply(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        """Apply rule to df, returning modified df and number of violations."""
        if self.column not in df.columns:
            return df, 0
        valid_mask    = self.check(df[self.column])
        violation_cnt = (~valid_mask).sum()
        if violation_cnt == 0:
            return df, 0

        logger.log(
            logging.WARNING if self.action in ("warn", "fix") else logging.ERROR,
            "Validation rule '%s' on column '%s': %d violations.",
            self.description, self.column, violation_cnt,
        )

        if self.action == "drop":
            df = df[valid_mask].copy()
        elif self.action == "fix" and self.fix_fn is not None:
            df.loc[~valid_mask, self.column] = self.fix_fn(df.loc[~valid_mask, self.column])

        return df, int(violation_cnt)


ACCOUNT_VALIDATION_RULES: List[ValidationRule] = [
    ValidationRule(
        column="Name",
        description="Name must not be blank",
        check=lambda s: s.notna() & (s.str.strip() != ""),
        action="error",
    ),
    ValidationRule(
        column="Name",
        description="Name max length 255",
        check=lambda s: s.str.len() <= 255,
        action="fix",
        fix_fn=lambda s: s.str[:255],
    ),
    ValidationRule(
        column="Phone",
        description="Phone max length 40",
        check=lambda s: s.isna() | (s.str.len() <= 40),
        action="fix",
        fix_fn=lambda s: s.str[:40],
    ),
    ValidationRule(
        column="Website",
        description="Website max length 255",
        check=lambda s: s.isna() | (s.str.len() <= 255),
        action="fix",
        fix_fn=lambda s: s.where(s.str.len() <= 255, None),
    ),
    ValidationRule(
        column="AnnualRevenue",
        description="AnnualRevenue must be non-negative",
        check=lambda s: s.isna() | (s >= 0),
        action="fix",
        fix_fn=lambda s: s.where(s >= 0, None),
    ),
    ValidationRule(
        column="Legacy_ID__c",
        description="Legacy_ID__c must not be null",
        check=lambda s: s.notna() & (s.astype(str).str.strip() != ""),
        action="error",
    ),
]

CONTACT_VALIDATION_RULES: List[ValidationRule] = [
    ValidationRule(
        column="LastName",
        description="LastName must not be blank",
        check=lambda s: s.notna() & (s.str.strip() != ""),
        action="fix",
        fix_fn=lambda s: s.fillna("UNKNOWN"),
    ),
    ValidationRule(
        column="Email",
        description="Email must be valid format",
        check=lambda s: s.isna() | s.str.match(
            r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"),
        action="fix",
        fix_fn=lambda s: s.where(
            s.str.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"), None),
    ),
    ValidationRule(
        column="LastName",
        description="LastName max length 80",
        check=lambda s: s.isna() | (s.str.len() <= 80),
        action="fix",
        fix_fn=lambda s: s.str[:80],
    ),
    ValidationRule(
        column="FirstName",
        description="FirstName max length 40",
        check=lambda s: s.isna() | (s.str.len() <= 40),
        action="fix",
        fix_fn=lambda s: s.str[:40],
    ),
]


# ─── Rules Engine ─────────────────────────────────────────────────────────────

@dataclass
class RulesEngineResult:
    """Summary of a rules engine execution."""
    total_input_rows:     int = 0
    total_output_rows:    int = 0
    total_violations:     int = 0
    total_errors:         int = 0
    rows_dropped:         int = 0
    violations_by_rule:   Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_input_rows":   self.total_input_rows,
            "total_output_rows":  self.total_output_rows,
            "total_violations":   self.total_violations,
            "total_errors":       self.total_errors,
            "rows_dropped":       self.rows_dropped,
            "violations_by_rule": self.violations_by_rule,
        }


class RulesEngine:
    """
    Applies a list of ValidationRules to a DataFrame.
    Collects metrics, logs violations, and returns cleaned data.
    """

    def __init__(self, rules: List[ValidationRule]) -> None:
        self.rules = rules

    def apply(self, df: pd.DataFrame) -> Tuple[pd.DataFrame, RulesEngineResult]:
        result = RulesEngineResult(total_input_rows=len(df))
        for rule in self.rules:
            df, violations = rule.apply(df)
            if violations:
                result.total_violations += violations
                result.violations_by_rule[rule.description] = violations
                if rule.action == "error":
                    result.total_errors += violations
        result.total_output_rows = len(df)
        result.rows_dropped      = result.total_input_rows - result.total_output_rows
        return df, result

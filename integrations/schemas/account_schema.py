"""
Pydantic v2 schemas for Account data validation.

Covers:
  - LegacyAccount     – raw data as ingested from the legacy ERP system
  - SalesforceAccount – canonical Salesforce Account sObject shape
  - AccountMigrationRecord – links legacy → Salesforce with audit fields
  - Transformation helpers
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------


class AccountType(str, Enum):
    PROSPECT = "Prospect"
    CUSTOMER = "Customer - Direct"
    CHANNEL_CUSTOMER = "Customer - Channel"
    PARTNER = "Partner"
    COMPETITOR = "Competitor"
    ANALYST = "Analyst"
    PRESS = "Press"
    OTHER = "Other"


class Industry(str, Enum):
    AGRICULTURE = "Agriculture"
    APPAREL = "Apparel"
    BANKING = "Banking"
    BIOTECHNOLOGY = "Biotechnology"
    CHEMICALS = "Chemicals"
    COMMUNICATIONS = "Communications"
    CONSTRUCTION = "Construction"
    CONSULTING = "Consulting"
    EDUCATION = "Education"
    ELECTRONICS = "Electronics"
    ENERGY = "Energy"
    ENGINEERING = "Engineering"
    ENTERTAINMENT = "Entertainment"
    ENVIRONMENTAL = "Environmental"
    FINANCE = "Finance"
    FOOD_AND_BEVERAGE = "Food & Beverage"
    GOVERNMENT = "Government"
    HEALTHCARE = "Healthcare"
    HOSPITALITY = "Hospitality"
    INSURANCE = "Insurance"
    MACHINERY = "Machinery"
    MANUFACTURING = "Manufacturing"
    MEDIA = "Media"
    NOT_FOR_PROFIT = "Not For Profit"
    RECREATION = "Recreation"
    RETAIL = "Retail"
    SHIPPING = "Shipping"
    TECHNOLOGY = "Technology"
    TELECOMMUNICATIONS = "Telecommunications"
    TRANSPORTATION = "Transportation"
    UTILITIES = "Utilities"
    OTHER = "Other"


class MigrationStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"


class RecordRating(str, Enum):
    HOT = "Hot"
    WARM = "Warm"
    COLD = "Cold"


# ---------------------------------------------------------------------------
# Shared address model
# ---------------------------------------------------------------------------


class Address(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    street: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)
    country_code: Optional[str] = Field(None, min_length=2, max_length=2)

    @field_validator("postal_code")
    @classmethod
    def normalise_postal_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return v.strip().upper()

    @field_validator("country_code")
    @classmethod
    def upper_country_code(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v


# ---------------------------------------------------------------------------
# Legacy Account (raw ingestion)
# ---------------------------------------------------------------------------


class LegacyAccount(BaseModel):
    """
    Raw account record as returned by the legacy ERP/CRM system.

    All fields are intentionally permissive to accept whatever the legacy
    system provides; validation and transformation happen in the mapping layer.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="allow",
    )

    # Identifiers
    customer_id: str = Field(..., alias="customerId", description="Legacy primary key")
    account_number: Optional[str] = Field(None, alias="accountNumber")

    # Core fields
    company_name: str = Field(..., alias="companyName", min_length=1, max_length=255)
    legal_name: Optional[str] = Field(None, alias="legalName", max_length=255)
    dba_name: Optional[str] = Field(None, alias="dbaName", max_length=255)

    # Contact
    phone: Optional[str] = Field(None, max_length=40)
    fax: Optional[str] = Field(None, max_length=40)
    website: Optional[str] = Field(None, max_length=255)
    primary_email: Optional[str] = Field(None, alias="primaryEmail", max_length=255)

    # Address (flat legacy format)
    billing_street: Optional[str] = Field(None, alias="billingStreet")
    billing_city: Optional[str] = Field(None, alias="billingCity")
    billing_state: Optional[str] = Field(None, alias="billingState")
    billing_zip: Optional[str] = Field(None, alias="billingZip")
    billing_country: Optional[str] = Field(None, alias="billingCountry")

    # Financial
    annual_revenue: Optional[str] = Field(None, alias="annualRevenue")  # raw string
    credit_limit: Optional[str] = Field(None, alias="creditLimit")
    currency_code: Optional[str] = Field(None, alias="currencyCode", max_length=3)

    # Classification
    industry_code: Optional[str] = Field(None, alias="industryCode")
    customer_type: Optional[str] = Field(None, alias="customerType")
    sic_code: Optional[str] = Field(None, alias="sicCode", max_length=10)
    naics_code: Optional[str] = Field(None, alias="naicsCode", max_length=10)
    employees: Optional[int] = Field(None, alias="numberOfEmployees", ge=0)

    # Audit
    created_date: Optional[datetime] = Field(None, alias="createdDate")
    modified_date: Optional[datetime] = Field(None, alias="modifiedDate")
    is_active: Optional[bool] = Field(None, alias="isActive")
    erp_region: Optional[str] = Field(None, alias="erpRegion")

    # External IDs
    tax_id: Optional[str] = Field(None, alias="taxId", max_length=50)
    duns_number: Optional[str] = Field(None, alias="dunsNumber", max_length=15)

    @field_validator("phone", "fax", mode="before")
    @classmethod
    def clean_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        cleaned = re.sub(r"[^\d\+\-\(\)\s\.]", "", str(v)).strip()
        return cleaned or None

    @field_validator("annual_revenue", "credit_limit", mode="before")
    @classmethod
    def clean_currency_string(cls, v: Optional[Any]) -> Optional[str]:
        if v is None:
            return v
        cleaned = re.sub(r"[^\d\.\-]", "", str(v))
        return cleaned or None

    @field_validator("website", mode="before")
    @classmethod
    def normalise_url(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            v = f"https://{v}"
        return v


# ---------------------------------------------------------------------------
# Salesforce Account (canonical target)
# ---------------------------------------------------------------------------


class SalesforceAccount(BaseModel):
    """
    Canonical Salesforce Account sObject ready for API submission.

    All fields align with the Salesforce Account standard object field API names.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    # Salesforce system fields (populated after creation)
    Id: Optional[str] = Field(None, description="Salesforce 18-char record ID")

    # Standard fields
    Name: str = Field(..., min_length=1, max_length=255)
    AccountNumber: Optional[str] = Field(None, max_length=40)
    Type: Optional[AccountType] = None
    Industry: Optional[Industry] = None
    Rating: Optional[RecordRating] = None

    # Contact
    Phone: Optional[str] = Field(None, max_length=40)
    Fax: Optional[str] = Field(None, max_length=40)
    Website: Optional[str] = Field(None, max_length=255)

    # Billing address
    BillingStreet: Optional[str] = Field(None, max_length=255)
    BillingCity: Optional[str] = Field(None, max_length=100)
    BillingState: Optional[str] = Field(None, max_length=100)
    BillingPostalCode: Optional[str] = Field(None, max_length=20)
    BillingCountry: Optional[str] = Field(None, max_length=100)
    BillingCountryCode: Optional[str] = Field(None, max_length=2)

    # Shipping address
    ShippingStreet: Optional[str] = Field(None, max_length=255)
    ShippingCity: Optional[str] = Field(None, max_length=100)
    ShippingState: Optional[str] = Field(None, max_length=100)
    ShippingPostalCode: Optional[str] = Field(None, max_length=20)
    ShippingCountry: Optional[str] = Field(None, max_length=100)

    # Financial
    AnnualRevenue: Optional[Decimal] = Field(None, ge=Decimal("0"))
    NumberOfEmployees: Optional[int] = Field(None, ge=0)

    # Identifiers
    Sic: Optional[str] = Field(None, max_length=20)
    NaicsCode: Optional[str] = Field(None, max_length=8)

    # Custom fields (migration-specific)
    Legacy_Customer_ID__c: Optional[str] = Field(None, max_length=50)
    Legacy_Account_Number__c: Optional[str] = Field(None, max_length=50)
    Tax_ID__c: Optional[str] = Field(None, max_length=50)
    DUNS_Number__c: Optional[str] = Field(None, max_length=15)
    Migration_Status__c: Optional[str] = Field(None, max_length=50)
    Migration_Date__c: Optional[datetime] = None
    ERP_Region__c: Optional[str] = Field(None, max_length=50)

    @field_validator("BillingCountryCode", mode="before")
    @classmethod
    def upper_country_code(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    def to_api_dict(self) -> Dict[str, Any]:
        """
        Serialise to a dict suitable for the Salesforce REST API.

        Excludes None values and the system-managed ``Id`` field on creation.
        """
        return {
            k: v
            for k, v in self.model_dump(exclude_none=True).items()
            if k != "Id" or v is not None
        }


# ---------------------------------------------------------------------------
# Migration record
# ---------------------------------------------------------------------------


class ValidationError(BaseModel):
    """A single field-level validation error."""

    field: str
    message: str
    severity: str = "error"   # "error" | "warning" | "info"
    raw_value: Optional[Any] = None


class AccountMigrationRecord(BaseModel):
    """
    Audit record linking a legacy account to its Salesforce counterpart.

    Created once per account in the migration pipeline and updated as the
    record moves through each stage.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    migration_id: str = Field(description="UUID for this migration attempt")
    legacy_customer_id: str
    salesforce_id: Optional[str] = None
    status: MigrationStatus = MigrationStatus.PENDING
    batch_id: Optional[str] = None

    # Snapshot references
    legacy_snapshot: Optional[LegacyAccount] = None
    salesforce_payload: Optional[SalesforceAccount] = None

    # Validation
    validation_errors: List[ValidationError] = Field(default_factory=list)
    validation_passed: bool = True

    # Timing
    created_at: datetime = Field(default_factory=lambda: datetime.now())
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None

    # Error tracking
    error_message: Optional[str] = None
    error_stack: Optional[str] = None
    retry_count: int = 0

    # Metadata
    migrated_by: Optional[str] = None  # service or user name
    notes: Optional[str] = None

    @model_validator(mode="after")
    def compute_validation_passed(self) -> "AccountMigrationRecord":
        errors = [e for e in self.validation_errors if e.severity == "error"]
        self.validation_passed = len(errors) == 0
        return self

    def mark_started(self) -> None:
        self.status = MigrationStatus.IN_PROGRESS
        self.started_at = datetime.now()

    def mark_completed(self, sf_id: str) -> None:
        self.status = MigrationStatus.COMPLETED
        self.salesforce_id = sf_id
        self.completed_at = datetime.now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def mark_failed(self, error: str, stack: Optional[str] = None) -> None:
        self.status = MigrationStatus.FAILED
        self.error_message = error
        self.error_stack = stack
        self.completed_at = datetime.now()

    def add_validation_error(
        self, field: str, message: str, severity: str = "error", raw_value: Any = None
    ) -> None:
        self.validation_errors.append(
            ValidationError(
                field=field,
                message=message,
                severity=severity,
                raw_value=raw_value,
            )
        )


# ---------------------------------------------------------------------------
# Transformation helper
# ---------------------------------------------------------------------------


_INDUSTRY_CODE_MAP: Dict[str, Industry] = {
    "TECH": Industry.TECHNOLOGY,
    "FIN": Industry.FINANCE,
    "BANK": Industry.BANKING,
    "HLTH": Industry.HEALTHCARE,
    "MFG": Industry.MANUFACTURING,
    "RETL": Industry.RETAIL,
    "CONS": Industry.CONSULTING,
    "ENGY": Industry.ENERGY,
    "TELE": Industry.TELECOMMUNICATIONS,
    "EDUC": Industry.EDUCATION,
    "GOVT": Industry.GOVERNMENT,
    "INSU": Industry.INSURANCE,
    "MEDA": Industry.MEDIA,
}

_CUSTOMER_TYPE_MAP: Dict[str, AccountType] = {
    "DIRECT": AccountType.CUSTOMER,
    "CHANNEL": AccountType.CHANNEL_CUSTOMER,
    "PARTNER": AccountType.PARTNER,
    "PROSPECT": AccountType.PROSPECT,
}


def transform_legacy_to_salesforce(
    legacy: LegacyAccount,
) -> Tuple["SalesforceAccount", List["ValidationError"]]:
    """
    Transform a :class:`LegacyAccount` to a :class:`SalesforceAccount`.

    Returns the mapped account and a list of any validation warnings.
    """
    from datetime import datetime as _dt

    warnings: List[ValidationError] = []

    # Resolve industry
    industry: Optional[Industry] = None
    if legacy.industry_code:
        industry = _INDUSTRY_CODE_MAP.get(legacy.industry_code.upper())
        if not industry:
            warnings.append(
                ValidationError(
                    field="Industry",
                    message=f"Unknown industry_code '{legacy.industry_code}' – defaulting to Other",
                    severity="warning",
                    raw_value=legacy.industry_code,
                )
            )
            industry = Industry.OTHER

    # Resolve account type
    account_type: Optional[AccountType] = None
    if legacy.customer_type:
        account_type = _CUSTOMER_TYPE_MAP.get(legacy.customer_type.upper())

    # Resolve revenue
    annual_revenue: Optional[Decimal] = None
    if legacy.annual_revenue:
        try:
            annual_revenue = Decimal(legacy.annual_revenue)
        except Exception:
            warnings.append(
                ValidationError(
                    field="AnnualRevenue",
                    message=f"Cannot parse annual_revenue '{legacy.annual_revenue}'",
                    severity="warning",
                    raw_value=legacy.annual_revenue,
                )
            )

    sf_account = SalesforceAccount(
        Name=legacy.legal_name or legacy.company_name,
        AccountNumber=legacy.account_number,
        Type=account_type,
        Industry=industry,
        Phone=legacy.phone,
        Fax=legacy.fax,
        Website=legacy.website,
        BillingStreet=legacy.billing_street,
        BillingCity=legacy.billing_city,
        BillingState=legacy.billing_state,
        BillingPostalCode=legacy.billing_zip,
        BillingCountry=legacy.billing_country,
        AnnualRevenue=annual_revenue,
        NumberOfEmployees=legacy.employees,
        Sic=legacy.sic_code,
        NaicsCode=legacy.naics_code,
        Legacy_Customer_ID__c=legacy.customer_id,
        Legacy_Account_Number__c=legacy.account_number,
        Tax_ID__c=legacy.tax_id,
        DUNS_Number__c=legacy.duns_number,
        Migration_Status__c=MigrationStatus.IN_PROGRESS.value,
        Migration_Date__c=_dt.now(),
        ERP_Region__c=legacy.erp_region,
    )

    return sf_account, warnings


# Convenience type alias
Tuple = tuple  # re-export so callers don't need typing.Tuple

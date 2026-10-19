"""Admin request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CareType,
    CitizenshipType,
    SubsidyMetric,
    UserRole,
)


class AdminProfileResponse(BaseModel):
    """Administrator profile response schema."""

    user_id: str
    email: str
    full_name: str
    role: UserRole
    account_status: AccountStatus
    admin_level: int | None = Field(default=None)

    model_config = ConfigDict(from_attributes=True)


class AdminLogoutResponse(BaseModel):
    """Administrator logout response schema."""

    message: str = "Successfully logged out"


class SubsidyRuleResponse(BaseModel):
    """Administrative subsidy rule representation schema."""

    rule_id: str
    subsidy_name: str
    tier_name: str | None = None
    care_type: CareType
    metric: SubsidyMetric
    applicant_status: ApplicantStatus | None = None
    citizenship: CitizenshipType
    income_min: float | None = None
    income_max: float | None = None
    subsidy_amount: float
    effective_date: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SubsidyRuleCreate(BaseModel):
    """Payload for creating an individual subsidy rule or defining an item in bulk payload."""

    rule_id: str | None = Field(
        default=None,
        description="Optional custom rule ID; generated as UUID if omitted",
    )
    subsidy_name: str = Field(description="Display name of the subsidy rule")
    tier_name: str | None = Field(
        default=None, description="Optional tier categorization"
    )
    care_type: CareType = Field(
        description="Applicable care type (INFANT_CARE, CHILD_CARE)"
    )
    metric: SubsidyMetric = Field(
        description="Means-testing metric (FIXED, HOUSEHOLD_INCOME, PER_CAPITA_INCOME)"
    )
    applicant_status: ApplicantStatus | None = Field(
        default=None, description="Working or non-working status"
    )
    citizenship: CitizenshipType = Field(
        default=CitizenshipType.SC, description="Target citizenship classification"
    )
    income_min: float | None = Field(
        default=None, description="Minimum qualifying income bound"
    )
    income_max: float | None = Field(
        default=None, description="Maximum qualifying income bound"
    )
    subsidy_amount: float = Field(
        ge=0, description="Subsidy entitlement grant amount in SGD"
    )
    effective_date: datetime | None = Field(
        default=None, description="Effective commencement timestamp"
    )

    @model_validator(mode="after")
    def validate_income_range(self) -> "SubsidyRuleCreate":
        if (
            self.income_min is not None
            and self.income_max is not None
            and self.income_min > self.income_max
        ):
            raise ValueError("income_min cannot be greater than income_max")
        return self


class SubsidyRuleUpdate(BaseModel):
    """Payload for updating an existing subsidy rule in-place."""

    subsidy_name: str | None = None
    tier_name: str | None = None
    care_type: CareType | None = None
    metric: SubsidyMetric | None = None
    applicant_status: ApplicantStatus | None = None
    citizenship: CitizenshipType | None = None
    income_min: float | None = None
    income_max: float | None = None
    subsidy_amount: float | None = Field(default=None, ge=0)
    effective_date: datetime | None = None

    @model_validator(mode="after")
    def validate_income_range(self) -> "SubsidyRuleUpdate":
        if (
            self.income_min is not None
            and self.income_max is not None
            and self.income_min > self.income_max
        ):
            raise ValueError("income_min cannot be greater than income_max")
        return self


class BulkSubsidyRulesRequest(BaseModel):
    """Payload for bulk subsidy rule matrix operations."""

    rules: list[SubsidyRuleCreate]

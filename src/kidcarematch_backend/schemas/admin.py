"""Admin request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CareType,
    CitizenshipType,
    ModerationStatus,
    SubsidyMetric,
    SyncState,
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


class AdminUserResponse(BaseModel):
    """Detailed user account profile for administrative management."""

    user_id: str
    firebase_uid: str
    email: str
    full_name: str
    role: UserRole
    account_status: AccountStatus
    admin_level: int | None = Field(default=None)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminUserListResponse(BaseModel):
    """Paginated envelope for administrative user listings."""

    items: list[AdminUserResponse]
    total: int
    limit: int
    offset: int


class AdminUserCreateRequest(BaseModel):
    """Payload for administrative user account creation and provisioning."""

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=6, max_length=128)
    role: UserRole = Field(default=UserRole.PARENT)
    admin_level: int | None = Field(default=None, ge=1)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Full name cannot be blank or whitespace only")
        if len(stripped) > 100:
            raise ValueError("Full name cannot exceed 100 characters")
        return stripped


class AdminUserUpdateRequest(BaseModel):
    """Payload for administrative user profile update."""

    full_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: UserRole | None = Field(default=None)
    admin_level: int | None = Field(default=None, ge=1)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Full name cannot be blank or whitespace only")
        if len(stripped) > 100:
            raise ValueError("Full name cannot exceed 100 characters")
        return stripped


class AdminReviewItem(BaseModel):
    """Preschool centre review item enriched with centre and author metadata for administrators."""

    review_id: str
    centre_id: int
    centre_name: str
    user_id: str
    full_name: str
    email: str
    rating: int
    review_description: str | None = None
    moderation_status: ModerationStatus
    moderated_at: datetime | None = None
    moderated_by: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminReviewListResponse(BaseModel):
    """Paginated list response of preschool centre reviews for administrators."""

    items: list[AdminReviewItem]
    total: int
    limit: int
    offset: int


class AdminReviewStatusUpdateRequest(BaseModel):
    """Payload for updating preschool centre review moderation status."""

    moderation_status: ModerationStatus


class AdminReviewResponse(BaseModel):
    """Schema for preschool centre review details after moderation."""

    review_id: str
    centre_id: int
    user_id: str
    rating: int
    review_description: str | None = None
    moderation_status: ModerationStatus
    moderated_at: datetime | None = None
    moderated_by: str | None = None
    created_at: datetime
    centre_name: str | None = None
    full_name: str | None = None
    email: str | None = None

    model_config = ConfigDict(from_attributes=True)


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


class AdminSyncResponse(BaseModel):
    """Response schema for asynchronous dataset synchronization trigger."""

    status: SyncState = SyncState.RUNNING
    message: str = "ECDA synchronization started in background"
    source_id: str = "ECDA_DATASTORE"


class DataSourceStatusResponse(BaseModel):
    """Operational heartbeat record response schema for monitored external data sources."""

    source_id: str
    source_name: str
    sync_state: SyncState
    last_successful_sync: datetime | None = None
    last_attempted_sync: datetime | None = None
    records_processed: int = 0
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)

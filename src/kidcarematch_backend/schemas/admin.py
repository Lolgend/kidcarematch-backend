"""Admin request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from kidcarematch_backend.db.db import AccountStatus, ModerationStatus, UserRole


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

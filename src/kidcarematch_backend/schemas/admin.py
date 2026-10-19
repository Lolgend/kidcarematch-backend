"""Admin request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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

"""Preschool centre review request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kidcarematch_backend.db.db import ModerationStatus


class ReviewCreateRequest(BaseModel):
    """Payload for submitting a preschool centre review."""

    rating: int = Field(ge=1, le=5, description="Preschool centre rating from 1 to 5 stars")
    review_description: str | None = Field(
        default=None,
        description="Optional text commentary from the parent",
    )


class ReviewUpdateRequest(BaseModel):
    """Payload for updating an existing preschool centre review."""

    rating: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Preschool centre rating from 1 to 5 stars",
    )
    review_description: str | None = Field(
        default=None,
        description="Optional text commentary from the parent",
    )

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int | None) -> int | None:
        if v is None:
            raise ValueError("Rating cannot be null")
        return v


class ReviewBaseResponse(BaseModel):
    """Base schema for preschool centre review response representations."""

    review_id: str
    centre_id: int
    rating: int
    review_description: str | None = None
    moderation_status: ModerationStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewResponse(ReviewBaseResponse):
    """Complete preschool centre review details."""

    user_id: str


class UserReviewItem(ReviewBaseResponse):
    """Parent portfolio review item enriched with centre metadata."""

    centre_name: str


class UserReviewListResponse(BaseModel):
    """Paginated portfolio list of parent-authored reviews."""

    items: list[UserReviewItem]
    total: int
    limit: int
    offset: int

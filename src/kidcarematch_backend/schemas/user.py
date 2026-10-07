"""User request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kidcarematch_backend.db.db import AccountStatus, UserRole


class UserSignupRequest(BaseModel):
    """Payload for user onboarding and signup."""

    full_name: str = Field(min_length=1, max_length=100)


class UserUpdateRequest(BaseModel):
    """Payload for user profile update."""

    full_name: str | None = Field(default=None, min_length=1, max_length=100)


class UserResponse(BaseModel):
    """Public user profile response."""

    user_id: str
    firebase_uid: str
    email: str
    full_name: str
    role: UserRole
    account_status: AccountStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

"""User request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kidcarematch_backend.db.db import AccountStatus, UserRole


class UserSignupRequest(BaseModel):
    """Payload for user signup."""

    full_name: str = Field(min_length=1, max_length=100)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Full name cannot be blank or whitespace only")
        if len(stripped) > 100:
            raise ValueError("Full name cannot exceed 100 characters")
        return stripped


class UserUpdateRequest(BaseModel):
    """Payload for user profile update."""

    full_name: str | None = Field(default=None, min_length=1, max_length=100)

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

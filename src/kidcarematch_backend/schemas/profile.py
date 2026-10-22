"""Family Preference Profile request and response schemas."""

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kidcarematch_backend.db.db import (
    CitizenshipType,
    LocationPreference,
    PreschoolLevel,
)

SINGAPORE_POSTAL_REGEX = re.compile(r"^\d{6}$")
ENROLLMENT_MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ProfileCreateRequest(BaseModel):
    """Payload for creating a Family Preference Profile."""

    profile_name: str = Field(
        min_length=1,
        max_length=100,
        description="Descriptive name or label for the preference profile (unique per parent)",
    )
    level: PreschoolLevel = Field(
        description="Preschool cohort developmental age level",
    )
    citizenship_status: CitizenshipType = Field(
        default=CitizenshipType.SC,
        description="Child's citizenship status for fee and subsidy determination",
    )
    service_required: str = Field(
        min_length=1,
        max_length=100,
        description="Required service offering type (e.g. Full Day, Half Day)",
    )
    home_postal_code: str = Field(
        description="6-digit Singapore home postal code",
    )
    workplace_postal_code: str | None = Field(
        default=None,
        description="Optional 6-digit Singapore workplace postal code",
    )
    location_preference: LocationPreference = Field(
        default=LocationPreference.HOME,
        description="Proximity anchor prioritization preference (HOME, WORKPLACE, EITHER)",
    )
    household_income: float = Field(
        ge=0.0,
        description="Gross monthly household income in SGD",
    )
    household_member_count: int = Field(
        ge=1,
        description="Total number of household members for per-capita income calculation",
    )
    budget_max: float = Field(
        gt=0.0,
        description="Maximum monthly net preschool fee budget ceiling in SGD",
    )
    enrollment_month_year: str = Field(
        description="Target enrollment month and year in YYYY-MM format",
    )
    cost_weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Trade-off weight assigned to cost factor (0.0 to 1.0)",
    )
    travel_weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Trade-off weight assigned to travel factor (0.0 to 1.0)",
    )

    @field_validator("profile_name")
    @classmethod
    def validate_profile_name(cls, v: str) -> str:
        """Validate profile_name is non-empty after stripping whitespace."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("profile_name cannot be empty or whitespace only")
        return stripped

    @field_validator("service_required")
    @classmethod
    def validate_service_required(cls, v: str) -> str:
        """Validate service_required is non-empty after stripping whitespace."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("service_required cannot be empty or whitespace only")
        return stripped

    @field_validator("home_postal_code")
    @classmethod
    def validate_home_postal(cls, v: str) -> str:
        """Validate 6-digit Singapore postal code format for home residence."""
        if not SINGAPORE_POSTAL_REGEX.match(v):
            raise ValueError(
                "home_postal_code must be a valid 6-digit Singapore postal code"
            )
        return v

    @field_validator("workplace_postal_code")
    @classmethod
    def validate_workplace_postal(cls, v: str | None) -> str | None:
        """Validate 6-digit Singapore postal code format for workplace if provided."""
        if v is not None and not SINGAPORE_POSTAL_REGEX.match(v):
            raise ValueError(
                "workplace_postal_code must be a valid 6-digit Singapore postal code"
            )
        return v

    @field_validator("enrollment_month_year")
    @classmethod
    def validate_enrollment_month(cls, v: str) -> str:
        """Validate target enrollment month matches YYYY-MM format."""
        if not ENROLLMENT_MONTH_REGEX.match(v):
            raise ValueError(
                "enrollment_month_year must be in YYYY-MM format (e.g. 2027-03)"
            )
        return v

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> "ProfileCreateRequest":
        """Enforce domain invariants across location preferences and trade-off weights."""
        if (
            self.location_preference
            in (LocationPreference.WORKPLACE, LocationPreference.EITHER)
            and not self.workplace_postal_code
        ):
            raise ValueError(
                "Workplace postal code is required when location preference is WORKPLACE or EITHER"
            )
        if round(self.cost_weight + self.travel_weight, 2) != 1.0:
            raise ValueError("cost_weight and travel_weight must sum to 1.0")
        return self

    def get_enrollment_datetime(self) -> datetime:
        """Convert YYYY-MM to the 1st of the month at 00:00:00 UTC."""
        year, month = map(int, self.enrollment_month_year.split("-"))
        return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)


class ProfileResponse(BaseModel):
    """Response schema representing a Family Preference Profile."""

    profile_id: str
    user_id: str
    profile_name: str
    level: PreschoolLevel
    citizenship_status: CitizenshipType
    service_required: str
    home_postal_code: str
    workplace_postal_code: str | None = None
    location_preference: LocationPreference
    household_income: float
    household_member_count: int
    budget_max: float
    enrollment_month_year: str
    cost_weight: float
    travel_weight: float

    model_config = ConfigDict(from_attributes=True)

    @field_validator("enrollment_month_year", mode="before")
    @classmethod
    def format_enrollment_month_year(cls, v: Any) -> str:
        """Format enrollment_month_year datetime to YYYY-MM string."""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m")
        return str(v)

"""Watchlist request and response schemas."""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kidcarematch_backend.db.db import PreschoolLevel


class WatchlistCreateRequest(BaseModel):
    """Payload for creating a cohort watchlist subscription."""

    centre_id: int = Field(description="Preschool centre database ID")
    level: PreschoolLevel = Field(description="Preschool cohort developmental level")
    enrollment_month_year: datetime = Field(
        description="Target enrollment month and year (normalized to day 1, 00:00:00 UTC)"
    )

    @field_validator("enrollment_month_year")
    @classmethod
    def normalize_enrollment_month_year(cls, v: datetime) -> datetime:
        """Normalize enrollment month to day 1 at 00:00:00 UTC."""
        dt = v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=timezone.utc)


class WatchlistEntryResponse(BaseModel):
    """Created watchlist entry response."""

    watchlist_id: str
    user_id: str
    centre_id: int
    level: PreschoolLevel
    enrollment_month_year: datetime

    model_config = ConfigDict(from_attributes=True)


class UserWatchlistItem(BaseModel):
    """Enriched watchlist item for parent's portfolio dashboard."""

    watchlist_id: str
    centre_id: int
    centre_name: str
    centre_code: str
    postal: str | None = None
    address: str | None = None
    spark_certified: bool
    gov_subsidy: bool
    level: PreschoolLevel
    enrollment_month_year: datetime
    current_vacancy_status: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UserWatchlistListResponse(BaseModel):
    """Pagination envelope for user's watchlist portfolio."""

    items: list[UserWatchlistItem]
    total: int
    limit: int
    offset: int


class CentreWatchlistEntryItem(BaseModel):
    """Summary of an active watchlist entry for centre introspection."""

    watchlist_id: str
    centre_id: int
    level: PreschoolLevel
    enrollment_month_year: datetime

    model_config = ConfigDict(from_attributes=True)


class CentreWatchlistStatusResponse(BaseModel):
    """Introspection response indicating whether the current parent watches this centre."""

    centre_id: int
    is_watched: bool
    watched_entries: list[CentreWatchlistEntryItem]

    model_config = ConfigDict(from_attributes=True)

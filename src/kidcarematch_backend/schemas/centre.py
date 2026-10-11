"""Preschool centre request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CentreSummaryResponse(BaseModel):
    """Aggregate dashboard summary of preschool centres."""

    total_centres: int
    spark_certified_count: int
    gov_subsidy_count: int
    centres_with_vacancies_count: int
    last_sync_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PreschoolCentreItem(BaseModel):
    """Preschool centre summary item for paginated listings."""

    centre_id: int
    centre_code: str
    centre_name: str
    contact: str | None = None
    email: str | None = None
    postal: str | None = None
    website_url: str | None = None
    food_offered: str | None = None
    address: str | None = None
    second_languages: str | None = None
    spark_certified: bool
    weekday_full_day_hours: str | None = None
    saturday_hours: str | None = None
    extended_operating_hours: bool
    transport_provided: bool
    gov_subsidy: bool
    last_updated: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


PreschoolCentreResponse = PreschoolCentreItem


class PreschoolCentreListResponse(BaseModel):
    """Pagination envelope for preschool centres list."""

    items: list[PreschoolCentreItem]
    total: int
    limit: int
    offset: int


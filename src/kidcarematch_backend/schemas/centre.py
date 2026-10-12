"""Preschool centre request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from kidcarematch_backend.db.db import CitizenshipType, PreschoolLevel


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




class PreschoolCentreListResponse(BaseModel):
    """Pagination envelope for preschool centres list."""

    items: list[PreschoolCentreItem]
    total: int
    limit: int
    offset: int


class VacancyRecordResponse(BaseModel):
    """Preschool centre vacancy status record."""

    record_id: str | None = None
    centre_id: int
    level_type: PreschoolLevel
    enrollment_month_year: datetime
    vacancy_status: str

    model_config = ConfigDict(from_attributes=True)


class PreschoolServiceResponse(BaseModel):
    """Preschool centre service offering and fee details."""

    service_id: int | None = None
    centre_id: int
    type_of_service: str
    level: PreschoolLevel
    fee: float
    citizenship_type: CitizenshipType
    last_updated: datetime | None = None
    remarks: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ReviewSummaryResponse(BaseModel):
    """Aggregate review score summary for a preschool centre."""

    average_rating: float | None = None
    total_reviews: int = 0

    model_config = ConfigDict(from_attributes=True)


class PreschoolCentreDetailResponse(PreschoolCentreItem):
    """Preschool centre detailed profile with vacancies, services, and review summary."""

    vacancies: list[VacancyRecordResponse] = []
    services: list[PreschoolServiceResponse] = []
    review_summary: ReviewSummaryResponse


class CentreReviewItem(BaseModel):
    """Sanitized public review item for a preschool centre."""

    review_id: str
    rating: int
    comment: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CentreReviewListResponse(BaseModel):
    """Pagination envelope for preschool centre reviews."""

    items: list[CentreReviewItem]
    total: int
    limit: int
    offset: int




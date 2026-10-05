from datetime import datetime
from typing import Any, Optional
from pydantic import ValidationError, field_validator
from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint, create_engine

from kidcarematch_backend.core.config import settings


class PreschoolCentre(SQLModel, table=True):
    centre_id: Optional[int] = Field(default=None, primary_key=True)
    centre_code: str = Field(index=True, unique=True)
    centre_name: str = Field(index=True)
    contact: Optional[str] = None
    email: Optional[str] = None
    postal: Optional[str] = None
    website_url: Optional[str] = None
    food_offered: Optional[str] = None
    address: Optional[str] = None
    second_languages: Optional[str] = None
    spark_certified: bool = Field(default=False)
    weekday_full_day_hours: Optional[str] = None
    saturday_hours: Optional[str] = None
    extended_operating_hours: bool = Field(default=False)
    transport_provided: bool = Field(default=False)
    gov_subsidy: bool = Field(default=False)
    last_updated: Optional[datetime] = Field(default=None)

    # 1-to-many relationships
    vacancies: list["VacancyRecord"] = Relationship(back_populates="centre")
    services: list["PreschoolService"] = Relationship(back_populates="centre")
    reviews: list["Review"] = Relationship(back_populates="centre")
    watchlist_entries: list["WatchlistEntry"] = Relationship(back_populates="centre")


class VacancyRecord(SQLModel, table=True):
    record_id: Optional[str] = Field(default=None, primary_key=True)
    centre_id: int = Field(foreign_key="preschoolcentre.centre_id", nullable=False, index=True)
    level_type: str
    enrollment_month_year: datetime
    vacancy_status: str

    centre: Optional[PreschoolCentre] = Relationship(back_populates="vacancies")


class PreschoolService(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "centre_id",
            "type_of_service",
            "level",
            "citizenship_type",
            name="uq_preschool_service_offering",
        ),
    )

    service_id: Optional[int] = Field(default=None, primary_key=True)
    centre_id: int = Field(foreign_key="preschoolcentre.centre_id", nullable=False, index=True)
    type_of_service: str
    level: str
    fee: float
    citizenship_type: str
    last_updated: Optional[datetime] = Field(default=None)
    remarks: Optional[str] = None

    centre: Optional[PreschoolCentre] = Relationship(back_populates="services")


class User(SQLModel, table=True):
    user_id: Optional[str] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    full_name: str
    role: str = Field(default="PARENT", index=True)
    account_status: str = Field(default="ACTIVE")
    admin_level: Optional[int] = Field(default=None)

    # 1-to-many relationships
    preference_profiles: list["FamilyPreferenceProfile"] = Relationship(back_populates="parent")
    reviews: list["Review"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"foreign_keys": "[Review.user_id]"},
    )
    watchlist_entries: list["WatchlistEntry"] = Relationship(back_populates="user")


class FamilyPreferenceProfile(SQLModel, table=True):
    profile_id: Optional[str] = Field(default=None, primary_key=True)
    parent_id: str = Field(foreign_key="user.user_id", nullable=False, index=True)
    profile_name: str
    child_age_months: Optional[int] = Field(default=None)
    citizenship_status: str = Field(default="SC", index=True)
    service_required: str = Field(index=True)
    home_postal_code: Optional[str] = Field(default=None)
    workplace_postal_code: Optional[str] = Field(default=None)
    location_preference: Optional[str] = Field(default="HOME")
    household_income: Optional[float] = Field(default=None)
    budget_max: Optional[float] = Field(default=None)
    target_enrollment_date: Optional[datetime] = Field(default=None)
    cost_weight: Optional[float] = Field(default=0.5)
    travel_weight: Optional[float] = Field(default=0.5)
    created_at: Optional[datetime] = Field(default=None)

    parent: Optional[User] = Relationship(back_populates="preference_profiles")


class Review(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("centre_id", "user_id", name="uq_review_centre_user"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_1_to_5"),
    )

    review_id: Optional[str] = Field(default=None, primary_key=True)
    centre_id: int = Field(foreign_key="preschoolcentre.centre_id", nullable=False, index=True)
    user_id: str = Field(foreign_key="user.user_id", nullable=False, index=True)
    rating: int = Field(ge=1, le=5)
    review_description: Optional[str] = Field(default=None)
    moderation_status: str = Field(default="PENDING", index=True)
    moderated_at: Optional[datetime] = Field(default=None)
    moderated_by: Optional[str] = Field(default=None, foreign_key="user.user_id")
    created_at: Optional[datetime] = Field(default=None)

    centre: Optional[PreschoolCentre] = Relationship(back_populates="reviews")
    user: Optional[User] = Relationship(
        back_populates="reviews",
        sa_relationship_kwargs={"foreign_keys": "[Review.user_id]"},
    )

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int) -> int:
        if not (1 <= v <= 5):
            raise ValueError("Rating must be between 1 and 5 stars")
        return v

    def __init__(self, **data: Any):
        super().__init__(**data)
        if self.rating is not None and not (1 <= self.rating <= 5):
            raise ValueError(f"Rating must be between 1 and 5 stars, got {self.rating}")


class WatchlistEntry(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "centre_id",
            "level",
            "enrollment_month_year",
            name="uq_watchlist_entry",
        ),
    )

    watchlist_id: Optional[str] = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.user_id", nullable=False, index=True)
    centre_id: int = Field(foreign_key="preschoolcentre.centre_id", nullable=False, index=True)
    level: str = Field(index=True)
    enrollment_month_year: datetime = Field(index=True)
    created_at: Optional[datetime] = Field(default=None)

    centre: Optional[PreschoolCentre] = Relationship(back_populates="watchlist_entries")
    user: Optional[User] = Relationship(back_populates="watchlist_entries")


class SubsidyRule(SQLModel, table=True):
    rule_id: Optional[str] = Field(default=None, primary_key=True)
    subsidy_name: str = Field(index=True)
    tier_name: Optional[str] = Field(default=None)
    care_type: str = Field(index=True)
    metric: str = Field(index=True)
    applicant_status: Optional[str] = Field(default=None, index=True)
    citizenship: str = Field(default="SC", index=True)
    income_min: Optional[float] = Field(default=None)
    income_max: Optional[float] = Field(default=None)
    subsidy_amount: float
    effective_date: Optional[datetime] = Field(default=None)


class ExternalDataSourceStatus(SQLModel, table=True):
    source_id: str = Field(primary_key=True)
    source_name: str
    sync_state: str = Field(default="IDLE")
    last_successful_sync: Optional[datetime] = Field(default=None)
    last_attempted_sync: Optional[datetime] = Field(default=None)
    records_processed: int = Field(default=0)
    error_message: Optional[str] = Field(default=None)


database_url = settings.DATABASE_URL if settings.DATABASE_URL else "sqlite:///./kidcarematch.db"
engine = create_engine(database_url, echo=True)



def init_db(db_engine: Any = None) -> None:
    """Create database tables."""
    SQLModel.metadata.create_all(db_engine or engine)

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import field_validator
from sqlalchemy.orm import validates
from sqlmodel import CheckConstraint, Field, Relationship, SQLModel, UniqueConstraint, create_engine

from kidcarematch_backend.core.config import settings


class UserRole(str, Enum):
    PARENT = "PARENT"
    ADMIN = "ADMIN"


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class ModerationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SyncState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class CareType(str, Enum):
    INFANT_CARE = "INFANT_CARE"
    CHILD_CARE = "CHILD_CARE"


class SubsidyMetric(str, Enum):
    FIXED = "FIXED"
    HOUSEHOLD_INCOME = "HOUSEHOLD_INCOME"
    PER_CAPITA_INCOME = "PER_CAPITA_INCOME"


class ApplicantStatus(str, Enum):
    WORKING_APPLICANT = "WORKING_APPLICANT"
    NON_WORKING_APPLICANT = "NON_WORKING_APPLICANT"


class LocationPreference(str, Enum):
    HOME = "HOME"
    WORKPLACE = "WORKPLACE"
    EITHER = "EITHER"

class CitizenshipType(str, Enum):
    SC = "SC"
    SPR = "SPR"
    OTHERS = "OTHERS"

class PreschoolLevels(str, Enum):
    INFANT = "infant"
    PG = "pg"
    N1 = "n1"
    N2 = "n2"
    K1 = "k1"
    K2 = "k2"
    STUDENT_CARE = "student_care"




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

    # 1-to-many relationships with cascade deletion
    vacancies: list["VacancyRecord"] = Relationship(
        back_populates="centre",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    services: list["PreschoolService"] = Relationship(
        back_populates="centre",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    reviews: list["Review"] = Relationship(
        back_populates="centre",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    watchlist_entries: list["WatchlistEntry"] = Relationship(
        back_populates="centre",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class VacancyRecord(SQLModel, table=True):
    record_id: Optional[str] = Field(default=None, primary_key=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    level_type: PreschoolLevels
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
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
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
    role: UserRole = Field(default=UserRole.PARENT, index=True)
    account_status: AccountStatus = Field(default=AccountStatus.ACTIVE)
    admin_level: Optional[int] = Field(default=None)

    # 1-to-many relationships
    preference_profiles: list["FamilyPreferenceProfile"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    reviews: list["Review"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"foreign_keys": "[Review.user_id]", "cascade": "all, delete-orphan"},
    )
    watchlist_entries: list["WatchlistEntry"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class FamilyPreferenceProfile(SQLModel, table=True):
    profile_id: Optional[str] = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.user_id", ondelete="CASCADE", nullable=False, index=True)
    profile_name: str
    level: Optional[PreschoolLevels] = Field(default=None, index=True)
    citizenship_status: CitizenshipType = Field(default=CitizenshipType.SC, index=True)
    service_required: str = Field(index=True)
    home_postal_code: Optional[str] = Field(default=None)
    workplace_postal_code: Optional[str] = Field(default=None)
    location_preference: Optional[LocationPreference] = Field(default=LocationPreference.HOME)
    household_income: Optional[float] = Field(default=None)
    household_member_count: Optional[int] = Field(default=None)
    budget_max: Optional[float] = Field(default=None)
    enrollment_month_year: Optional[datetime] = Field(default=None)
    cost_weight: Optional[float] = Field(default=0.5)
    travel_weight: Optional[float] = Field(default=0.5)

    user: Optional[User] = Relationship(back_populates="preference_profiles")


class Review(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("centre_id", "user_id", name="uq_review_centre_user"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_1_to_5"),
        CheckConstraint(f"moderation_status IN {tuple(status.value for status in ModerationStatus)}", name="ck_review_moderation_status"),
    )

    review_id: Optional[str] = Field(default=None, primary_key=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    user_id: str = Field(foreign_key="user.user_id", nullable=False, ondelete="CASCADE", index=True)
    rating: int = Field(ge=1, le=5)
    review_description: Optional[str] = Field(default=None)
    moderation_status: ModerationStatus = Field(default=ModerationStatus.PENDING, index=True)
    moderated_at: Optional[datetime] = Field(default=None)
    moderated_by: Optional[str] = Field(default=None, foreign_key="user.user_id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

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

    @validates("rating")
    def _validate_rating_sa(self, key: str, value: int) -> int:
        return self.validate_rating(value)


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
    user_id: str = Field(foreign_key="user.user_id", ondelete="CASCADE", nullable=False, index=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    level: PreschoolLevels = Field(index=True)
    enrollment_month_year: datetime = Field(index=True)

    centre: Optional[PreschoolCentre] = Relationship(back_populates="watchlist_entries")
    user: Optional[User] = Relationship(back_populates="watchlist_entries")


class SubsidyRule(SQLModel, table=True):
    rule_id: Optional[str] = Field(default=None, primary_key=True)
    subsidy_name: str = Field(index=True)
    tier_name: Optional[str] = Field(default=None)
    care_type: CareType = Field(index=True)
    metric: SubsidyMetric = Field(index=True)
    applicant_status: Optional[ApplicantStatus] = Field(default=None, index=True)
    citizenship: CitizenshipType = Field(default=CitizenshipType.SC, index=True)
    income_min: Optional[float] = Field(default=None)
    income_max: Optional[float] = Field(default=None)
    subsidy_amount: float
    effective_date: Optional[datetime] = Field(default=None)


class ExternalDataSourceStatus(SQLModel, table=True):
    source_id: str = Field(primary_key=True)
    source_name: str
    sync_state: SyncState = Field(default=SyncState.IDLE)
    last_successful_sync: Optional[datetime] = Field(default=None)
    last_attempted_sync: Optional[datetime] = Field(default=None)
    records_processed: int = Field(default=0)
    error_message: Optional[str] = Field(default=None)


database_url = settings.DATABASE_URL
engine = create_engine(database_url, echo=True)


def init_db(db_engine: Any = None) -> None:
    """Create database tables."""
    SQLModel.metadata.create_all(db_engine or engine)

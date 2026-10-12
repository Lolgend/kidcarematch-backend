import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import field_validator
from sqlalchemy.orm import validates
from sqlmodel import (
    CheckConstraint,
    Field,
    Relationship,
    Session,
    SQLModel,
    UniqueConstraint,
    create_engine,
)

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

class PreschoolLevel(str, Enum):
    INFANT = "infant"
    PG = "pg"
    N1 = "n1"
    N2 = "n2"
    K1 = "k1"
    K2 = "k2"
    STUDENT_CARE = "student_care"

class PreschoolCentre(SQLModel, table=True):
    centre_id: int | None = Field(default=None, primary_key=True)
    centre_code: str = Field(index=True, unique=True)
    centre_name: str = Field(index=True)
    contact: str | None = None
    email: str | None = None
    postal: str | None = None
    website_url: str | None = None
    food_offered: str | None = None
    address: str | None = None
    second_languages: str | None = None
    spark_certified: bool = Field(default=False)
    weekday_full_day_hours: str | None = None
    saturday_hours: str | None = None
    extended_operating_hours: bool = Field(default=False)
    transport_provided: bool = Field(default=False)
    gov_subsidy: bool = Field(default=False)
    last_updated: datetime | None = Field(default=None)

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
    record_id: str | None = Field(default=None, primary_key=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    level_type: PreschoolLevel
    enrollment_month_year: datetime
    vacancy_status: str

    centre: PreschoolCentre | None = Relationship(back_populates="vacancies")


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

    service_id: int | None = Field(default=None, primary_key=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    type_of_service: str
    level: PreschoolLevel = Field(index=True)
    fee: float
    citizenship_type: CitizenshipType = Field(default=CitizenshipType.OTHERS, index=True)
    last_updated: datetime | None = Field(default=None)
    remarks: str | None = Field(default=None, max_length=1000)

    centre: PreschoolCentre | None = Relationship(back_populates="services")


class User(SQLModel, table=True):
    user_id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    firebase_uid: str = Field(unique=True, index=True, nullable=False)
    email: str = Field(index=True, unique=True, nullable=False)
    full_name: str
    role: UserRole = Field(default=UserRole.PARENT, index=True)
    account_status: AccountStatus = Field(default=AccountStatus.ACTIVE)
    admin_level: int | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

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
    profile_id: str | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.user_id", ondelete="CASCADE", nullable=False, index=True)
    profile_name: str
    level: PreschoolLevel = Field(index=True)
    citizenship_status: CitizenshipType = Field(default=CitizenshipType.SC, index=True)
    service_required: str = Field(index=True)
    home_postal_code: str = Field()
    workplace_postal_code: str | None = Field(default=None)
    location_preference: LocationPreference = Field(default=LocationPreference.HOME)
    household_income: float = Field()
    household_member_count: int = Field()
    budget_max: float = Field()
    enrollment_month_year: datetime = Field()
    cost_weight: float = Field(default=0.5)
    travel_weight: float = Field(default=0.5)

    user: User | None = Relationship(back_populates="preference_profiles")


class Review(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("centre_id", "user_id", name="uq_review_centre_user"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_review_rating_1_to_5"),
        CheckConstraint(f"moderation_status IN {tuple(status.value for status in ModerationStatus)}", name="ck_review_moderation_status"),
    )

    review_id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    user_id: str = Field(foreign_key="user.user_id", nullable=False, ondelete="CASCADE", index=True)
    rating: int = Field(ge=1, le=5)
    review_description: str | None = Field(default=None)
    moderation_status: ModerationStatus = Field(default=ModerationStatus.PENDING, index=True)
    moderated_at: datetime | None = Field(default=None)
    moderated_by: str | None = Field(default=None, foreign_key="user.user_id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    centre: PreschoolCentre | None = Relationship(back_populates="reviews")
    user: User | None = Relationship(
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

    watchlist_id: str | None = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
    )
    user_id: str = Field(foreign_key="user.user_id", ondelete="CASCADE", nullable=False, index=True)
    centre_id: int = Field(
        foreign_key="preschoolcentre.centre_id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    level: PreschoolLevel = Field(index=True)
    enrollment_month_year: datetime = Field(index=True)

    centre: PreschoolCentre | None = Relationship(back_populates="watchlist_entries")
    user: User | None = Relationship(back_populates="watchlist_entries")


class SubsidyRule(SQLModel, table=True):
    rule_id: str | None = Field(default=None, primary_key=True)
    subsidy_name: str = Field(index=True)
    tier_name: str | None = Field(default=None)
    care_type: CareType = Field(index=True)
    metric: SubsidyMetric = Field(index=True)
    applicant_status: ApplicantStatus | None = Field(default=None, index=True)
    citizenship: CitizenshipType = Field(default=CitizenshipType.SC, index=True)
    income_min: float | None = Field(default=None)
    income_max: float | None = Field(default=None)
    subsidy_amount: float
    effective_date: datetime | None = Field(default=None)


class ExternalDataSourceStatus(SQLModel, table=True):
    source_id: str = Field(primary_key=True)
    source_name: str
    sync_state: SyncState = Field(default=SyncState.IDLE)
    last_successful_sync: datetime | None = Field(default=None)
    last_attempted_sync: datetime | None = Field(default=None)
    records_processed: int = Field(default=0)
    error_message: str | None = Field(default=None, max_length=1000)


database_url = settings.DATABASE_URL
engine = create_engine(database_url, echo=True)


def init_db(db_engine: Any = None) -> None:
    """Create database tables."""
    SQLModel.metadata.create_all(db_engine or engine)


def get_session() -> Generator[Session, None, None]:
    """Yield a database session."""
    with Session(engine) as session:
        yield session

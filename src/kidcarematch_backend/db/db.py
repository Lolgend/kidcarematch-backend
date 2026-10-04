from datetime import datetime
from typing import Any, Optional
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint, create_engine

from kidcarematch_backend.core.config import settings


class PreschoolCentre(SQLModel, table=True):
    centre_id: Optional[int] = Field(default=None, primary_key=True)
    centre_code: str = Field(index=True, unique=True)
    tp_code: Optional[str] = Field(default=None, index=True)
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

    # 1-to-many mandatory relationships
    vacancies: list["VacancyRecord"] = Relationship(back_populates="centre")
    services: list["PreschoolService"] = Relationship(back_populates="centre")


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


engine = create_engine(settings.DATABASE_URL, echo=True)


def init_db(db_engine: Any = None) -> None:
    """Create database tables."""
    SQLModel.metadata.create_all(db_engine or engine)

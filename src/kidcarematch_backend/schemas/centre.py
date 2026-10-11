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

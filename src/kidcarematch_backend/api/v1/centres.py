"""Preschool centre API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import func
from sqlmodel import Session, col, select

from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    PreschoolCentre,
    VacancyRecord,
    get_session,
)
from kidcarematch_backend.schemas.centre import CentreSummaryResponse

router = APIRouter(prefix="/api/v1/centres", tags=["centres"])


@router.get(
    "/summary",
    response_model=CentreSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Dashboard Centre Summary",
)
def get_centre_summary(
    session: Annotated[Session, Depends(get_session)],
) -> CentreSummaryResponse:
    """Retrieve aggregate summary metrics of preschool centres for the main dashboard."""
    total_centres = (
        session.exec(select(func.count(col(PreschoolCentre.centre_id)))).one() or 0
    )
    spark_certified_count = (
        session.exec(
            select(func.count(col(PreschoolCentre.centre_id))).where(
                col(PreschoolCentre.spark_certified).is_(True)
            )
        ).one()
        or 0
    )
    gov_subsidy_count = (
        session.exec(
            select(func.count(col(PreschoolCentre.centre_id))).where(
                col(PreschoolCentre.gov_subsidy).is_(True)
            )
        ).one()
        or 0
    )
    centres_with_vacancies_count = (
        session.exec(
            select(func.count(func.distinct(col(VacancyRecord.centre_id)))).where(
                func.lower(col(VacancyRecord.vacancy_status)).in_(
                    ["immediate", "available"]
                )
            )
        ).one()
        or 0
    )
    last_sync_time = session.exec(
        select(func.max(col(ExternalDataSourceStatus.last_successful_sync)))
    ).one()

    return CentreSummaryResponse(
        total_centres=total_centres,
        spark_certified_count=spark_certified_count,
        gov_subsidy_count=gov_subsidy_count,
        centres_with_vacancies_count=centres_with_vacancies_count,
        last_sync_time=last_sync_time,
    )

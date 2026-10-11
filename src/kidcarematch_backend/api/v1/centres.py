"""Preschool centre API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    PreschoolCentre,
    PreschoolLevel,
    VacancyRecord,
    get_session,
)
from kidcarematch_backend.schemas.centre import (
    CentreSummaryResponse,
    PreschoolCentreItem,
    PreschoolCentreListResponse,
)

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


@router.get(
    "",
    response_model=PreschoolCentreListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Preschool Centres",
)
def list_centres(
    session: Annotated[Session, Depends(get_session)],
    query: Annotated[
        str | None,
        Query(description="Case-insensitive search across centre name and address"),
    ] = None,
    postal_prefix: Annotated[
        str | None,
        Query(description="Prefix match on Singapore postal code"),
    ] = None,
    spark_certified: Annotated[
        bool | None,
        Query(description="Filter by SPARK certification"),
    ] = None,
    gov_subsidy: Annotated[
        bool | None,
        Query(description="Filter by government subsidy eligibility"),
    ] = None,
    level: Annotated[
        PreschoolLevel | None,
        Query(description="Filter centres with open vacancies for preschool cohort level"),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of centres to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
) -> PreschoolCentreListResponse:
    """Retrieve a paginated list of preschool centres supporting multi-criteria filtering."""
    conditions = []

    if query is not None:
        cleaned_query = query.strip()
        if cleaned_query:
            pattern = f"%{cleaned_query.lower()}%"
            conditions.append(
                or_(
                    func.lower(col(PreschoolCentre.centre_name)).like(pattern),
                    func.lower(
                        func.coalesce(col(PreschoolCentre.address), "")
                    ).like(pattern),
                )
            )

    if postal_prefix is not None:
        cleaned_postal = postal_prefix.strip()
        if cleaned_postal:
            conditions.append(
                col(PreschoolCentre.postal).startswith(cleaned_postal)
            )

    if spark_certified is not None:
        conditions.append(col(PreschoolCentre.spark_certified) == spark_certified)

    if gov_subsidy is not None:
        conditions.append(col(PreschoolCentre.gov_subsidy) == gov_subsidy)

    if level is not None:
        open_vacancies_subquery = (
            select(col(VacancyRecord.centre_id))
            .where(
                col(VacancyRecord.level_type) == level,
                func.lower(col(VacancyRecord.vacancy_status)).in_(
                    ["immediate", "available"]
                ),
            )
            .distinct()
        )
        conditions.append(col(PreschoolCentre.centre_id).in_(open_vacancies_subquery))

    count_query = select(func.count(col(PreschoolCentre.centre_id)))
    if conditions:
        count_query = count_query.where(*conditions)
    total = session.exec(count_query).one() or 0

    items_query = select(PreschoolCentre)
    if conditions:
        items_query = items_query.where(*conditions)
    items_query = (
        items_query.order_by(col(PreschoolCentre.centre_id).asc())
        .offset(offset)
        .limit(limit)
    )
    centres = session.exec(items_query).all()

    items = [PreschoolCentreItem.model_validate(c) for c in centres]

    return PreschoolCentreListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


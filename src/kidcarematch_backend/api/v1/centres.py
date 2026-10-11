"""Preschool centre API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    ModerationStatus,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    Review,
    VacancyRecord,
    get_session,
)
from kidcarematch_backend.schemas.centre import (
    CentreSummaryResponse,
    PreschoolCentreDetailResponse,
    PreschoolCentreItem,
    PreschoolCentreListResponse,
    PreschoolServiceResponse,
    ReviewSummaryResponse,
    VacancyRecordResponse,
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


def _build_centre_detail_response(
    centre: PreschoolCentre,
    session: Session,
) -> PreschoolCentreDetailResponse:
    """Construct a detailed centre profile response including ordered vacancies, services, and review aggregates."""
    vacancies = session.exec(
        select(VacancyRecord)
        .where(col(VacancyRecord.centre_id) == centre.centre_id)
        .order_by(
            col(VacancyRecord.enrollment_month_year).asc(),
            col(VacancyRecord.level_type).asc(),
        )
    ).all()

    services = session.exec(
        select(PreschoolService)
        .where(col(PreschoolService.centre_id) == centre.centre_id)
        .order_by(
            col(PreschoolService.level).asc(),
            col(PreschoolService.citizenship_type).asc(),
        )
    ).all()

    review_stats = session.exec(
        select(
            func.count(col(Review.review_id)),
            func.avg(col(Review.rating)),
        ).where(
            col(Review.centre_id) == centre.centre_id,
            col(Review.moderation_status) == ModerationStatus.APPROVED,
        )
    ).one()

    total_reviews = review_stats[0] or 0
    raw_avg = review_stats[1]
    average_rating = (
        round(float(raw_avg), 2)
        if (raw_avg is not None and total_reviews > 0)
        else None
    )

    return PreschoolCentreDetailResponse(
        centre_id=centre.centre_id,  # type: ignore[arg-type]
        centre_code=centre.centre_code,
        centre_name=centre.centre_name,
        contact=centre.contact,
        email=centre.email,
        postal=centre.postal,
        website_url=centre.website_url,
        food_offered=centre.food_offered,
        address=centre.address,
        second_languages=centre.second_languages,
        spark_certified=centre.spark_certified,
        weekday_full_day_hours=centre.weekday_full_day_hours,
        saturday_hours=centre.saturday_hours,
        extended_operating_hours=centre.extended_operating_hours,
        transport_provided=centre.transport_provided,
        gov_subsidy=centre.gov_subsidy,
        last_updated=centre.last_updated,
        vacancies=[VacancyRecordResponse.model_validate(v) for v in vacancies],
        services=[PreschoolServiceResponse.model_validate(s) for s in services],
        review_summary=ReviewSummaryResponse(
            average_rating=average_rating,
            total_reviews=total_reviews,
        ),
    )


@router.get(
    "/code/{centre_code}",
    response_model=PreschoolCentreDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Preschool Centre Profile by Code",
)
def get_centre_by_code(
    centre_code: str,
    session: Annotated[Session, Depends(get_session)],
) -> PreschoolCentreDetailResponse:
    """Retrieve detailed profile of a preschool centre by its natural ECDA code."""
    centre = session.exec(
        select(PreschoolCentre).where(col(PreschoolCentre.centre_code) == centre_code)
    ).first()
    if not centre:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preschool centre not found",
        )
    return _build_centre_detail_response(centre, session)


@router.get(
    "/{centre_id}",
    response_model=PreschoolCentreDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Preschool Centre Profile by ID",
)
def get_centre_by_id(
    centre_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> PreschoolCentreDetailResponse:
    """Retrieve detailed profile of a preschool centre by its internal integer ID."""
    centre = session.exec(
        select(PreschoolCentre).where(col(PreschoolCentre.centre_id) == centre_id)
    ).first()
    if not centre:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preschool centre not found",
        )
    return _build_centre_detail_response(centre, session)



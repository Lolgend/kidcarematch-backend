"""Preschool centre API endpoints."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import false, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import (
    get_current_user,
    get_optional_current_user,
)
from kidcarematch_backend.core.postal_districts import (
    get_all_postal_districts,
    get_postal_sectors_for_districts,
)
from kidcarematch_backend.db.db import (
    ApplicantStatus,
    CareType,
    CitizenshipType,
    ExternalDataSourceStatus,
    FamilyPreferenceProfile,
    ModerationStatus,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    Review,
    SubsidyMetric,
    SubsidyRule,
    User,
    VacancyRecord,
    WatchlistEntry,
    get_session,
)
from kidcarematch_backend.schemas.centre import (
    CentreReviewItem,
    CentreReviewListResponse,
    CentreSummaryResponse,
    PostalDistrictItem,
    PreschoolCentreDetailResponse,
    PreschoolCentreItem,
    PreschoolCentreListResponse,
    PreschoolServiceResponse,
    ReviewSummaryResponse,
    VacancyRecordResponse,
)
from kidcarematch_backend.schemas.match import (
    CentreMatchRequest,
    CentreMatchResponse,
    FeeBreakdownItem,
    MatchCandidateItem,
)
from kidcarematch_backend.schemas.profile import (
    parse_year_month,
)
from kidcarematch_backend.schemas.review import (
    ReviewCreateRequest,
    ReviewResponse,
)
from kidcarematch_backend.schemas.watchlist import (
    CentreWatchlistEntryItem,
    CentreWatchlistStatusResponse,
)

router = APIRouter(prefix="/api/v1/centres", tags=["centres"])

OPEN_VACANCY_STATUSES = ("immediate", "available")

DEVELOPMENTAL_LEVEL_ORDER: dict[str, int] = {
    PreschoolLevel.INFANT.value: 0,
    PreschoolLevel.PG.value: 1,
    PreschoolLevel.N1.value: 2,
    PreschoolLevel.N2.value: 3,
    PreschoolLevel.K1.value: 4,
    PreschoolLevel.K2.value: 5,
    PreschoolLevel.STUDENT_CARE.value: 6,
}

CITIZENSHIP_PRIORITY_ORDER: dict[str, int] = {
    CitizenshipType.SC.value: 0,
    CitizenshipType.SPR.value: 1,
    CitizenshipType.OTHERS.value: 2,
}


def _level_order_key(level: PreschoolLevel | str) -> int:
    val = level.value if isinstance(level, PreschoolLevel) else str(level)
    return DEVELOPMENTAL_LEVEL_ORDER.get(val, 99)


def _citizenship_order_key(citizenship: CitizenshipType | str) -> int:
    val = (
        citizenship.value
        if isinstance(citizenship, CitizenshipType)
        else str(citizenship)
    )
    return CITIZENSHIP_PRIORITY_ORDER.get(val, 99)


def score_vacancy_fit(status: str | None) -> float:
    """Score Vacancy Fit based on ECDA vacancy status for the target enrollment month.

    - Available / Immediate: 100.0%
    - Limited: 50.0%
    - Full / Not Applicable / unrecorded / other: 0.0%
    """
    if not status:
        return 0.0
    normalized = status.strip().lower()
    if normalized in ("available", "immediate"):
        return 100.0
    if normalized == "limited":
        return 50.0
    return 0.0


def map_level_to_care_type(level: PreschoolLevel) -> CareType:
    """Map developmental preschool cohort level to ECDA subsidy CareType category."""
    if level == PreschoolLevel.INFANT:
        return CareType.INFANT_CARE
    return CareType.CHILD_CARE


def resolve_published_programme_fee(
    services: list[PreschoolService],
    requested_level: PreschoolLevel,
    service_required: str,
    citizenship_status: CitizenshipType,
) -> tuple[float, str | None]:
    """Resolve centre published programme fee matching requested level, service type, and citizenship.

    Priority and fallback logic:
    - Service Type: exact match (case-insensitive) -> fallback to "Full Day" -> fallback to any available service type.
    - Citizenship: exact match -> fallback to OTHERS -> fallback to any available citizenship tier.
    Returns (published_fee, remarks).
    """
    level_services = [s for s in services if s.level == requested_level]
    if not level_services:
        return 0.0, None

    req_clean = service_required.strip().lower()
    type_matches = [
        s for s in level_services if s.type_of_service.strip().lower() == req_clean
    ]
    if not type_matches and req_clean != "full day":
        type_matches = [
            s
            for s in level_services
            if s.type_of_service.strip().lower() == "full day"
        ]
    if not type_matches:
        type_matches = level_services

    cit_match = next(
        (s for s in type_matches if s.citizenship_type == citizenship_status),
        None,
    )
    if cit_match is None and citizenship_status != CitizenshipType.OTHERS:
        cit_match = next(
            (
                s
                for s in type_matches
                if s.citizenship_type == CitizenshipType.OTHERS
            ),
            None,
        )
    if cit_match is None:
        cit_match = type_matches[0]

    return cit_match.fee, cit_match.remarks


def evaluate_government_subsidy(
    session: Session,
    care_type: CareType,
    citizenship: CitizenshipType,
    applicant_status: ApplicantStatus,
    household_income: float,
    household_member_count: int,
) -> float:
    """Evaluate eligible government early childhood subsidies according to ECDA means-tested policy.

    - Fixed subsidies (e.g. Basic Subsidy) are summed universally.
    - Means-tested tiered subsidies (e.g. Additional Subsidy) evaluate both Gross Household Income (HHI)
      and Per-Capita Income (PCI = HHI / household_member_count), taking the maximum eligible benefit.
    """
    statement = (
        select(SubsidyRule)
        .where(
            col(SubsidyRule.care_type) == care_type,
            col(SubsidyRule.citizenship) == citizenship,
            or_(
                col(SubsidyRule.applicant_status) == applicant_status,
                col(SubsidyRule.applicant_status).is_(None),
            ),
        )
    )
    rules = session.exec(statement).all()
    if not rules:
        return 0.0

    member_count = max(1, household_member_count)
    pci = household_income / member_count

    fixed_subsidy = 0.0
    tiered_rules_by_name: dict[str, list[SubsidyRule]] = defaultdict(list)

    for r in rules:
        if r.metric == SubsidyMetric.FIXED:
            fixed_subsidy += r.subsidy_amount
        elif r.metric in (
            SubsidyMetric.HOUSEHOLD_INCOME,
            SubsidyMetric.PER_CAPITA_INCOME,
        ):
            tiered_rules_by_name[r.subsidy_name].append(r)

    total_tiered_subsidy = 0.0
    for sub_rules in tiered_rules_by_name.values():
        hhi_benefit = 0.0
        pci_benefit = 0.0
        for r in sub_rules:
            if (
                r.metric == SubsidyMetric.HOUSEHOLD_INCOME
                and (r.income_min is None or household_income >= r.income_min)
                and (r.income_max is None or household_income <= r.income_max)
            ):
                hhi_benefit = max(hhi_benefit, r.subsidy_amount)
            elif (
                r.metric == SubsidyMetric.PER_CAPITA_INCOME
                and (r.income_min is None or pci >= r.income_min)
                and (r.income_max is None or pci <= r.income_max)
            ):
                pci_benefit = max(pci_benefit, r.subsidy_amount)
        total_tiered_subsidy += max(hhi_benefit, pci_benefit)

    return round(fixed_subsidy + total_tiered_subsidy, 2)


def calculate_cost_fit(
    estimated_payable_fee: float,
    budget_max: float | None,
) -> float | None:
    """Calculate Cost Fit percentage (0% - 100%) comparing net payable fee against budget ceiling.

    - 100% if payable fee <= budget_max (or <= 0.0)
    - min(100.0, (budget_max / payable_fee) * 100.0) otherwise
    - None if budget_max is None
    """
    if budget_max is None:
        return None
    if estimated_payable_fee <= budget_max or estimated_payable_fee <= 0.0:
        return 100.0
    return round(min(100.0, (budget_max / estimated_payable_fee) * 100.0), 2)


def get_centre_or_404(centre_id: int, session: Session) -> PreschoolCentre:
    """Retrieve a preschool centre by ID or raise HTTP 404."""
    centre = session.exec(
        select(PreschoolCentre).where(col(PreschoolCentre.centre_id) == centre_id)
    ).first()
    if not centre:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preschool centre not found",
        )
    return centre


@router.get(
    "/districts",
    response_model=list[PostalDistrictItem],
    status_code=status.HTTP_200_OK,
    summary="List Singapore Postal Districts",
)
def list_postal_districts() -> list[PostalDistrictItem]:
    """Retrieve all 28 canonical Singapore Postal Districts and their postal sectors."""
    return [PostalDistrictItem.model_validate(d) for d in get_all_postal_districts()]


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
                func.lower(col(VacancyRecord.vacancy_status)).in_(OPEN_VACANCY_STATUSES)
            )
        ).one()
        or 0
    )
    ecda_status = session.exec(
        select(ExternalDataSourceStatus).where(
            col(ExternalDataSourceStatus.source_id) == "ECDA_DATASTORE"
        )
    ).first()
    last_sync_time: datetime | None
    if ecda_status is not None and ecda_status.last_successful_sync is not None:
        last_sync_time = ecda_status.last_successful_sync
    else:
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
    district: Annotated[
        list[Annotated[int, Field(ge=1, le=28)]] | None,
        Query(description="Filter centres by Singapore postal district (1 to 28)"),
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
        Query(
            description="Filter centres with open vacancies for preschool cohort level"
        ),
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
                    func.lower(func.coalesce(col(PreschoolCentre.address), "")).like(
                        pattern
                    ),
                )
            )

    if district:
        sectors = get_postal_sectors_for_districts(district)
        if sectors:
            conditions.append(col(PreschoolCentre.postal).is_not(None))
            conditions.append(func.length(col(PreschoolCentre.postal)) >= 2)
            conditions.append(
                func.substr(col(PreschoolCentre.postal), 1, 2).in_(sectors)
            )
        else:
            conditions.append(false())

    if postal_prefix is not None:
        cleaned_postal = postal_prefix.strip()
        if cleaned_postal:
            conditions.append(col(PreschoolCentre.postal).startswith(cleaned_postal))

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
                    OPEN_VACANCY_STATUSES
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
        select(VacancyRecord).where(col(VacancyRecord.centre_id) == centre.centre_id)
    ).all()
    sorted_vacancies = sorted(
        vacancies,
        key=lambda v: (
            v.enrollment_month_year,
            _level_order_key(v.level_type),
        ),
    )

    services = session.exec(
        select(PreschoolService).where(
            col(PreschoolService.centre_id) == centre.centre_id
        )
    ).all()
    sorted_services = sorted(
        services,
        key=lambda s: (
            _level_order_key(s.level),
            _citizenship_order_key(s.citizenship_type),
        ),
    )

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

    centre_item = PreschoolCentreItem.model_validate(centre)

    return PreschoolCentreDetailResponse(
        **centre_item.model_dump(),
        vacancies=[VacancyRecordResponse.model_validate(v) for v in sorted_vacancies],
        services=[PreschoolServiceResponse.model_validate(s) for s in sorted_services],
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
    "/{centre_id}/reviews",
    response_model=CentreReviewListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Preschool Centre Reviews",
)
def list_centre_reviews(
    centre_id: int,
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of reviews to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
) -> CentreReviewListResponse:
    """Retrieve a paginated list of approved community reviews for a preschool centre."""
    get_centre_or_404(centre_id, session)

    base_filter = [
        col(Review.centre_id) == centre_id,
        col(Review.moderation_status) == ModerationStatus.APPROVED,
    ]

    total = (
        session.exec(
            select(func.count(col(Review.review_id))).where(*base_filter)
        ).one()
        or 0
    )

    reviews = session.exec(
        select(Review)
        .where(*base_filter)
        .order_by(col(Review.created_at).desc(), col(Review.review_id).desc())
        .offset(offset)
        .limit(limit)
    ).all()

    items = [
        CentreReviewItem(
            review_id=r.review_id or "",
            rating=r.rating,
            comment=r.review_description,
            created_at=r.created_at,
        )
        for r in reviews
    ]

    return CentreReviewListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{centre_id}/reviews/mine",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Parent's Review for Preschool Centre",
)
def get_my_centre_review(
    centre_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> Review:
    """Retrieve the authenticated parent's authored review for a specific preschool centre.

    Returns 404 if the centre does not exist or if the parent has not reviewed this centre.
    """
    get_centre_or_404(centre_id, session)

    review = session.exec(
        select(Review).where(
            col(Review.centre_id) == centre_id,
            col(Review.user_id) == current_user.user_id,
        )
    ).first()

    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    return review


@router.post(
    "/{centre_id}/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit Preschool Centre Review",
)
def create_centre_review(
    centre_id: int,
    payload: ReviewCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> Review:
    """Submit a 1-to-5 star rating and optional commentary for a preschool centre.

    Requires an authenticated active user session.
    Defaults newly submitted reviews to APPROVED moderation status (reactive moderation).
    Enforces composite uniqueness per user and preschool centre (uq_review_centre_user).
    """
    get_centre_or_404(centre_id, session)

    # Check composite uniqueness: one review per centre per user
    existing_review = session.exec(
        select(Review).where(
            col(Review.centre_id) == centre_id,
            col(Review.user_id) == current_user.user_id,
        )
    ).first()
    if existing_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already submitted a review for this preschool centre",
        )

    assert current_user.user_id is not None

    review = Review(
        centre_id=centre_id,
        user_id=current_user.user_id,
        rating=payload.rating,
        review_description=payload.review_description,
        moderation_status=ModerationStatus.APPROVED,
    )

    try:
        session.add(review)
        session.commit()
        session.refresh(review)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already submitted a review for this preschool centre",
        ) from exc

    return review


@router.get(
    "/{centre_id}/watchlist",
    response_model=CentreWatchlistStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Introspect Centre Watch Status",
)
def get_centre_watchlist_status(
    centre_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> CentreWatchlistStatusResponse:
    """Introspect whether the authenticated parent is currently watching this centre.

    Returns the parent's active watchlist subscriptions for the centre, or an empty list if not watched.
    """
    get_centre_or_404(centre_id, session)
    assert current_user.user_id is not None

    entries = session.exec(
        select(WatchlistEntry)
        .where(
            col(WatchlistEntry.centre_id) == centre_id,
            col(WatchlistEntry.user_id) == current_user.user_id,
        )
        .order_by(
            col(WatchlistEntry.enrollment_month_year).asc(),
            col(WatchlistEntry.watchlist_id).asc(),
        )
    ).all()

    is_watched = len(entries) > 0
    watched_entries = [
        CentreWatchlistEntryItem(
            watchlist_id=e.watchlist_id or "",
            centre_id=e.centre_id,
            level=e.level,
            enrollment_month_year=e.enrollment_month_year,
        )
        for e in entries
    ]

    return CentreWatchlistStatusResponse(
        centre_id=centre_id,
        is_watched=is_watched,
        watched_entries=watched_entries,
    )


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
    centre = get_centre_or_404(centre_id, session)
    return _build_centre_detail_response(centre, session)


@router.post(
    "/match",
    response_model=CentreMatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Multi-Criteria Preschool Centre Matching",
)
def match_centres(
    request: CentreMatchRequest,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User | None, Depends(get_optional_current_user)] = None,
) -> CentreMatchResponse:
    """Evaluate and rank candidate preschool centres against multi-criteria requirements."""
    # 1. Resolve search criteria from profile or inline payload
    if request.profile_id is not None:
        if current_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required when profile_id is provided",
                headers={"WWW-Authenticate": "Bearer"},
            )
        profile = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.profile_id == request.profile_id,
                FamilyPreferenceProfile.user_id == current_user.user_id,
            )
        ).first()
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Family preference profile not found",
            )
        requested_level = profile.level
        enrollment_month_str = profile.enrollment_month_year.strftime("%Y-%m")
        target_dt = datetime(
            profile.enrollment_month_year.year,
            profile.enrollment_month_year.month,
            1,
            0,
            0,
            0,
            tzinfo=timezone.utc,
        )
        citizenship_status = profile.citizenship_status
        service_required = profile.service_required
        household_income = profile.household_income
        household_member_count = profile.household_member_count
        budget_max: float | None = profile.budget_max
        applicant_status = profile.applicant_status
        vacancy_weight = profile.vacancy_weight
        cost_weight = profile.cost_weight
        travel_weight = profile.travel_weight
    else:
        assert request.level is not None
        assert request.enrollment_month_year is not None
        requested_level = request.level
        enrollment_month_str = request.enrollment_month_year
        target_dt = parse_year_month(enrollment_month_str)
        citizenship_status = request.citizenship_status
        service_required = request.service_required
        household_income = request.household_income
        household_member_count = request.household_member_count
        budget_max = request.budget_max
        applicant_status = request.applicant_status
        vacancy_weight = request.vacancy_weight
        cost_weight = request.cost_weight
        travel_weight = request.travel_weight

    _ = travel_weight  # Travel Fit integration scheduled for Ticket #70

    # 2. Strict cohort filtering: candidates must offer requested level
    candidate_centres = session.exec(
        select(PreschoolCentre)
        .where(
            col(PreschoolCentre.centre_id).in_(
                select(PreschoolService.centre_id).where(
                    col(PreschoolService.level) == requested_level
                )
            )
        )
        .order_by(col(PreschoolCentre.centre_name).asc())
    ).all()

    total_candidates = len(candidate_centres)
    if total_candidates == 0:
        return CentreMatchResponse(
            total_candidates=0,
            returned_count=0,
            level=requested_level,
            enrollment_month_year=enrollment_month_str,
            results=[],
            explanation=None,
            profile_id=request.profile_id,
        )

    # 3. Prefetch vacancy and service records for all candidate centres
    candidate_ids = [c.centre_id for c in candidate_centres if c.centre_id is not None]
    vacancies = session.exec(
        select(VacancyRecord).where(
            col(VacancyRecord.centre_id).in_(candidate_ids),
            col(VacancyRecord.level_type) == requested_level,
        )
    ).all()

    vacancy_map: dict[int, VacancyRecord] = {}
    for v in vacancies:
        if (
            v.centre_id is not None
            and v.enrollment_month_year is not None
            and v.enrollment_month_year.year == target_dt.year
            and v.enrollment_month_year.month == target_dt.month
        ):
            vacancy_map[v.centre_id] = v

    services = session.exec(
        select(PreschoolService).where(
            col(PreschoolService.centre_id).in_(candidate_ids),
            col(PreschoolService.level) == requested_level,
        )
    ).all()

    services_map: dict[int, list[PreschoolService]] = defaultdict(list)
    for s in services:
        if s.centre_id is not None:
            services_map[s.centre_id].append(s)

    care_type = map_level_to_care_type(requested_level)
    estimated_subsidy = evaluate_government_subsidy(
        session=session,
        care_type=care_type,
        citizenship=citizenship_status,
        applicant_status=applicant_status,
        household_income=household_income,
        household_member_count=household_member_count,
    )

    # 4. Score Vacancy Fit and Cost Fit for each candidate centre
    scored_candidates: list[MatchCandidateItem] = []
    for centre in candidate_centres:
        v_rec = (
            vacancy_map.get(centre.centre_id) if centre.centre_id is not None else None
        )
        v_status = v_rec.vacancy_status if v_rec else None
        v_score = score_vacancy_fit(v_status)

        centre_services = (
            services_map.get(centre.centre_id, [])
            if centre.centre_id is not None
            else []
        )
        published_fee, remarks = resolve_published_programme_fee(
            services=centre_services,
            requested_level=requested_level,
            service_required=service_required,
            citizenship_status=citizenship_status,
        )
        payable_fee = max(0.0, round(published_fee - estimated_subsidy, 2))
        cost_fit = calculate_cost_fit(payable_fee, budget_max)

        fee_breakdown = FeeBreakdownItem(
            published_fee=published_fee,
            estimated_subsidy=estimated_subsidy,
            estimated_monthly_payable_fee=payable_fee,
            cost_fit=cost_fit,
            remarks=remarks,
            incidental_fee_remarks=remarks,
        )

        if cost_fit is not None:
            active_weight = vacancy_weight + cost_weight
            overall_fit_score = (
                round(
                    ((v_score * vacancy_weight) + (cost_fit * cost_weight))
                    / active_weight,
                    2,
                )
                if active_weight > 0
                else 0.0
            )
        else:
            overall_fit_score = v_score

        candidate_item = MatchCandidateItem(
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
            level=requested_level,
            enrollment_month_year=enrollment_month_str,
            vacancy_status=v_status,
            vacancy_fit_score=v_score,
            cost_fit_score=cost_fit,
            travel_fit_score=None,
            overall_fit_score=overall_fit_score,
            fee_breakdown=fee_breakdown,
            travel_breakdown=None,
            highlight=None,
        )
        scored_candidates.append(candidate_item)

    # 5. Rank candidate centres: overall_fit_score desc, vacancy_fit_score desc, centre_name asc, centre_id asc
    scored_candidates.sort(
        key=lambda item: (
            -item.overall_fit_score,
            -item.vacancy_fit_score,
            item.centre_name,
            item.centre_id,
        )
    )

    # 6. Apply top_n limit
    top_results = scored_candidates[: request.top_n]

    return CentreMatchResponse(
        total_candidates=total_candidates,
        returned_count=len(top_results),
        level=requested_level,
        enrollment_month_year=enrollment_month_str,
        results=top_results,
        explanation=None,
        profile_id=request.profile_id,
    )

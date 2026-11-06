"""Preschool centre API endpoints."""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import false, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import (
    get_current_parent,
    get_current_user,
    get_optional_current_user,
)
from kidcarematch_backend.core.commute import (
    CommuteAnchors,
    build_commute_samples,
    build_negotiated_commute_sample,
    resolve_commute_postal,
    validate_cycle_token,
    validate_sample_pair,
)
from kidcarematch_backend.core.matching import (
    calculate_cost_fit,
    evaluate_government_subsidy,
    execute_multi_criteria_match,
    map_level_to_care_type,
    resolve_match_criteria,
    resolve_published_programme_fee,
    score_vacancy_fit,
)
from kidcarematch_backend.core.postal_districts import (
    get_all_postal_districts,
    get_postal_sectors_for_districts,
)
from kidcarematch_backend.db.db import (
    CitizenshipType,
    ExternalDataSourceStatus,
    FamilyPreferenceProfile,
    ModerationStatus,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    Review,
    User,
    VacancyRecord,
    WatchlistEntry,
    get_session,
)
from kidcarematch_backend.schemas.centre import (
    CentreLocationResponse,
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
from kidcarematch_backend.schemas.commute import (
    CommuteRequest,
    CommuteResponse,
    CommuteSamplingResponse,
    EvaluationMetadata,
    NegotiatedCommuteResponse,
)
from kidcarematch_backend.schemas.commute import (
    Coordinate as CommuteCoordinate,
)
from kidcarematch_backend.schemas.match import (
    CentreMatchRequest,
    CentreMatchResponse,
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

__all__ = [
    "calculate_cost_fit",
    "evaluate_government_subsidy",
    "get_centre_or_404",
    "map_level_to_care_type",
    "resolve_match_criteria",
    "resolve_published_programme_fee",
    "router",
    "score_vacancy_fit",
]


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
        items_query.order_by(
            col(PreschoolCentre.centre_name).asc(),
            col(PreschoolCentre.centre_id).asc(),
        )
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
    "/{centre_id}/location",
    response_model=CentreLocationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Preschool Centre Geospatial Location",
)
def get_centre_location(
    centre_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> CentreLocationResponse:
    """Retrieve verified OneMap geospatial coordinates for a preschool centre.

    Public endpoint (no auth required).
    Resolves postal code via OneMap search verifying exact returned postal match.
    Returns status='available' with coordinates if exact match, otherwise status='unavailable'.
    """
    centre = get_centre_or_404(centre_id, session)

    coord = resolve_commute_postal(centre.postal, session=session)
    if coord is not None:
        return CentreLocationResponse(
            centre_id=centre.centre_id or centre_id,
            coordinate=coord,
            status="available",
        )

    return CentreLocationResponse(
        centre_id=centre.centre_id or centre_id,
        coordinate=None,
        status="unavailable",
    )


@router.post(
    "/{centre_id}/commute",
    response_model=NegotiatedCommuteResponse | CommuteResponse,
    status_code=status.HTTP_200_OK,
    summary="Get personalised commute comparison to preschool centre",
)
def get_centre_commute(
    centre_id: int,
    payload: CommuteRequest,
    session: Annotated[Session, Depends(get_session)],
    current_user: Annotated[User, Depends(get_current_parent)],
) -> NegotiatedCommuteResponse | CommuteResponse:
    """Calculate personalised commute route(s) from home through centre (and optional workplace)."""
    centre = get_centre_or_404(centre_id, session)

    profile = session.get(FamilyPreferenceProfile, str(payload.profile_id))
    if not profile or profile.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preference profile not found",
        )

    if not profile.home_postal_code or not profile.home_postal_code.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Profile is incomplete: home_postal_code is required",
        )

    clean_centre_postal = (centre.postal or "").strip()
    centre_coord = resolve_commute_postal(clean_centre_postal, session=session)

    clean_home_postal = profile.home_postal_code.strip()
    home_coord = resolve_commute_postal(clean_home_postal, session=session)

    clean_workplace_postal = (profile.workplace_postal_code or "").strip()
    has_workplace = bool(clean_workplace_postal)
    workplace_coord: CommuteCoordinate | None = None
    if has_workplace:
        workplace_coord = resolve_commute_postal(
            clean_workplace_postal, session=session
        )

    anchors = CommuteAnchors(
        home=home_coord,
        centre=centre_coord,
        workplace=workplace_coord,
        has_workplace=has_workplace,
    )

    if payload.sampling is not None:
        if payload.mode != "pt":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sampling is only permitted for public transport (mode='pt')",
            )

        cycle_res = validate_cycle_token(
            cycle_id=payload.sampling.cycle_id,
            sample_index=payload.sampling.sample_index,
            policy=payload.sampling.policy,
        )

        selected_sample_def = cycle_res.allowed_samples[cycle_res.selected_index]
        sample = build_negotiated_commute_sample(
            kind="weekday",
            sampled_at=selected_sample_def.departure_at,
            mode="pt",
            anchors=anchors,
            conditions_id=selected_sample_def.conditions_id,
        )

        now_utc = datetime.now(timezone.utc)
        profile_raw = f"profile:{profile.profile_id}:{profile.home_postal_code or ''}:{profile.workplace_postal_code or ''}"
        profile_revision = hashlib.sha256(profile_raw.encode("utf-8")).hexdigest()

        home_str = (
            f"{home_coord.lat:.6f},{home_coord.lng:.6f}" if home_coord else "none"
        )
        centre_str = (
            f"{centre_coord.lat:.6f},{centre_coord.lng:.6f}"
            if centre_coord
            else "none"
        )
        workplace_str = (
            f"{workplace_coord.lat:.6f},{workplace_coord.lng:.6f}"
            if workplace_coord
            else "none"
        )
        anchor_raw = f"anchors:{home_str}:{centre_str}:{workplace_str}"
        anchor_revision = hashlib.sha256(anchor_raw.encode("utf-8")).hexdigest()

        evaluation = EvaluationMetadata(
            evaluation_id=f"eval_{secrets.token_hex(12)}",
            evaluated_at=now_utc,
            profile_revision=profile_revision,
            anchor_revision=anchor_revision,
        )

        sampling_response = CommuteSamplingResponse(
            policy=cycle_res.policy,  # type: ignore[arg-type]
            cycle_id=cycle_res.cycle_id,
            cycle_expires_at=cycle_res.cycle_expires_at,
            selected_index=cycle_res.selected_index,  # type: ignore[arg-type]
            allowed_samples=cycle_res.allowed_samples,
        )

        return NegotiatedCommuteResponse(
            centre_id=centre.centre_id or centre_id,
            profile_id=payload.profile_id,
            mode=payload.mode,
            anchors=anchors.to_dict(include_workplace_key=True),
            sample_pair=None,
            samples=[sample],
            comparison_contract="direct_baseline_v1"
            if payload.include_direct_baseline
            else None,
            sampling_contract="weekday_0800_wed_thu_fri_v1",
            evaluation=evaluation,
            sampling=sampling_response,
        )

    wednesday_dt, saturday_dt = validate_sample_pair(payload.sample_pair)
    response_sample_pair = [wednesday_dt.isoformat(), saturday_dt.isoformat()]

    samples = build_commute_samples(
        mode=payload.mode,
        anchors=anchors,
        sample_pair=(wednesday_dt, saturday_dt),
    )

    return CommuteResponse(
        centre_id=centre.centre_id or centre_id,
        profile_id=payload.profile_id,
        mode=payload.mode,
        sample_pair=response_sample_pair,
        anchors=anchors.to_dict(),
        samples=samples,
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
    criteria = resolve_match_criteria(request, session, current_user)
    return execute_multi_criteria_match(session, criteria)

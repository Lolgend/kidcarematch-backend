"""Cohort Watchlist Subscription API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from kidcarematch_backend.auth.security import get_current_user
from kidcarematch_backend.db.db import (
    PreschoolCentre,
    User,
    VacancyRecord,
    WatchlistEntry,
    get_session,
)
from kidcarematch_backend.schemas.watchlist import (
    UserWatchlistItem,
    UserWatchlistListResponse,
    WatchlistCreateRequest,
    WatchlistEntryResponse,
)

router = APIRouter(prefix="/api/v1/watchlist", tags=["watchlist"])


WATCHLIST_CONFLICT_DETAIL = (
    "Watchlist subscription already exists for this centre, cohort level, and enrollment month"
)


@router.post(
    "",
    response_model=WatchlistEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Cohort Watchlist Subscription",
)
def create_watchlist_entry(
    payload: WatchlistCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> WatchlistEntry:
    """Subscribe to vacancy alerts for a centre, cohort level, and enrollment month.

    Requires an authenticated active user session.
    Validates preschool centre existence.
    Enforces composite uniqueness per user, centre, cohort level, and enrollment month.
    """
    centre = session.get(PreschoolCentre, payload.centre_id)
    if not centre:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preschool centre not found",
        )

    assert current_user.user_id is not None

    existing = session.exec(
        select(WatchlistEntry).where(
            col(WatchlistEntry.user_id) == current_user.user_id,
            col(WatchlistEntry.centre_id) == payload.centre_id,
            col(WatchlistEntry.level) == payload.level,
            col(WatchlistEntry.enrollment_month_year) == payload.enrollment_month_year,
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WATCHLIST_CONFLICT_DETAIL,
        )

    entry = WatchlistEntry(
        user_id=current_user.user_id,
        centre_id=payload.centre_id,
        level=payload.level,
        enrollment_month_year=payload.enrollment_month_year,
    )
    try:
        session.add(entry)
        session.commit()
        session.refresh(entry)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WATCHLIST_CONFLICT_DETAIL,
        ) from exc

    return entry


@router.get(
    "/me",
    response_model=UserWatchlistListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Authenticated User's Watchlist Portfolio",
)
def get_my_watchlist(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of watchlist items to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
) -> UserWatchlistListResponse:
    """Retrieve paginated portfolio of watchlist subscriptions for the authenticated parent.

    Enriched with preschool centre metadata and current vacancy availability.
    Sorted chronologically by enrollment_month_year ASC, watchlist_id ASC.
    """
    assert current_user.user_id is not None

    total = (
        session.exec(
            select(func.count(col(WatchlistEntry.watchlist_id))).where(
                col(WatchlistEntry.user_id) == current_user.user_id
            )
        ).one()
        or 0
    )

    query = (
        select(
            WatchlistEntry,
            PreschoolCentre,
            VacancyRecord.vacancy_status,
        )
        .join(
            PreschoolCentre,
            col(WatchlistEntry.centre_id) == col(PreschoolCentre.centre_id),
        )
        .outerjoin(
            VacancyRecord,
            (col(VacancyRecord.centre_id) == col(WatchlistEntry.centre_id))
            & (col(VacancyRecord.level_type) == col(WatchlistEntry.level))
            & (
                col(VacancyRecord.enrollment_month_year)
                == col(WatchlistEntry.enrollment_month_year)
            ),
        )
        .where(col(WatchlistEntry.user_id) == current_user.user_id)
        .order_by(
            col(WatchlistEntry.enrollment_month_year).asc(),
            col(WatchlistEntry.watchlist_id).asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    results = session.exec(query).all()

    items = [
        UserWatchlistItem(
            watchlist_id=entry.watchlist_id or "",
            centre_id=centre.centre_id or 0,
            centre_name=centre.centre_name,
            centre_code=centre.centre_code,
            postal=centre.postal,
            address=centre.address,
            spark_certified=centre.spark_certified,
            gov_subsidy=centre.gov_subsidy,
            level=entry.level,
            enrollment_month_year=entry.enrollment_month_year,
            current_vacancy_status=vacancy_status,
        )
        for entry, centre, vacancy_status in results
    ]

    return UserWatchlistListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/{watchlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Watchlist Subscription",
)
def delete_watchlist_entry(
    watchlist_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Permanently delete an authored cohort watchlist subscription.

    Requires an authenticated active user session.
    Enforces author ownership: only the user who created the subscription may delete it.
    Returns HTTP 204 No Content upon successful hard-deletion.
    """
    entry = session.get(WatchlistEntry, watchlist_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Watchlist entry not found",
        )

    if entry.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this watchlist entry",
        )

    session.delete(entry)
    session.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)

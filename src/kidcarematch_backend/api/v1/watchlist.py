"""Cohort Watchlist Subscription API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_user
from kidcarematch_backend.db.db import (
    PreschoolCentre,
    User,
    WatchlistEntry,
    get_session,
)
from kidcarematch_backend.schemas.watchlist import (
    WatchlistCreateRequest,
    WatchlistEntryResponse,
)

router = APIRouter(prefix="/api/v1/watchlist", tags=["watchlist"])


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
            detail="Watchlist subscription already exists for this centre, cohort level, and enrollment month",
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
            detail="Watchlist subscription already exists for this centre, cohort level, and enrollment month",
        ) from exc

    return entry

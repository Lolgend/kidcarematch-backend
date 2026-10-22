"""Family Preference Profile API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kidcarematch_backend.auth.security import get_current_parent
from kidcarematch_backend.db.db import FamilyPreferenceProfile, User, get_session
from kidcarematch_backend.schemas.profile import (
    ProfileCreateRequest,
    ProfileResponse,
)

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Family Preference Profile",
)
@router.post(
    "/",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_profile(
    payload: ProfileCreateRequest,
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> FamilyPreferenceProfile:
    """Create a new Family Preference Profile for the authenticated parent.

    Enforces:
    - Parent role requirement (ADMIN forbidden).
    - Uniqueness of profile name per parent account.
    - Validated 6-digit Singapore postal codes and conditional workplace postal requirement.
    - Balanced trade-off weights summing to 1.0.
    - Normalized UTC datetime for target enrollment month.
    """
    if not current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authenticated parent user has no valid user ID.",
        )

    # Check for existing profile with the same name under this parent
    existing_profile = session.exec(
        select(FamilyPreferenceProfile).where(
            FamilyPreferenceProfile.user_id == current_user.user_id,
            FamilyPreferenceProfile.profile_name == payload.profile_name,
        )
    ).first()
    if existing_profile:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
        )

    profile = FamilyPreferenceProfile(
        user_id=current_user.user_id,
        profile_name=payload.profile_name,
        level=payload.level,
        citizenship_status=payload.citizenship_status,
        service_required=payload.service_required,
        home_postal_code=payload.home_postal_code,
        workplace_postal_code=payload.workplace_postal_code,
        location_preference=payload.location_preference,
        household_income=payload.household_income,
        household_member_count=payload.household_member_count,
        budget_max=payload.budget_max,
        enrollment_month_year=payload.get_enrollment_datetime(),
        cost_weight=payload.cost_weight,
        travel_weight=payload.travel_weight,
    )
    session.add(profile)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
        ) from exc

    session.refresh(profile)
    return profile

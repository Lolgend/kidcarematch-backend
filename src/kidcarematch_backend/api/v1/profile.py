"""Family Preference Profile API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kidcarematch_backend.auth.security import get_current_parent
from kidcarematch_backend.db.db import (
    FamilyPreferenceProfile,
    User,
    get_session,
)
from kidcarematch_backend.schemas.profile import (
    ProfileCreateRequest,
    ProfilePatchRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    parse_year_month,
    validate_location_and_weights,
)

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


def is_profile_name_unique_violation(exc: IntegrityError) -> bool:
    """Determine whether an IntegrityError is caused by the profile name unique constraint."""
    err_str = str(exc).lower()
    return (
        "uq_family_profile_user_profile_name" in err_str
        or "familypreferenceprofile.user_id, familypreferenceprofile.profile_name" in err_str
        or ("unique constraint failed" in err_str and "profile_name" in err_str)
    )


def get_profile_or_404(
    profile_id: str,
    user_id: str,
    session: Session,
) -> FamilyPreferenceProfile:
    """Retrieve a single Family Preference Profile by ID for the user, raising 404 if not found."""
    profile = session.exec(
        select(FamilyPreferenceProfile).where(
            FamilyPreferenceProfile.profile_id == profile_id,
            FamilyPreferenceProfile.user_id == user_id,
        )
    ).first()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preference profile not found",
        )
    return profile


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Family Preference Profile",
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
        if not is_profile_name_unique_violation(exc):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
        ) from exc

    session.refresh(profile)
    return profile


@router.get(
    "",
    response_model=list[ProfileResponse],
    status_code=status.HTTP_200_OK,
    summary="List Family Preference Profiles",
)
def list_profiles(
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> list[FamilyPreferenceProfile]:
    """Retrieve all Family Preference Profiles belonging to the authenticated parent.

    Enforces parent authorization and strict user isolation.
    """
    profiles = session.exec(
        select(FamilyPreferenceProfile).where(
            FamilyPreferenceProfile.user_id == current_user.user_id
        )
    ).all()
    return list(profiles)


@router.get(
    "/{profile_id}",
    response_model=ProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Family Preference Profile by ID",
)
def get_profile(
    profile_id: str,
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> FamilyPreferenceProfile:
    """Retrieve a single Family Preference Profile by ID for the authenticated parent.

    Returns HTTP 404 if the profile does not exist or belongs to another user
    (preventing ID enumeration).
    """
    assert current_user.user_id is not None
    return get_profile_or_404(profile_id, current_user.user_id, session)


@router.put(
    "/{profile_id}",
    response_model=ProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Replace Family Preference Profile",
)
def update_profile(
    profile_id: str,
    payload: ProfileUpdateRequest,
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> FamilyPreferenceProfile:
    """Fully replace an existing Family Preference Profile.

    Enforces:
    - Profile existence and user ownership (HTTP 404).
    - Uniqueness of profile name per parent account if renamed (HTTP 409).
    - Domain validation of all criteria (HTTP 422).
    """
    assert current_user.user_id is not None
    existing_profile = get_profile_or_404(profile_id, current_user.user_id, session)

    # Check for name collision if renaming
    if payload.profile_name != existing_profile.profile_name:
        collision = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.user_id == current_user.user_id,
                FamilyPreferenceProfile.profile_name == payload.profile_name,
                FamilyPreferenceProfile.profile_id != profile_id,
            )
        ).first()
        if collision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
            )

    existing_profile.profile_name = payload.profile_name
    existing_profile.level = payload.level
    existing_profile.citizenship_status = payload.citizenship_status
    existing_profile.service_required = payload.service_required
    existing_profile.home_postal_code = payload.home_postal_code
    existing_profile.workplace_postal_code = payload.workplace_postal_code
    existing_profile.location_preference = payload.location_preference
    existing_profile.household_income = payload.household_income
    existing_profile.household_member_count = payload.household_member_count
    existing_profile.budget_max = payload.budget_max
    existing_profile.enrollment_month_year = payload.get_enrollment_datetime()
    existing_profile.cost_weight = payload.cost_weight
    existing_profile.travel_weight = payload.travel_weight

    session.add(existing_profile)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not is_profile_name_unique_violation(exc):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
        ) from exc

    session.refresh(existing_profile)
    return existing_profile


@router.patch(
    "/{profile_id}",
    response_model=ProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Partially Update Family Preference Profile",
)
def patch_profile(
    profile_id: str,
    payload: ProfilePatchRequest,
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> FamilyPreferenceProfile:
    """Partially update an existing Family Preference Profile.

    Enforces:
    - Profile existence and user ownership (HTTP 404).
    - Uniqueness of profile name per parent account if renaming (HTTP 409).
    - Cross-field validation on merged state:
      - Location preference and workplace postal code consistency (HTTP 422).
      - Balanced weights summing to 1.0 (HTTP 422).
    """
    assert current_user.user_id is not None
    existing_profile = get_profile_or_404(profile_id, current_user.user_id, session)

    patch_data = payload.model_dump(exclude_unset=True)
    if not patch_data:
        return existing_profile

    # Check for name collision if profile_name is being changed
    if (
        "profile_name" in patch_data
        and payload.profile_name is not None
        and payload.profile_name != existing_profile.profile_name
    ):
        collision = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.user_id == current_user.user_id,
                FamilyPreferenceProfile.profile_name == payload.profile_name,
                FamilyPreferenceProfile.profile_id != profile_id,
            )
        ).first()
        if collision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A profile with the name '{payload.profile_name}' already exists for your account.",
            )

    # Merge values for cross-field validation
    merged_location_pref = patch_data.get(
        "location_preference", existing_profile.location_preference
    )
    merged_workplace_postal = patch_data.get(
        "workplace_postal_code", existing_profile.workplace_postal_code
    )
    merged_cost_weight = patch_data.get("cost_weight", existing_profile.cost_weight)
    merged_travel_weight = patch_data.get(
        "travel_weight", existing_profile.travel_weight
    )

    try:
        validate_location_and_weights(
            location_preference=merged_location_pref,
            workplace_postal_code=merged_workplace_postal,
            cost_weight=merged_cost_weight,
            travel_weight=merged_travel_weight,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    for field_name, value in patch_data.items():
        if field_name == "enrollment_month_year":
            if value is not None:
                existing_profile.enrollment_month_year = parse_year_month(value)
        else:
            setattr(existing_profile, field_name, value)

    session.add(existing_profile)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not is_profile_name_unique_violation(exc):
            raise
        name = payload.profile_name or existing_profile.profile_name
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A profile with the name '{name}' already exists for your account.",
        ) from exc

    session.refresh(existing_profile)
    return existing_profile


@router.delete(
    "/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Family Preference Profile",
)
def delete_profile(
    profile_id: str,
    current_user: Annotated[User, Depends(get_current_parent)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Delete a single Family Preference Profile by ID for the authenticated parent.

    Enforces:
    - Parent role requirement (ADMIN forbidden).
    - Profile existence and strict user isolation (HTTP 404 if not found or owned by another user).
    - Database deletion committed and returns HTTP 204 No Content.
    """
    assert current_user.user_id is not None
    profile = get_profile_or_404(profile_id, current_user.user_id, session)

    session.delete(profile)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

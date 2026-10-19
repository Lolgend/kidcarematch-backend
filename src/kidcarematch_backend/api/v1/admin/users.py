"""Admin user management and provisioning endpoints."""

import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from firebase_admin import auth, exceptions  # type: ignore[import-untyped]
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select, update

from kidcarematch_backend.auth.security import get_current_admin, get_firebase_app

logger = logging.getLogger(__name__)
from kidcarematch_backend.db.db import (
    AccountStatus,
    Review,
    User,
    UserRole,
    get_session,
)
from kidcarematch_backend.schemas.admin import (
    AdminUserCreateRequest,
    AdminUserListResponse,
    AdminUserResponse,
    AdminUserUpdateRequest,
)

router = APIRouter()


@router.get(
    "",
    response_model=AdminUserListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Registered Users",
)
def list_users(
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of users to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
    role: Annotated[
        UserRole | None,
        Query(description="Filter users by role"),
    ] = None,
    query: Annotated[
        str | None,
        Query(description="Case-insensitive search matching name or email"),
    ] = None,
) -> AdminUserListResponse:
    """Retrieve a paginated list of registered users supporting role filtering and text search."""
    conditions = []

    if role is not None:
        conditions.append(col(User.role) == role)

    if query is not None:
        cleaned_query = query.strip()
        if cleaned_query:
            pattern = f"%{cleaned_query.lower()}%"
            conditions.append(
                or_(
                    func.lower(col(User.full_name)).like(pattern),
                    func.lower(col(User.email)).like(pattern),
                )
            )

    total_query = select(func.count(col(User.user_id)))
    if conditions:
        total_query = total_query.where(*conditions)
    total = session.exec(total_query).one() or 0

    items_query = (
        select(User)
        .order_by(col(User.created_at).desc(), col(User.user_id).asc())
        .offset(offset)
        .limit(limit)
    )
    if conditions:
        items_query = items_query.where(*conditions)

    users = session.exec(items_query).all()

    return AdminUserListResponse(
        items=[AdminUserResponse.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{user_id}",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get User Details",
)
def get_user_detail(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminUserResponse:
    """Retrieve complete profile details for an individual user."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        )
    return AdminUserResponse.model_validate(user)


@router.post(
    "",
    response_model=AdminUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision New User Account",
)
def create_user(
    payload: AdminUserCreateRequest,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminUserResponse:
    """Provision a new user account with Firebase authentication and persist the local User record."""
    # Check if user email already exists locally
    existing_user = session.exec(
        select(User).where(func.lower(col(User.email)) == str(payload.email).lower())
    ).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{payload.email}' already exists",
        )

    # Auto-generate secure password if omitted
    password = payload.password or secrets.token_urlsafe(16)

    # Initialize Firebase app and create user in Firebase Auth
    get_firebase_app()
    try:
        fb_user = auth.create_user(
            email=str(payload.email),
            password=password,
            display_name=payload.full_name,
        )
    except auth.EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{payload.email}' already exists in Firebase",
        ) from exc
    except Exception as exc:
        if "EMAIL_EXISTS" in str(exc) or "already exists" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User with email '{payload.email}' already exists",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create user in Firebase: {exc}",
        ) from exc

    admin_lvl = payload.admin_level if payload.role == UserRole.ADMIN else None
    user = User(
        firebase_uid=fb_user.uid,
        email=str(payload.email),
        full_name=payload.full_name,
        role=payload.role,
        account_status=AccountStatus.ACTIVE,
        admin_level=admin_lvl,
    )

    try:
        session.add(user)
        session.commit()
        session.refresh(user)
    except IntegrityError as exc:
        session.rollback()
        # Roll back Firebase account creation if database persistence fails
        try:
            auth.delete_user(fb_user.uid)
        except (exceptions.FirebaseError, ValueError) as cleanup_exc:
            logger.warning(
                "Failed to clean up Firebase user '%s' after database rollback: %s",
                fb_user.uid,
                cleanup_exc,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email or Firebase UID already exists",
        ) from exc

    return AdminUserResponse.model_validate(user)


@router.put(
    "/{user_id}",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Update User Profile",
)
def update_user(
    user_id: str,
    payload: AdminUserUpdateRequest,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminUserResponse:
    """Update profile attributes (full_name, role, admin_level) for an individual user."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        )

    fields_set = payload.model_fields_set

    if "full_name" in fields_set and payload.full_name is not None:
        user.full_name = payload.full_name

    if "role" in fields_set and payload.role is not None:
        user.role = payload.role
        if payload.role == UserRole.PARENT:
            user.admin_level = None

    if "admin_level" in fields_set:
        if user.role == UserRole.ADMIN:
            user.admin_level = payload.admin_level
        else:
            user.admin_level = None

    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    session.refresh(user)

    return AdminUserResponse.model_validate(user)


@router.patch(
    "/{user_id}/deactivate",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Deactivate User Account",
)
def deactivate_user(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminUserResponse:
    """Deactivate a user account by setting its status to SUSPENDED."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        )

    user.account_status = AccountStatus.SUSPENDED
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    session.refresh(user)

    return AdminUserResponse.model_validate(user)


@router.patch(
    "/{user_id}/reactivate",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Reactivate User Account",
)
def reactivate_user(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminUserResponse:
    """Reactivate a suspended user account by setting its status to ACTIVE."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        )

    user.account_status = AccountStatus.ACTIVE
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    session.refresh(user)

    return AdminUserResponse.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete User Account",
)
def delete_user(
    user_id: str,
    session: Annotated[Session, Depends(get_session)],
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> Response:
    """Permanently delete a user account from database (with cascade) and Firebase Auth."""
    if current_admin.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Administrators cannot delete their own account.",
        )

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found",
        )

    firebase_uid = user.firebase_uid

    # Delete from Firebase Auth
    get_firebase_app()
    try:
        auth.delete_user(firebase_uid)
    except auth.UserNotFoundError:
        pass
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user in Firebase: {exc}",
        ) from exc

    # Nullify any review moderation audits where this user was the moderator
    session.exec(
        update(Review).where(col(Review.moderated_by) == user.user_id).values(moderated_by=None)  # type: ignore[arg-type]
    )

    # Permanently delete user from database (relational cascade deletes child records)
    session.delete(user)
    session.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""User API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import AccountStatus, User, UserRole, get_session
from kidcarematch_backend.schemas.user import UserResponse, UserSignupRequest

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="User Onboarding and Signup",
)
def signup(
    payload: UserSignupRequest,
    claims: Annotated[FirebaseTokenClaims, Depends(get_token_claims)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    """Create a new user profile using verified Firebase ID token claims."""
    if not claims.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email claim is missing from authentication token",
        )

    existing_uid = session.exec(select(User).where(User.firebase_uid == claims.uid)).first()
    if existing_uid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this Firebase UID already exists",
        )

    existing_email = session.exec(select(User).where(User.email == claims.email)).first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        )

    user = User(
        firebase_uid=claims.uid,
        email=claims.email,
        full_name=payload.full_name,
        role=UserRole.PARENT,
        account_status=AccountStatus.ACTIVE,
    )

    try:
        session.add(user)
        session.commit()
        session.refresh(user)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this Firebase UID or email already exists",
        ) from exc

    return user

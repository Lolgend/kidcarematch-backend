"""Authentication and security dependencies for KidCareMatch."""

from typing import Annotated

import firebase_admin  # type: ignore[import-untyped]
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth, credentials  # type: ignore[import-untyped]
from pydantic import BaseModel
from sqlmodel import Session, select

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import AccountStatus, User, get_session


class FirebaseTokenClaims(BaseModel):
    """Claims extracted from a verified Firebase ID token."""

    uid: str
    email: str


bearer_scheme = HTTPBearer(auto_error=False)


def get_firebase_app() -> firebase_admin.App:
    """Initialize or retrieve the Firebase app singleton."""
    if not firebase_admin._apps:
        if settings.FIREBASE_CREDENTIALS_PATH:
            cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
            return firebase_admin.initialize_app(cred)
        if settings.FIREBASE_PROJECT_ID:
            return firebase_admin.initialize_app(options={"projectId": settings.FIREBASE_PROJECT_ID})
        return firebase_admin.initialize_app()
    return firebase_admin.get_app()


def get_token_claims(
    auth_credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> FirebaseTokenClaims:
    """Extract and verify Firebase ID token from the Bearer authorization header."""
    if not auth_credentials or auth_credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_credentials.credentials
    try:
        get_firebase_app()
        decoded_claims = auth.verify_id_token(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid = decoded_claims.get("uid") or decoded_claims.get("sub")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token claims",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email = decoded_claims.get("email", "")
    return FirebaseTokenClaims(uid=uid, email=email)


def get_current_user(
    claims: Annotated[FirebaseTokenClaims, Depends(get_token_claims)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    """Retrieve the current user from database by verified Firebase UID."""
    user = session.exec(select(User).where(User.firebase_uid == claims.uid)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found. Please complete signup onboarding.",
        )
    if user.account_status == AccountStatus.SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended.",
        )
    return user


"""Admin authentication and session management endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.db.db import AccountStatus, User
from kidcarematch_backend.schemas.admin import AdminLogoutResponse, AdminProfileResponse

router = APIRouter()


@router.post(
    "/login",
    response_model=AdminProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin Login",
)
def admin_login(
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminProfileResponse:
    """Verify administrator identity and return administrative profile."""
    if current_admin.account_status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended.",
        )
    return AdminProfileResponse.model_validate(current_admin)


@router.post(
    "/logout",
    response_model=AdminLogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin Logout",
)
def admin_logout(
    current_admin: Annotated[User, Depends(get_current_admin)],
) -> AdminLogoutResponse:
    """Acknowledge administrator session teardown."""
    return AdminLogoutResponse(message="Successfully logged out")

"""Admin API package providing modular routing for administrative functions."""

from fastapi import APIRouter

from kidcarematch_backend.api.v1.admin.auth import router as auth_router
from kidcarematch_backend.api.v1.admin.users import router as users_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["admin-auth"])
router.include_router(users_router, prefix="/users", tags=["admin-users"])

__all__ = ["router"]

"""Admin API package providing modular routing for administrative functions."""

from fastapi import APIRouter

from kidcarematch_backend.api.v1.admin.auth import router as auth_router
from kidcarematch_backend.api.v1.admin.status import router as status_router
from kidcarematch_backend.api.v1.admin.sync import router as sync_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["admin-auth"])
router.include_router(sync_router, prefix="/sync", tags=["admin-sync"])
router.include_router(status_router, prefix="/status", tags=["admin-status"])

__all__ = ["router"]

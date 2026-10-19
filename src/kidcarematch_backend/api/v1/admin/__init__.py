"""Admin API package providing modular routing for administrative functions."""

from fastapi import APIRouter

from kidcarematch_backend.api.v1.admin.auth import router as auth_router
from kidcarematch_backend.api.v1.admin.reviews import router as reviews_router
from kidcarematch_backend.api.v1.admin.status import router as status_router
from kidcarematch_backend.api.v1.admin.subsidy_rules import (
    router as subsidy_rules_router,
)
from kidcarematch_backend.api.v1.admin.sync import router as sync_router
from kidcarematch_backend.api.v1.admin.users import router as users_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["admin-auth"])
router.include_router(users_router, prefix="/users", tags=["admin-users"])
router.include_router(reviews_router, prefix="/reviews", tags=["admin-reviews"])
router.include_router(
    subsidy_rules_router, prefix="/subsidy-rules", tags=["admin-subsidy-rules"]
)
router.include_router(sync_router, prefix="/sync", tags=["admin-sync"])
router.include_router(status_router, prefix="/status", tags=["admin-status"])

__all__ = ["router"]

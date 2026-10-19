"""Admin API package providing modular routing for administrative functions."""

from fastapi import APIRouter

from kidcarematch_backend.api.v1.admin.auth import router as auth_router
from kidcarematch_backend.api.v1.admin.subsidy_rules import (
    router as subsidy_rules_router,
)

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["admin-auth"])
router.include_router(
    subsidy_rules_router, prefix="/subsidy-rules", tags=["admin-subsidy-rules"]
)

__all__ = ["router"]

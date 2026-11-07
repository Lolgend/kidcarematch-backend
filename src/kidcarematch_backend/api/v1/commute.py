"""Commute API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from kidcarematch_backend.auth.security import get_current_parent
from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import User
from kidcarematch_backend.schemas.commute import (
    CommuteCapabilitiesResponse,
    DirectBaselineContract,
    SamplingContract,
)

router = APIRouter(prefix="/api/v1/commute", tags=["commute"])


@router.get(
    "/capabilities",
    response_model=CommuteCapabilitiesResponse,
    status_code=status.HTTP_200_OK,
    summary="Get available commute comparison capability contracts",
)
def get_commute_capabilities(
    current_user: Annotated[User, Depends(get_current_parent)],
) -> CommuteCapabilitiesResponse:
    """Discover server-supported commute comparison and sampling capabilities."""
    direct_baseline_contract: DirectBaselineContract | None = (
        "direct_baseline_v1"
        if settings.COMMUTE_ENABLE_DIRECT_BASELINE
        else None
    )
    sampling_contract: SamplingContract | None = (
        "weekday_0800_wed_thu_fri_v1"
        if settings.COMMUTE_ENABLE_WEEKDAY_SAMPLING
        else None
    )
    return CommuteCapabilitiesResponse(
        direct_baseline_contract=direct_baseline_contract,
        sampling_contract=sampling_contract,
    )

"""Preschool centre review API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from kidcarematch_backend.auth.security import get_current_user
from kidcarematch_backend.db.db import ModerationStatus, Review, User, get_session
from kidcarematch_backend.schemas.review import ReviewResponse, ReviewUpdateRequest

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])


@router.get(
    "/{review_id}",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Preschool Centre Review by ID",
)
def get_review_by_id(
    review_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> Review:
    """Retrieve detailed information of a single preschool centre review by ID."""
    review = session.get(Review, review_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )
    return review


@router.patch(
    "/{review_id}",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Preschool Centre Review",
)
def update_review(
    review_id: str,
    payload: ReviewUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> Review:
    """Update rating and/or description of an authored review in-place.

    Requires an authenticated active user session.
    Enforces author ownership: only the user who created the review may edit it.
    Retains APPROVED moderation status immediately.
    """
    review = session.get(Review, review_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    if review.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to edit this review",
        )

    if "rating" in payload.model_fields_set and payload.rating is not None:
        review.rating = payload.rating

    if "review_description" in payload.model_fields_set:
        review.review_description = payload.review_description

    review.moderation_status = ModerationStatus.APPROVED

    session.add(review)
    session.commit()
    session.refresh(review)

    return review

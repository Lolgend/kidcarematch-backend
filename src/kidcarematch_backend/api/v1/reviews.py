"""Preschool centre review API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_user
from kidcarematch_backend.db.db import (
    ModerationStatus,
    PreschoolCentre,
    Review,
    User,
    get_session,
)
from kidcarematch_backend.schemas.review import (
    ReviewResponse,
    ReviewUpdateRequest,
    UserReviewItem,
    UserReviewListResponse,
)

router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])


@router.get(
    "/me",
    response_model=UserReviewListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Authenticated User's Reviews",
)
def get_my_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of reviews to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
) -> UserReviewListResponse:
    """Retrieve paginated portfolio of reviews authored by the authenticated parent."""
    total = (
        session.exec(
            select(func.count(col(Review.review_id))).where(
                col(Review.user_id) == current_user.user_id
            )
        ).one()
        or 0
    )

    query = (
        select(Review, PreschoolCentre.centre_name)
        .join(PreschoolCentre, col(Review.centre_id) == col(PreschoolCentre.centre_id))
        .where(col(Review.user_id) == current_user.user_id)
        .order_by(col(Review.created_at).desc(), col(Review.review_id).desc())
        .offset(offset)
        .limit(limit)
    )
    results = session.exec(query).all()

    items = [
        UserReviewItem(
            review_id=review.review_id or "",
            centre_id=review.centre_id,
            centre_name=centre_name,
            rating=review.rating,
            review_description=review.review_description,
            moderation_status=review.moderation_status,
            created_at=review.created_at,
        )
        for review, centre_name in results
    ]

    return UserReviewListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


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

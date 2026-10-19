"""Admin preschool centre review moderation and purge API endpoints."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.db.db import (
    AccountStatus,
    ModerationStatus,
    PreschoolCentre,
    Review,
    User,
    get_session,
)
from kidcarematch_backend.schemas.admin import (
    AdminReviewItem,
    AdminReviewListResponse,
    AdminReviewResponse,
    AdminReviewStatusUpdateRequest,
)

router = APIRouter()


@router.get(
    "",
    response_model=AdminReviewListResponse,
    status_code=status.HTTP_200_OK,
    summary="List Preschool Centre Reviews for Moderation",
)
def list_admin_reviews(
    current_admin: Annotated[User, Depends(get_current_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[
        int,
        Query(ge=1, le=100, description="Number of reviews to return"),
    ] = 20,
    offset: Annotated[
        int,
        Query(ge=0, description="Offset index for pagination"),
    ] = 0,
    moderation_status: Annotated[
        ModerationStatus | None,
        Query(description="Filter reviews by moderation status"),
    ] = None,
    centre_id: Annotated[
        int | None,
        Query(description="Filter reviews by preschool centre ID"),
    ] = None,
    rating: Annotated[
        int | None,
        Query(ge=1, le=5, description="Filter reviews by rating (1-5 stars)"),
    ] = None,
) -> AdminReviewListResponse:
    """Retrieve a paginated, enriched feed of reviews supporting multi-criteria filtering."""
    if current_admin.account_status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended.",
        )

    conditions = []
    if moderation_status is not None:
        conditions.append(col(Review.moderation_status) == moderation_status)
    if centre_id is not None:
        conditions.append(col(Review.centre_id) == centre_id)
    if rating is not None:
        conditions.append(col(Review.rating) == rating)

    count_query = select(func.count(col(Review.review_id)))
    if conditions:
        count_query = count_query.where(*conditions)
    total = session.exec(count_query).one() or 0

    data_query = (
        select(Review, PreschoolCentre.centre_name, User.full_name, User.email)
        .join(PreschoolCentre, col(Review.centre_id) == col(PreschoolCentre.centre_id))
        .join(User, col(Review.user_id) == col(User.user_id))
    )
    if conditions:
        data_query = data_query.where(*conditions)

    data_query = (
        data_query.order_by(col(Review.created_at).desc(), col(Review.review_id).desc())
        .offset(offset)
        .limit(limit)
    )
    results = session.exec(data_query).all()

    items = [
        AdminReviewItem(
            review_id=review.review_id or "",
            centre_id=review.centre_id,
            centre_name=centre_name,
            user_id=review.user_id,
            full_name=full_name,
            email=email,
            rating=review.rating,
            review_description=review.review_description,
            moderation_status=review.moderation_status,
            moderated_at=review.moderated_at,
            moderated_by=review.moderated_by,
            created_at=review.created_at,
        )
        for review, centre_name, full_name, email in results
    ]

    return AdminReviewListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{review_id}/status",
    response_model=AdminReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Preschool Centre Review Moderation Status",
)
def update_review_moderation_status(
    review_id: str,
    payload: AdminReviewStatusUpdateRequest,
    current_admin: Annotated[User, Depends(get_current_admin)],
    session: Annotated[Session, Depends(get_session)],
) -> AdminReviewResponse:
    """Update review moderation status and record administrative audit metadata."""
    if current_admin.account_status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended.",
        )

    review = session.get(Review, review_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    review.moderation_status = payload.moderation_status
    review.moderated_at = datetime.now(timezone.utc)
    review.moderated_by = current_admin.user_id

    session.add(review)
    session.commit()
    session.refresh(review)

    return AdminReviewResponse(
        review_id=review.review_id or "",
        centre_id=review.centre_id,
        user_id=review.user_id,
        rating=review.rating,
        review_description=review.review_description,
        moderation_status=review.moderation_status,
        moderated_at=review.moderated_at,
        moderated_by=review.moderated_by,
        created_at=review.created_at,
        centre_name=review.centre.centre_name if review.centre else None,
        full_name=review.user.full_name if review.user else None,
        email=review.user.email if review.user else None,
    )


@router.delete(
    "/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Administrative Hard Delete Preschool Centre Review",
)
def delete_admin_review(
    review_id: str,
    current_admin: Annotated[User, Depends(get_current_admin)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Permanently delete a preschool centre review row from the database."""
    if current_admin.account_status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended.",
        )

    review = session.get(Review, review_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    session.delete(review)
    session.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)

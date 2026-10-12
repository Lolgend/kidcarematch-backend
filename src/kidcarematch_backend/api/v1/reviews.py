"""Review API endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_user
from kidcarematch_backend.db.db import (
    PreschoolCentre,
    Review,
    User,
    get_session,
)
from kidcarematch_backend.schemas.review import (
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

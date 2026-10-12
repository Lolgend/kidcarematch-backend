"""Tests for Preschool Centre Review Submission and Review API endpoints."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    ModerationStatus,
    PreschoolCentre,
    Review,
    User,
    UserRole,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(test_engine):
    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_centre(test_engine) -> PreschoolCentre:
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_TEST_01",
            centre_name="Blossom Discovery Centre",
            postal="560123",
            address="123 Blossom Avenue",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        return centre


@pytest.fixture
def sample_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="usr_parent_01",
            firebase_uid="firebase_parent_01",
            email="parent1@example.com",
            full_name="Alice Tan",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def suspended_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="usr_suspended_01",
            firebase_uid="firebase_suspended_01",
            email="suspended@example.com",
            full_name="Suspended Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.SUSPENDED,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def set_mock_auth(user: User):
    mock_claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims


# ==============================================================================
# POST /api/v1/centres/{centre_id}/reviews Tests
# ==============================================================================


def test_create_review_missing_auth_token_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Submitting review without Authorization header must return HTTP 401 Unauthorized."""
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        json={"rating": 5, "review_description": "Great centre!"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_create_review_invalid_auth_scheme_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Submitting review with non-bearer auth header must return HTTP 401 Unauthorized."""
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Basic not-a-bearer-token"},
        json={"rating": 5, "review_description": "Great centre!"},
    )
    assert response.status_code == 401


def test_create_review_suspended_user_returns_403(
    client: TestClient, sample_centre: PreschoolCentre, suspended_user: User
):
    """Submitting review by a suspended user must return HTTP 403 Forbidden."""
    set_mock_auth(suspended_user)
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 4, "review_description": "Attempted review from suspended user."},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_create_review_centre_not_found_returns_404(
    client: TestClient, sample_user: User
):
    """Submitting review for non-existent centre_id must return HTTP 404 Not Found."""
    set_mock_auth(sample_user)
    response = client.post(
        "/api/v1/centres/99999/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5, "review_description": "Non-existent centre review."},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


@pytest.mark.parametrize("invalid_rating", [0, -1, 6, 10, 100])
def test_create_review_out_of_bounds_rating_returns_422(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, invalid_rating: int
):
    """Submitting rating outside 1..5 range must return HTTP 422 Unprocessable Entity."""
    set_mock_auth(sample_user)
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": invalid_rating, "review_description": "Bad rating value."},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("invalid_rating_type", ["five", 4.5, None, ""])
def test_create_review_invalid_rating_data_type_returns_422(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, invalid_rating_type: object
):
    """Submitting non-integer rating data type must return HTTP 422 Unprocessable Entity."""
    set_mock_auth(sample_user)
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": invalid_rating_type, "review_description": "Non-integer rating."},
    )
    assert response.status_code == 422


def test_create_review_success_with_description(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, test_engine
):
    """Submitting valid review with rating and commentary returns 201 Created and defaults to APPROVED."""
    set_mock_auth(sample_user)
    payload = {
        "rating": 5,
        "review_description": "Exceptional curriculum and caring educators!",
    }
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["user_id"] == sample_user.user_id
    assert data["rating"] == 5
    assert data["review_description"] == "Exceptional curriculum and caring educators!"
    assert data["moderation_status"] == ModerationStatus.APPROVED.value
    assert "review_id" in data
    assert "created_at" in data

    # Verify review_id is a valid UUID
    review_uuid = uuid.UUID(data["review_id"])
    assert str(review_uuid) == data["review_id"]

    # Verify persisted in database
    with Session(test_engine) as session:
        db_review = session.get(Review, data["review_id"])
        assert db_review is not None
        assert db_review.centre_id == sample_centre.centre_id
        assert db_review.user_id == sample_user.user_id
        assert db_review.rating == 5
        assert db_review.review_description == "Exceptional curriculum and caring educators!"
        assert db_review.moderation_status == ModerationStatus.APPROVED


def test_create_review_success_without_description(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, test_engine
):
    """Submitting review with rating only and null/omitted description returns 201 Created."""
    set_mock_auth(sample_user)
    payload = {"rating": 4}
    response = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["user_id"] == sample_user.user_id
    assert data["rating"] == 4
    assert data["review_description"] is None
    assert data["moderation_status"] == ModerationStatus.APPROVED.value

    # Verify persisted in database
    with Session(test_engine) as session:
        db_review = session.get(Review, data["review_id"])
        assert db_review is not None
        assert db_review.rating == 4
        assert db_review.review_description is None
        assert db_review.moderation_status == ModerationStatus.APPROVED


def test_create_review_duplicate_submission_returns_409(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Submitting a second review for the same centre by the same user returns HTTP 409 Conflict."""
    set_mock_auth(sample_user)
    payload = {"rating": 5, "review_description": "First review."}

    # First submission succeeds
    response1 = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert response1.status_code == 201

    # Second submission returns 409 Conflict
    response2 = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 3, "review_description": "Trying to submit duplicate review."},
    )
    assert response2.status_code == 409
    assert "already" in response2.json()["detail"].lower()


def test_create_review_different_users_same_centre_success(
    client: TestClient, sample_centre: PreschoolCentre, test_engine
):
    """Multiple distinct parents can independently review the same preschool centre."""
    with Session(test_engine) as session:
        user1 = User(
            user_id="usr_p1",
            firebase_uid="fb_p1",
            email="p1@example.com",
            full_name="Parent One",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        user2 = User(
            user_id="usr_p2",
            firebase_uid="fb_p2",
            email="p2@example.com",
            full_name="Parent Two",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add_all([user1, user2])
        session.commit()
        session.refresh(user1)
        session.refresh(user2)

    # User 1 submits
    set_mock_auth(user1)
    r1 = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5, "review_description": "Parent 1 review."},
    )
    assert r1.status_code == 201

    # User 2 submits
    set_mock_auth(user2)
    r2 = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 4, "review_description": "Parent 2 review."},
    )
    assert r2.status_code == 201


def test_create_review_same_user_different_centres_success(
    client: TestClient, sample_user: User, test_engine
):
    """A parent can review multiple different preschool centres without conflict."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(centre_code="CTR_DIFF_01", centre_name="Centre Alpha")
        c2 = PreschoolCentre(centre_code="CTR_DIFF_02", centre_name="Centre Beta")
        session.add_all([c1, c2])
        session.commit()
        session.refresh(c1)
        session.refresh(c2)

    set_mock_auth(sample_user)

    # Submit for c1
    r1 = client.post(
        f"/api/v1/centres/{c1.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5, "review_description": "Review for Alpha."},
    )
    assert r1.status_code == 201

    # Submit for c2
    r2 = client.post(
        f"/api/v1/centres/{c2.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 4, "review_description": "Review for Beta."},
    )
    assert r2.status_code == 201


def test_create_review_immediately_reflected_in_public_endpoints(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Reactive moderation ensures newly created review immediately updates public centre summary and list."""
    set_mock_auth(sample_user)

    # 1. Before review: review_summary has 0 reviews
    r_before = client.get(f"/api/v1/centres/{sample_centre.centre_id}")
    assert r_before.status_code == 200
    assert r_before.json()["review_summary"] == {"average_rating": None, "total_reviews": 0}

    # 2. Submit 5-star review
    r_post = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5, "review_description": "Immediate public feedback!"},
    )
    assert r_post.status_code == 201
    created_review_id = r_post.json()["review_id"]

    # 3. After review: centre profile reflects 1 review with 5.0 average
    r_after = client.get(f"/api/v1/centres/{sample_centre.centre_id}")
    assert r_after.status_code == 200
    assert r_after.json()["review_summary"] == {"average_rating": 5.0, "total_reviews": 1}

    # 4. Public centre reviews endpoint reflects the new review
    r_list = client.get(f"/api/v1/centres/{sample_centre.centre_id}/reviews")
    assert r_list.status_code == 200
    data_list = r_list.json()
    assert data_list["total"] == 1
    assert len(data_list["items"]) == 1
    assert data_list["items"][0]["review_id"] == created_review_id
    assert data_list["items"][0]["rating"] == 5
    assert data_list["items"][0]["comment"] == "Immediate public feedback!"


@pytest.fixture
def other_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="usr_parent_02",
            firebase_uid="firebase_parent_02",
            email="parent2@example.com",
            full_name="Bob Tan",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def sample_review(test_engine, sample_centre: PreschoolCentre, sample_user: User) -> Review:
    assert sample_centre.centre_id is not None
    assert sample_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id=str(uuid.uuid4()),
            centre_id=sample_centre.centre_id,
            user_id=sample_user.user_id,
            rating=4,
            review_description="Initial great experience.",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        return review


# ==============================================================================
# GET /api/v1/reviews/{review_id} Tests
# ==============================================================================


def test_get_review_by_id_success(
    client: TestClient, sample_review: Review
):
    """Retrieving an existing review by ID returns 200 OK with ReviewResponse without authentication."""
    response = client.get(f"/api/v1/reviews/{sample_review.review_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == sample_review.review_id
    assert data["centre_id"] == sample_review.centre_id
    assert data["user_id"] == sample_review.user_id
    assert data["rating"] == sample_review.rating
    assert data["review_description"] == sample_review.review_description
    assert data["moderation_status"] == ModerationStatus.APPROVED.value
    assert "created_at" in data


def test_get_review_by_id_not_found_returns_404(client: TestClient):
    """Querying a non-existent review ID returns HTTP 404 Not Found."""
    response = client.get("/api/v1/reviews/non-existent-review-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "Review not found"


# ==============================================================================
# PATCH /api/v1/reviews/{review_id} Tests
# ==============================================================================


def test_patch_review_missing_auth_token_returns_401(
    client: TestClient, sample_review: Review
):
    """Updating a review without Authorization header returns HTTP 401 Unauthorized."""
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        json={"rating": 5},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_patch_review_invalid_auth_scheme_returns_401(
    client: TestClient, sample_review: Review
):
    """Updating a review with invalid auth header returns HTTP 401 Unauthorized."""
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Basic invalid-token"},
        json={"rating": 5},
    )
    assert response.status_code == 401


def test_patch_review_suspended_user_returns_403(
    client: TestClient, sample_centre: PreschoolCentre, suspended_user: User, test_engine
):
    """A suspended user attempting to update a review returns HTTP 403 Forbidden."""
    assert sample_centre.centre_id is not None
    assert suspended_user.user_id is not None
    with Session(test_engine) as session:
        rev = Review(
            review_id=str(uuid.uuid4()),
            centre_id=sample_centre.centre_id,
            user_id=suspended_user.user_id,
            rating=3,
            review_description="Suspended user review.",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(rev)
        session.commit()
        session.refresh(rev)

    set_mock_auth(suspended_user)
    response = client.patch(
        f"/api/v1/reviews/{rev.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 4},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_patch_review_not_found_returns_404(
    client: TestClient, sample_user: User
):
    """Updating a non-existent review returns HTTP 404 Not Found."""
    set_mock_auth(sample_user)
    response = client.patch(
        "/api/v1/reviews/non-existent-review-id",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Review not found"


def test_patch_review_unauthorized_other_user_returns_403(
    client: TestClient, sample_review: Review, other_user: User
):
    """A user attempting to update another user's review returns HTTP 403 Forbidden."""
    set_mock_auth(other_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 2, "review_description": "Tampered description."},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "You do not have permission to edit this review"


def test_patch_review_success_update_rating_only(
    client: TestClient, sample_review: Review, sample_user: User, test_engine
):
    """Author successfully updates only the rating; description remains unchanged and status remains APPROVED."""
    set_mock_auth(sample_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 5},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == sample_review.review_id
    assert data["rating"] == 5
    assert data["review_description"] == sample_review.review_description
    assert data["moderation_status"] == ModerationStatus.APPROVED.value

    # Verify database state
    with Session(test_engine) as session:
        updated = session.get(Review, sample_review.review_id)
        assert updated is not None
        assert updated.rating == 5
        assert updated.review_description == sample_review.review_description
        assert updated.moderation_status == ModerationStatus.APPROVED


def test_patch_review_success_update_description_only(
    client: TestClient, sample_review: Review, sample_user: User, test_engine
):
    """Author successfully updates only the description; rating remains unchanged."""
    set_mock_auth(sample_user)
    new_desc = "Updated comprehensive review notes."
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"review_description": new_desc},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == sample_review.review_id
    assert data["rating"] == sample_review.rating
    assert data["review_description"] == new_desc
    assert data["moderation_status"] == ModerationStatus.APPROVED.value

    # Verify database state
    with Session(test_engine) as session:
        updated = session.get(Review, sample_review.review_id)
        assert updated is not None
        assert updated.rating == sample_review.rating
        assert updated.review_description == new_desc


def test_patch_review_success_update_both_fields(
    client: TestClient, sample_review: Review, sample_user: User, test_engine
):
    """Author successfully updates both rating and description simultaneously."""
    set_mock_auth(sample_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 1, "review_description": "Service deteriorated significantly."},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["rating"] == 1
    assert data["review_description"] == "Service deteriorated significantly."
    assert data["moderation_status"] == ModerationStatus.APPROVED.value

    # Verify database state
    with Session(test_engine) as session:
        updated = session.get(Review, sample_review.review_id)
        assert updated is not None
        assert updated.rating == 1
        assert updated.review_description == "Service deteriorated significantly."


def test_patch_review_success_clear_description(
    client: TestClient, sample_review: Review, sample_user: User, test_engine
):
    """Author explicitly sets review_description to null, clearing it."""
    set_mock_auth(sample_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"review_description": None},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["rating"] == sample_review.rating
    assert data["review_description"] is None

    # Verify database state
    with Session(test_engine) as session:
        updated = session.get(Review, sample_review.review_id)
        assert updated is not None
        assert updated.review_description is None


@pytest.mark.parametrize("invalid_rating", [0, -1, 6, 10, 100])
def test_patch_review_out_of_bounds_rating_returns_422(
    client: TestClient, sample_review: Review, sample_user: User, invalid_rating: int, test_engine
):
    """Submitting rating outside 1..5 range returns HTTP 422 and does not modify the review."""
    set_mock_auth(sample_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": invalid_rating},
    )
    assert response.status_code == 422

    with Session(test_engine) as session:
        unchanged = session.get(Review, sample_review.review_id)
        assert unchanged is not None
        assert unchanged.rating == sample_review.rating


@pytest.mark.parametrize("invalid_rating_type", ["five", 4.5, None])
def test_patch_review_invalid_rating_type_returns_422(
    client: TestClient, sample_review: Review, sample_user: User, invalid_rating_type: object, test_engine
):
    """Submitting non-integer or null rating returns HTTP 422 and does not modify the review."""
    set_mock_auth(sample_user)
    response = client.patch(
        f"/api/v1/reviews/{sample_review.review_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": invalid_rating_type},
    )
    assert response.status_code == 422

    with Session(test_engine) as session:
        unchanged = session.get(Review, sample_review.review_id)
        assert unchanged is not None
        assert unchanged.rating == sample_review.rating


def test_patch_review_recalculates_centre_aggregate_rating(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Updating a review's rating immediately affects the public centre summary average rating."""
    set_mock_auth(sample_user)

    # 1. Create initial review with rating 4
    r_post = client.post(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 4, "review_description": "Initial rating 4."},
    )
    assert r_post.status_code == 201
    rev_id = r_post.json()["review_id"]

    # Verify centre summary has 4.0
    r_centre1 = client.get(f"/api/v1/centres/{sample_centre.centre_id}")
    assert r_centre1.status_code == 200
    assert r_centre1.json()["review_summary"]["average_rating"] == 4.0

    # 2. Patch rating to 2
    r_patch = client.patch(
        f"/api/v1/reviews/{rev_id}",
        headers={"Authorization": "Bearer mock-token"},
        json={"rating": 2},
    )
    assert r_patch.status_code == 200

    # 3. Verify centre summary immediately reflects 2.0
    r_centre2 = client.get(f"/api/v1/centres/{sample_centre.centre_id}")
    assert r_centre2.status_code == 200
    assert r_centre2.json()["review_summary"]["average_rating"] == 2.0


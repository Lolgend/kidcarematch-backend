"""Tests for Preschool Centre Review Submission and Review API endpoints."""

import uuid
from datetime import datetime, timedelta, timezone

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


# ==============================================================================
# GET /api/v1/centres/{centre_id}/reviews/mine Tests
# ==============================================================================


def test_get_centre_review_mine_missing_auth_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Retrieving author review without Authorization header must return HTTP 401 Unauthorized."""
    response = client.get(f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine")
    assert response.status_code == 401


def test_get_centre_review_mine_suspended_user_returns_403(
    client: TestClient, sample_centre: PreschoolCentre, suspended_user: User
):
    """Retrieving author review by a suspended user must return HTTP 403 Forbidden."""
    set_mock_auth(suspended_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_get_centre_review_mine_centre_not_found_returns_404(
    client: TestClient, sample_user: User
):
    """Retrieving author review for non-existent centre must return HTTP 404 Not Found."""
    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/centres/99999/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_get_centre_review_mine_no_review_exists_returns_404(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Retrieving author review when no review was submitted must return HTTP 404 Not Found."""
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Review not found"


def test_get_centre_review_mine_success(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, test_engine
):
    """Retrieving author review when review exists returns HTTP 200 OK with ReviewResponse."""
    assert sample_centre.centre_id is not None
    assert sample_user.user_id is not None

    with Session(test_engine) as session:
        review = Review(
            review_id=str(uuid.uuid4()),
            centre_id=sample_centre.centre_id,
            user_id=sample_user.user_id,
            rating=5,
            review_description="Loved this centre!",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        review_id = review.review_id

    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == review_id
    assert data["centre_id"] == sample_centre.centre_id
    assert data["user_id"] == sample_user.user_id
    assert data["rating"] == 5
    assert data["review_description"] == "Loved this centre!"
    assert data["moderation_status"] == ModerationStatus.APPROVED.value


def test_get_centre_review_mine_does_not_return_other_user_review(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, test_engine
):
    """Retrieving author review isolates reviews per user, not leaking other users' reviews."""
    assert sample_centre.centre_id is not None

    with Session(test_engine) as session:
        other_user = User(
            user_id="usr_other_01",
            firebase_uid="fb_other_01",
            email="other@example.com",
            full_name="Bob Other",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(other_user)
        session.commit()
        session.refresh(other_user)
        assert other_user.user_id is not None

        review = Review(
            review_id=str(uuid.uuid4()),
            centre_id=sample_centre.centre_id,
            user_id=other_user.user_id,
            rating=4,
            review_description="Other user review",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(review)
        session.commit()

    # Querying as sample_user should return 404 since sample_user hasn't reviewed
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Review not found"


def test_get_centre_review_mine_returns_own_review_regardless_of_moderation_status(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User, test_engine
):
    """Author can retrieve their own review even if moderation_status is PENDING or REJECTED."""
    assert sample_centre.centre_id is not None
    assert sample_user.user_id is not None

    with Session(test_engine) as session:
        review = Review(
            review_id=str(uuid.uuid4()),
            centre_id=sample_centre.centre_id,
            user_id=sample_user.user_id,
            rating=2,
            review_description="Under review commentary",
            moderation_status=ModerationStatus.PENDING,
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        review_id = review.review_id

    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/reviews/mine",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == review_id
    assert data["moderation_status"] == ModerationStatus.PENDING.value


# ==============================================================================
# GET /api/v1/reviews/me Tests
# ==============================================================================


def test_get_my_reviews_missing_auth_returns_401(client: TestClient):
    """Accessing parent review portfolio without auth header must return HTTP 401 Unauthorized."""
    response = client.get("/api/v1/reviews/me")
    assert response.status_code == 401


def test_get_my_reviews_suspended_user_returns_403(
    client: TestClient, suspended_user: User
):
    """Accessing parent review portfolio with suspended account must return HTTP 403 Forbidden."""
    set_mock_auth(suspended_user)
    response = client.get(
        "/api/v1/reviews/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_get_my_reviews_empty_returns_200(client: TestClient, sample_user: User):
    """When parent has not authored any reviews, returns 200 OK with empty items list and total 0."""
    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/reviews/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


def test_get_my_reviews_success_with_centre_metadata(
    client: TestClient, sample_user: User, test_engine
):
    """Returns parent portfolio reviews enriched with centre metadata (centre_name)."""
    assert sample_user.user_id is not None

    with Session(test_engine) as session:
        c1 = PreschoolCentre(centre_code="CTR_P_01", centre_name="Sunflower Montessori")
        c2 = PreschoolCentre(centre_code="CTR_P_02", centre_name="Rainbow Preschool")
        session.add_all([c1, c2])
        session.commit()
        session.refresh(c1)
        session.refresh(c2)
        assert c1.centre_id is not None
        assert c2.centre_id is not None

        r1 = Review(
            review_id=str(uuid.uuid4()),
            centre_id=c1.centre_id,
            user_id=sample_user.user_id,
            rating=5,
            review_description="Outstanding teachers!",
            moderation_status=ModerationStatus.APPROVED,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        r2 = Review(
            review_id=str(uuid.uuid4()),
            centre_id=c2.centre_id,
            user_id=sample_user.user_id,
            rating=3,
            review_description="Average facilities.",
            moderation_status=ModerationStatus.PENDING,
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.add_all([r1, r2])
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/reviews/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Newest created_at should be first (r2 was created 1 day ago, r1 was created 2 days ago)
    assert data["items"][0]["centre_name"] == "Rainbow Preschool"
    assert data["items"][0]["rating"] == 3
    assert data["items"][0]["review_description"] == "Average facilities."
    assert data["items"][0]["moderation_status"] == ModerationStatus.PENDING.value

    assert data["items"][1]["centre_name"] == "Sunflower Montessori"
    assert data["items"][1]["rating"] == 5
    assert data["items"][1]["review_description"] == "Outstanding teachers!"
    assert data["items"][1]["moderation_status"] == ModerationStatus.APPROVED.value


def test_get_my_reviews_user_isolation(
    client: TestClient, sample_user: User, test_engine
):
    """Parent portfolio only returns reviews written by current authenticated user."""
    assert sample_user.user_id is not None

    with Session(test_engine) as session:
        centre = PreschoolCentre(centre_code="CTR_ISO_01", centre_name="Isolation Centre")
        other_user = User(
            user_id="usr_iso_other",
            firebase_uid="fb_iso_other",
            email="iso_other@example.com",
            full_name="Other Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add_all([centre, other_user])
        session.commit()
        session.refresh(centre)
        session.refresh(other_user)
        assert centre.centre_id is not None
        assert other_user.user_id is not None

        r_mine = Review(
            review_id=str(uuid.uuid4()),
            centre_id=centre.centre_id,
            user_id=sample_user.user_id,
            rating=4,
            review_description="My review",
            moderation_status=ModerationStatus.APPROVED,
        )
        r_other = Review(
            review_id=str(uuid.uuid4()),
            centre_id=centre.centre_id,
            user_id=other_user.user_id,
            rating=1,
            review_description="Other's review",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add_all([r_mine, r_other])
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/reviews/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["review_description"] == "My review"


def test_get_my_reviews_pagination_and_ordering(
    client: TestClient, sample_user: User, test_engine
):
    """Reviews pagination supports limit and offset parameters and orders desc by created_at."""
    assert sample_user.user_id is not None

    with Session(test_engine) as session:
        centres = []
        for i in range(5):
            c = PreschoolCentre(centre_code=f"CTR_PAG_{i}", centre_name=f"Centre {i}")
            session.add(c)
            centres.append(c)
        session.commit()
        for c in centres:
            session.refresh(c)

        now = datetime.now(timezone.utc)
        reviews = []
        for i, c in enumerate(centres):
            assert c.centre_id is not None
            r = Review(
                review_id=f"rev_pag_{i}",
                centre_id=c.centre_id,
                user_id=sample_user.user_id,
                rating=i + 1,
                review_description=f"Review #{i}",
                moderation_status=ModerationStatus.APPROVED,
                created_at=now + timedelta(minutes=i),
            )
            reviews.append(r)
            session.add(r)
        session.commit()

    set_mock_auth(sample_user)

    # Page 1: limit=2, offset=0 (should have reviews 4 and 3)
    r_page1 = client.get(
        "/api/v1/reviews/me?limit=2&offset=0",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert r_page1.status_code == 200
    d1 = r_page1.json()
    assert d1["total"] == 5
    assert d1["limit"] == 2
    assert d1["offset"] == 0
    assert len(d1["items"]) == 2
    assert d1["items"][0]["review_id"] == "rev_pag_4"
    assert d1["items"][1]["review_id"] == "rev_pag_3"

    # Page 2: limit=2, offset=2 (should have reviews 2 and 1)
    r_page2 = client.get(
        "/api/v1/reviews/me?limit=2&offset=2",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert r_page2.status_code == 200
    d2 = r_page2.json()
    assert d2["total"] == 5
    assert len(d2["items"]) == 2
    assert d2["items"][0]["review_id"] == "rev_pag_2"
    assert d2["items"][1]["review_id"] == "rev_pag_1"

    # Page 3: limit=2, offset=4 (should have review 0)
    r_page3 = client.get(
        "/api/v1/reviews/me?limit=2&offset=4",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert r_page3.status_code == 200
    d3 = r_page3.json()
    assert d3["total"] == 5
    assert len(d3["items"]) == 1
    assert d3["items"][0]["review_id"] == "rev_pag_0"


@pytest.mark.parametrize(
    "invalid_param",
    [
        "limit=0",
        "limit=-5",
        "limit=101",
        "offset=-1",
    ],
)
def test_get_my_reviews_pagination_validation(
    client: TestClient, sample_user: User, invalid_param: str
):
    """Invalid limit (<1 or >100) or offset (<0) returns HTTP 422 Unprocessable Entity."""
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/reviews/me?{invalid_param}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


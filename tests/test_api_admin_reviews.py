"""Tests for Administrative Review Moderation and Purge API endpoints."""

from datetime import datetime, timezone

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
def admin_user(test_engine) -> User:
    with Session(test_engine) as session:
        admin = User(
            user_id="admin_user_01",
            firebase_uid="firebase_admin_01",
            email="moderator@kidcarematch.sg",
            full_name="Admin Moderator",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            admin_level=1,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


@pytest.fixture
def parent_user(test_engine) -> User:
    with Session(test_engine) as session:
        parent = User(
            user_id="parent_user_01",
            firebase_uid="firebase_parent_01",
            email="parent1@example.com",
            full_name="Parent One",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return parent


@pytest.fixture
def parent_user_two(test_engine) -> User:
    with Session(test_engine) as session:
        parent = User(
            user_id="parent_user_02",
            firebase_uid="firebase_parent_02",
            email="parent2@example.com",
            full_name="Parent Two",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return parent


@pytest.fixture
def suspended_admin(test_engine) -> User:
    with Session(test_engine) as session:
        admin = User(
            user_id="admin_suspended_01",
            firebase_uid="firebase_admin_suspended_01",
            email="suspended_admin@kidcarematch.sg",
            full_name="Suspended Admin",
            role=UserRole.ADMIN,
            account_status=AccountStatus.SUSPENDED,
            admin_level=2,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


@pytest.fixture
def sample_centre_one(test_engine) -> PreschoolCentre:
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_MOD_01",
            centre_name="Little Gems Academy",
            postal="123456",
            address="10 Orchard Road",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        return centre


@pytest.fixture
def sample_centre_two(test_engine) -> PreschoolCentre:
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_MOD_02",
            centre_name="Sunshine Preschool",
            postal="654321",
            address="20 Tampines Ave",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        return centre


def set_mock_auth(user: User):
    mock_claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims


# ==============================================================================
# Security & Authorization Guard Tests
# ==============================================================================


def test_admin_reviews_endpoints_require_auth(client: TestClient):
    """Endpoints under /api/v1/admin/reviews must return 401 without Bearer token."""
    res_get = client.get("/api/v1/admin/reviews")
    assert res_get.status_code == 401

    res_patch = client.patch(
        "/api/v1/admin/reviews/rev-1/status",
        json={"moderation_status": "APPROVED"},
    )
    assert res_patch.status_code == 401

    res_del = client.delete("/api/v1/admin/reviews/rev-1")
    assert res_del.status_code == 401


def test_admin_reviews_endpoints_forbidden_for_parent(
    client: TestClient, parent_user: User
):
    """Endpoints under /api/v1/admin/reviews must return 403 for non-admin users."""
    set_mock_auth(parent_user)
    headers = {"Authorization": "Bearer mock-token"}

    res_get = client.get("/api/v1/admin/reviews", headers=headers)
    assert res_get.status_code == 403
    assert "administrative privileges required" in res_get.json()["detail"].lower()

    res_patch = client.patch(
        "/api/v1/admin/reviews/rev-1/status",
        headers=headers,
        json={"moderation_status": "APPROVED"},
    )
    assert res_patch.status_code == 403

    res_del = client.delete("/api/v1/admin/reviews/rev-1", headers=headers)
    assert res_del.status_code == 403


def test_admin_reviews_endpoints_forbidden_for_suspended_admin(
    client: TestClient, suspended_admin: User
):
    """Endpoints under /api/v1/admin/reviews must return 403 for suspended administrators."""
    set_mock_auth(suspended_admin)
    headers = {"Authorization": "Bearer mock-token"}

    res_get = client.get("/api/v1/admin/reviews", headers=headers)
    assert res_get.status_code == 403
    assert "suspended" in res_get.json()["detail"].lower()

    res_patch = client.patch(
        "/api/v1/admin/reviews/rev-1/status",
        headers=headers,
        json={"moderation_status": "APPROVED"},
    )
    assert res_patch.status_code == 403

    res_del = client.delete("/api/v1/admin/reviews/rev-1", headers=headers)
    assert res_del.status_code == 403


# ==============================================================================
# GET /api/v1/admin/reviews Tests (Listing, Enrichment, Filtering, Pagination)
# ==============================================================================


def test_list_admin_reviews_empty(client: TestClient, admin_user: User):
    """GET /api/v1/admin/reviews returns empty list when no reviews exist."""
    set_mock_auth(admin_user)
    response = client.get(
        "/api/v1/admin/reviews",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


def test_list_admin_reviews_enrichment(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    """GET /api/v1/admin/reviews enriches items with centre_name, full_name, and email."""
    assert sample_centre_one.centre_id is not None
    assert parent_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id="rev-enrich-01",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=5,
            review_description="Outstanding teachers and environment!",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    response = client.get(
        "/api/v1/admin/reviews",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["review_id"] == "rev-enrich-01"
    assert item["centre_id"] == sample_centre_one.centre_id
    assert item["centre_name"] == "Little Gems Academy"
    assert item["user_id"] == parent_user.user_id
    assert item["full_name"] == "Parent One"
    assert item["email"] == "parent1@example.com"
    assert item["rating"] == 5
    assert item["review_description"] == "Outstanding teachers and environment!"
    assert item["moderation_status"] == "APPROVED"
    assert item["moderated_at"] is None
    assert item["moderated_by"] is None
    assert "created_at" in item


def test_list_admin_reviews_pagination(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    """GET /api/v1/admin/reviews pagination supports limit and offset parameters."""
    with Session(test_engine) as session:
        # Create additional users and centres for distinct reviews
        for i in range(1, 6):
            u = User(
                user_id=f"usr_page_{i}",
                firebase_uid=f"firebase_page_{i}",
                email=f"page_user_{i}@example.com",
                full_name=f"Page User {i}",
                role=UserRole.PARENT,
            )
            session.add(u)
            session.commit()
            assert sample_centre_one.centre_id is not None
            assert u.user_id is not None
            review = Review(
                review_id=f"rev_page_{i}",
                centre_id=sample_centre_one.centre_id,
                user_id=u.user_id,
                rating=i,
                review_description=f"Review number {i}",
                moderation_status=ModerationStatus.APPROVED,
            )
            session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    headers = {"Authorization": "Bearer mock-token"}

    # Fetch page 1 (limit 2, offset 0)
    res1 = client.get("/api/v1/admin/reviews?limit=2&offset=0", headers=headers)
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["total"] == 5
    assert len(d1["items"]) == 2
    assert d1["limit"] == 2
    assert d1["offset"] == 0

    # Fetch page 2 (limit 2, offset 2)
    res2 = client.get("/api/v1/admin/reviews?limit=2&offset=2", headers=headers)
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["total"] == 5
    assert len(d2["items"]) == 2
    assert d2["offset"] == 2

    # Assert no overlap between page 1 and page 2
    page1_ids = {item["review_id"] for item in d1["items"]}
    page2_ids = {item["review_id"] for item in d2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_list_admin_reviews_pagination_validation(client: TestClient, admin_user: User):
    """Limit must be between 1 and 100, and offset must be non-negative."""
    set_mock_auth(admin_user)
    headers = {"Authorization": "Bearer mock-token"}

    # Limit = 0 -> 422
    assert (
        client.get("/api/v1/admin/reviews?limit=0", headers=headers).status_code == 422
    )
    # Limit = 101 -> 422
    assert (
        client.get("/api/v1/admin/reviews?limit=101", headers=headers).status_code
        == 422
    )
    # Offset = -1 -> 422
    assert (
        client.get("/api/v1/admin/reviews?offset=-1", headers=headers).status_code
        == 422
    )
    # Rating < 1 -> 422
    assert (
        client.get("/api/v1/admin/reviews?rating=0", headers=headers).status_code == 422
    )
    # Rating > 5 -> 422
    assert (
        client.get("/api/v1/admin/reviews?rating=6", headers=headers).status_code == 422
    )
    # Invalid moderation_status -> 422
    assert (
        client.get(
            "/api/v1/admin/reviews?moderation_status=INVALID_STATUS", headers=headers
        ).status_code
        == 422
    )


def test_list_admin_reviews_filtering(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    parent_user_two: User,
    sample_centre_one: PreschoolCentre,
    sample_centre_two: PreschoolCentre,
):
    assert sample_centre_one.centre_id is not None
    assert sample_centre_two.centre_id is not None
    assert parent_user.user_id is not None
    assert parent_user_two.user_id is not None
    with Session(test_engine) as session:
        r1 = Review(
            review_id="rev_filter_01",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=5,
            review_description="5-star approved for centre 1",
            moderation_status=ModerationStatus.APPROVED,
        )
        r2 = Review(
            review_id="rev_filter_02",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user_two.user_id,
            rating=1,
            review_description="1-star pending for centre 1",
            moderation_status=ModerationStatus.PENDING,
        )
        # Create third user for centre 2 review
        u3 = User(
            user_id="parent_user_03",
            firebase_uid="firebase_parent_03",
            email="parent3@example.com",
            full_name="Parent Three",
            role=UserRole.PARENT,
        )
        session.add(u3)
        session.commit()
        assert u3.user_id is not None
        r3 = Review(
            review_id="rev_filter_03",
            centre_id=sample_centre_two.centre_id,
            user_id=u3.user_id,
            rating=5,
            review_description="5-star rejected for centre 2",
            moderation_status=ModerationStatus.REJECTED,
        )
        session.add_all([r1, r2, r3])
        session.commit()

    set_mock_auth(admin_user)
    headers = {"Authorization": "Bearer mock-token"}

    # Filter by moderation_status=PENDING
    res_pending = client.get(
        "/api/v1/admin/reviews?moderation_status=PENDING", headers=headers
    )
    assert res_pending.status_code == 200
    d_pending = res_pending.json()
    assert d_pending["total"] == 1
    assert d_pending["items"][0]["review_id"] == "rev_filter_02"

    # Filter by moderation_status=REJECTED
    res_rejected = client.get(
        "/api/v1/admin/reviews?moderation_status=REJECTED", headers=headers
    )
    assert res_rejected.status_code == 200
    d_rejected = res_rejected.json()
    assert d_rejected["total"] == 1
    assert d_rejected["items"][0]["review_id"] == "rev_filter_03"

    # Filter by centre_id = sample_centre_one
    res_centre1 = client.get(
        f"/api/v1/admin/reviews?centre_id={sample_centre_one.centre_id}",
        headers=headers,
    )
    assert res_centre1.status_code == 200
    assert res_centre1.json()["total"] == 2

    # Filter by rating = 5
    res_rating5 = client.get("/api/v1/admin/reviews?rating=5", headers=headers)
    assert res_rating5.status_code == 200
    assert res_rating5.json()["total"] == 2

    # Combined filter: centre_id=sample_centre_one AND rating=5
    res_comb = client.get(
        f"/api/v1/admin/reviews?centre_id={sample_centre_one.centre_id}&rating=5",
        headers=headers,
    )
    assert res_comb.status_code == 200
    d_comb = res_comb.json()
    assert d_comb["total"] == 1
    assert d_comb["items"][0]["review_id"] == "rev_filter_01"


# ==============================================================================
# PATCH /api/v1/admin/reviews/{review_id}/status Tests
# ==============================================================================


def test_patch_review_status_success(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    """PATCH updates moderation_status, records audit stamps (moderated_at, moderated_by), and returns 200."""
    assert sample_centre_one.centre_id is not None
    assert parent_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id="rev_patch_01",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=2,
            review_description="Spam or inappropriate text",
            moderation_status=ModerationStatus.PENDING,
        )
        session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    headers = {"Authorization": "Bearer mock-token"}

    before_time = datetime.now(timezone.utc)
    response = client.patch(
        "/api/v1/admin/reviews/rev_patch_01/status",
        headers=headers,
        json={"moderation_status": "REJECTED"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["review_id"] == "rev_patch_01"
    assert data["moderation_status"] == "REJECTED"
    assert data["moderated_by"] == admin_user.user_id
    assert data["moderated_at"] is not None

    raw_moderated_at = data["moderated_at"].replace("Z", "+00:00")
    moderated_at = datetime.fromisoformat(raw_moderated_at)
    # Convert to UTC comparison
    if moderated_at.tzinfo is None:
        moderated_at = moderated_at.replace(tzinfo=timezone.utc)
    assert moderated_at >= before_time

    # Verify database persistence
    with Session(test_engine) as session:
        db_review = session.get(Review, "rev_patch_01")
        assert db_review is not None
        assert db_review.moderation_status == ModerationStatus.REJECTED
        assert db_review.moderated_by == admin_user.user_id
        assert db_review.moderated_at is not None


def test_patch_review_status_to_approved(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    """Admin can transition a PENDING or REJECTED review to APPROVED."""
    assert sample_centre_one.centre_id is not None
    assert parent_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id="rev_patch_02",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=4,
            review_description="Legitimate review after investigation",
            moderation_status=ModerationStatus.REJECTED,
        )
        session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    response = client.patch(
        "/api/v1/admin/reviews/rev_patch_02/status",
        headers={"Authorization": "Bearer mock-token"},
        json={"moderation_status": "APPROVED"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["moderation_status"] == "APPROVED"
    assert data["moderated_by"] == admin_user.user_id


def test_patch_review_status_not_found_returns_404(
    client: TestClient, admin_user: User
):
    """PATCH status for non-existent review ID must return HTTP 404 Not Found."""
    set_mock_auth(admin_user)
    response = client.patch(
        "/api/v1/admin/reviews/non-existent-review-id/status",
        headers={"Authorization": "Bearer mock-token"},
        json={"moderation_status": "REJECTED"},
    )
    assert response.status_code == 404
    assert "review not found" in response.json()["detail"].lower()


def test_patch_review_status_invalid_status_returns_422(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    """PATCH with invalid status value must return HTTP 422 Unprocessable Entity."""
    assert sample_centre_one.centre_id is not None
    assert parent_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id="rev_patch_03",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=3,
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    response = client.patch(
        "/api/v1/admin/reviews/rev_patch_03/status",
        headers={"Authorization": "Bearer mock-token"},
        json={"moderation_status": "BANNED"},
    )
    assert response.status_code == 422


# ==============================================================================
# DELETE /api/v1/admin/reviews/{review_id} Tests (Administrative Hard Deletion)
# ==============================================================================


def test_delete_review_success(
    test_engine,
    client: TestClient,
    admin_user: User,
    parent_user: User,
    sample_centre_one: PreschoolCentre,
):
    assert sample_centre_one.centre_id is not None
    assert parent_user.user_id is not None
    with Session(test_engine) as session:
        review = Review(
            review_id="rev_del_01",
            centre_id=sample_centre_one.centre_id,
            user_id=parent_user.user_id,
            rating=1,
            review_description="Malicious abuse spam to be purged",
            moderation_status=ModerationStatus.REJECTED,
        )
        session.add(review)
        session.commit()

    set_mock_auth(admin_user)
    response = client.delete(
        "/api/v1/admin/reviews/rev_del_01",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 204

    # Verify review row no longer exists in database
    with Session(test_engine) as session:
        db_review = session.get(Review, "rev_del_01")
        assert db_review is None


def test_delete_review_not_found_returns_404(client: TestClient, admin_user: User):
    """Admin DELETE for non-existent review ID must return HTTP 404 Not Found."""
    set_mock_auth(admin_user)
    response = client.delete(
        "/api/v1/admin/reviews/non-existent-review-id",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert "review not found" in response.json()["detail"].lower()


# ==============================================================================
# Rating Isolation & Public API Consistency Tests
# ==============================================================================


def test_rating_isolation_upon_rejection_and_deletion(
    test_engine,
    client: TestClient,
    admin_user: User,
    sample_centre_one: PreschoolCentre,
):
    """Verify that transitioning to REJECTED and hard deletion isolate reviews from public queries & ratings."""
    assert sample_centre_one.centre_id is not None
    # Seed 3 parents and 3 reviews for sample_centre_one:
    # Review A: 5 stars (APPROVED)
    # Review B: 1 star (APPROVED initially)
    # Review C: 4 stars (APPROVED)
    with Session(test_engine) as session:
        u_a = User(
            user_id="user_iso_a",
            firebase_uid="fb_iso_a",
            email="iso_a@example.com",
            full_name="User A",
        )
        u_b = User(
            user_id="user_iso_b",
            firebase_uid="fb_iso_b",
            email="iso_b@example.com",
            full_name="User B",
        )
        u_c = User(
            user_id="user_iso_c",
            firebase_uid="fb_iso_c",
            email="iso_c@example.com",
            full_name="User C",
        )
        session.add_all([u_a, u_b, u_c])
        session.commit()
        assert u_a.user_id is not None
        assert u_b.user_id is not None
        assert u_c.user_id is not None

        r_a = Review(
            review_id="rev_iso_a",
            centre_id=sample_centre_one.centre_id,
            user_id=u_a.user_id,
            rating=5,
            review_description="5 stars great",
            moderation_status=ModerationStatus.APPROVED,
        )
        r_b = Review(
            review_id="rev_iso_b",
            centre_id=sample_centre_one.centre_id,
            user_id=u_b.user_id,
            rating=1,
            review_description="1 star fake",
            moderation_status=ModerationStatus.APPROVED,
        )
        r_c = Review(
            review_id="rev_iso_c",
            centre_id=sample_centre_one.centre_id,
            user_id=u_c.user_id,
            rating=4,
            review_description="4 stars good",
            moderation_status=ModerationStatus.APPROVED,
        )
        session.add_all([r_a, r_b, r_c])
        session.commit()

    # Public check: initially all 3 are approved -> avg = (5 + 1 + 4) / 3 = 3.33
    pub_detail = client.get(f"/api/v1/centres/{sample_centre_one.centre_id}")
    assert pub_detail.status_code == 200
    summary = pub_detail.json()["review_summary"]
    assert summary["total_reviews"] == 3
    assert summary["average_rating"] == 3.33

    pub_reviews = client.get(f"/api/v1/centres/{sample_centre_one.centre_id}/reviews")
    assert pub_reviews.status_code == 200
    assert pub_reviews.json()["total"] == 3

    # Step 1: Admin rejects Review B (1-star)
    set_mock_auth(admin_user)
    headers = {"Authorization": "Bearer mock-token"}

    res_patch = client.patch(
        "/api/v1/admin/reviews/rev_iso_b/status",
        headers=headers,
        json={"moderation_status": "REJECTED"},
    )
    assert res_patch.status_code == 200

    # Public check: Review B must now be completely excluded!
    # Remaining: Review A (5), Review C (4) -> avg = (5 + 4) / 2 = 4.5
    pub_detail_after_reject = client.get(
        f"/api/v1/centres/{sample_centre_one.centre_id}"
    )
    assert pub_detail_after_reject.status_code == 200
    summary_after_reject = pub_detail_after_reject.json()["review_summary"]
    assert summary_after_reject["total_reviews"] == 2
    assert summary_after_reject["average_rating"] == 4.5

    pub_reviews_after_reject = client.get(
        f"/api/v1/centres/{sample_centre_one.centre_id}/reviews"
    )
    assert pub_reviews_after_reject.status_code == 200
    pub_rev_data = pub_reviews_after_reject.json()
    assert pub_rev_data["total"] == 2
    returned_ids = {item["review_id"] for item in pub_rev_data["items"]}
    assert "rev_iso_b" not in returned_ids
    assert returned_ids == {"rev_iso_a", "rev_iso_c"}

    # Step 2: Admin permanently deletes Review A (5-star)
    res_del = client.delete("/api/v1/admin/reviews/rev_iso_a", headers=headers)
    assert res_del.status_code == 204

    # Public check: Review A is deleted, only Review C (4-star) remains!
    pub_detail_after_del = client.get(f"/api/v1/centres/{sample_centre_one.centre_id}")
    assert pub_detail_after_del.status_code == 200
    summary_after_del = pub_detail_after_del.json()["review_summary"]
    assert summary_after_del["total_reviews"] == 1
    assert summary_after_del["average_rating"] == 4.0

    pub_reviews_after_del = client.get(
        f"/api/v1/centres/{sample_centre_one.centre_id}/reviews"
    )
    assert pub_reviews_after_del.status_code == 200
    assert pub_reviews_after_del.json()["total"] == 1
    assert pub_reviews_after_del.json()["items"][0]["review_id"] == "rev_iso_c"

    # Step 3: Admin sets Review C to PENDING
    res_pending = client.patch(
        "/api/v1/admin/reviews/rev_iso_c/status",
        headers=headers,
        json={"moderation_status": "PENDING"},
    )
    assert res_pending.status_code == 200

    # Public check: No approved reviews remain -> avg = None, total = 0
    pub_detail_pending = client.get(f"/api/v1/centres/{sample_centre_one.centre_id}")
    assert pub_detail_pending.status_code == 200
    summary_pending = pub_detail_pending.json()["review_summary"]
    assert summary_pending["total_reviews"] == 0
    assert summary_pending["average_rating"] is None

    pub_reviews_pending = client.get(
        f"/api/v1/centres/{sample_centre_one.centre_id}/reviews"
    )
    assert pub_reviews_pending.status_code == 200
    assert pub_reviews_pending.json()["total"] == 0
    assert pub_reviews_pending.json()["items"] == []

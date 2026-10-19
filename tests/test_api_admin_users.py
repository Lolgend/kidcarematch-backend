"""Tests for administrative user management and provisioning endpoints."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth  # type: ignore[import-untyped]
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    CitizenshipType,
    FamilyPreferenceProfile,
    PreschoolCentre,
    PreschoolLevel,
    Review,
    User,
    UserRole,
    WatchlistEntry,
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
def seed_admin_user(test_engine):
    with Session(test_engine) as session:
        admin = User(
            user_id="admin-uuid-1",
            firebase_uid="firebase_admin_123",
            email="admin@kidcarematch.sg",
            full_name="Admin Boss",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            admin_level=1,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


@pytest.fixture
def seed_parent_user(test_engine):
    with Session(test_engine) as session:
        parent = User(
            user_id="parent-uuid-1",
            firebase_uid="firebase_parent_123",
            email="parent@example.com",
            full_name="Parent User",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return parent


@pytest.fixture
def seed_suspended_admin(test_engine):
    with Session(test_engine) as session:
        admin = User(
            user_id="admin-suspended-1",
            firebase_uid="firebase_suspended_admin_123",
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
def admin_headers(seed_admin_user):
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid,
        email=seed_admin_user.email,
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    return {"Authorization": "Bearer mock-admin-token"}


@pytest.fixture
def parent_headers(seed_parent_user):
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid,
        email=seed_parent_user.email,
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    return {"Authorization": "Bearer mock-parent-token"}


@pytest.fixture
def suspended_admin_headers(seed_suspended_admin):
    mock_claims = FirebaseTokenClaims(
        uid=seed_suspended_admin.firebase_uid,
        email=seed_suspended_admin.email,
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    return {"Authorization": "Bearer mock-suspended-admin-token"}


# ==============================================================================
# Security & Authorization Boundary Tests
# ==============================================================================


@pytest.mark.parametrize(
    "method,endpoint,payload",
    [
        ("GET", "/api/v1/admin/users", None),
        ("GET", "/api/v1/admin/users/user-123", None),
        ("POST", "/api/v1/admin/users", {"email": "new@test.com", "full_name": "New"}),
        ("PUT", "/api/v1/admin/users/user-123", {"full_name": "Updated"}),
        ("PATCH", "/api/v1/admin/users/user-123/deactivate", None),
        ("PATCH", "/api/v1/admin/users/user-123/reactivate", None),
        ("DELETE", "/api/v1/admin/users/user-123", None),
    ],
)
def test_admin_users_unauthenticated_returns_401(client: TestClient, method, endpoint, payload):
    """Accessing any user administration endpoint without a Bearer token must return HTTP 401."""
    if method == "GET":
        resp = client.get(endpoint)
    elif method == "POST":
        resp = client.post(endpoint, json=payload)
    elif method == "PUT":
        resp = client.put(endpoint, json=payload)
    elif method == "PATCH":
        resp = client.patch(endpoint)
    elif method == "DELETE":
        resp = client.delete(endpoint)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method,endpoint,payload",
    [
        ("GET", "/api/v1/admin/users", None),
        ("GET", "/api/v1/admin/users/user-123", None),
        ("POST", "/api/v1/admin/users", {"email": "new@test.com", "full_name": "New"}),
        ("PUT", "/api/v1/admin/users/user-123", {"full_name": "Updated"}),
        ("PATCH", "/api/v1/admin/users/user-123/deactivate", None),
        ("PATCH", "/api/v1/admin/users/user-123/reactivate", None),
        ("DELETE", "/api/v1/admin/users/user-123", None),
    ],
)
def test_admin_users_parent_role_returns_403(
    client: TestClient, parent_headers, method, endpoint, payload
):
    """Authenticated non-admin user (PARENT) must receive HTTP 403 Forbidden."""
    if method == "GET":
        resp = client.get(endpoint, headers=parent_headers)
    elif method == "POST":
        resp = client.post(endpoint, json=payload, headers=parent_headers)
    elif method == "PUT":
        resp = client.put(endpoint, json=payload, headers=parent_headers)
    elif method == "PATCH":
        resp = client.patch(endpoint, headers=parent_headers)
    elif method == "DELETE":
        resp = client.delete(endpoint, headers=parent_headers)
    assert resp.status_code == 403
    assert "administrative privileges required" in resp.json()["detail"].lower()


@pytest.mark.parametrize(
    "method,endpoint,payload",
    [
        ("GET", "/api/v1/admin/users", None),
        ("GET", "/api/v1/admin/users/user-123", None),
        ("POST", "/api/v1/admin/users", {"email": "new@test.com", "full_name": "New"}),
        ("PUT", "/api/v1/admin/users/user-123", {"full_name": "Updated"}),
        ("PATCH", "/api/v1/admin/users/user-123/deactivate", None),
        ("PATCH", "/api/v1/admin/users/user-123/reactivate", None),
        ("DELETE", "/api/v1/admin/users/user-123", None),
    ],
)
def test_admin_users_suspended_admin_returns_403(
    client: TestClient, suspended_admin_headers, method, endpoint, payload
):
    """Suspended administrator must receive HTTP 403 Forbidden."""
    if method == "GET":
        resp = client.get(endpoint, headers=suspended_admin_headers)
    elif method == "POST":
        resp = client.post(endpoint, json=payload, headers=suspended_admin_headers)
    elif method == "PUT":
        resp = client.put(endpoint, json=payload, headers=suspended_admin_headers)
    elif method == "PATCH":
        resp = client.patch(endpoint, headers=suspended_admin_headers)
    elif method == "DELETE":
        resp = client.delete(endpoint, headers=suspended_admin_headers)
    assert resp.status_code == 403
    assert "suspended" in resp.json()["detail"].lower()


# ==============================================================================
# GET /api/v1/admin/users (List, Search, Pagination, Filtering)
# ==============================================================================


def test_admin_list_users_pagination_and_total(client: TestClient, admin_headers, test_engine):
    """Admin users list must return paginated items with accurate total count."""
    with Session(test_engine) as session:
        for i in range(15):
            u = User(
                user_id=f"test-parent-{i}",
                firebase_uid=f"fb_parent_list_{i}",
                email=f"parent_{i}@example.com",
                full_name=f"Parent Number {i}",
                role=UserRole.PARENT,
                account_status=AccountStatus.ACTIVE,
            )
            session.add(u)
        session.commit()

    # Default pagination: limit=20, offset=0
    # 1 admin from fixture + 15 parents = 16 users total
    resp = client.get("/api/v1/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 16
    assert data["limit"] == 20
    assert data["offset"] == 0
    assert len(data["items"]) == 16

    # Specific limit and offset
    resp_slice = client.get("/api/v1/admin/users?limit=5&offset=2", headers=admin_headers)
    assert resp_slice.status_code == 200
    slice_data = resp_slice.json()
    assert slice_data["total"] == 16
    assert slice_data["limit"] == 5
    assert slice_data["offset"] == 2
    assert len(slice_data["items"]) == 5


def test_admin_list_users_filter_by_role(client: TestClient, admin_headers, test_engine):
    """Admin users list must support role filtering for PARENT and ADMIN."""
    with Session(test_engine) as session:
        admin_extra = User(
            user_id="admin-extra-2",
            firebase_uid="fb_admin_extra_2",
            email="extra_admin@kidcarematch.sg",
            full_name="Second Admin",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            admin_level=2,
        )
        parent_extra = User(
            user_id="parent-extra-1",
            firebase_uid="fb_parent_extra_1",
            email="extra_parent@example.com",
            full_name="Solo Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add_all([admin_extra, parent_extra])
        session.commit()

    # Filter ADMIN
    resp_admin = client.get("/api/v1/admin/users?role=ADMIN", headers=admin_headers)
    assert resp_admin.status_code == 200
    admin_data = resp_admin.json()
    assert admin_data["total"] == 2
    assert all(item["role"] == "ADMIN" for item in admin_data["items"])

    # Filter PARENT
    resp_parent = client.get("/api/v1/admin/users?role=PARENT", headers=admin_headers)
    assert resp_parent.status_code == 200
    parent_data = resp_parent.json()
    assert parent_data["total"] == 1
    assert all(item["role"] == "PARENT" for item in parent_data["items"])


def test_admin_list_users_search_query(client: TestClient, admin_headers, test_engine):
    """Admin users list must search case-insensitively across full_name and email."""
    with Session(test_engine) as session:
        u1 = User(
            user_id="search-u1",
            firebase_uid="fb_search_1",
            email="sarah.tan@family.sg",
            full_name="Sarah Tan",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        u2 = User(
            user_id="search-u2",
            firebase_uid="fb_search_2",
            email="michael.lee@corporate.com",
            full_name="Michael Lee",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add_all([u1, u2])
        session.commit()

    # Search by partial full_name
    resp_name = client.get("/api/v1/admin/users?query=sarah", headers=admin_headers)
    assert resp_name.status_code == 200
    data_name = resp_name.json()
    assert data_name["total"] == 1
    assert data_name["items"][0]["email"] == "sarah.tan@family.sg"

    # Search by partial email
    resp_email = client.get("/api/v1/admin/users?query=corporate.com", headers=admin_headers)
    assert resp_email.status_code == 200
    data_email = resp_email.json()
    assert data_email["total"] == 1
    assert data_email["items"][0]["full_name"] == "Michael Lee"

    # Empty result search
    resp_empty = client.get("/api/v1/admin/users?query=nonexistent", headers=admin_headers)
    assert resp_empty.status_code == 200
    assert resp_empty.json()["total"] == 0
    assert len(resp_empty.json()["items"]) == 0


def test_admin_list_users_invalid_pagination_bounds(client: TestClient, admin_headers):
    """Invalid pagination limits (< 1 or > 100) or negative offset must return 422."""
    assert client.get("/api/v1/admin/users?limit=0", headers=admin_headers).status_code == 422
    assert client.get("/api/v1/admin/users?limit=101", headers=admin_headers).status_code == 422
    assert client.get("/api/v1/admin/users?offset=-1", headers=admin_headers).status_code == 422


# ==============================================================================
# GET /api/v1/admin/users/{user_id} (Detail)
# ==============================================================================


def test_admin_get_user_detail_success(client: TestClient, admin_headers, seed_admin_user):
    """Retrieving an existing user by ID must return complete profile details."""
    resp = client.get(f"/api/v1/admin/users/{seed_admin_user.user_id}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == seed_admin_user.user_id
    assert data["email"] == seed_admin_user.email
    assert data["full_name"] == seed_admin_user.full_name
    assert data["role"] == "ADMIN"
    assert data["account_status"] == "ACTIVE"
    assert data["admin_level"] == 1
    assert "created_at" in data
    assert "updated_at" in data


def test_admin_get_user_detail_not_found(client: TestClient, admin_headers):
    """Retrieving a non-existent user must return HTTP 404 Not Found."""
    resp = client.get("/api/v1/admin/users/non-existent-user-id", headers=admin_headers)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ==============================================================================
# POST /api/v1/admin/users (Account Creation & Provisioning)
# ==============================================================================


def test_admin_create_user_with_explicit_password(client: TestClient, admin_headers, test_engine):
    """Admin creating user with explicit password calls Firebase SDK and persists User record."""
    mock_record = MagicMock(uid="firebase_new_user_123")
    with patch("firebase_admin.auth.create_user", return_value=mock_record) as mock_create:
        payload = {
            "email": "newparent@example.com",
            "password": "SecurePassword123!",
            "full_name": "New Parent User",
            "role": "PARENT",
        }
        resp = client.post("/api/v1/admin/users", json=payload, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newparent@example.com"
        assert data["full_name"] == "New Parent User"
        assert data["role"] == "PARENT"
        assert data["account_status"] == "ACTIVE"
        assert data["firebase_uid"] == "firebase_new_user_123"

        # Verify Firebase SDK call arguments
        mock_create.assert_called_once_with(
            email="newparent@example.com",
            password="SecurePassword123!",
            display_name="New Parent User",
        )

    # Verify user exists in database
    with Session(test_engine) as session:
        db_user = session.exec(
            select(User).where(User.email == "newparent@example.com")
        ).first()
        assert db_user is not None
        assert db_user.firebase_uid == "firebase_new_user_123"


def test_admin_create_user_with_auto_generated_password(
    client: TestClient, admin_headers, test_engine
):
    """Admin creating user without password auto-generates secure password for Firebase SDK."""
    mock_record = MagicMock(uid="firebase_auto_pass_456")
    with patch("firebase_admin.auth.create_user", return_value=mock_record) as mock_create:
        payload = {
            "email": "autopass@example.com",
            "full_name": "Auto Pass User",
            "role": "ADMIN",
            "admin_level": 2,
        }
        resp = client.post("/api/v1/admin/users", json=payload, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "autopass@example.com"
        assert data["role"] == "ADMIN"
        assert data["admin_level"] == 2
        assert data["firebase_uid"] == "firebase_auto_pass_456"

        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["email"] == "autopass@example.com"
        assert len(kwargs["password"]) >= 12  # Auto-generated password should be strong
        assert kwargs["display_name"] == "Auto Pass User"


def test_admin_create_user_conflict_db_email(
    client: TestClient, admin_headers, seed_admin_user
):
    """Admin attempting to create user with email existing in local DB returns HTTP 409 Conflict."""
    with patch("firebase_admin.auth.create_user") as mock_create:
        payload = {
            "email": seed_admin_user.email,
            "full_name": "Duplicate Admin",
            "role": "PARENT",
        }
        resp = client.post("/api/v1/admin/users", json=payload, headers=admin_headers)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()
        mock_create.assert_not_called()


def test_admin_create_user_conflict_firebase_email(client: TestClient, admin_headers):
    """Admin creating user when Firebase reports EmailAlreadyExistsError returns HTTP 409 Conflict."""
    with patch(
        "firebase_admin.auth.create_user",
        side_effect=auth.EmailAlreadyExistsError("Email already exists in Firebase", None, None),
    ):
        payload = {
            "email": "firebase_conflict@example.com",
            "full_name": "Firebase Conflict User",
            "role": "PARENT",
        }
        resp = client.post("/api/v1/admin/users", json=payload, headers=admin_headers)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


def test_admin_create_user_validation_error(client: TestClient, admin_headers):
    """Invalid email or blank name in user creation payload returns HTTP 422."""
    # Blank name
    resp1 = client.post(
        "/api/v1/admin/users",
        json={"email": "valid@example.com", "full_name": "   "},
        headers=admin_headers,
    )
    assert resp1.status_code == 422

    # Invalid email
    resp2 = client.post(
        "/api/v1/admin/users",
        json={"email": "not-an-email", "full_name": "Valid Name"},
        headers=admin_headers,
    )
    assert resp2.status_code == 422


# ==============================================================================
# PUT /api/v1/admin/users/{user_id} (Profile Updates)
# ==============================================================================


def test_admin_update_user_in_place(client: TestClient, admin_headers, test_engine):
    """Admin updating user in place updates full_name, role, and admin_level."""
    with Session(test_engine) as session:
        u = User(
            user_id="target-update-1",
            firebase_uid="fb_target_update_1",
            email="target@example.com",
            full_name="Target Initial Name",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(u)
        session.commit()

    update_payload = {
        "full_name": "Promoted Admin User",
        "role": "ADMIN",
        "admin_level": 3,
    }
    resp = client.put(
        "/api/v1/admin/users/target-update-1",
        json=update_payload,
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["full_name"] == "Promoted Admin User"
    assert data["role"] == "ADMIN"
    assert data["admin_level"] == 3

    # Demote back to PARENT clears admin_level
    demote_payload = {
        "role": "PARENT",
    }
    resp_demote = client.put(
        "/api/v1/admin/users/target-update-1",
        json=demote_payload,
        headers=admin_headers,
    )
    assert resp_demote.status_code == 200
    demoted_data = resp_demote.json()
    assert demoted_data["role"] == "PARENT"
    assert demoted_data["admin_level"] is None


def test_admin_update_user_not_found(client: TestClient, admin_headers):
    """Updating a non-existent user ID returns HTTP 404 Not Found."""
    resp = client.put(
        "/api/v1/admin/users/non-existent-user",
        json={"full_name": "Nobody"},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ==============================================================================
# PATCH /api/v1/admin/users/{user_id}/deactivate & reactivate
# ==============================================================================


def test_admin_deactivate_and_reactivate_lifecycle(
    client: TestClient, admin_headers, test_engine
):
    """Admin can deactivate user to SUSPENDED and reactivate user to ACTIVE."""
    with Session(test_engine) as session:
        u = User(
            user_id="target-lifecycle-1",
            firebase_uid="fb_target_lifecycle_1",
            email="lifecycle@example.com",
            full_name="Lifecycle User",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(u)
        session.commit()

    # Deactivate
    resp_deact = client.patch(
        "/api/v1/admin/users/target-lifecycle-1/deactivate",
        headers=admin_headers,
    )
    assert resp_deact.status_code == 200
    assert resp_deact.json()["account_status"] == "SUSPENDED"

    # Verify user session is now rejected on user endpoints
    mock_claims = FirebaseTokenClaims(uid="fb_target_lifecycle_1", email="lifecycle@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    resp_user_me = client.get("/api/v1/users/me", headers={"Authorization": "Bearer token"})
    assert resp_user_me.status_code == 403
    assert "suspended" in resp_user_me.json()["detail"].lower()

    # Reactivate
    app.dependency_overrides[get_token_claims] = lambda: FirebaseTokenClaims(
        uid="firebase_admin_123", email="admin@kidcarematch.sg"
    )
    resp_react = client.patch(
        "/api/v1/admin/users/target-lifecycle-1/reactivate",
        headers=admin_headers,
    )
    assert resp_react.status_code == 200
    assert resp_react.json()["account_status"] == "ACTIVE"

    # Verify user session is now accepted again
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    resp_user_me_again = client.get(
        "/api/v1/users/me", headers={"Authorization": "Bearer token"}
    )
    assert resp_user_me_again.status_code == 200
    assert resp_user_me_again.json()["email"] == "lifecycle@example.com"


def test_admin_deactivate_reactivate_not_found(client: TestClient, admin_headers):
    """Deactivating or reactivating a non-existent user returns HTTP 404."""
    assert (
        client.patch(
            "/api/v1/admin/users/non-existent/deactivate", headers=admin_headers
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/api/v1/admin/users/non-existent/reactivate", headers=admin_headers
        ).status_code
        == 404
    )


# ==============================================================================
# DELETE /api/v1/admin/users/{user_id} (Purge, Cascade & Self-Deletion)
# ==============================================================================


def test_admin_delete_user_cascade_and_firebase_cleanup(
    client: TestClient, admin_headers, test_engine
):
    """Deleting user permanently cascades database records and calls auth.delete_user."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="CTR_ADMIN_DEL_01",
            centre_name="Admin Delete Test Centre",
        )
        target_user = User(
            user_id="target-delete-1",
            firebase_uid="fb_target_delete_1",
            email="delete_target@example.com",
            full_name="Delete Target",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add_all([centre, target_user])
        session.commit()
        assert centre.centre_id is not None
        assert target_user.user_id is not None

        # Add child records
        profile = FamilyPreferenceProfile(
            profile_id="pref-target-1",
            user_id=target_user.user_id,
            profile_name="Delete Profile",
            service_required="Full Day",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            home_postal_code="123456",
            household_income=4000.0,
            household_member_count=3,
            budget_max=1200.0,
            enrollment_month_year=datetime(2027, 2, 1, tzinfo=timezone.utc),
        )
        review = Review(
            review_id="rev-target-1",
            centre_id=centre.centre_id,
            user_id=target_user.user_id,
            rating=5,
            review_description="Excellent centre.",
        )
        watchlist = WatchlistEntry(
            watchlist_id="watch-target-1",
            user_id=target_user.user_id,
            centre_id=centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        session.add_all([profile, review, watchlist])
        session.commit()
        centre_id = centre.centre_id

    with patch("firebase_admin.auth.delete_user") as mock_delete:
        resp = client.delete("/api/v1/admin/users/target-delete-1", headers=admin_headers)
        assert resp.status_code in (200, 204)
        mock_delete.assert_called_once_with("fb_target_delete_1")

    # Verify user and related records are completely deleted
    with Session(test_engine) as session:
        assert session.get(User, "target-delete-1") is None
        assert session.get(FamilyPreferenceProfile, "pref-target-1") is None
        assert session.get(Review, "rev-target-1") is None
        assert session.get(WatchlistEntry, "watch-target-1") is None
        # Centre itself must remain
        assert session.get(PreschoolCentre, centre_id) is not None


def test_admin_delete_user_not_found(client: TestClient, admin_headers):
    """Deleting a non-existent user returns HTTP 404 Not Found."""
    resp = client.delete("/api/v1/admin/users/non-existent-user", headers=admin_headers)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_admin_prevent_self_deletion(client: TestClient, admin_headers, seed_admin_user, test_engine):
    """Admin attempting to delete their own account must be rejected with 400 or 403."""
    with patch("firebase_admin.auth.delete_user") as mock_delete:
        resp = client.delete(
            f"/api/v1/admin/users/{seed_admin_user.user_id}",
            headers=admin_headers,
        )
        assert resp.status_code in (400, 403)
        assert "cannot delete" in resp.json()["detail"].lower() or "self" in resp.json()["detail"].lower()
        mock_delete.assert_not_called()

    # Verify admin record was NOT deleted
    with Session(test_engine) as session:
        admin_in_db = session.get(User, seed_admin_user.user_id)
        assert admin_in_db is not None

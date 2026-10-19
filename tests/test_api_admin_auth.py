"""Tests for administrative authentication and session verification endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
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


# --- Login Endpoint Tests ---


def test_admin_login_missing_token_returns_401(client: TestClient):
    """Missing Bearer token on admin login must return HTTP 401 Unauthorized."""
    response = client.post("/api/v1/admin/auth/login")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_admin_login_malformed_token_header_returns_401(client: TestClient):
    """Malformed Authorization header must return HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Basic not-a-bearer-token"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_admin_login_invalid_token_returns_401(client: TestClient):
    """Invalid Bearer token that fails Firebase validation must return HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Bearer invalid.firebase.token"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_admin_login_unregistered_user_returns_404(client: TestClient):
    """Valid token for non-existent user profile must return HTTP 404 Not Found."""
    mock_claims = FirebaseTokenClaims(uid="firebase_unknown_user", email="ghost@kidcarematch.sg")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_admin_login_parent_role_returns_403(client: TestClient, seed_parent_user):
    """Authenticated user with role PARENT must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(uid=seed_parent_user.firebase_uid, email=seed_parent_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_admin_login_suspended_admin_returns_403(client: TestClient, seed_suspended_admin):
    """Administrator account in SUSPENDED status must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_admin_login_active_admin_returns_200_with_profile(client: TestClient, seed_admin_user):
    """Active administrator with role ADMIN must receive HTTP 200 with full admin profile."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/login",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == seed_admin_user.user_id
    assert data["email"] == seed_admin_user.email
    assert data["full_name"] == seed_admin_user.full_name
    assert data["role"] == "ADMIN"
    assert data["account_status"] == "ACTIVE"
    assert data["admin_level"] == 1


# --- Logout Endpoint Tests ---


def test_admin_logout_missing_token_returns_401(client: TestClient):
    """Missing Bearer token on admin logout must return HTTP 401 Unauthorized."""
    response = client.post("/api/v1/admin/auth/logout")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_admin_logout_parent_role_returns_403(client: TestClient, seed_parent_user):
    """Authenticated user with role PARENT calling admin logout must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(uid=seed_parent_user.firebase_uid, email=seed_parent_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/logout",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_admin_logout_suspended_admin_returns_403(client: TestClient, seed_suspended_admin):
    """Suspended admin calling logout must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/logout",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_admin_logout_active_admin_returns_200_ok(client: TestClient, seed_admin_user):
    """Active administrator calling logout must receive HTTP 200 OK acknowledging session teardown."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/auth/logout",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "logged out" in data["message"].lower() or "terminated" in data["message"].lower() or "session" in data["message"].lower()


# --- Routing Prefix Verification ---


def test_admin_route_prefix_isolation(client: TestClient):
    """Direct route without /api/v1/admin namespace prefix must return 404 Not Found."""
    response = client.post("/auth/login")
    assert response.status_code == 404

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import (
    FirebaseTokenClaims,
    get_current_admin,
    get_token_claims,
)
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


def test_signup_missing_token_returns_401(client: TestClient):
    """Missing Authorization header must return HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/users/signup",
        json={"full_name": "Alice Tan"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_signup_malformed_token_returns_401(client: TestClient):
    """Malformed or invalid Authorization header must return HTTP 401 Unauthorized."""
    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Basic not-a-bearer-token"},
        json={"full_name": "Alice Tan"},
    )
    assert response.status_code == 401


def test_signup_success_returns_201_and_complete_profile(client: TestClient, test_engine):
    """Valid signup must create user with role PARENT and status ACTIVE and return 201."""
    mock_claims = FirebaseTokenClaims(uid="firebase_alice_123", email="alice@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "Alice Tan"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["firebase_uid"] == "firebase_alice_123"
    assert data["email"] == "alice@example.com"
    assert data["full_name"] == "Alice Tan"
    assert data["role"] == "PARENT"
    assert data["account_status"] == "ACTIVE"
    assert data.get("user_id")
    assert "created_at" in data
    assert "updated_at" in data

    # Verify persisted in database
    with Session(test_engine) as session:
        user = session.exec(select(User).where(User.firebase_uid == "firebase_alice_123")).first()
        assert user is not None
        assert user.email == "alice@example.com"
        assert user.full_name == "Alice Tan"
        assert user.role == UserRole.PARENT
        assert user.account_status == AccountStatus.ACTIVE


def test_signup_duplicate_firebase_uid_returns_409(client: TestClient, test_engine):
    """Attempting signup when firebase_uid already exists must return HTTP 409 Conflict."""
    with Session(test_engine) as session:
        existing_user = User(
            firebase_uid="firebase_duplicate_uid",
            email="original@example.com",
            full_name="Original User",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(existing_user)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid="firebase_duplicate_uid", email="different@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "Duplicate UID User"},
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_signup_duplicate_email_returns_409(client: TestClient, test_engine):
    """Attempting signup when email already exists must return HTTP 409 Conflict."""
    with Session(test_engine) as session:
        existing_user = User(
            firebase_uid="firebase_existing_user",
            email="taken@example.com",
            full_name="Taken Email User",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(existing_user)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid="firebase_brand_new_uid", email="taken@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "New User with Taken Email"},
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_signup_ignores_email_in_request_body(client: TestClient, test_engine):
    """Signup extracts email strictly from token claims, ignoring any email in request body."""
    mock_claims = FirebaseTokenClaims(uid="firebase_charlie_789", email="token_email@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "Charlie", "email": "spoofed_email@example.com"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "token_email@example.com"


def test_get_me_missing_token_returns_401(client: TestClient):
    """GET /api/v1/users/me with missing Authorization header must return 401."""
    response = client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_me_invalid_token_header_returns_401(client: TestClient):
    """GET /api/v1/users/me with invalid authorization scheme must return 401."""
    response = client.get(
        "/api/v1/users/me",
        headers={"Authorization": "Basic not-a-bearer-token"},
    )
    assert response.status_code == 401


def test_get_me_unregistered_user_returns_404(client: TestClient):
    """GET /api/v1/users/me for authenticated user not in DB must return 404."""
    mock_claims = FirebaseTokenClaims(uid="firebase_unregistered_uid", email="newuser@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "User profile not found. Please complete user signup."


def test_get_me_suspended_user_returns_403(client: TestClient, test_engine):
    """GET /api/v1/users/me for suspended user must return 403 Forbidden."""
    with Session(test_engine) as session:
        suspended_user = User(
            firebase_uid="firebase_suspended_uid",
            email="suspended@example.com",
            full_name="Suspended User",
            role=UserRole.PARENT,
            account_status=AccountStatus.SUSPENDED,
        )
        session.add(suspended_user)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid="firebase_suspended_uid", email="suspended@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_get_me_active_user_returns_200(client: TestClient, test_engine):
    """GET /api/v1/users/me for active user must return 200 and UserResponse."""
    with Session(test_engine) as session:
        active_user = User(
            firebase_uid="firebase_active_uid",
            email="active@example.com",
            full_name="Active Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(active_user)
        session.commit()
        session.refresh(active_user)
        user_id = active_user.user_id

    mock_claims = FirebaseTokenClaims(uid="firebase_active_uid", email="active@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["firebase_uid"] == "firebase_active_uid"
    assert data["email"] == "active@example.com"
    assert data["full_name"] == "Active Parent"
    assert data["role"] == "PARENT"
    assert data["account_status"] == "ACTIVE"
    assert "created_at" in data
    assert "updated_at" in data


def test_patch_me_missing_token_returns_401(client: TestClient):
    """PATCH /api/v1/users/me without Authorization header must return 401."""
    response = client.patch(
        "/api/v1/users/me",
        json={"full_name": "Updated Name"},
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_patch_me_invalid_token_header_returns_401(client: TestClient):
    """PATCH /api/v1/users/me with invalid authorization scheme must return 401."""
    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Basic not-a-bearer-token"},
        json={"full_name": "Updated Name"},
    )
    assert response.status_code == 401


def test_patch_me_suspended_user_returns_403(client: TestClient, test_engine):
    """PATCH /api/v1/users/me for suspended user must return 403 Forbidden."""
    with Session(test_engine) as session:
        suspended_user = User(
            firebase_uid="firebase_suspended_patch_uid",
            email="suspended_patch@example.com",
            full_name="Suspended User",
            role=UserRole.PARENT,
            account_status=AccountStatus.SUSPENDED,
        )
        session.add(suspended_user)
        session.commit()

    mock_claims = FirebaseTokenClaims(
        uid="firebase_suspended_patch_uid", email="suspended_patch@example.com"
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "Attempted Name Change"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_patch_me_success_updates_full_name_and_refreshes_updated_at(
    client: TestClient, test_engine
):
    """PATCH /api/v1/users/me updates full_name, refreshes updated_at, and returns 200."""
    with Session(test_engine) as session:
        user = User(
            firebase_uid="firebase_patch_uid",
            email="parent@example.com",
            full_name="Original Name",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        original_updated_at = user.updated_at
        user_id = user.user_id

    mock_claims = FirebaseTokenClaims(uid="firebase_patch_uid", email="parent@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "   Updated Name   "},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["full_name"] == "Updated Name"
    assert data["email"] == "parent@example.com"

    # Verify updated in DB
    with Session(test_engine) as session:
        db_user = session.exec(
            select(User).where(User.firebase_uid == "firebase_patch_uid")
        ).first()
        assert db_user is not None
        assert db_user.full_name == "Updated Name"
        assert db_user.updated_at >= original_updated_at


def test_patch_me_immutable_fields_remain_unchanged(client: TestClient, test_engine):
    """PATCH /api/v1/users/me ignores or rejects any email/role/status fields in request payload."""
    with Session(test_engine) as session:
        user = User(
            firebase_uid="firebase_immutable_uid",
            email="keep_original@example.com",
            full_name="Original Name",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.user_id

    mock_claims = FirebaseTokenClaims(
        uid="firebase_immutable_uid", email="keep_original@example.com"
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "full_name": "New Display Name",
            "email": "hacker@evil.com",
            "role": "ADMIN",
            "account_status": "SUSPENDED",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["full_name"] == "New Display Name"
    assert data["email"] == "keep_original@example.com"
    assert data["role"] == "PARENT"
    assert data["account_status"] == "ACTIVE"

    # Verify database values remain untouched
    with Session(test_engine) as session:
        db_user = session.exec(select(User).where(User.user_id == user_id)).first()
        assert db_user is not None
        assert db_user.email == "keep_original@example.com"
        assert db_user.role == UserRole.PARENT
        assert db_user.account_status == AccountStatus.ACTIVE


def test_patch_me_empty_payload_leaves_profile_unchanged(client: TestClient, test_engine):
    """PATCH /api/v1/users/me with empty payload leaves fields unchanged and returns 200."""
    with Session(test_engine) as session:
        user = User(
            firebase_uid="firebase_noop_uid",
            email="noop@example.com",
            full_name="Static Name",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.user_id

    mock_claims = FirebaseTokenClaims(uid="firebase_noop_uid", email="noop@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
        json={},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["full_name"] == "Static Name"


def test_signup_missing_email_claim_returns_401(client: TestClient):
    """Token missing email claim must return HTTP 401 Unauthorized."""
    mock_claims = FirebaseTokenClaims(uid="firebase_no_email_uid", email="")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "No Email User"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication token missing email claim"


def test_signup_whitespace_only_full_name_returns_422(client: TestClient):
    """Signup with whitespace-only full_name must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(uid="firebase_whitespace_uid", email="whitespace@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "   "},
    )
    assert response.status_code == 422
    assert "Full name cannot be blank or whitespace only" in response.text


def test_signup_strips_leading_and_trailing_whitespace(client: TestClient, test_engine):
    """Signup with leading/trailing whitespace must persist and return stripped full_name."""
    mock_claims = FirebaseTokenClaims(uid="firebase_strip_uid", email="strip@example.com")
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/users/signup",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "   Bob Tan   "},
    )
    assert response.status_code == 201
    assert response.json()["full_name"] == "Bob Tan"

    with Session(test_engine) as session:
        db_user = session.exec(select(User).where(User.firebase_uid == "firebase_strip_uid")).first()
        assert db_user is not None
        assert db_user.full_name == "Bob Tan"


def test_patch_me_whitespace_only_full_name_returns_422(client: TestClient, test_engine):
    """PATCH /api/v1/users/me with whitespace-only full_name must return HTTP 422."""
    with Session(test_engine) as session:
        user = User(
            firebase_uid="firebase_patch_ws_uid",
            email="patch_ws@example.com",
            full_name="Valid Name",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()

    mock_claims = FirebaseTokenClaims(
        uid="firebase_patch_ws_uid", email="patch_ws@example.com"
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.patch(
        "/api/v1/users/me",
        headers={"Authorization": "Bearer mock-token"},
        json={"full_name": "     "},
    )
    assert response.status_code == 422
    assert "Full name cannot be blank or whitespace only" in response.text


def test_get_current_admin_allows_admin():
    """get_current_admin returns user when user role is ADMIN."""
    admin_user = User(
        user_id="admin_123",
        firebase_uid="firebase_admin_uid",
        email="admin@example.com",
        full_name="Admin User",
        role=UserRole.ADMIN,
        account_status=AccountStatus.ACTIVE,
    )
    result = get_current_admin(current_user=admin_user)
    assert result == admin_user


def test_get_current_admin_rejects_parent():
    """get_current_admin raises 403 Forbidden when user role is PARENT."""
    parent_user = User(
        user_id="parent_123",
        firebase_uid="firebase_parent_uid",
        email="parent@example.com",
        full_name="Parent User",
        role=UserRole.PARENT,
        account_status=AccountStatus.ACTIVE,
    )
    with pytest.raises(HTTPException) as exc_info:
        get_current_admin(current_user=parent_user)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Administrative privileges required."





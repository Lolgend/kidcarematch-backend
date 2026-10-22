"""Tests for Family Preference Profile API endpoints (Ticket #58)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    CitizenshipType,
    FamilyPreferenceProfile,
    LocationPreference,
    PreschoolLevel,
    User,
    UserRole,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app


@pytest.fixture
def test_engine():
    """Create isolated in-memory SQLite database engine."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(test_engine):
    """FastAPI TestClient with overridden database session."""

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_parent(test_engine) -> User:
    """Active parent user fixture."""
    with Session(test_engine) as session:
        user = User(
            user_id="parent-uuid-001",
            firebase_uid="firebase_parent_001",
            email="parent1@example.com",
            full_name="Sarah Tan",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def second_parent(test_engine) -> User:
    """Second active parent user fixture."""
    with Session(test_engine) as session:
        user = User(
            user_id="parent-uuid-002",
            firebase_uid="firebase_parent_002",
            email="parent2@example.com",
            full_name="David Lee",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def admin_user(test_engine) -> User:
    """Active administrator user fixture."""
    with Session(test_engine) as session:
        user = User(
            user_id="admin-uuid-001",
            firebase_uid="firebase_admin_001",
            email="admin@kidcarematch.sg",
            full_name="Admin Boss",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            admin_level=1,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def suspended_parent(test_engine) -> User:
    """Suspended parent user fixture."""
    with Session(test_engine) as session:
        user = User(
            user_id="parent-suspended-001",
            firebase_uid="firebase_parent_suspended",
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
    """Set mock authenticated Firebase token claims."""
    mock_claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims


def valid_profile_payload(**overrides) -> dict:
    """Generate a valid profile creation payload with optional overrides."""
    payload = {
        "profile_name": "Infant Liam",
        "level": "infant",
        "citizenship_status": "SC",
        "service_required": "Full Day",
        "home_postal_code": "560123",
        "workplace_postal_code": "018989",
        "location_preference": "HOME",
        "household_income": 8500.0,
        "household_member_count": 4,
        "budget_max": 1200.0,
        "enrollment_month_year": "2027-03",
        "cost_weight": 0.6,
        "travel_weight": 0.4,
    }
    payload.update(overrides)
    return payload


# ==============================================================================
# POST /api/v1/profile Tests (Ticket #58)
# ==============================================================================


def test_create_profile_success(client: TestClient, sample_parent: User, test_engine):
    """Creating a profile for an authenticated parent returns 201 Created and ProfileResponse."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload()

    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()

    assert "profile_id" in data
    assert data["profile_id"] is not None
    assert len(data["profile_id"]) > 0
    assert data["user_id"] == sample_parent.user_id
    assert data["profile_name"] == "Infant Liam"
    assert data["level"] == "infant"
    assert data["citizenship_status"] == "SC"
    assert data["service_required"] == "Full Day"
    assert data["home_postal_code"] == "560123"
    assert data["workplace_postal_code"] == "018989"
    assert data["location_preference"] == "HOME"
    assert data["household_income"] == 8500.0
    assert data["household_member_count"] == 4
    assert data["budget_max"] == 1200.0
    assert data["enrollment_month_year"] == "2027-03"
    assert data["cost_weight"] == 0.6
    assert data["travel_weight"] == 0.4

    # Verify persistence in database and UTC datetime normalization
    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, data["profile_id"])
        assert profile is not None
        assert profile.user_id == sample_parent.user_id
        assert profile.profile_name == "Infant Liam"
        assert profile.enrollment_month_year.year == 2027
        assert profile.enrollment_month_year.month == 3
        assert profile.enrollment_month_year.day == 1


def test_create_profile_duplicate_name_same_parent_returns_409(
    client: TestClient, sample_parent: User
):
    """Duplicate profile_name for the same parent returns HTTP 409 Conflict."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(profile_name="Maya - PG")

    first_res = client.post("/api/v1/profile", json=payload)
    assert first_res.status_code == 201

    duplicate_res = client.post("/api/v1/profile", json=payload)
    assert duplicate_res.status_code == 409
    assert "already exists" in duplicate_res.json()["detail"].lower()


def test_create_multiple_distinct_profiles_same_parent(
    client: TestClient, sample_parent: User, test_engine
):
    """Multiple distinct profile names can be created by the same parent."""
    set_mock_auth(sample_parent)

    res1 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(profile_name="Infant Liam", level="infant"),
    )
    assert res1.status_code == 201

    res2 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(profile_name="Kindergarten Emma", level="k1"),
    )
    assert res2.status_code == 201

    res3 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            profile_name="Workplace Scenario",
            location_preference="WORKPLACE",
            workplace_postal_code="018989",
        ),
    )
    assert res3.status_code == 201

    with Session(test_engine) as session:
        profiles = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.user_id == sample_parent.user_id
            )
        ).all()
        assert len(profiles) == 3
        names = {p.profile_name for p in profiles}
        assert names == {"Infant Liam", "Kindergarten Emma", "Workplace Scenario"}


def test_different_parents_can_use_same_profile_name(
    client: TestClient, sample_parent: User, second_parent: User
):
    """Different parents can create profiles with identical profile names without conflict."""
    set_mock_auth(sample_parent)
    res1 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="General Scenario")
    )
    assert res1.status_code == 201

    set_mock_auth(second_parent)
    res2 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="General Scenario")
    )
    assert res2.status_code == 201


@pytest.mark.parametrize("invalid_postal", ["12345", "1234567", "ABCDEF", "56012A", ""])
def test_create_profile_invalid_home_postal_returns_422(
    client: TestClient, sample_parent: User, invalid_postal: str
):
    """Invalid Singapore postal code format for home_postal_code returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(home_postal_code=invalid_postal)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("invalid_postal", ["12345", "1234567", "ABCDEF", "01898X"])
def test_create_profile_invalid_workplace_postal_returns_422(
    client: TestClient, sample_parent: User, invalid_postal: str
):
    """Invalid Singapore postal code format for workplace_postal_code returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(workplace_postal_code=invalid_postal)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_create_profile_location_preference_workplace_missing_postal_returns_422(
    client: TestClient, sample_parent: User
):
    """Selecting WORKPLACE location preference without workplace_postal_code returns 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        location_preference="WORKPLACE", workplace_postal_code=None
    )
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422
    assert "workplace postal code" in response.text.lower()


def test_create_profile_location_preference_either_missing_postal_returns_422(
    client: TestClient, sample_parent: User
):
    """Selecting EITHER location preference without workplace_postal_code returns 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        location_preference="EITHER", workplace_postal_code=None
    )
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422
    assert "workplace postal code" in response.text.lower()


def test_create_profile_location_preference_home_without_workplace_postal_succeeds(
    client: TestClient, sample_parent: User
):
    """Selecting HOME location preference without workplace_postal_code succeeds."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        location_preference="HOME", workplace_postal_code=None
    )
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 201
    assert response.json()["workplace_postal_code"] is None


@pytest.mark.parametrize(
    "cost_w, travel_w",
    [
        (0.5, 0.6),  # Sum = 1.1
        (0.3, 0.3),  # Sum = 0.6
        (0.0, 0.9),  # Sum = 0.9
        (1.0, 0.1),  # Sum = 1.1
    ],
)
def test_create_profile_weights_not_summing_to_one_returns_422(
    client: TestClient, sample_parent: User, cost_w: float, travel_w: float
):
    """Cost and travel weights must sum to 1.0; otherwise returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(cost_weight=cost_w, travel_weight=travel_w)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422
    assert "1.0" in response.text


@pytest.mark.parametrize(
    "cost_w, travel_w",
    [
        (-0.1, 1.1),  # sum=1.0 but out of bounds
        (1.2, -0.2),
    ],
)
def test_create_profile_weights_out_of_bounds_returns_422(
    client: TestClient, sample_parent: User, cost_w: float, travel_w: float
):
    """Weights out of [0.0, 1.0] interval return HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(cost_weight=cost_w, travel_weight=travel_w)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_month",
    [
        "2027/03",
        "2027-13",
        "2027-00",
        "2027-3",
        "March 2027",
        "2027-03-01",
        "",
    ],
)
def test_create_profile_invalid_enrollment_format_returns_422(
    client: TestClient, sample_parent: User, invalid_month: str
):
    """Target enrollment month not in YYYY-MM format returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(enrollment_month_year=invalid_month)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_create_profile_negative_income_returns_422(
    client: TestClient, sample_parent: User
):
    """Negative household income returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(household_income=-500.0)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_create_profile_zero_member_count_returns_422(
    client: TestClient, sample_parent: User
):
    """Household member count less than 1 returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(household_member_count=0)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_create_profile_zero_budget_returns_422(
    client: TestClient, sample_parent: User
):
    """Budget max less than or equal to 0 returns HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(budget_max=0.0)
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_create_profile_missing_auth_returns_401(client: TestClient):
    """Unauthenticated request without Bearer token returns HTTP 401."""
    response = client.post("/api/v1/profile", json=valid_profile_payload())
    assert response.status_code == 401
    assert "detail" in response.json()


def test_create_profile_admin_role_returns_403(client: TestClient, admin_user: User):
    """Non-parent role (ADMIN) returns HTTP 403 Forbidden."""
    set_mock_auth(admin_user)
    response = client.post("/api/v1/profile", json=valid_profile_payload())
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_create_profile_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """Suspended parent account returns HTTP 403 Forbidden."""
    set_mock_auth(suspended_parent)
    response = client.post("/api/v1/profile", json=valid_profile_payload())
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_database_level_composite_uniqueness(test_engine, sample_parent: User):
    """Direct database insertion verifies composite uniqueness on (user_id, profile_name)."""
    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        p1 = FamilyPreferenceProfile(
            user_id=sample_parent.user_id,
            profile_name="Unique Check",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            location_preference=LocationPreference.HOME,
            household_income=5000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2027, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
            cost_weight=0.5,
            travel_weight=0.5,
        )
        session.add(p1)
        session.commit()

        p2 = FamilyPreferenceProfile(
            user_id=sample_parent.user_id,
            profile_name="Unique Check",
            level=PreschoolLevel.PG,
            citizenship_status=CitizenshipType.SC,
            service_required="Half Day",
            home_postal_code="560123",
            location_preference=LocationPreference.HOME,
            household_income=5000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2027, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
            cost_weight=0.5,
            travel_weight=0.5,
        )
        session.add(p2)
        with pytest.raises(IntegrityError):
            session.commit()

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


# ==============================================================================
# GET /api/v1/profile & GET /api/v1/profile/{profile_id} Tests (Ticket #59)
# ==============================================================================


def test_list_profiles_empty_for_new_parent(client: TestClient, sample_parent: User):
    """GET /api/v1/profile returns HTTP 200 with an empty list when parent has no profiles."""
    set_mock_auth(sample_parent)
    response = client.get("/api/v1/profile")
    assert response.status_code == 200
    assert response.json() == []


def test_list_profiles_user_isolation(
    client: TestClient, sample_parent: User, second_parent: User
):
    """GET /api/v1/profile returns only profiles belonging to the authenticated parent."""
    # Create two profiles for sample_parent
    set_mock_auth(sample_parent)
    res1 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(profile_name="Infant Liam", level="infant"),
    )
    assert res1.status_code == 201
    res2 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            profile_name="Kindergarten Emma",
            level="k1",
            enrollment_month_year="2028-01",
        ),
    )
    assert res2.status_code == 201

    # Create a profile for second_parent
    set_mock_auth(second_parent)
    res3 = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(profile_name="Second Parent Infant"),
    )
    assert res3.status_code == 201

    # Query as sample_parent
    set_mock_auth(sample_parent)
    list_res = client.get("/api/v1/profile")
    assert list_res.status_code == 200
    profiles = list_res.json()
    assert len(profiles) == 2

    # Check user isolation and serialization
    names = {p["profile_name"] for p in profiles}
    assert names == {"Infant Liam", "Kindergarten Emma"}
    for p in profiles:
        assert p["user_id"] == sample_parent.user_id
        # Enrollment month year must be serialized as YYYY-MM
        assert len(p["enrollment_month_year"]) == 7
        assert "-" in p["enrollment_month_year"]

    # Query as second_parent
    set_mock_auth(second_parent)
    list_res2 = client.get("/api/v1/profile")
    assert list_res2.status_code == 200
    profiles2 = list_res2.json()
    assert len(profiles2) == 1
    assert profiles2[0]["profile_name"] == "Second Parent Infant"
    assert profiles2[0]["user_id"] == second_parent.user_id


def test_list_profiles_missing_auth_returns_401(client: TestClient):
    """GET /api/v1/profile without authentication token returns HTTP 401."""
    response = client.get("/api/v1/profile")
    assert response.status_code == 401


def test_list_profiles_admin_role_returns_403(client: TestClient, admin_user: User):
    """GET /api/v1/profile by non-parent user (e.g. ADMIN) returns HTTP 403 Forbidden."""
    set_mock_auth(admin_user)
    response = client.get("/api/v1/profile")
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_list_profiles_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """GET /api/v1/profile by suspended parent returns HTTP 403 Forbidden."""
    set_mock_auth(suspended_parent)
    response = client.get("/api/v1/profile")
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_get_single_profile_success(client: TestClient, sample_parent: User):
    """GET /api/v1/profile/{profile_id} returns HTTP 200 with matching ProfileResponse."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    created_data = create_res.json()
    profile_id = created_data["profile_id"]

    get_res = client.get(f"/api/v1/profile/{profile_id}")
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["profile_id"] == profile_id
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


def test_get_single_profile_not_found(client: TestClient, sample_parent: User):
    """GET /api/v1/profile/{profile_id} returns HTTP 404 when profile does not exist."""
    set_mock_auth(sample_parent)
    response = client.get("/api/v1/profile/non-existent-uuid")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preference profile not found"


def test_get_single_profile_other_user_returns_404(
    client: TestClient, sample_parent: User, second_parent: User
):
    """GET /api/v1/profile/{profile_id} returns HTTP 404 when profile belongs to another parent."""
    # Create profile as sample_parent
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Try to access as second_parent
    set_mock_auth(second_parent)
    get_res = client.get(f"/api/v1/profile/{profile_id}")
    # Must return 404 to avoid ID enumeration
    assert get_res.status_code == 404
    assert get_res.json()["detail"] == "Preference profile not found"


def test_get_single_profile_missing_auth_returns_401(client: TestClient):
    """GET /api/v1/profile/{profile_id} without authentication returns HTTP 401."""
    response = client.get("/api/v1/profile/some-profile-id")
    assert response.status_code == 401


def test_get_single_profile_admin_role_returns_403(
    client: TestClient, admin_user: User
):
    """GET /api/v1/profile/{profile_id} by non-parent user returns HTTP 403 Forbidden."""
    set_mock_auth(admin_user)
    response = client.get("/api/v1/profile/some-profile-id")
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_get_single_profile_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """GET /api/v1/profile/{profile_id} by suspended parent returns HTTP 403 Forbidden."""
    set_mock_auth(suspended_parent)
    response = client.get("/api/v1/profile/some-profile-id")
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


# ==============================================================================
# PUT /api/v1/profile/{profile_id} Tests (Ticket #60)
# ==============================================================================


def test_put_profile_success(client: TestClient, sample_parent: User, test_engine):
    """Full replacement of profile criteria returns HTTP 200 and updated ProfileResponse."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    updated_payload = {
        "profile_name": "Toddler Liam - Updated",
        "level": "pg",
        "citizenship_status": "SPR",
        "service_required": "Half Day",
        "home_postal_code": "650123",
        "workplace_postal_code": "049318",
        "location_preference": "WORKPLACE",
        "household_income": 9500.0,
        "household_member_count": 5,
        "budget_max": 1800.0,
        "enrollment_month_year": "2027-06",
        "cost_weight": 0.3,
        "travel_weight": 0.7,
    }

    put_res = client.put(f"/api/v1/profile/{profile_id}", json=updated_payload)
    assert put_res.status_code == 200, put_res.text
    data = put_res.json()

    assert data["profile_id"] == profile_id
    assert data["user_id"] == sample_parent.user_id
    assert data["profile_name"] == "Toddler Liam - Updated"
    assert data["level"] == "pg"
    assert data["citizenship_status"] == "SPR"
    assert data["service_required"] == "Half Day"
    assert data["home_postal_code"] == "650123"
    assert data["workplace_postal_code"] == "049318"
    assert data["location_preference"] == "WORKPLACE"
    assert data["household_income"] == 9500.0
    assert data["household_member_count"] == 5
    assert data["budget_max"] == 1800.0
    assert data["enrollment_month_year"] == "2027-06"
    assert data["cost_weight"] == 0.3
    assert data["travel_weight"] == 0.7

    # Verify persistence in database
    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.profile_name == "Toddler Liam - Updated"
        assert profile.level == PreschoolLevel.PG
        assert profile.enrollment_month_year.year == 2027
        assert profile.enrollment_month_year.month == 6


def test_put_profile_same_name_allowed(client: TestClient, sample_parent: User):
    """PUT request keeping the same profile_name does NOT raise 409 Conflict."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Same Name")
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    updated_payload = valid_profile_payload(
        profile_name="Same Name", budget_max=1500.0
    )
    put_res = client.put(f"/api/v1/profile/{profile_id}", json=updated_payload)
    assert put_res.status_code == 200
    assert put_res.json()["budget_max"] == 1500.0
    assert put_res.json()["profile_name"] == "Same Name"


def test_put_profile_rename_conflict_returns_409(
    client: TestClient, sample_parent: User
):
    """PUT renaming profile to an existing name of the same parent returns HTTP 409 Conflict."""
    set_mock_auth(sample_parent)
    res1 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Profile 1")
    )
    assert res1.status_code == 201
    profile1_id = res1.json()["profile_id"]

    res2 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Profile 2")
    )
    assert res2.status_code == 201

    # Try renaming Profile 1 to "Profile 2"
    rename_payload = valid_profile_payload(profile_name="Profile 2")
    put_res = client.put(f"/api/v1/profile/{profile1_id}", json=rename_payload)
    assert put_res.status_code == 409
    assert "already exists" in put_res.json()["detail"].lower()


def test_put_profile_rename_to_another_parent_same_name_allowed(
    client: TestClient, sample_parent: User, second_parent: User
):
    """PUT renaming to a name used by another parent succeeds (names are unique per parent)."""
    set_mock_auth(second_parent)
    res2 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Shared Name")
    )
    assert res2.status_code == 201

    set_mock_auth(sample_parent)
    res1 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Initial Name")
    )
    assert res1.status_code == 201
    profile1_id = res1.json()["profile_id"]

    # sample_parent renames their profile to "Shared Name"
    rename_payload = valid_profile_payload(profile_name="Shared Name")
    put_res = client.put(f"/api/v1/profile/{profile1_id}", json=rename_payload)
    assert put_res.status_code == 200
    assert put_res.json()["profile_name"] == "Shared Name"


def test_put_profile_not_found(client: TestClient, sample_parent: User):
    """PUT /api/v1/profile/{profile_id} returns HTTP 404 if profile does not exist."""
    set_mock_auth(sample_parent)
    response = client.put(
        "/api/v1/profile/non-existent-id", json=valid_profile_payload()
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preference profile not found"


def test_put_profile_other_user_returns_404(
    client: TestClient, sample_parent: User, second_parent: User
):
    """PUT /api/v1/profile/{profile_id} returns HTTP 404 if profile belongs to another parent."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    set_mock_auth(second_parent)
    put_res = client.put(
        f"/api/v1/profile/{profile_id}", json=valid_profile_payload(budget_max=2000.0)
    )
    assert put_res.status_code == 404
    assert put_res.json()["detail"] == "Preference profile not found"


@pytest.mark.parametrize(
    "override_field, override_value",
    [
        ("home_postal_code", "12345"),
        ("home_postal_code", "ABCDEF"),
        ("workplace_postal_code", "999"),
        ("location_preference", "WORKPLACE"),  # without workplace postal
        ("cost_weight", 0.7),  # sum = 0.7 + 0.4 = 1.1 != 1.0
        ("enrollment_month_year", "2027-13"),
        ("enrollment_month_year", "2027/03"),
        ("household_income", -10.0),
        ("household_member_count", 0),
        ("budget_max", 0.0),
        ("budget_max", -500.0),
    ],
)
def test_put_profile_revalidates_domain_criteria(
    client: TestClient, sample_parent: User, override_field: str, override_value
):
    """PUT revalidates all domain criteria, returning HTTP 422 for invalid payloads."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    if override_field == "location_preference" and override_value == "WORKPLACE":
        payload = valid_profile_payload(
            location_preference="WORKPLACE", workplace_postal_code=None
        )
    else:
        payload = valid_profile_payload(**{override_field: override_value})

    put_res = client.put(f"/api/v1/profile/{profile_id}", json=payload)
    assert put_res.status_code == 422


def test_put_profile_unauthenticated_returns_401(client: TestClient):
    """PUT /api/v1/profile/{profile_id} returns HTTP 401 if unauthenticated."""
    response = client.put(
        "/api/v1/profile/any-profile-id", json=valid_profile_payload()
    )
    assert response.status_code == 401


def test_put_profile_admin_role_returns_403(client: TestClient, admin_user: User):
    """PUT /api/v1/profile/{profile_id} returns HTTP 403 for non-parent role (ADMIN)."""
    set_mock_auth(admin_user)
    response = client.put(
        "/api/v1/profile/any-profile-id", json=valid_profile_payload()
    )
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_put_profile_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """PUT /api/v1/profile/{profile_id} returns HTTP 403 for suspended parent account."""
    set_mock_auth(suspended_parent)
    response = client.put(
        "/api/v1/profile/any-profile-id", json=valid_profile_payload()
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


# ==============================================================================
# PATCH /api/v1/profile/{profile_id} Tests (Ticket #60)
# ==============================================================================


def test_patch_profile_budget_max_success(
    client: TestClient, sample_parent: User, test_engine
):
    """PATCH updating just budget_max updates that field and preserves other fields."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile", json=valid_profile_payload(budget_max=1200.0)
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}", json={"budget_max": 2500.0}
    )
    assert patch_res.status_code == 200, patch_res.text
    data = patch_res.json()

    assert data["profile_id"] == profile_id
    assert data["budget_max"] == 2500.0
    # Original fields preserved
    assert data["profile_name"] == "Infant Liam"
    assert data["household_income"] == 8500.0
    assert data["cost_weight"] == 0.6
    assert data["travel_weight"] == 0.4

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.budget_max == 2500.0


def test_patch_profile_income_and_members_success(
    client: TestClient, sample_parent: User
):
    """PATCH updating household_income and household_member_count succeeds."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"household_income": 12000.0, "household_member_count": 5},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["household_income"] == 12000.0
    assert data["household_member_count"] == 5


def test_patch_profile_weights_pair_success(client: TestClient, sample_parent: User):
    """PATCH updating both cost_weight and travel_weight summing to 1.0 succeeds."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(cost_weight=0.6, travel_weight=0.4),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"cost_weight": 0.8, "travel_weight": 0.2},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["cost_weight"] == 0.8
    assert data["travel_weight"] == 0.2


def test_patch_profile_single_weight_merged_sum_invalid_returns_422(
    client: TestClient, sample_parent: User
):
    """PATCH updating one weight such that merged sum != 1.0 returns HTTP 422."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(cost_weight=0.6, travel_weight=0.4),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Existing travel_weight is 0.4. Patching cost_weight to 0.9 makes sum = 1.3
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}", json={"cost_weight": 0.9}
    )
    assert patch_res.status_code == 422
    assert "1.0" in patch_res.text


def test_patch_profile_single_weight_merged_sum_valid_success(
    client: TestClient, sample_parent: User
):
    """PATCH updating one weight where merged sum equals 1.0 succeeds."""
    set_mock_auth(sample_parent)
    # Create with 0.5, 0.5
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(cost_weight=0.5, travel_weight=0.5),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Existing cost_weight is 0.5. Patching travel_weight to 0.5 keeps sum = 1.0
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}", json={"travel_weight": 0.5}
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["travel_weight"] == 0.5


def test_patch_profile_location_pref_workplace_missing_postal_returns_422(
    client: TestClient, sample_parent: User
):
    """PATCH switching location_preference to WORKPLACE without workplace_postal_code returns 422."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            location_preference="HOME", workplace_postal_code=None
        ),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"location_preference": "WORKPLACE"},
    )
    assert patch_res.status_code == 422
    assert "workplace postal code" in patch_res.text.lower()


def test_patch_profile_location_pref_workplace_with_postal_success(
    client: TestClient, sample_parent: User
):
    """PATCH updating location_preference and workplace_postal_code together succeeds."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            location_preference="HOME", workplace_postal_code=None
        ),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={
            "location_preference": "WORKPLACE",
            "workplace_postal_code": "018989",
        },
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["location_preference"] == "WORKPLACE"
    assert data["workplace_postal_code"] == "018989"


def test_patch_profile_clear_workplace_postal_when_workplace_pref_returns_422(
    client: TestClient, sample_parent: User
):
    """PATCH clearing workplace_postal_code when location_preference is WORKPLACE returns 422."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            location_preference="WORKPLACE", workplace_postal_code="018989"
        ),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"workplace_postal_code": None},
    )
    assert patch_res.status_code == 422
    assert "workplace postal code" in patch_res.text.lower()


def test_patch_profile_enrollment_month_success(
    client: TestClient, sample_parent: User, test_engine
):
    """PATCH updating enrollment_month_year normalizes date in database."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"enrollment_month_year": "2028-11"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["enrollment_month_year"] == "2028-11"

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.enrollment_month_year.year == 2028
        assert profile.enrollment_month_year.month == 11
        assert profile.enrollment_month_year.day == 1


def test_patch_profile_same_name_allowed(client: TestClient, sample_parent: User):
    """PATCH submitting unchanged profile_name does NOT raise 409 Conflict."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Stable Name")
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"profile_name": "Stable Name", "budget_max": 1600.0},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["profile_name"] == "Stable Name"
    assert patch_res.json()["budget_max"] == 1600.0


def test_patch_profile_rename_conflict_returns_409(
    client: TestClient, sample_parent: User
):
    """PATCH renaming profile to collide with another profile of same parent returns 409."""
    set_mock_auth(sample_parent)
    res1 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Profile Alpha")
    )
    assert res1.status_code == 201
    profile1_id = res1.json()["profile_id"]

    res2 = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Profile Beta")
    )
    assert res2.status_code == 201

    patch_res = client.patch(
        f"/api/v1/profile/{profile1_id}",
        json={"profile_name": "Profile Beta"},
    )
    assert patch_res.status_code == 409
    assert "already exists" in patch_res.json()["detail"].lower()


def test_patch_profile_not_found(client: TestClient, sample_parent: User):
    """PATCH /api/v1/profile/{profile_id} returns HTTP 404 if profile does not exist."""
    set_mock_auth(sample_parent)
    response = client.patch(
        "/api/v1/profile/non-existent-id", json={"budget_max": 2000.0}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preference profile not found"


def test_patch_profile_other_user_returns_404(
    client: TestClient, sample_parent: User, second_parent: User
):
    """PATCH /api/v1/profile/{profile_id} returns HTTP 404 for profile of another parent."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    set_mock_auth(second_parent)
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}", json={"budget_max": 2000.0}
    )
    assert patch_res.status_code == 404
    assert patch_res.json()["detail"] == "Preference profile not found"


@pytest.mark.parametrize(
    "patch_body",
    [
        {"household_income": -1.0},
        {"household_member_count": 0},
        {"budget_max": 0.0},
        {"budget_max": -100.0},
        {"home_postal_code": "123"},
        {"home_postal_code": "ABCDEF"},
        {"workplace_postal_code": "invalid"},
        {"enrollment_month_year": "2027-13"},
        {"enrollment_month_year": "invalid-date"},
        {"cost_weight": -0.1},
        {"cost_weight": 1.2},
    ],
)
def test_patch_profile_field_validations_return_422(
    client: TestClient, sample_parent: User, patch_body: dict
):
    """PATCH invalid field formats or bounds return HTTP 422."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(f"/api/v1/profile/{profile_id}", json=patch_body)
    assert patch_res.status_code == 422


def test_patch_profile_unauthenticated_returns_401(client: TestClient):
    """PATCH /api/v1/profile/{profile_id} returns HTTP 401 if unauthenticated."""
    response = client.patch(
        "/api/v1/profile/any-profile-id", json={"budget_max": 1500.0}
    )
    assert response.status_code == 401


def test_patch_profile_admin_role_returns_403(client: TestClient, admin_user: User):
    """PATCH /api/v1/profile/{profile_id} returns HTTP 403 for non-parent role (ADMIN)."""
    set_mock_auth(admin_user)
    response = client.patch(
        "/api/v1/profile/any-profile-id", json={"budget_max": 1500.0}
    )
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_patch_profile_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """PATCH /api/v1/profile/{profile_id} returns HTTP 403 for suspended parent account."""
    set_mock_auth(suspended_parent)
    response = client.patch(
        "/api/v1/profile/any-profile-id", json={"budget_max": 1500.0}
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()

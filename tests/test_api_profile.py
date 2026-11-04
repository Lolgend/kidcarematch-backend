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
    ApplicantStatus,
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
        "vacancy_weight": 0.4,
        "cost_weight": 0.3,
        "travel_weight": 0.3,
        "applicant_status": "WORKING_APPLICANT",
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
    assert data["vacancy_weight"] == 0.4
    assert data["cost_weight"] == 0.3
    assert data["travel_weight"] == 0.3
    assert data["applicant_status"] == "WORKING_APPLICANT"

    # Verify persistence in database and UTC datetime normalization
    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, data["profile_id"])
        assert profile is not None
        assert profile.user_id == sample_parent.user_id
        assert profile.profile_name == "Infant Liam"
        assert profile.enrollment_month_year.year == 2027
        assert profile.enrollment_month_year.month == 3
        assert profile.enrollment_month_year.day == 1
        assert profile.vacancy_weight == 0.4
        assert profile.cost_weight == 0.3
        assert profile.travel_weight == 0.3
        assert profile.applicant_status == ApplicantStatus.WORKING_APPLICANT


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
        (0.5, 0.6),  # Sum with vacancy 0.4 = 1.5 != 1.0
        (0.3, 0.4),  # Sum with vacancy 0.4 = 1.1 != 1.0
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
    assert data["vacancy_weight"] == 0.4
    assert data["cost_weight"] == 0.3
    assert data["travel_weight"] == 0.3
    assert data["applicant_status"] == "WORKING_APPLICANT"


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
        "vacancy_weight": 0.2,
        "cost_weight": 0.3,
        "travel_weight": 0.5,
        "applicant_status": "NON_WORKING_APPLICANT",
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
    assert data["vacancy_weight"] == 0.2
    assert data["cost_weight"] == 0.3
    assert data["travel_weight"] == 0.5
    assert data["applicant_status"] == "NON_WORKING_APPLICANT"

    # Verify persistence in database
    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.profile_name == "Toddler Liam - Updated"
        assert profile.level == PreschoolLevel.PG
        assert profile.enrollment_month_year.year == 2027
        assert profile.enrollment_month_year.month == 6
        assert profile.vacancy_weight == 0.2
        assert profile.cost_weight == 0.3
        assert profile.travel_weight == 0.5
        assert profile.applicant_status == ApplicantStatus.NON_WORKING_APPLICANT


def test_put_profile_same_name_allowed(client: TestClient, sample_parent: User):
    """PUT request keeping the same profile_name does NOT raise 409 Conflict."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile", json=valid_profile_payload(profile_name="Same Name")
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    updated_payload = valid_profile_payload(profile_name="Same Name", budget_max=1500.0)
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
    assert data["vacancy_weight"] == 0.4
    assert data["cost_weight"] == 0.3
    assert data["travel_weight"] == 0.3
    assert data["applicant_status"] == "WORKING_APPLICANT"

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
        json=valid_profile_payload(vacancy_weight=0.4, cost_weight=0.3, travel_weight=0.3),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"cost_weight": 0.4, "travel_weight": 0.2},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["vacancy_weight"] == 0.4
    assert data["cost_weight"] == 0.4
    assert data["travel_weight"] == 0.2


def test_patch_profile_single_weight_merged_sum_invalid_returns_422(
    client: TestClient, sample_parent: User
):
    """PATCH updating one weight such that merged sum != 1.0 returns HTTP 422."""
    set_mock_auth(sample_parent)
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(vacancy_weight=0.4, cost_weight=0.3, travel_weight=0.3),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Existing travel_weight is 0.3, vacancy_weight is 0.4. Patching cost_weight to 0.9 makes sum = 1.6
    patch_res = client.patch(f"/api/v1/profile/{profile_id}", json={"cost_weight": 0.9})
    assert patch_res.status_code == 422
    assert "1.0" in patch_res.text


def test_patch_profile_single_weight_merged_sum_valid_success(
    client: TestClient, sample_parent: User
):
    """PATCH updating one weight where merged sum equals 1.0 succeeds."""
    set_mock_auth(sample_parent)
    # Create with 0.4, 0.3, 0.3
    create_res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(vacancy_weight=0.4, cost_weight=0.3, travel_weight=0.3),
    )
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Existing cost_weight is 0.3, vacancy_weight is 0.4. Patching travel_weight to 0.3 keeps sum = 1.0
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}", json={"travel_weight": 0.3}
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["travel_weight"] == 0.3


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


# =============================================================================
# DELETE /api/v1/profile/{profile_id} Tests (Ticket #61)
# =============================================================================


def test_delete_profile_success(client: TestClient, sample_parent: User, test_engine):
    """DELETE /api/v1/profile/{profile_id} successfully removes the profile and returns 204."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Verify profile exists before deletion
    get_res = client.get(f"/api/v1/profile/{profile_id}")
    assert get_res.status_code == 200

    # Delete profile
    delete_res = client.delete(f"/api/v1/profile/{profile_id}")
    assert delete_res.status_code == 204
    assert delete_res.text == ""

    # Subsequent GET returns 404
    get_after_delete = client.get(f"/api/v1/profile/{profile_id}")
    assert get_after_delete.status_code == 404
    assert get_after_delete.json()["detail"] == "Preference profile not found"

    # Profile should no longer appear in profile list
    list_res = client.get("/api/v1/profile")
    assert list_res.status_code == 200
    assert not any(p["profile_id"] == profile_id for p in list_res.json())

    # Directly verify in database session
    with Session(test_engine) as session:
        db_profile = session.get(FamilyPreferenceProfile, profile_id)
        assert db_profile is None


def test_delete_profile_not_found(client: TestClient, sample_parent: User):
    """DELETE /api/v1/profile/{profile_id} returns HTTP 404 if profile does not exist."""
    set_mock_auth(sample_parent)
    response = client.delete("/api/v1/profile/non-existent-profile-id")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preference profile not found"


def test_delete_profile_other_user_isolation(
    client: TestClient, sample_parent: User, second_parent: User, test_engine
):
    """DELETE /api/v1/profile/{profile_id} returns HTTP 404 for profile of another parent."""
    # First parent creates a profile
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Second parent attempts to delete first parent's profile
    set_mock_auth(second_parent)
    delete_res = client.delete(f"/api/v1/profile/{profile_id}")
    assert delete_res.status_code == 404
    assert delete_res.json()["detail"] == "Preference profile not found"

    # First parent's profile is still intact
    set_mock_auth(sample_parent)
    get_res = client.get(f"/api/v1/profile/{profile_id}")
    assert get_res.status_code == 200
    assert get_res.json()["profile_id"] == profile_id

    # Verify in DB
    with Session(test_engine) as session:
        db_profile = session.get(FamilyPreferenceProfile, profile_id)
        assert db_profile is not None


def test_delete_profile_unauthenticated_returns_401(client: TestClient):
    """DELETE /api/v1/profile/{profile_id} returns HTTP 401 if unauthenticated."""
    response = client.delete("/api/v1/profile/any-profile-id")
    assert response.status_code == 401


def test_delete_profile_admin_role_returns_403(client: TestClient, admin_user: User):
    """DELETE /api/v1/profile/{profile_id} returns HTTP 403 for non-parent role (ADMIN)."""
    set_mock_auth(admin_user)
    response = client.delete("/api/v1/profile/any-profile-id")
    assert response.status_code == 403
    assert "parent" in response.json()["detail"].lower()


def test_delete_profile_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """DELETE /api/v1/profile/{profile_id} returns HTTP 403 for suspended parent account."""
    set_mock_auth(suspended_parent)
    response = client.delete("/api/v1/profile/any-profile-id")
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_cascade_delete_user_removes_preference_profiles(
    test_engine, sample_parent: User
):
    """Deleting a parent User record automatically cascade-deletes all associated profiles."""
    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        # Add two profiles for sample_parent
        p1 = FamilyPreferenceProfile(
            profile_id="cascade-prof-1",
            user_id=sample_parent.user_id,
            profile_name="Profile Child 1",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="123456",
            location_preference=LocationPreference.HOME,
            household_income=6000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2027, 5, 1, tzinfo=timezone.utc),
            cost_weight=0.5,
            travel_weight=0.5,
        )
        p2 = FamilyPreferenceProfile(
            profile_id="cascade-prof-2",
            user_id=sample_parent.user_id,
            profile_name="Profile Child 2",
            level=PreschoolLevel.PG,
            citizenship_status=CitizenshipType.SC,
            service_required="Half Day",
            home_postal_code="123456",
            location_preference=LocationPreference.HOME,
            household_income=6000.0,
            household_member_count=3,
            budget_max=800.0,
            enrollment_month_year=datetime(2027, 8, 1, tzinfo=timezone.utc),
            cost_weight=0.5,
            travel_weight=0.5,
        )
        session.add_all([p1, p2])
        session.commit()

        # Confirm profiles exist
        assert session.get(FamilyPreferenceProfile, "cascade-prof-1") is not None
        assert session.get(FamilyPreferenceProfile, "cascade-prof-2") is not None

        # Fetch the user within the same session and delete
        user = session.get(User, sample_parent.user_id)
        assert user is not None
        session.delete(user)
        session.commit()

        # Confirm cascade deletion of both preference profiles
        assert session.get(FamilyPreferenceProfile, "cascade-prof-1") is None
        assert session.get(FamilyPreferenceProfile, "cascade-prof-2") is None
        profiles_remaining = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.user_id == sample_parent.user_id
            )
        ).all()
        assert len(profiles_remaining) == 0


# ==============================================================================
# Code Review Refactor & Standards Verification Tests
# ==============================================================================


def test_schema_exports():
    """Verify ProfileUpdateRequest and ProfilePatchRequest are exported in schemas."""
    from kidcarematch_backend import schemas
    from kidcarematch_backend.schemas import (
        ProfilePatchRequest,
        ProfileUpdateRequest,
    )

    assert "ProfileUpdateRequest" in schemas.__all__
    assert "ProfilePatchRequest" in schemas.__all__
    assert ProfileUpdateRequest is not None
    assert ProfilePatchRequest is not None


@pytest.mark.parametrize(
    "field_name",
    [
        "profile_name",
        "level",
        "citizenship_status",
        "service_required",
        "home_postal_code",
        "location_preference",
        "household_income",
        "household_member_count",
        "budget_max",
        "enrollment_month_year",
        "cost_weight",
        "travel_weight",
    ],
)
def test_patch_profile_explicit_null_on_non_nullable_fields_returns_422(
    client: TestClient, sample_parent: User, field_name: str
):
    """PATCH with explicit null on mandatory database fields returns HTTP 422."""
    set_mock_auth(sample_parent)
    res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert res.status_code == 201
    profile_id = res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={field_name: None},
    )
    assert patch_res.status_code == 422
    assert "null" in patch_res.text.lower() or "input should be" in patch_res.text.lower()


def test_patch_profile_explicit_null_workplace_postal_code_when_home_pref_succeeds(
    client: TestClient, sample_parent: User
):
    """PATCH explicit null on workplace_postal_code succeeds when location_preference is HOME."""
    set_mock_auth(sample_parent)
    res = client.post(
        "/api/v1/profile",
        json=valid_profile_payload(
            location_preference="HOME",
            workplace_postal_code="018989",
        ),
    )
    assert res.status_code == 201
    profile_id = res.json()["profile_id"]
    assert res.json()["workplace_postal_code"] == "018989"

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"workplace_postal_code": None},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["workplace_postal_code"] is None


def test_no_trailing_slash_alias_endpoints(client: TestClient, sample_parent: User):
    """Verify unrequested trailing-slash routes are removed and not accepted directly without redirect."""
    set_mock_auth(sample_parent)
    res = client.post(
        "/api/v1/profile/",
        json=valid_profile_payload(),
        follow_redirects=False,
    )
    assert res.status_code in (307, 404, 405)


def test_profile_helper_functions(test_engine):
    """Verify extracted helper functions: parse_year_month, validate_location_and_weights, get_profile_or_404."""
    from fastapi import HTTPException

    from kidcarematch_backend.api.v1.profile import get_profile_or_404
    from kidcarematch_backend.schemas.profile import (
        parse_year_month,
        validate_location_and_weights,
    )

    # 1. parse_year_month
    dt = parse_year_month("2028-09")
    assert dt == datetime(2028, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

    # 2. validate_location_and_weights
    with pytest.raises(ValueError, match="Workplace postal code is required"):
        validate_location_and_weights(
            location_preference=LocationPreference.WORKPLACE,
            workplace_postal_code=None,
            vacancy_weight=0.4,
            cost_weight=0.3,
            travel_weight=0.3,
        )

    with pytest.raises(ValueError, match="sum to 1.0"):
        validate_location_and_weights(
            location_preference=LocationPreference.HOME,
            workplace_postal_code=None,
            vacancy_weight=0.4,
            cost_weight=0.6,
            travel_weight=0.6,
        )

    # 3. get_profile_or_404
    with Session(test_engine) as session:
        with pytest.raises(HTTPException) as exc_info:
            get_profile_or_404("non-existent", "user-123", session)
        assert exc_info.value.status_code == 404


def test_unexpected_integrity_error_re_raised(client: TestClient, sample_parent: User, monkeypatch):
    """Verify unexpected database IntegrityError is re-raised and not swallowed as 409 conflict."""
    set_mock_auth(sample_parent)

    def faulty_commit(self):
        raise IntegrityError("FOREIGN KEY constraint failed", None, None)

    monkeypatch.setattr(Session, "commit", faulty_commit)

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        client.post("/api/v1/profile", json=valid_profile_payload())


# ==============================================================================
# Ticket #67: 3-Factor Weights and Applicant Status Tests
# ==============================================================================


def test_create_profile_with_3_factor_weights_and_applicant_status_defaults(
    client: TestClient, sample_parent: User, test_engine
):
    """POST /api/v1/profile omitting weights & applicant_status uses 0.40, 0.30, 0.30, and WORKING_APPLICANT."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(profile_name="Weights Default Scenario")
    payload.pop("cost_weight", None)
    payload.pop("travel_weight", None)
    payload.pop("vacancy_weight", None)
    payload.pop("applicant_status", None)

    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["vacancy_weight"] == 0.40
    assert data["cost_weight"] == 0.30
    assert data["travel_weight"] == 0.30
    assert data["applicant_status"] == "WORKING_APPLICANT"

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, data["profile_id"])
        assert profile is not None
        assert profile.vacancy_weight == 0.40
        assert profile.cost_weight == 0.30
        assert profile.travel_weight == 0.30
        assert profile.applicant_status == ApplicantStatus.WORKING_APPLICANT


def test_create_profile_with_custom_3_factor_weights_and_applicant_status(
    client: TestClient, sample_parent: User, test_engine
):
    """POST /api/v1/profile with custom 3-factor weights and applicant_status persists correctly."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        profile_name="Custom 3-Factor Scenario",
        vacancy_weight=0.50,
        cost_weight=0.20,
        travel_weight=0.30,
        applicant_status="NON_WORKING_APPLICANT",
    )

    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["vacancy_weight"] == 0.50
    assert data["cost_weight"] == 0.20
    assert data["travel_weight"] == 0.30
    assert data["applicant_status"] == "NON_WORKING_APPLICANT"

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, data["profile_id"])
        assert profile is not None
        assert profile.vacancy_weight == 0.50
        assert profile.cost_weight == 0.20
        assert profile.travel_weight == 0.30
        assert profile.applicant_status == ApplicantStatus.NON_WORKING_APPLICANT


@pytest.mark.parametrize(
    "v_weight, c_weight, t_weight",
    [
        (0.4, 0.4, 0.4),  # sum = 1.2
        (0.2, 0.2, 0.2),  # sum = 0.6
        (0.0, 0.0, 0.0),  # sum = 0.0
        (0.5, 0.5, 0.5),  # sum = 1.5
    ],
)
def test_create_profile_3_factor_weights_not_summing_to_one_returns_422(
    client: TestClient, sample_parent: User, v_weight: float, c_weight: float, t_weight: float
):
    """POST /api/v1/profile rejecting weights whose rounded sum != 1.0 with HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        vacancy_weight=v_weight,
        cost_weight=c_weight,
        travel_weight=t_weight,
    )
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422
    assert "1.0" in response.text


def test_create_profile_invalid_applicant_status_returns_422(
    client: TestClient, sample_parent: User
):
    """POST /api/v1/profile rejecting invalid applicant_status enum value with HTTP 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(applicant_status="INVALID_STATUS")
    response = client.post("/api/v1/profile", json=payload)
    assert response.status_code == 422


def test_get_profile_serializes_3_factor_weights_and_applicant_status(
    client: TestClient, sample_parent: User
):
    """GET /api/v1/profile/{profile_id} includes vacancy_weight, cost_weight, travel_weight, applicant_status."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        profile_name="Serialization Check",
        vacancy_weight=0.50,
        cost_weight=0.25,
        travel_weight=0.25,
        applicant_status="NON_WORKING_APPLICANT",
    )
    create_res = client.post("/api/v1/profile", json=payload)
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    get_res = client.get(f"/api/v1/profile/{profile_id}")
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["vacancy_weight"] == 0.50
    assert data["cost_weight"] == 0.25
    assert data["travel_weight"] == 0.25
    assert data["applicant_status"] == "NON_WORKING_APPLICANT"


def test_put_profile_updates_3_factor_weights_and_applicant_status(
    client: TestClient, sample_parent: User, test_engine
):
    """PUT /api/v1/profile/{profile_id} updates 3-factor weights and applicant_status."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    put_payload = valid_profile_payload(
        profile_name="Updated 3-Factor",
        vacancy_weight=0.60,
        cost_weight=0.20,
        travel_weight=0.20,
        applicant_status="NON_WORKING_APPLICANT",
    )
    put_res = client.put(f"/api/v1/profile/{profile_id}", json=put_payload)
    assert put_res.status_code == 200
    data = put_res.json()
    assert data["vacancy_weight"] == 0.60
    assert data["cost_weight"] == 0.20
    assert data["travel_weight"] == 0.20
    assert data["applicant_status"] == "NON_WORKING_APPLICANT"

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.vacancy_weight == 0.60
        assert profile.cost_weight == 0.20
        assert profile.travel_weight == 0.20
        assert profile.applicant_status == ApplicantStatus.NON_WORKING_APPLICANT


def test_patch_profile_weights_3_factor_merged_sum_success(
    client: TestClient, sample_parent: User
):
    """PATCH updating weights where merged 3-factor sum == 1.0 succeeds."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        vacancy_weight=0.40,
        cost_weight=0.30,
        travel_weight=0.30,
    )
    create_res = client.post("/api/v1/profile", json=payload)
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Patch cost_weight=0.40 and travel_weight=0.20 (vacancy_weight remains 0.40; 0.40+0.40+0.20=1.0)
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"cost_weight": 0.40, "travel_weight": 0.20},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["vacancy_weight"] == 0.40
    assert data["cost_weight"] == 0.40
    assert data["travel_weight"] == 0.20


def test_patch_profile_weights_3_factor_merged_sum_invalid_returns_422(
    client: TestClient, sample_parent: User
):
    """PATCH updating single weight making merged 3-factor sum != 1.0 returns 422."""
    set_mock_auth(sample_parent)
    payload = valid_profile_payload(
        vacancy_weight=0.40,
        cost_weight=0.30,
        travel_weight=0.30,
    )
    create_res = client.post("/api/v1/profile", json=payload)
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    # Patch vacancy_weight=0.70 (merged sum = 0.70 + 0.30 + 0.30 = 1.30 != 1.0)
    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"vacancy_weight": 0.70},
    )
    assert patch_res.status_code == 422
    assert "1.0" in patch_res.text


def test_patch_profile_applicant_status_success(
    client: TestClient, sample_parent: User, test_engine
):
    """PATCH updating applicant_status preserves weights and updates status."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"applicant_status": "NON_WORKING_APPLICANT"},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["applicant_status"] == "NON_WORKING_APPLICANT"

    with Session(test_engine) as session:
        profile = session.get(FamilyPreferenceProfile, profile_id)
        assert profile is not None
        assert profile.applicant_status == ApplicantStatus.NON_WORKING_APPLICANT


@pytest.mark.parametrize("null_field", ["vacancy_weight", "applicant_status"])
def test_patch_profile_null_field_returns_422(
    client: TestClient, sample_parent: User, null_field: str
):
    """PATCH sending null for non-nullable vacancy_weight or applicant_status returns 422."""
    set_mock_auth(sample_parent)
    create_res = client.post("/api/v1/profile", json=valid_profile_payload())
    assert create_res.status_code == 201
    profile_id = create_res.json()["profile_id"]

    patch_res = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={null_field: None},
    )
    assert patch_res.status_code == 422


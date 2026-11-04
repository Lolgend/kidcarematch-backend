"""Tests for Multi-Criteria Centre Matching API endpoint (POST /api/v1/centres/match - Ticket #68)."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import (
    FirebaseTokenClaims,
    get_optional_token_claims,
    get_token_claims,
)
from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CitizenshipType,
    FamilyPreferenceProfile,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    User,
    UserRole,
    VacancyRecord,
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
            user_id="parent-match-001",
            firebase_uid="firebase_parent_match_001",
            email="parent_match@example.com",
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
            user_id="parent-match-002",
            firebase_uid="firebase_parent_match_002",
            email="parent2_match@example.com",
            full_name="David Lee",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
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
            user_id="parent-suspended-match-001",
            firebase_uid="firebase_suspended_match_001",
            email="suspended_match@example.com",
            full_name="Suspended Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.SUSPENDED,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def set_mock_auth(user: User):
    """Set mock authenticated user."""
    mock_claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    app.dependency_overrides[get_optional_token_claims] = lambda: mock_claims


# Helper to seed centres with services and vacancies
def create_centres_with_cohorts(test_engine):
    """Seed test centres:
    - Centre 1 (Alpha): Offers infant (Full Day) and PG. Vacancy: Available in 2026-06.
    - Centre 2 (Beta): Offers infant (Full Day). Vacancy: Limited in 2026-06.
    - Centre 3 (Gamma): Offers infant (Full Day). Vacancy: Full in 2026-06.
    - Centre 4 (Delta): Offers infant (Full Day). Vacancy: unrecorded in 2026-06 (no record).
    - Centre 5 (Epsilon): Offers infant (Full Day). Vacancy: Not Applicable in 2026-06.
    - Centre 6 (Zeta): Offers infant (Full Day). Vacancy: Immediate in 2026-06 (legacy).
    - Centre 7 (Omega): Offers ONLY K1 and K2 (NO infant service!). Vacancy: Available in 2026-06.
    """
    with Session(test_engine) as session:
        target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
        other_month = datetime(2026, 7, 1, tzinfo=timezone.utc)

        c1 = PreschoolCentre(
            centre_id=1,
            centre_code="C001",
            centre_name="Alpha Preschool",
            postal="560123",
            address="123 Ang Mo Kio Ave 3",
            spark_certified=True,
            gov_subsidy=True,
        )
        c2 = PreschoolCentre(
            centre_id=2,
            centre_code="C002",
            centre_name="Beta Childcare",
            postal="560124",
            address="124 Ang Mo Kio Ave 3",
            spark_certified=False,
            gov_subsidy=True,
        )
        c3 = PreschoolCentre(
            centre_id=3,
            centre_code="C003",
            centre_name="Gamma Learning",
            postal="560125",
            address="125 Ang Mo Kio Ave 3",
            spark_certified=True,
            gov_subsidy=False,
        )
        c4 = PreschoolCentre(
            centre_id=4,
            centre_code="C004",
            centre_name="Delta Kids Haven",
            postal="560126",
            address="126 Ang Mo Kio Ave 3",
            spark_certified=False,
            gov_subsidy=False,
        )
        c5 = PreschoolCentre(
            centre_id=5,
            centre_code="C005",
            centre_name="Epsilon Infant Academy",
            postal="560127",
            address="127 Ang Mo Kio Ave 3",
            spark_certified=True,
            gov_subsidy=True,
        )
        c6 = PreschoolCentre(
            centre_id=6,
            centre_code="C006",
            centre_name="Zeta Early Years",
            postal="560128",
            address="128 Ang Mo Kio Ave 3",
            spark_certified=False,
            gov_subsidy=True,
        )
        c7 = PreschoolCentre(
            centre_id=7,
            centre_code="C007",
            centre_name="Omega Kindergarten",
            postal="560129",
            address="129 Ang Mo Kio Ave 3",
            spark_certified=True,
            gov_subsidy=True,
        )
        session.add_all([c1, c2, c3, c4, c5, c6, c7])
        session.commit()

        # Services
        s1_infant = PreschoolService(
            centre_id=1,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1200.0,
            citizenship_type=CitizenshipType.SC,
        )
        s1_pg = PreschoolService(
            centre_id=1,
            type_of_service="Full Day",
            level=PreschoolLevel.PG,
            fee=900.0,
            citizenship_type=CitizenshipType.SC,
        )
        s2_infant = PreschoolService(
            centre_id=2,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1300.0,
            citizenship_type=CitizenshipType.SC,
        )
        s3_infant = PreschoolService(
            centre_id=3,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1400.0,
            citizenship_type=CitizenshipType.SC,
        )
        s4_infant = PreschoolService(
            centre_id=4,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1100.0,
            citizenship_type=CitizenshipType.SC,
        )
        s5_infant = PreschoolService(
            centre_id=5,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1250.0,
            citizenship_type=CitizenshipType.SC,
        )
        s6_infant = PreschoolService(
            centre_id=6,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1350.0,
            citizenship_type=CitizenshipType.SC,
        )
        # c7 only offers K1 and K2
        s7_k1 = PreschoolService(
            centre_id=7,
            type_of_service="Full Day",
            level=PreschoolLevel.K1,
            fee=800.0,
            citizenship_type=CitizenshipType.SC,
        )
        session.add_all(
            [
                s1_infant,
                s1_pg,
                s2_infant,
                s3_infant,
                s4_infant,
                s5_infant,
                s6_infant,
                s7_k1,
            ]
        )

        # Vacancies for 2026-06
        v1 = VacancyRecord(
            record_id="VR001",
            centre_id=1,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        v2 = VacancyRecord(
            record_id="VR002",
            centre_id=2,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Limited",
        )
        v3 = VacancyRecord(
            record_id="VR003",
            centre_id=3,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Full",
        )
        # c4 has NO vacancy record (unrecorded)
        v5 = VacancyRecord(
            record_id="VR005",
            centre_id=5,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Not Applicable",
        )
        v6 = VacancyRecord(
            record_id="VR006",
            centre_id=6,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Immediate",
        )
        # c7 has Available vacancy for K1, and Available for Infant (even though it doesn't offer infant service)
        v7_infant = VacancyRecord(
            record_id="VR007_infant",
            centre_id=7,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        # Vacancy for c1 in a DIFFERENT month (should be ignored when matching 2026-06)
        v1_other = VacancyRecord(
            record_id="VR001_other",
            centre_id=1,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=other_month,
            vacancy_status="Full",
        )
        session.add_all([v1, v2, v3, v5, v6, v7_infant, v1_other])
        session.commit()


# ==============================================================================
# Cohort Filtering & Vacancy Scoring Tests (Ticket #68)
# ==============================================================================


def test_inline_match_cohort_filtering_strictly_excludes_non_offering_centres(
    client: TestClient, test_engine
):
    """Candidate centres not offering the requested PreschoolLevel cohort are strictly excluded."""
    create_centres_with_cohorts(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "top_n": 10,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Centre 7 (Omega) offers ONLY K1, so it MUST NOT be included, despite having an Available infant vacancy record
    returned_codes = [c["centre_code"] for c in data["results"]]
    assert "C007" not in returned_codes
    assert data["total_candidates"] == 6  # Centres 1 to 6


def test_inline_match_vacancy_fit_scoring_and_ranking_order(
    client: TestClient, test_engine
):
    """Vacancy Fit is scored: Available/Immediate=100%, Limited=50%, Full/NA/unrecorded=0%.

    Centres with 0% vacancy fit remain eligible and visible in rankings as lower-priority alternatives.
    """
    create_centres_with_cohorts(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "top_n": 10,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    results = data["results"]
    assert len(results) == 6

    # 100% tier: Alpha (Available) and Zeta (Immediate)
    # Alpha Preschool vs Zeta Early Years -> Alpha comes first alphabetically
    assert results[0]["centre_code"] == "C001"
    assert results[0]["vacancy_fit_score"] == 100.0
    assert results[0]["vacancy_status"] == "Available"

    assert results[1]["centre_code"] == "C006"
    assert results[1]["vacancy_fit_score"] == 100.0
    assert results[1]["vacancy_status"] == "Immediate"

    # 50% tier: Beta (Limited)
    assert results[2]["centre_code"] == "C002"
    assert results[2]["vacancy_fit_score"] == 50.0
    assert results[2]["vacancy_status"] == "Limited"

    # 0% tier: Gamma (Full), Delta (unrecorded), Epsilon (Not Applicable)
    # Sorted alphabetically by centre_name: Delta Kids Haven, Epsilon Infant Academy, Gamma Learning
    zero_tier_codes = [r["centre_code"] for r in results[3:]]
    assert zero_tier_codes == ["C004", "C005", "C003"]
    for r in results[3:]:
        assert r["vacancy_fit_score"] == 0.0

    assert results[3]["centre_code"] == "C004"
    assert results[3]["vacancy_status"] is None  # unrecorded

    assert results[4]["centre_code"] == "C005"
    assert results[4]["vacancy_status"] == "Not Applicable"

    assert results[5]["centre_code"] == "C003"
    assert results[5]["vacancy_status"] == "Full"


def test_inline_match_top_n_limits_results_but_preserves_total_candidates(
    client: TestClient, test_engine
):
    """top_n parameter limits the number of returned results while reporting accurate total_candidates."""
    create_centres_with_cohorts(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "top_n": 2,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["total_candidates"] == 6
    assert data["returned_count"] == 2
    assert len(data["results"]) == 2
    # Only top 2 (the 100% tier)
    assert [r["centre_code"] for r in data["results"]] == ["C001", "C006"]


def test_inline_match_target_month_isolation(client: TestClient, test_engine):
    """Vacancy scores are evaluated against target enrollment month, ignoring other months."""
    create_centres_with_cohorts(test_engine)

    # For 2026-07: Centre 1 (Alpha) has vacancy_status="Full" (0%), unlike 2026-06 where it was Available (100%)
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-07",
        "top_n": 5,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    alpha = next(r for r in data["results"] if r["centre_code"] == "C001")
    assert alpha["vacancy_fit_score"] == 0.0
    assert alpha["vacancy_status"] == "Full"


def test_inline_match_case_insensitivity_of_vacancy_status(
    client: TestClient, test_engine
):
    """Vacancy status strings are evaluated case-insensitively."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=10,
            centre_code="C010",
            centre_name="Case Preschool",
            postal="560123",
        )
        session.add(centre)
        session.commit()
        session.add(
            PreschoolService(
                centre_id=10,
                type_of_service="Full Day",
                level=PreschoolLevel.PG,
                fee=800.0,
                citizenship_type=CitizenshipType.SC,
            )
        )
        session.add(
            VacancyRecord(
                record_id="VR010",
                centre_id=10,
                level_type=PreschoolLevel.PG,
                enrollment_month_year=datetime(2027, 1, 1, tzinfo=timezone.utc),
                vacancy_status="aVaiLaBLe",
            )
        )
        session.commit()

    payload = {
        "level": "pg",
        "enrollment_month_year": "2027-01",
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["vacancy_fit_score"] == 100.0


def test_inline_match_authenticated_user_without_profile(
    client: TestClient, sample_parent: User, test_engine
):
    """An authenticated parent can use inline search criteria without passing profile_id."""
    create_centres_with_cohorts(test_engine)
    set_mock_auth(sample_parent)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "top_n": 3,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["returned_count"] == 3
    assert data["profile_id"] is None


# ==============================================================================
# Profile-Linked Matching Tests (Ticket #68)
# ==============================================================================


def test_profile_match_success(client: TestClient, sample_parent: User, test_engine):
    """POST /api/v1/centres/match with profile_id loads criteria from user's profile."""
    create_centres_with_cohorts(test_engine)
    set_mock_auth(sample_parent)

    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        profile = FamilyPreferenceProfile(
            profile_id="profile-uuid-001",
            user_id=sample_parent.user_id,
            profile_name="Baby Liam",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=6000.0,
            household_member_count=3,
            budget_max=1200.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=0.50,
            cost_weight=0.30,
            travel_weight=0.20,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
        )
        session.add(profile)
        session.commit()

    payload = {"profile_id": "profile-uuid-001", "top_n": 4}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["profile_id"] == "profile-uuid-001"
    assert data["level"] == "infant"
    assert data["enrollment_month_year"] == "2026-06"
    assert data["total_candidates"] == 6
    assert data["returned_count"] == 4
    assert len(data["results"]) == 4


def test_profile_match_requires_authentication_when_profile_id_provided(
    client: TestClient, test_engine
):
    """When profile_id is provided without authentication, returns 401 Unauthorized."""
    create_centres_with_cohorts(test_engine)

    payload = {"profile_id": str(uuid.uuid4())}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 401
    assert "authentication" in response.json()["detail"].lower()


def test_profile_match_non_existent_profile_returns_404(
    client: TestClient, sample_parent: User, test_engine
):
    """When authenticated user provides a non-existent profile_id, returns 404 Not Found."""
    set_mock_auth(sample_parent)

    payload = {"profile_id": str(uuid.uuid4())}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 404
    assert "profile not found" in response.json()["detail"].lower()


def test_profile_match_other_user_profile_returns_404(
    client: TestClient, sample_parent: User, second_parent: User, test_engine
):
    """User cannot match against a profile belonging to another user (returns 404)."""
    assert second_parent.user_id is not None
    with Session(test_engine) as session:
        other_profile = FamilyPreferenceProfile(
            profile_id="other-user-profile-001",
            user_id=second_parent.user_id,
            profile_name="David's Child",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=8000.0,
            household_member_count=4,
            budget_max=1500.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        session.add(other_profile)
        session.commit()

    # Authenticate as sample_parent (NOT second_parent)
    set_mock_auth(sample_parent)

    payload = {"profile_id": "other-user-profile-001"}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 404


def test_profile_match_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User, test_engine
):
    """Suspended parent account is rejected with 403 Forbidden."""
    set_mock_auth(suspended_parent)

    payload = {"profile_id": "any-profile-id"}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


# ==============================================================================
# Validation & Edge Case Tests (Ticket #68)
# ==============================================================================


def test_match_missing_both_profile_and_level_returns_422(client: TestClient):
    """Neither profile_id nor level provided returns 422 Unprocessable Entity."""
    payload = {"enrollment_month_year": "2026-06"}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 422


def test_match_missing_enrollment_month_when_no_profile_returns_422(client: TestClient):
    """Level provided but enrollment_month_year missing returns 422 Unprocessable Entity."""
    payload = {"level": "infant"}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 422


def test_match_invalid_enrollment_month_format_returns_422(client: TestClient):
    """Invalid enrollment_month_year format returns 422 Unprocessable Entity."""
    invalid_formats = ["2026-13", "2026/06", "June-2026", "2026-00", "not-a-date"]
    for bad_month in invalid_formats:
        payload = {"level": "infant", "enrollment_month_year": bad_month}
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 422


def test_match_invalid_top_n_bounds_returns_422(client: TestClient):
    """top_n parameter outside [1, 20] range returns 422 Unprocessable Entity."""
    for bad_n in [0, -1, 21, 100]:
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "top_n": bad_n,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 422


def test_match_weights_not_summing_to_one_returns_422(client: TestClient):
    """Custom weights that do not sum to 1.0 return 422 Unprocessable Entity."""
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "vacancy_weight": 0.50,
        "cost_weight": 0.50,
        "travel_weight": 0.50,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 422


def test_match_invalid_postal_code_returns_422(client: TestClient):
    """Invalid postal code format returns 422 Unprocessable Entity."""
    for bad_postal in ["12345", "1234567", "ABCDEF"]:
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "home_postal_code": bad_postal,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 422


def test_match_workplace_pref_without_workplace_postal_returns_422(client: TestClient):
    """Location preference WORKPLACE without workplace_postal_code returns 422."""
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "location_preference": "WORKPLACE",
        "workplace_postal_code": None,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 422


def test_match_no_matching_centres_returns_200_empty_results(
    client: TestClient, test_engine
):
    """When database has centres but none offer requested level, returns 200 with empty list."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=99,
            centre_code="C099",
            centre_name="Senior Student Care",
            postal="560123",
        )
        session.add(centre)
        session.commit()
        session.add(
            PreschoolService(
                centre_id=99,
                type_of_service="Full Day",
                level=PreschoolLevel.STUDENT_CARE,
                fee=600.0,
            )
        )
        session.commit()

    payload = {"level": "infant", "enrollment_month_year": "2026-06"}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["total_candidates"] == 0
    assert data["returned_count"] == 0
    assert data["results"] == []


def test_match_response_serializes_centre_details_and_district(
    client: TestClient, test_engine
):
    """Response serializes rich centre metadata including postal district calculation."""
    create_centres_with_cohorts(test_engine)

    payload = {"level": "infant", "enrollment_month_year": "2026-06", "top_n": 1}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert len(data["results"]) == 1
    item = data["results"][0]
    assert item["centre_name"] == "Alpha Preschool"
    assert item["postal"] == "560123"
    assert (
        item["district"] == 20
    )  # Sector 56 is District 20 (Ang Mo Kio, Bishan, Thomson)
    assert "Ang Mo Kio" in item["district_name"]
    assert item["spark_certified"] is True
    assert item["gov_subsidy"] is True
    assert item["level"] == "infant"
    assert item["enrollment_month_year"] == "2026-06"
    assert item["vacancy_fit_score"] == 100.0
    assert item["overall_fit_score"] == 100.0
    assert item["cost_fit_score"] is None
    assert item["travel_fit_score"] is None
    assert item["highlight"] is None
    assert data["explanation"] is None

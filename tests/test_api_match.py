"""Tests for Multi-Criteria Centre Matching API endpoint (POST /api/v1/centres/match - Ticket #68)."""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import (
    FirebaseTokenClaims,
    get_optional_token_claims,
    get_token_claims,
)
from kidcarematch_backend.core.config import settings
from kidcarematch_backend.core.explanation import (
    MatchCriteria,
    build_explanation_prompt,
    generate_deterministic_fallback,
)
from kidcarematch_backend.core.onemap import onemap_token_manager
from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CareType,
    CitizenshipType,
    FamilyPreferenceProfile,
    PostalCoordinateCache,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    SubsidyMetric,
    SubsidyRule,
    User,
    UserRole,
    VacancyRecord,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app
from kidcarematch_backend.schemas.match import (
    CentreMatchRequest,
    FitExplanationResponse,
    MatchCandidateItem,
)


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


def test_profile_match_success(client: TestClient, sample_parent: User, test_engine, monkeypatch):
    """POST /api/v1/centres/match with profile_id loads criteria from user's profile."""
    create_centres_with_cohorts(test_engine)
    setup_onemap_mock(monkeypatch)
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
            vacancy_weight=3,
            cost_weight=3,
            travel_weight=3,
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


def test_match_weights_default_to_3_and_allow_independent_ratings(client: TestClient):
    """Inline matching defaults weights to 3/3/3 when omitted and accepts independent 1-5 integers."""
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200

    # Custom independent ratings e.g. 5/5/1 without sum-to-1.0 restriction
    payload_custom = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "vacancy_weight": 5,
        "cost_weight": 5,
        "travel_weight": 1,
    }
    response_custom = client.post("/api/v1/centres/match", json=payload_custom)
    assert response_custom.status_code == 200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vacancy_weight", 0),
        ("vacancy_weight", -1),
        ("vacancy_weight", 6),
        ("vacancy_weight", 10),
        ("cost_weight", 0),
        ("cost_weight", 6),
        ("travel_weight", 0),
        ("travel_weight", 6),
        ("vacancy_weight", 0.4),
        ("cost_weight", 3.0),
        ("travel_weight", 2.5),
        ("vacancy_weight", "3"),
        ("cost_weight", True),
        ("travel_weight", False),
        ("vacancy_weight", None),
        ("cost_weight", None),
        ("travel_weight", None),
    ],
)
def test_match_weights_invalid_values_return_422(
    client: TestClient, field: str, value: Any
):
    """Weights < 1, > 5, floats, strings, booleans, and nulls return 422."""
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        field: value,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 422


def test_match_criteria_effective_weights_normalization():
    """MatchCriteria normalizes raw ratings (1-5) or proportion inputs to Effective Matching Weights."""
    # Default 3/3/3
    c_default = MatchCriteria(level=PreschoolLevel.INFANT, enrollment_month_year="2026-06")
    assert abs(c_default.vacancy_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_default.cost_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_default.travel_weight - (1.0 / 3.0)) < 1e-6

    # Tied 1/1/1
    c_tied_1 = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=1,
        cost_weight=1,
        travel_weight=1,
    )
    assert abs(c_tied_1.vacancy_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_tied_1.cost_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_tied_1.travel_weight - (1.0 / 3.0)) < 1e-6

    # Tied 5/5/5
    c_tied_5 = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=5,
        cost_weight=5,
        travel_weight=5,
    )
    assert abs(c_tied_5.vacancy_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_tied_5.cost_weight - (1.0 / 3.0)) < 1e-6
    assert abs(c_tied_5.travel_weight - (1.0 / 3.0)) < 1e-6

    # Skewed 5/5/1 -> 5/11, 5/11, 1/11
    c_skewed = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=5,
        cost_weight=5,
        travel_weight=1,
    )
    assert abs(c_skewed.vacancy_weight - (5.0 / 11.0)) < 1e-6
    assert abs(c_skewed.cost_weight - (5.0 / 11.0)) < 1e-6
    assert abs(c_skewed.travel_weight - (1.0 / 11.0)) < 1e-6
    assert abs(c_skewed.vacancy_weight + c_skewed.cost_weight + c_skewed.travel_weight - 1.0) < 1e-6

    # Pre-normalized floats summing to 1.0 are preserved
    c_float = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=0.50,
        cost_weight=0.25,
        travel_weight=0.25,
    )
    assert abs(c_float.vacancy_weight - 0.50) < 1e-6
    assert abs(c_float.cost_weight - 0.25) < 1e-6
    assert abs(c_float.travel_weight - 0.25) < 1e-6


def test_fit_explanation_effective_weights_percentage_formatting():
    """Fit explanation prompt and fallback format Effective Matching Weights as round(w * 100)%."""
    # 5/5/1 -> 45%, 45%, 9%
    c_551 = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=5,
        cost_weight=5,
        travel_weight=1,
    )
    cand = MatchCandidateItem(
        centre_id=1,
        centre_code="C001",
        centre_name="Sample Centre",
        contact="12345678",
        email=None,
        postal="560123",
        website_url=None,
        food_offered="Yes",
        address="123 Street",
        second_languages="Chinese",
        spark_certified=False,
        weekday_full_day_hours="07:00 - 19:00",
        saturday_hours=None,
        extended_operating_hours=False,
        transport_provided=False,
        gov_subsidy=True,
        last_updated=None,
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_status="Available",
        vacancy_fit_score=100.0,
        cost_fit_score=100.0,
        travel_fit_score=100.0,
        overall_fit_score=100.0,
    )
    prompt_551 = build_explanation_prompt(criteria=c_551, total_candidates=1, candidates=[cand])
    assert "Vacancy Weight: 45%, Cost Weight: 45%, Travel Weight: 9%" in prompt_551

    fallback_551 = generate_deterministic_fallback(criteria=c_551, total_candidates=1, candidates=[cand])
    assert "45% Vacancy Fit, 45% Cost Fit, and 9% Travel Fit" in fallback_551.summary_narrative

    # 3/3/3 -> 33%, 33%, 33%
    c_333 = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        vacancy_weight=3,
        cost_weight=3,
        travel_weight=3,
    )
    prompt_333 = build_explanation_prompt(criteria=c_333, total_candidates=1, candidates=[cand])
    assert "Vacancy Weight: 33%, Cost Weight: 33%, Travel Weight: 33%" in prompt_333

    fallback_333 = generate_deterministic_fallback(criteria=c_333, total_candidates=1, candidates=[cand])
    assert "33% Vacancy Fit, 33% Cost Fit, and 33% Travel Fit" in fallback_333.summary_narrative


def test_profile_linked_match_equal_ratings_produce_identical_results(
    client: TestClient, sample_parent: User, test_engine, monkeypatch
):
    """Profiles with 1/1/1 and 5/5/5 produce identical Effective Matching Weights (1/3 each) and fit scores."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)
    setup_onemap_mock(monkeypatch)
    set_mock_auth(sample_parent)

    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        p1 = FamilyPreferenceProfile(
            profile_id="profile-tied-111",
            user_id=sample_parent.user_id,
            profile_name="Tied 1",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=5000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=1,
            cost_weight=1,
            travel_weight=1,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
        )
        p5 = FamilyPreferenceProfile(
            profile_id="profile-tied-555",
            user_id=sample_parent.user_id,
            profile_name="Tied 5",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=5000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=5,
            cost_weight=5,
            travel_weight=5,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
        )
        session.add_all([p1, p5])
        session.commit()

    res1 = client.post("/api/v1/centres/match", json={"profile_id": "profile-tied-111", "top_n": 5})
    assert res1.status_code == 200
    data1 = res1.json()

    res5 = client.post("/api/v1/centres/match", json={"profile_id": "profile-tied-555", "top_n": 5})
    assert res5.status_code == 200
    data5 = res5.json()

    assert len(data1["results"]) == len(data5["results"])
    for r1, r5 in zip(data1["results"], data5["results"]):
        assert r1["centre_code"] == r5["centre_code"]
        assert r1["overall_fit_score"] == r5["overall_fit_score"]
        assert r1["vacancy_fit_score"] == r5["vacancy_fit_score"]
        assert r1["cost_fit_score"] == r5["cost_fit_score"]
        assert r1["travel_fit_score"] == r5["travel_fit_score"]


def test_profile_linked_match_5_5_1_effective_weights_and_explanation(
    client: TestClient, sample_parent: User, test_engine, monkeypatch
):
    """Profile-linked match with 5/5/1 derives 5/11, 5/11, 1/11 and reflects 45%, 45%, 9% in fallback."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)
    setup_onemap_mock(monkeypatch)
    set_mock_auth(sample_parent)

    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        profile = FamilyPreferenceProfile(
            profile_id="profile-skewed-551",
            user_id=sample_parent.user_id,
            profile_name="Skewed 551",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=5000.0,
            household_member_count=3,
            budget_max=1000.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=5,
            cost_weight=5,
            travel_weight=1,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
        )
        session.add(profile)
        session.commit()

    res = client.post("/api/v1/centres/match", json={"profile_id": "profile-skewed-551", "top_n": 2})
    assert res.status_code == 200
    data = res.json()

    assert data["explanation"] is not None
    narrative = data["explanation"]["summary_narrative"]
    assert "45% Vacancy Fit, 45% Cost Fit, and 9% Travel Fit" in narrative


def test_profile_linked_match_updates_reflect_immediately(
    client: TestClient, sample_parent: User, test_engine, monkeypatch
):
    """Updating a saved profile via PATCH immediately alters subsequent matching results and explanations."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)
    setup_onemap_mock(monkeypatch)
    set_mock_auth(sample_parent)

    # 1. Create profile with 1/1/1
    res_create = client.post(
        "/api/v1/profile",
        json={
            "profile_name": "Dynamic Emma",
            "level": "infant",
            "citizenship_status": "SC",
            "service_required": "Full Day",
            "enrollment_month_year": "2026-06",
            "home_postal_code": "560123",
            "household_income": 5000.0,
            "household_member_count": 3,
            "budget_max": 1000.0,
            "vacancy_weight": 1,
            "cost_weight": 1,
            "travel_weight": 1,
        },
    )
    assert res_create.status_code == 201
    profile_id = res_create.json()["profile_id"]

    # Match initial 1/1/1
    res_m1 = client.post("/api/v1/centres/match", json={"profile_id": profile_id, "top_n": 2})
    assert res_m1.status_code == 200
    assert "33% Vacancy Fit, 33% Cost Fit, and 33% Travel Fit" in res_m1.json()["explanation"]["summary_narrative"]

    # 2. Patch to 5/5/1
    res_patch = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"vacancy_weight": 5, "cost_weight": 5, "travel_weight": 1},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["vacancy_weight"] == 5
    assert res_patch.json()["cost_weight"] == 5
    assert res_patch.json()["travel_weight"] == 1

    # Match immediately
    res_m2 = client.post("/api/v1/centres/match", json={"profile_id": profile_id, "top_n": 2})
    assert res_m2.status_code == 200
    assert "45% Vacancy Fit, 45% Cost Fit, and 9% Travel Fit" in res_m2.json()["explanation"]["summary_narrative"]


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
    assert item["highlight"] is not None
    assert data["explanation"] is not None
    assert "Alpha Preschool" in data["explanation"]["top_centre_rationale"]


# ==============================================================================
# Subsidy Engine and Cost Fit Integration Tests (Ticket #69)
# ==============================================================================


def seed_ecda_subsidy_rules(test_engine):
    """Seed comprehensive ECDA subsidy rules for Infant Care and Child Care."""
    with Session(test_engine) as session:
        rules = [
            # 1. Infant Care - Basic Subsidy (Universal for Working Applicant SC)
            SubsidyRule(
                rule_id="RULE_BASIC_INFANT_WA",
                subsidy_name="Basic Subsidy",
                tier_name="Standard",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.FIXED,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                subsidy_amount=600.0,
            ),
            # 2. Infant Care - Basic Subsidy for Non-Working Applicant SC
            SubsidyRule(
                rule_id="RULE_BASIC_INFANT_NWA",
                subsidy_name="Basic Subsidy",
                tier_name="Standard",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.FIXED,
                applicant_status=ApplicantStatus.NON_WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                subsidy_amount=150.0,
            ),
            # 3. Child Care - Basic Subsidy for Working Applicant SC
            SubsidyRule(
                rule_id="RULE_BASIC_CC_WA",
                subsidy_name="Basic Subsidy",
                tier_name="Standard",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.FIXED,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                subsidy_amount=300.0,
            ),
            # 4. Child Care - Basic Subsidy for Non-Working Applicant SC
            SubsidyRule(
                rule_id="RULE_BASIC_CC_NWA",
                subsidy_name="Basic Subsidy",
                tier_name="Standard",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.FIXED,
                applicant_status=ApplicantStatus.NON_WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                subsidy_amount=150.0,
            ),
            # 5. Infant Care - Additional Subsidy (HHI Tiers)
            SubsidyRule(
                rule_id="RULE_ADD_INFANT_HHI_T1",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 1",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.HOUSEHOLD_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=0.0,
                income_max=3000.0,
                subsidy_amount=710.0,
            ),
            SubsidyRule(
                rule_id="RULE_ADD_INFANT_HHI_T2",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 2",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.HOUSEHOLD_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=3000.01,
                income_max=6000.0,
                subsidy_amount=540.0,
            ),
            # 6. Infant Care - Additional Subsidy (PCI Tiers)
            SubsidyRule(
                rule_id="RULE_ADD_INFANT_PCI_T1",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 1 PCI",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.PER_CAPITA_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=0.0,
                income_max=1500.0,
                subsidy_amount=710.0,
            ),
            SubsidyRule(
                rule_id="RULE_ADD_INFANT_PCI_T2",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 2 PCI",
                care_type=CareType.INFANT_CARE,
                metric=SubsidyMetric.PER_CAPITA_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=1500.01,
                income_max=2500.0,
                subsidy_amount=540.0,
            ),
            # 7. Child Care - Additional Subsidy (HHI Tiers)
            SubsidyRule(
                rule_id="RULE_ADD_CC_HHI_T1",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 1",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.HOUSEHOLD_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=0.0,
                income_max=3000.0,
                subsidy_amount=467.0,
            ),
            SubsidyRule(
                rule_id="RULE_ADD_CC_HHI_T2",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 2",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.HOUSEHOLD_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=3000.01,
                income_max=6000.0,
                subsidy_amount=340.0,
            ),
            # 8. Child Care - Additional Subsidy (PCI Tiers)
            SubsidyRule(
                rule_id="RULE_ADD_CC_PCI_T1",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 1 PCI",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.PER_CAPITA_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=0.0,
                income_max=1500.0,
                subsidy_amount=467.0,
            ),
            SubsidyRule(
                rule_id="RULE_ADD_CC_PCI_T2",
                subsidy_name="Additional Subsidy",
                tier_name="Tier 2 PCI",
                care_type=CareType.CHILD_CARE,
                metric=SubsidyMetric.PER_CAPITA_INCOME,
                applicant_status=ApplicantStatus.WORKING_APPLICANT,
                citizenship=CitizenshipType.SC,
                income_min=1500.01,
                income_max=2500.0,
                subsidy_amount=340.0,
            ),
        ]
        session.add_all(rules)
        session.commit()


def test_match_resolves_published_fee_exact_service_and_citizenship(
    client: TestClient, test_engine
):
    """Preschool service lookup resolves exact match for requested level, service type, and citizenship."""
    create_centres_with_cohorts(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "service_required": "Full Day",
        "citizenship_status": "SC",
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1
    item = data["results"][0]

    # Centre 1 Alpha has fee=1200.0 for infant Full Day SC
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1200.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 1200.0


def test_match_resolves_published_fee_citizenship_fallback_to_others(
    client: TestClient, test_engine
):
    """When child's citizenship tier (e.g. SPR) is not published, service lookup falls back to OTHERS tier."""
    # Seed centre with only OTHERS fee
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=101,
            centre_code="C101",
            centre_name="International Montessori",
            postal="123456",
        )
        session.add(centre)
        session.commit()

        service = PreschoolService(
            centre_id=101,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1850.0,
            citizenship_type=CitizenshipType.OTHERS,
        )
        vacancy = VacancyRecord(
            record_id="VR101",
            centre_id=101,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_status="Available",
        )
        session.add_all([service, vacancy])
        session.commit()

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SPR",
        "service_required": "Full Day",
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["centre_code"] == "C101"
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1850.0


def test_match_resolves_published_fee_service_type_fallback_to_full_day(
    client: TestClient, test_engine
):
    """When requested service type (e.g. Half Day) is not offered, lookup falls back to Full Day."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=102,
            centre_code="C102",
            centre_name="Full Day Only Academy",
            postal="234567",
        )
        session.add(centre)
        session.commit()

        service = PreschoolService(
            centre_id=102,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1400.0,
            citizenship_type=CitizenshipType.SC,
        )
        vacancy = VacancyRecord(
            record_id="VR102",
            centre_id=102,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_status="Available",
        )
        session.add_all([service, vacancy])
        session.commit()

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "service_required": "Half Day",
        "citizenship_status": "SC",
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["centre_code"] == "C102"
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1400.0


def test_match_extracts_incidental_fee_remarks(
    client: TestClient, test_engine
):
    """Published service remarks and incidental fees are captured in fee_breakdown."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=103,
            centre_code="C103",
            centre_name="Sunny Days Learning",
            postal="345678",
        )
        session.add(centre)
        session.commit()

        service = PreschoolService(
            centre_id=103,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1250.0,
            citizenship_type=CitizenshipType.SC,
            remarks="Registration Fee: $80 (non-refundable), Deposit: 1 month, Uniform: $35/set",
        )
        vacancy = VacancyRecord(
            record_id="VR103",
            centre_id=103,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_status="Available",
        )
        session.add_all([service, vacancy])
        session.commit()

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "service_required": "Full Day",
        "citizenship_status": "SC",
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["centre_code"] == "C103"
    assert item["fee_breakdown"] is not None
    assert "Registration Fee: $80" in item["fee_breakdown"]["remarks"]
    assert "Registration Fee: $80" in item["fee_breakdown"]["incidental_fee_remarks"]


def test_match_subsidy_engine_infant_care_basic_and_additional_subsidy(
    client: TestClient, test_engine
):
    """Subsidy engine sums Fixed Basic Subsidy and means-tested Additional Subsidy for working applicants."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    # Working mother with HHI $5,000, 3 members (PCI = $1,666.67)
    # Basic Subsidy = $600
    # HHI $5,000 -> Tier 2 ($540)
    # PCI $1,666.67 -> Tier 2 PCI ($540)
    # Total Subsidy = 600 + 540 = $1,140
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "applicant_status": "WORKING_APPLICANT",
        "household_income": 5000.0,
        "household_member_count": 3,
        "budget_max": 200.0,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1200.0
    assert item["fee_breakdown"]["estimated_subsidy"] == 1140.0
    # Net payable fee = 1200 - 1140 = $60.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 60.0
    # Budget max = 200.0 >= 60.0 -> Cost Fit = 100.0%
    assert item["fee_breakdown"]["cost_fit"] == 100.0
    assert item["cost_fit_score"] == 100.0


def test_match_subsidy_engine_pci_generosity_for_large_households(
    client: TestClient, test_engine
):
    """Large household qualifies for higher Additional Subsidy under Per-Capita Income (PCI) than HHI."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    # Household income = $6,000, 5 members:
    # HHI $6,000 falls in Tier 2 ($540)
    # PCI = $6,000 / 5 = $1,200 falls in Tier 1 ($710)!
    # Engine must apply the maximum eligible benefit -> $710
    # Total Subsidy = Basic ($600) + Additional ($710) = $1,310
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "applicant_status": "WORKING_APPLICANT",
        "household_income": 6000.0,
        "household_member_count": 5,
        "budget_max": 100.0,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1200.0
    assert item["fee_breakdown"]["estimated_subsidy"] == 1310.0
    # Published fee 1200.0 < Subsidy 1310.0 -> payable fee is capped at 0.0 (never negative)
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 0.0
    assert item["fee_breakdown"]["cost_fit"] == 100.0
    assert item["cost_fit_score"] == 100.0


def test_match_subsidy_engine_care_type_mapping_child_care(
    client: TestClient, test_engine
):
    """Preschool cohort levels PG, N1, N2, K1, K2 map to CareType.CHILD_CARE."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    # Level PG -> CareType.CHILD_CARE
    # Working applicant, HHI $2,500, 3 members (PCI = $833.33)
    # CC Basic Subsidy = $300
    # CC Additional Subsidy Tier 1 (HHI <= 3000) = $467
    # Total Subsidy = 300 + 467 = $767.0
    payload = {
        "level": "pg",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "applicant_status": "WORKING_APPLICANT",
        "household_income": 2500.0,
        "household_member_count": 3,
        "budget_max": 300.0,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    # Centre 1 offers PG at $900.0
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 900.0
    assert item["fee_breakdown"]["estimated_subsidy"] == 767.0
    # Net payable fee = 900 - 767 = $133.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 133.0
    assert item["fee_breakdown"]["cost_fit"] == 100.0  # 133 <= 300


def test_match_subsidy_engine_non_working_applicant_subsidy(
    client: TestClient, test_engine
):
    """Non-working applicants receive non-working subsidy schedules ($150) and no Additional Subsidy."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "applicant_status": "NON_WORKING_APPLICANT",
        "household_income": 2000.0,
        "household_member_count": 3,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    # Non-working applicant gets fixed basic subsidy = $150.0 only
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1200.0
    assert item["fee_breakdown"]["estimated_subsidy"] == 150.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 1050.0


def test_match_subsidy_engine_foreigner_child_zero_subsidy(
    client: TestClient, test_engine
):
    """Children with citizenship_status OTHERS receive 0 government subsidy."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "OTHERS",
        "household_income": 3000.0,
        "household_member_count": 3,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["estimated_subsidy"] == 0.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == item["fee_breakdown"]["published_fee"]


def test_match_cost_fit_scoring_proportional_scaling_when_budget_exceeded(
    client: TestClient, test_engine
):
    """Cost Fit scales down proportionally when payable fee exceeds parent's budget ceiling."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)

    # Published fee = $1200. Basic Subsidy only = $600 (high income $15,000 -> no Additional Subsidy)
    # Net payable fee = $600.
    # Budget max = $450.
    # Cost Fit = min(100.0, (450 / 600) * 100) = 75.0%
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "household_income": 15000.0,
        "household_member_count": 3,
        "budget_max": 450.0,
        "top_n": 1,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    assert item["fee_breakdown"]["published_fee"] == 1200.0
    assert item["fee_breakdown"]["estimated_subsidy"] == 600.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 600.0
    assert item["fee_breakdown"]["cost_fit"] == 75.0
    assert item["cost_fit_score"] == 75.0


def test_match_candidate_ranking_incorporates_cost_fit_and_vacancy_fit(
    client: TestClient, test_engine
):
    """Centres with identical Vacancy Fit are differentiated by Cost Fit in ranking."""
    seed_ecda_subsidy_rules(test_engine)

    with Session(test_engine) as session:
        # Centre A: Available, Net fee = $800
        # Centre B: Available, Net fee = $1200
        cA = PreschoolCentre(
            centre_id=201,
            centre_code="C201",
            centre_name="Affordable Centre",
            postal="560201",
        )
        cB = PreschoolCentre(
            centre_id=202,
            centre_code="C202",
            centre_name="Expensive Centre",
            postal="560202",
        )
        session.add_all([cA, cB])
        session.commit()

        sA = PreschoolService(
            centre_id=201,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=800.0,
            citizenship_type=CitizenshipType.SC,
        )
        sB = PreschoolService(
            centre_id=202,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1200.0,
            citizenship_type=CitizenshipType.SC,
        )
        vA = VacancyRecord(
            record_id="VR201",
            centre_id=201,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_status="Available",
        )
        vB = VacancyRecord(
            record_id="VR202",
            centre_id=202,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_status="Available",
        )
        session.add_all([sA, sB, vA, vB])
        session.commit()

    # Budget max = 500, Income = 15000 (Basic subsidy $600 only)
    # Centre A payable = 800 - 600 = $200 <= 500 -> Cost Fit = 100.0%
    # Centre B payable = 1200 - 600 = $600 > 500 -> Cost Fit = (500 / 600) * 100 = 83.33%
    # Both have Vacancy Fit = 100.0%.
    # Centre A has overall fit > Centre B -> Centre A ranks 1st!
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "household_income": 15000.0,
        "household_member_count": 3,
        "budget_max": 500.0,
        "vacancy_weight": 3,
        "cost_weight": 3,
        "travel_weight": 3,
        "top_n": 2,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    results = data["results"]
    assert len(results) == 2
    assert results[0]["centre_code"] == "C201"
    assert results[0]["cost_fit_score"] == 100.0
    assert results[0]["overall_fit_score"] == 100.0

    assert results[1]["centre_code"] == "C202"
    assert results[1]["cost_fit_score"] == 83.33
    assert results[1]["overall_fit_score"] < 100.0


def test_match_profile_linked_end_to_end_fee_and_cost_fit(
    client: TestClient, sample_parent: User, test_engine, monkeypatch
):
    """Profile-linked match executes complete fee resolution, subsidy means-testing, and cost fit scoring."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)
    setup_onemap_mock(monkeypatch)
    set_mock_auth(sample_parent)

    assert sample_parent.user_id is not None
    with Session(test_engine) as session:
        profile = FamilyPreferenceProfile(
            profile_id="profile-subsidy-001",
            user_id=sample_parent.user_id,
            profile_name="Baby Emma",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code="560123",
            household_income=5000.0,
            household_member_count=3,
            budget_max=100.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=3,
            cost_weight=3,
            travel_weight=3,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
        )
        session.add(profile)
        session.commit()

    payload = {"profile_id": "profile-subsidy-001", "top_n": 1}
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["profile_id"] == "profile-subsidy-001"
    item = data["results"][0]
    assert item["fee_breakdown"] is not None
    assert item["fee_breakdown"]["published_fee"] == 1200.0
    # Basic ($600) + Additional ($540) = $1140
    assert item["fee_breakdown"]["estimated_subsidy"] == 1140.0
    assert item["fee_breakdown"]["estimated_monthly_payable_fee"] == 60.0
    assert item["fee_breakdown"]["cost_fit"] == 100.0
    assert item["cost_fit_score"] == 100.0


# ==============================================================================
# OneMap Two-Stage Detour Routing and Travel Fit Tests (Ticket #70)
# ==============================================================================


def setup_onemap_mock(
    monkeypatch,
    postal_coords: dict[str, tuple[float, float]] | None = None,
    route_durations: dict[str, int] | None = None,
    route_fails: bool = False,
    auth_token: str = "mock-onemap-token-jwt",
    token_fails: bool = False,
    auth_call_count: dict[str, int] | None = None,
    route_401_first_n: int = 0,
    search_401_first_n: int = 0,
    recorded_headers: list[dict[str, str]] | None = None,
):
    """Setup mock for OneMap auth, search (geocoding), and routing endpoints."""
    onemap_token_manager.invalidate_token()

    route_401_remaining = {"count": route_401_first_n}
    search_401_remaining = {"count": search_401_first_n}
    auth_calls = auth_call_count if auth_call_count is not None else {"count": 0}

    default_coords = {
        "560123": (1.3691, 103.8512),  # Ang Mo Kio (Home)
        "560124": (1.3695, 103.8520),  # Close to Home
        "560125": (1.3780, 103.8550),  # Moderate distance
        "560126": (1.3800, 103.8560),  # Delta Kids Haven
        "560127": (1.3820, 103.8570),  # Epsilon Infant Academy
        "560128": (1.3840, 103.8580),  # Zeta Early Years
        "560129": (1.3860, 103.8590),  # Omega Kindergarten
        "048581": (1.2840, 103.8510),  # Raffles Place (Workplace)
        "600123": (1.3320, 103.7430),  # Jurong East (Far outlier)
        "520123": (1.3520, 103.9440),  # Tampines
    }
    coords_table = {**default_coords, **(postal_coords or {})}
    search_call_count = {"count": 0}
    route_call_count = {"count": 0}

    original_get = httpx.Client.get
    original_post = httpx.Client.post

    def mock_post(self, url, *args, **kwargs):
        url_str = str(url)
        if "onemap.gov.sg/api/auth/post/getToken" in url_str:
            auth_calls["count"] += 1
            if token_fails:
                mock_resp = MagicMock()
                mock_resp.status_code = 401

                def _raise_auth():
                    req = httpx.Request("POST", url)
                    resp = httpx.Response(401, request=req)
                    raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

                mock_resp.raise_for_status = _raise_auth
                mock_resp.json.return_value = {"error": "Invalid service account credentials"}
                return mock_resp

            token = f"{auth_token}-v{auth_calls['count']}" if auth_calls["count"] > 1 else auth_token
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "access_token": token,
                "expiry_timestamp": time.time() + 86400,
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp
        return original_post(self, url, *args, **kwargs)

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if recorded_headers is not None:
            recorded_headers.append(dict(kwargs.get("headers") or {}))

        if "onemap.gov.sg/api/common/elastic/search" in url_str:
            search_call_count["count"] += 1
            if search_401_remaining["count"] > 0:
                search_401_remaining["count"] -= 1
                mock_resp = MagicMock()
                mock_resp.status_code = 401

                def _raise_search():
                    req = httpx.Request("GET", url)
                    resp = httpx.Response(401, request=req)
                    raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

                mock_resp.raise_for_status = _raise_search
                mock_resp.json.return_value = {"error": "Unauthorized"}
                return mock_resp

            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", "")).strip()
            if postal in coords_table:
                lat, lon = coords_table[postal]
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "found": 1,
                    "totalNumPages": 1,
                    "pageNum": 1,
                    "results": [
                        {
                            "SEARCHVAL": f"POSTAL_{postal}",
                            "POSTAL": postal,
                            "LATITUDE": str(lat),
                            "LONGITUDE": str(lon),
                            "X": "29800.0",
                            "Y": "39100.0",
                            "ADDRESS": f"Address for {postal}",
                        }
                    ],
                }
                mock_resp.raise_for_status = MagicMock()
                return mock_resp
            else:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"found": 0, "results": []}
                mock_resp.raise_for_status = MagicMock()
                return mock_resp

        elif "onemap.gov.sg/api/public/routingsvc/route" in url_str:
            route_call_count["count"] += 1
            if route_fails:
                raise httpx.ConnectError("OneMap Routing service unavailable")

            if route_401_remaining["count"] > 0:
                route_401_remaining["count"] -= 1
                mock_resp = MagicMock()
                mock_resp.status_code = 401

                def _raise_route():
                    req = httpx.Request("GET", url)
                    resp = httpx.Response(401, request=req)
                    raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

                mock_resp.raise_for_status = _raise_route
                mock_resp.json.return_value = {"error": "Unauthorized"}
                return mock_resp

            params = kwargs.get("params", {})
            start_str = str(params.get("start", ""))
            end_str = str(params.get("end", ""))

            def _norm_coord(s: str) -> str:
                parts = s.split(",")
                if len(parts) == 2:
                    return f"{float(parts[0]):.4f},{float(parts[1]):.4f}"
                return s

            norm_durations = {
                f"{_norm_coord(k.split('->')[0])}->{_norm_coord(k.split('->')[1])}": v
                for k, v in (route_durations or {}).items()
            }
            norm_key = f"{_norm_coord(start_str)}->{_norm_coord(end_str)}"
            duration_secs = norm_durations.get(norm_key, 1200)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": duration_secs,
                        }
                    ]
                }
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    monkeypatch.setattr(httpx.Client, "post", mock_post)
    return search_call_count, route_call_count


def test_onemap_coordinate_resolution_and_persistent_caching(
    client: TestClient, test_engine, monkeypatch
):
    """Postal codes are resolved via OneMap API and persistently cached in database."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=101,
            centre_code="C_GEO_01",
            centre_name="Geo Centre 1",
            postal="560124",
        )
        session.add(c1)
        s1 = PreschoolService(
            centre_id=101,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        session.add(s1)
        v1 = VacancyRecord(
            record_id="v_geo_01",
            centre_id=101,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add(v1)
        session.commit()

    search_count, _ = setup_onemap_mock(monkeypatch)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "top_n": 5,
    }

    # 1. First invocation: resolves postal codes via OneMap Search API
    response1 = client.post("/api/v1/centres/match", json=payload)
    assert response1.status_code == 200
    first_calls = search_count["count"]
    assert first_calls >= 2  # Home "560123" and Centre "560124"

    # Verify cached in database table PostalCoordinateCache
    with Session(test_engine) as session:
        home_cached = session.get(PostalCoordinateCache, "560123")
        assert home_cached is not None
        assert home_cached.latitude == 1.3691
        assert home_cached.longitude == 103.8512

        centre_cached = session.get(PostalCoordinateCache, "560124")
        assert centre_cached is not None
        assert centre_cached.latitude == 1.3695
        assert centre_cached.longitude == 103.8520

    # 2. Second invocation: must hit persistent database cache and NOT query OneMap
    response2 = client.post("/api/v1/centres/match", json=payload)
    assert response2.status_code == 200
    assert search_count["count"] == first_calls  # Zero additional API calls!


def test_single_anchor_travel_fit_normalization(
    client: TestClient, test_engine, monkeypatch
):
    """Single-anchor search computes physical distance; best candidate receives 100%, worst receives 0%."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        # Centre A: very close (postal 560124)
        c_a = PreschoolCentre(
            centre_id=111,
            centre_code="C_A",
            centre_name="Centre Close",
            postal="560124",
        )
        s_a = PreschoolService(
            centre_id=111,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v_a = VacancyRecord(
            record_id="v_a",
            centre_id=111,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        # Centre B: moderate distance (postal 560125)
        c_b = PreschoolCentre(
            centre_id=112,
            centre_code="C_B",
            centre_name="Centre Mid",
            postal="560125",
        )
        s_b = PreschoolService(
            centre_id=112,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v_b = VacancyRecord(
            record_id="v_b",
            centre_id=112,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        # Centre C: distant (postal 600123)
        c_c = PreschoolCentre(
            centre_id=113,
            centre_code="C_C",
            centre_name="Centre Far",
            postal="600123",
        )
        s_c = PreschoolService(
            centre_id=113,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v_c = VacancyRecord(
            record_id="v_c",
            centre_id=113,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        session.add_all([c_a, s_a, v_a, c_b, s_b, v_b, c_c, s_c, v_c])
        session.commit()

    setup_onemap_mock(monkeypatch)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "top_n": 3,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    results = data["results"]
    assert len(results) == 3

    # Close centre ranks 1st with 100.0% travel fit
    assert results[0]["centre_code"] == "C_A"
    assert results[0]["travel_fit_score"] == 100.0
    assert results[0]["travel_breakdown"] is not None
    assert results[0]["travel_breakdown"]["commute_distance_km"] is not None
    assert results[0]["travel_breakdown"]["commute_duration_mins"] is not None

    # Far centre ranks last with 0.0% travel fit
    assert results[2]["centre_code"] == "C_C"
    assert results[2]["travel_fit_score"] == 0.0

    # Middle centre has linear normalized score between 0 and 100
    assert results[1]["centre_code"] == "C_B"
    assert 0.0 < results[1]["travel_fit_score"] < 100.0


def test_dual_anchor_commute_deviation_detour_routing(
    client: TestClient, test_engine, monkeypatch
):
    """Dual-anchor search evaluates detour delay deviation relative to direct baseline commute route."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        # Centre 1: on corridor with small detour
        c1 = PreschoolCentre(
            centre_id=121,
            centre_code="C_DETOUR_01",
            centre_name="On-Route Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=121,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_d1",
            centre_id=121,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        # Centre 2: big detour
        c2 = PreschoolCentre(
            centre_id=122,
            centre_code="C_DETOUR_02",
            centre_name="Off-Route Centre",
            postal="520123",
        )
        s2 = PreschoolService(
            centre_id=122,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v2 = VacancyRecord(
            record_id="v_d2",
            centre_id=122,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        session.add_all([c1, s1, v1, c2, s2, v2])
        session.commit()

    # Define route travel times:
    # Home: 1.3691,103.8512
    # Workplace: 1.2840,103.8510
    # C1: 1.3695,103.8520
    # C2: 1.3520,103.9440
    route_durations = {
        # Baseline: Home -> Workplace = 1800s (30 mins)
        "1.3691,103.8512->1.2840,103.8510": 1800,
        # C1 route: Home -> C1 (600s = 10 mins) + C1 -> Workplace (1500s = 25 mins) => 35 mins (detour = 5 mins)
        "1.3691,103.8512->1.3695,103.852": 600,
        "1.3695,103.852->1.2840,103.8510": 1500,
        # C2 route: Home -> C2 (1800s = 30 mins) + C2 -> Workplace (2700s = 45 mins) => 75 mins (detour = 45 mins)
        "1.3691,103.8512->1.352,103.944": 1800,
        "1.352,103.944->1.2840,103.8510": 2700,
    }
    setup_onemap_mock(monkeypatch, route_durations=route_durations)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 2,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    results = data["results"]
    assert len(results) == 2

    # C1 has least detour delay (5 mins vs 45 mins) -> 100.0% Travel Fit
    assert results[0]["centre_code"] == "C_DETOUR_01"
    assert results[0]["travel_fit_score"] == 100.0
    tb1 = results[0]["travel_breakdown"]
    assert tb1 is not None
    assert tb1["commute_duration_mins"] == 35.0
    assert tb1["commute_deviation_mins"] == 5.0
    assert tb1["baseline_duration_mins"] == 30.0

    # C2 has greatest detour delay -> 0.0% Travel Fit
    assert results[1]["centre_code"] == "C_DETOUR_02"
    assert results[1]["travel_fit_score"] == 0.0
    tb2 = results[1]["travel_breakdown"]
    assert tb2 is not None
    assert tb2["commute_duration_mins"] == 75.0
    assert tb2["commute_deviation_mins"] == 45.0
    assert tb2["baseline_duration_mins"] == 30.0


def test_stage1_spatial_corridor_prefiltering_top_30(
    client: TestClient, test_engine, monkeypatch
):
    """When candidate pool has > 30 candidates, Stage 1 prunes distant outliers down to top 30."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {"560123": (1.3691, 103.8512)}  # Home

    with Session(test_engine) as session:
        # Create 30 centres close to Home (small variations in lat/lon)
        for i in range(1, 31):
            p_code = f"56{i:04d}"
            coords_dict[p_code] = (1.3691 + (i * 0.001), 103.8512 + (i * 0.001))
            c = PreschoolCentre(
                centre_id=200 + i,
                centre_code=f"C_NEAR_{i:02d}",
                centre_name=f"Near Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=200 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_near_{i}",
                centre_id=200 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])

        # Create 5 distant outlier centres (e.g. Tuas / Jurong > 20 km away)
        for j in range(1, 6):
            p_code = f"63{j:04d}"
            coords_dict[p_code] = (1.3100, 103.6500 + (j * 0.005))
            c = PreschoolCentre(
                centre_id=300 + j,
                centre_code=f"C_OUTLIER_{j:02d}",
                centre_name=f"Outlier Centre {j:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=300 + j,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_outlier_{j}",
                centre_id=300 + j,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])

        session.commit()

    setup_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "top_n": 20,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Total candidates in database is 35
    assert data["total_candidates"] == 35
    assert data["returned_count"] == 20

    # Ensure none of the 5 distant outliers are returned in the top results
    returned_codes = {r["centre_code"] for r in data["results"]}
    for j in range(1, 6):
        assert f"C_OUTLIER_{j:02d}" not in returned_codes


def test_travel_fit_normalization_identical_deviations_receives_100(
    client: TestClient, test_engine, monkeypatch
):
    """When candidates have identical travel deviations (dev_max == dev_min), all receive 100.0%."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=401,
            centre_code="C_SAME_01",
            centre_name="Same Distance 1",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=401,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_same_1",
            centre_id=401,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        c2 = PreschoolCentre(
            centre_id=402,
            centre_code="C_SAME_02",
            centre_name="Same Distance 2",
            postal="560124",  # Exactly same postal / distance
        )
        s2 = PreschoolService(
            centre_id=402,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v2 = VacancyRecord(
            record_id="v_same_2",
            centre_id=402,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        session.add_all([c1, s1, v1, c2, s2, v2])
        session.commit()

    setup_onemap_mock(monkeypatch)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "top_n": 2,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["travel_fit_score"] == 100.0
    assert data["results"][1]["travel_fit_score"] == 100.0


def test_travel_fit_graceful_fallback_when_onemap_routing_fails(
    client: TestClient, test_engine, monkeypatch
):
    """When OneMap Routing API degrades or fails, endpoint falls back gracefully to straight-line estimation without crashing."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=501,
            centre_code="C_FALLBACK_01",
            centre_name="Fallback Centre 1",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=501,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_fb_1",
            centre_id=501,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    # Configure mock with route_fails = True
    setup_onemap_mock(monkeypatch, route_fails=True)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200  # Zero API crashes!
    data = response.json()
    assert len(data["results"]) == 1
    item = data["results"][0]
    assert item["travel_fit_score"] == 100.0  # Only candidate
    assert item["travel_breakdown"] is not None
    assert item["travel_breakdown"]["commute_distance_km"] is not None
    assert item["travel_breakdown"]["commute_duration_mins"] is not None


def test_three_factor_overall_fit_composite_ranking(
    client: TestClient, test_engine, monkeypatch
):
    """Composite Overall Fit combines Vacancy Fit, Cost Fit, and Travel Fit weighted by parental priorities."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        # Centre 1: Vacancy=Available (100%), Fee=$400 (Cost Fit=100%), Travel=Close (100%)
        # Overall = 100*0.4 + 100*0.3 + 100*0.3 = 100.0%
        c1 = PreschoolCentre(
            centre_id=601,
            centre_code="C_COMP_01",
            centre_name="Perfect Match Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=601,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=400.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_comp_1",
            centre_id=601,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )

        # Centre 2: Vacancy=Limited (50%), Fee=$1000 (Cost Fit=50% with $500 budget), Travel=Far (0%)
        # Overall = 50*0.4 + 50*0.3 + 0*0.3 = 20 + 15 + 0 = 35.0%
        c2 = PreschoolCentre(
            centre_id=602,
            centre_code="C_COMP_02",
            centre_name="Lower Match Centre",
            postal="600123",
        )
        s2 = PreschoolService(
            centre_id=602,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v2 = VacancyRecord(
            record_id="v_comp_2",
            centre_id=602,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Limited",
        )

        session.add_all([c1, s1, v1, c2, s2, v2])
        session.commit()

    setup_onemap_mock(monkeypatch)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "budget_max": 500.0,
        "vacancy_weight": 4,
        "cost_weight": 3,
        "travel_weight": 3,
        "top_n": 2,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    results = data["results"]
    assert len(results) == 2

    # Perfect match ranks 1st
    assert results[0]["centre_code"] == "C_COMP_01"
    assert results[0]["vacancy_fit_score"] == 100.0
    assert results[0]["cost_fit_score"] == 100.0
    assert results[0]["travel_fit_score"] == 100.0
    assert results[0]["overall_fit_score"] == 100.0

    # Lower match ranks 2nd
    assert results[1]["centre_code"] == "C_COMP_02"
    assert results[1]["vacancy_fit_score"] == 50.0
    assert results[1]["cost_fit_score"] == 50.0
    assert results[1]["travel_fit_score"] == 0.0
    assert results[1]["overall_fit_score"] == 35.0




# ==============================================================================
# Gemini AI Explanation Service and Resilient Fallback Tests (Ticket #71)
# ==============================================================================


def test_match_gemini_ai_explanation_success(
    client: TestClient, test_engine, monkeypatch
):
    """Verify Gemini AI explanation service produces synthesized narrative, top rationale, and candidate highlights."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

    mock_explanation_data = {
        "summary_narrative": "Alpha Preschool is the top recommendation for infant care enrollment in June 2026 based on high vacancy certainty and balanced affordability.",
        "top_centre_rationale": "Alpha Preschool achieved the #1 rank with 100% Vacancy Fit and competitive monthly fees.",
        "candidate_highlights": [
            "Alpha Preschool: Immediate availability for infant care.",
            "Zeta Early Years: Legacy immediate vacancy status.",
            "Beta Childcare: Limited availability with moderate fees.",
        ],
    }

    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_explanation_data)
    mock_response.parsed = FitExplanationResponse.model_validate(mock_explanation_data)
    mock_client_instance.models.generate_content.return_value = mock_response

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 3,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["explanation"] is not None
        assert data["explanation"]["summary_narrative"] == mock_explanation_data["summary_narrative"]
        assert data["explanation"]["top_centre_rationale"] == mock_explanation_data["top_centre_rationale"]
        assert data["explanation"]["candidate_highlights"] == mock_explanation_data["candidate_highlights"]

        results = data["results"]
        assert len(results) == 3
        assert results[0]["highlight"] == mock_explanation_data["candidate_highlights"][0]
        assert results[1]["highlight"] == mock_explanation_data["candidate_highlights"][1]
        assert results[2]["highlight"] == mock_explanation_data["candidate_highlights"][2]

        assert mock_client_instance.models.generate_content.called
        call_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash"
        contents = call_kwargs["contents"]
        assert "INFANT" in contents
        assert "2026-06" in contents
        assert "Alpha Preschool" in contents


def test_match_resilient_fallback_when_gemini_api_key_is_none(
    client: TestClient, test_engine, monkeypatch
):
    """Verify that when GEMINI_API_KEY is None, the endpoint gracefully falls back to deterministic structured explanation."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)

    with patch("google.genai.Client") as mock_client:
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 2,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert not mock_client.called

        explanation = data["explanation"]
        assert explanation is not None
        assert "INFANT" in explanation["summary_narrative"]
        assert "2026-06" in explanation["summary_narrative"]
        assert "Alpha Preschool" in explanation["top_centre_rationale"]
        assert len(explanation["candidate_highlights"]) == 2
        assert "Alpha Preschool" in explanation["candidate_highlights"][0]

        results = data["results"]
        assert results[0]["highlight"] == explanation["candidate_highlights"][0]
        assert results[1]["highlight"] == explanation["candidate_highlights"][1]


def test_match_resilient_fallback_when_gemini_times_out(
    client: TestClient, test_engine, monkeypatch
):
    """Verify graceful fallback to deterministic explanation when Gemini API times out."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.side_effect = TimeoutError("Request to Gemini API timed out")

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 2,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["explanation"] is not None
        assert "Alpha Preschool" in data["explanation"]["top_centre_rationale"]
        assert len(data["explanation"]["candidate_highlights"]) == 2


def test_match_resilient_fallback_when_gemini_quota_exceeded(
    client: TestClient, test_engine, monkeypatch
):
    """Verify graceful fallback when Gemini quota/rate limits are exceeded."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.side_effect = Exception("429 Resource has been exhausted (check quota)")

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 2,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["explanation"] is not None
        assert "Alpha Preschool" in data["explanation"]["top_centre_rationale"]


def test_match_resilient_fallback_when_gemini_network_error(
    client: TestClient, test_engine, monkeypatch
):
    """Verify graceful fallback when network error occurs during Gemini call."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.side_effect = httpx.ConnectError("Failed to connect to generativelanguage.googleapis.com")

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 2,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["explanation"] is not None
        assert len(data["explanation"]["candidate_highlights"]) == 2


def test_match_resilient_fallback_when_gemini_returns_malformed_json(
    client: TestClient, test_engine, monkeypatch
):
    """Verify graceful fallback when Gemini returns invalid non-JSON output."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Here is the explanation without JSON structure."
    mock_response.parsed = None
    mock_client_instance.models.generate_content.return_value = mock_response

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "top_n": 2,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["explanation"] is not None
        assert "Alpha Preschool" in data["explanation"]["top_centre_rationale"]


def test_match_explanation_empty_candidate_pool(
    client: TestClient, test_engine, monkeypatch
):
    """Verify explanation behavior when no candidate centres match the requested cohort."""
    create_centres_with_cohorts(test_engine)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    payload = {
        "level": "student_care",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["explanation"] is not None
    assert "No preschool centres" in data["explanation"]["summary_narrative"]
    assert "No candidate centres available" in data["explanation"]["top_centre_rationale"]
    assert data["explanation"]["candidate_highlights"] == []


def test_match_prompt_structure_includes_search_params_and_candidate_breakdowns(
    client: TestClient, test_engine, monkeypatch
):
    """Verify that Gemini prompt contains all search parameters and candidate metrics."""
    create_centres_with_cohorts(test_engine)
    seed_ecda_subsidy_rules(test_engine)
    setup_onemap_mock(monkeypatch)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")

    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_explanation_data = {
        "summary_narrative": "Detailed narrative test.",
        "top_centre_rationale": "Top rationale test.",
        "candidate_highlights": ["Highlight 1"],
    }
    mock_response.text = json.dumps(mock_explanation_data)
    mock_response.parsed = FitExplanationResponse.model_validate(mock_explanation_data)
    mock_client_instance.models.generate_content.return_value = mock_response

    with patch("google.genai.Client", return_value=mock_client_instance):
        payload = {
            "level": "infant",
            "enrollment_month_year": "2026-06",
            "citizenship_status": "SC",
            "household_income": 6000.0,
            "household_member_count": 3,
            "budget_max": 800.0,
            "home_postal_code": "560123",
            "workplace_postal_code": "018989",
            "location_preference": "HOME",
            "vacancy_weight": 2,
            "cost_weight": 1,
            "travel_weight": 1,
            "applicant_status": "WORKING_APPLICANT",
            "top_n": 1,
        }
        response = client.post("/api/v1/centres/match", json=payload)
        assert response.status_code == 200

        assert mock_client_instance.models.generate_content.called
        call_kwargs = mock_client_instance.models.generate_content.call_args.kwargs
        contents = call_kwargs["contents"]

        # Search parameters
        assert "INFANT" in contents
        assert "2026-06" in contents
        assert "6000" in contents
        assert "800" in contents
        assert "560123" in contents
        assert "018989" in contents
        assert "50%" in contents
        assert "25%" in contents
        assert "WORKING_APPLICANT" in contents

        # Candidate breakdown metrics
        assert "Alpha Preschool" in contents
        assert "Overall Fit" in contents
        assert "Vacancy Fit" in contents
        assert "Cost Fit" in contents
        assert "Travel Fit" in contents
        assert "Payable Fee" in contents or "payable fee" in contents.lower()


# ==============================================================================
# OneMap Service Account Dynamic Token & Reactive 401 Tests (Ticket #75)
# ==============================================================================


def test_match_endpoint_with_authenticated_onemap_service_account(
    client: TestClient, test_engine, monkeypatch
):
    """End-to-end matching succeeds with authenticated OneMap dynamic token management."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=601,
            centre_code="C_AUTH_01",
            centre_name="Authenticated Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=601,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_auth_01",
            centre_id=601,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_account@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "valid_secret_pass")

    auth_calls = {"count": 0}
    recorded_headers: list[dict[str, str]] = []
    setup_onemap_mock(
        monkeypatch,
        auth_call_count=auth_calls,
        recorded_headers=recorded_headers,
    )

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 5,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) >= 1
    assert auth_calls["count"] >= 1
    # Verify Authorization header was sent in OneMap requests
    bearer_headers = [h["Authorization"] for h in recorded_headers if "Authorization" in h]
    assert len(bearer_headers) > 0
    assert any("Bearer mock-onemap-token-jwt" in b for b in bearer_headers)


def test_match_endpoint_reactive_401_retry_on_routing(
    client: TestClient, test_engine, monkeypatch
):
    """401 Unauthorized during match routing triggers token invalidation, refresh, and successful retry."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=602,
            centre_code="C_AUTH_02",
            centre_name="Retry Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=602,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_auth_02",
            centre_id=602,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_account@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "valid_secret_pass")

    auth_calls = {"count": 0}
    setup_onemap_mock(
        monkeypatch,
        route_401_first_n=1,
        auth_call_count=auth_calls,
    )

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1
    # Two token fetches: initial + refresh after 401
    assert auth_calls["count"] >= 2


def test_match_endpoint_unrecoverable_auth_failure_graceful_fallback(
    client: TestClient, test_engine, monkeypatch
):
    """Unrecoverable authentication or routing failure degrades gracefully without crashing the match response."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=603,
            centre_code="C_AUTH_03",
            centre_name="Unauth Fallback Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=603,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1000.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_auth_03",
            centre_id=603,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_account@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "bad_password")

    setup_onemap_mock(
        monkeypatch,
        token_fails=True,
        route_401_first_n=999,
    )

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1
    item = data["results"][0]
    assert item["travel_fit_score"] == 100.0
    assert item["travel_breakdown"] is not None
    assert item["travel_breakdown"]["commute_distance_km"] is not None
    assert item["travel_breakdown"]["commute_duration_mins"] is not None


# ==============================================================================
# Ticket #99: Top-6 Match Shortlist & Stage 1 Scaling Tests
# ==============================================================================


def test_centre_match_request_and_criteria_default_top_n_to_six():
    """CentreMatchRequest and MatchCriteria default top_n to 6 while preserving ge=1, le=20 bounds."""
    # Schema defaults
    req = CentreMatchRequest(level=PreschoolLevel.INFANT, enrollment_month_year="2026-06")
    assert req.top_n == 6

    # Criteria defaults
    crit = MatchCriteria(level=PreschoolLevel.INFANT, enrollment_month_year="2026-06")
    assert crit.top_n == 6

    from pydantic import ValidationError

    # Validation bounds preserved
    with pytest.raises(ValidationError):
        CentreMatchRequest(level=PreschoolLevel.INFANT, enrollment_month_year="2026-06", top_n=0)
    with pytest.raises(ValidationError):
        CentreMatchRequest(level=PreschoolLevel.INFANT, enrollment_month_year="2026-06", top_n=21)


def test_api_match_defaults_to_top_six_shortlist(client: TestClient, test_engine):
    """Calling POST /api/v1/centres/match without top_n returns a Top 6 shortlist when >= 6 match."""
    create_centres_with_cohorts(test_engine)

    # Seed 1 additional infant centre so total candidates is 7 (> 6)
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        c8 = PreschoolCentre(
            centre_id=8,
            centre_code="C008",
            centre_name="Zeta Care",
            postal="560128",
        )
        s8 = PreschoolService(
            centre_id=8,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1200.0,
            citizenship_type=CitizenshipType.SC,
        )
        v8 = VacancyRecord(
            record_id="VR008",
            centre_id=8,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c8, s8, v8])
        session.commit()

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["total_candidates"] == 7
    assert data["returned_count"] == 6
    assert len(data["results"]) == 6


def test_stage1_candidate_cap_is_fifteen():
    """STAGE1_MAX_CANDIDATES in core/onemap is 15."""
    from kidcarematch_backend.core.onemap import STAGE1_MAX_CANDIDATES

    assert STAGE1_MAX_CANDIDATES == 15


def test_stage1_candidate_cap_fifteen_default(
    client: TestClient, test_engine, monkeypatch
):
    """When candidate pool has 25 candidates, default top_n (6) evaluates only 15 via OneMap Stage 2 routing."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {"560123": (1.3691, 103.8512)}  # Home

    with Session(test_engine) as session:
        for i in range(1, 26):
            p_code = f"57{i:04d}"
            coords_dict[p_code] = (1.3691 + (i * 0.001), 103.8512 + (i * 0.001))
            c = PreschoolCentre(
                centre_id=500 + i,
                centre_code=f"C_POOL_{i:02d}",
                centre_name=f"Pool Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=500 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_pool_{i}",
                centre_id=500 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])
        session.commit()

    _, route_count = setup_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        # top_n omitted -> defaults to 6 <= 15
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["total_candidates"] == 25
    assert data["returned_count"] == 6
    # Single anchor mode: 1 route call per Stage 2 candidate. Cap is 15 -> exactly 15 route calls!
    assert route_count["count"] == 15


def test_stage1_candidate_cap_dynamic_scaling_when_top_n_exceeds_fifteen(
    client: TestClient, test_engine, monkeypatch
):
    """When caller specifies top_n = 18 (> 15), Stage 1 dynamically scales to 18 candidates."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {"560123": (1.3691, 103.8512)}  # Home

    with Session(test_engine) as session:
        for i in range(1, 26):
            p_code = f"58{i:04d}"
            coords_dict[p_code] = (1.3691 + (i * 0.001), 103.8512 + (i * 0.001))
            c = PreschoolCentre(
                centre_id=600 + i,
                centre_code=f"C_SCALE_{i:02d}",
                centre_name=f"Scale Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=600 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_scale_{i}",
                centre_id=600 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])
        session.commit()

    _, route_count = setup_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "top_n": 18,
    }
    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["total_candidates"] == 25
    assert data["returned_count"] == 18
    # Dynamic scaling max(15, 18) = 18 -> exactly 18 route calls!
    assert route_count["count"] == 18


def test_matching_reuses_single_httpx_client_across_coordinates_and_routing(
    client: TestClient, test_engine, monkeypatch
):
    """execute_multi_criteria_match shares a single httpx.Client instance across batch coords and Stage 2 routing."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {
        "560123": (1.3691, 103.8512),  # Home
        "048581": (1.2840, 103.8510),  # Workplace
        "560124": (1.3695, 103.8520),  # Centre 1
        "560125": (1.3780, 103.8550),  # Centre 2
    }
    with Session(test_engine) as session:
        for i in [1, 2]:
            p_code = f"56012{3+i}"
            c = PreschoolCentre(
                centre_id=800 + i,
                centre_code=f"C_CLIENT_{i:02d}",
                centre_name=f"Client Reuse Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=800 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_client_{i}",
                centre_id=800 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])
        session.commit()

    setup_onemap_mock(monkeypatch, postal_coords=coords_dict)

    from kidcarematch_backend.core import matching as matching_mod

    observed_clients: list[tuple[str, Any]] = []

    orig_resolve_batch = matching_mod.resolve_coordinates_batch
    orig_get_route = matching_mod.get_route_duration_and_distance

    def spy_resolve_batch(*args, **kwargs):
        client_arg = kwargs.get("client")
        observed_clients.append(("resolve_batch", client_arg))
        return orig_resolve_batch(*args, **kwargs)

    def spy_get_route(*args, **kwargs):
        client_arg = kwargs.get("client")
        observed_clients.append(("get_route", client_arg))
        return orig_get_route(*args, **kwargs)

    monkeypatch.setattr(matching_mod, "resolve_coordinates_batch", spy_resolve_batch)
    monkeypatch.setattr(matching_mod, "get_route_duration_and_distance", spy_get_route)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "top_n": 2,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200

    # Ensure calls were made and observed (resolve_batch + 1 baseline + 2*2 candidate legs = 6 calls)
    assert len(observed_clients) >= 4
    # Check that client is NOT None for all calls
    clients = [c for _, c in observed_clients]
    assert all(c is not None for c in clients), f"Observed None client in calls: {observed_clients}"
    assert all(isinstance(c, httpx.Client) for c in clients)
    # Check that it is the exact same httpx.Client instance across all calls
    first_client = clients[0]
    assert all(c is first_client for c in clients), "Expected identical shared httpx.Client instance across all calls"


def test_matching_reuses_single_httpx_client_in_single_anchor_mode(
    client: TestClient, test_engine, monkeypatch
):
    """execute_multi_criteria_match shares a single httpx.Client instance in single anchor mode."""
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {
        "560123": (1.3691, 103.8512),  # Home
        "560124": (1.3695, 103.8520),  # Centre 1
        "560125": (1.3780, 103.8550),  # Centre 2
    }
    with Session(test_engine) as session:
        for i in [1, 2]:
            p_code = f"56012{3+i}"
            c = PreschoolCentre(
                centre_id=900 + i,
                centre_code=f"C_SINGLE_{i:02d}",
                centre_name=f"Single Client Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=900 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_single_{i}",
                centre_id=900 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add_all([c, s, v])
        session.commit()

    setup_onemap_mock(monkeypatch, postal_coords=coords_dict)

    from kidcarematch_backend.core import matching as matching_mod

    observed_clients: list[tuple[str, Any]] = []

    orig_resolve_batch = matching_mod.resolve_coordinates_batch
    orig_get_route = matching_mod.get_route_duration_and_distance

    def spy_resolve_batch(*args, **kwargs):
        client_arg = kwargs.get("client")
        observed_clients.append(("resolve_batch", client_arg))
        return orig_resolve_batch(*args, **kwargs)

    def spy_get_route(*args, **kwargs):
        client_arg = kwargs.get("client")
        observed_clients.append(("get_route", client_arg))
        return orig_get_route(*args, **kwargs)

    monkeypatch.setattr(matching_mod, "resolve_coordinates_batch", spy_resolve_batch)
    monkeypatch.setattr(matching_mod, "get_route_duration_and_distance", spy_get_route)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "location_preference": "HOME",
        "top_n": 2,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200

    # Ensure calls were made and observed (resolve_batch + 2 single anchor legs = 3 calls)
    assert len(observed_clients) >= 3
    clients = [c for _, c in observed_clients]
    assert all(c is not None for c in clients), f"Observed None client in calls: {observed_clients}"
    assert all(isinstance(c, httpx.Client) for c in clients)
    first_client = clients[0]
    assert all(c is first_client for c in clients), "Expected identical shared httpx.Client instance across all calls"

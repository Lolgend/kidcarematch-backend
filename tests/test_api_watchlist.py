"""Tests for Cohort Watchlist Subscription API endpoints."""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    PreschoolCentre,
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
def second_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="usr_parent_02",
            firebase_uid="firebase_parent_02",
            email="parent2@example.com",
            full_name="Bob Lim",
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
# POST /api/v1/watchlist Tests (Ticket #33)
# ==============================================================================


def test_create_watchlist_missing_auth_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Creating a watchlist subscription without Authorization header returns 401."""
    response = client.post(
        "/api/v1/watchlist",
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_create_watchlist_invalid_auth_scheme_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Creating a watchlist subscription with non-Bearer auth scheme returns 401."""
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Basic not-a-bearer-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert response.status_code == 401


def test_create_watchlist_suspended_user_returns_403(
    client: TestClient, sample_centre: PreschoolCentre, suspended_user: User
):
    """Creating a watchlist subscription as a suspended user returns 403."""
    set_mock_auth(suspended_user)
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_create_watchlist_centre_not_found_returns_404(
    client: TestClient, sample_user: User
):
    """Creating a watchlist subscription with a non-existent centre_id returns 404."""
    set_mock_auth(sample_user)
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": 99999,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


@pytest.mark.parametrize(
    "invalid_level",
    ["kindergarten", "infants", "toddler", "PRIMARY", 123, ""],
)
def test_create_watchlist_invalid_level_returns_422(
    client: TestClient,
    sample_centre: PreschoolCentre,
    sample_user: User,
    invalid_level: object,
):
    """Creating a watchlist subscription with an invalid cohort level returns 422."""
    set_mock_auth(sample_user)
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": invalid_level,
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_date",
    ["not-a-date", "2027-13-01", "2027/03/01", "tomorrow"],
)
def test_create_watchlist_invalid_date_format_returns_422(
    client: TestClient,
    sample_centre: PreschoolCentre,
    sample_user: User,
    invalid_date: str,
):
    """Creating a watchlist subscription with malformed date returns 422."""
    set_mock_auth(sample_user)
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": invalid_date,
        },
    )
    assert response.status_code == 422


def test_create_watchlist_success_returns_201_and_uuid(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Creating a valid watchlist subscription returns HTTP 201 with generated UUID and fields."""
    set_mock_auth(sample_user)
    payload = {
        "centre_id": sample_centre.centre_id,
        "level": "infant",
        "enrollment_month_year": "2027-03-01T00:00:00Z",
    }
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert response.status_code == 201
    data = response.json()
    assert "watchlist_id" in data
    # Ensure watchlist_id is a valid UUID
    uuid_obj = uuid.UUID(data["watchlist_id"])
    assert str(uuid_obj) == data["watchlist_id"]
    assert data["user_id"] == sample_user.user_id
    assert data["centre_id"] == sample_centre.centre_id
    assert data["level"] == "infant"
    assert data["enrollment_month_year"].startswith("2027-03-01")


def test_create_watchlist_normalizes_enrollment_month_to_day_1_utc(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Payload enrollment_month_year is normalized to day 1, 00:00:00 UTC."""
    set_mock_auth(sample_user)
    payload = {
        "centre_id": sample_centre.centre_id,
        "level": "pg",
        "enrollment_month_year": "2027-06-25T15:45:30Z",
    }
    response = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert response.status_code == 201
    data = response.json()
    normalized_dt = datetime.fromisoformat(
        data["enrollment_month_year"].replace("Z", "+00:00")
    )
    assert normalized_dt.year == 2027
    assert normalized_dt.month == 6
    assert normalized_dt.day == 1
    assert normalized_dt.hour == 0
    assert normalized_dt.minute == 0
    assert normalized_dt.second == 0


def test_create_watchlist_duplicate_subscription_returns_409(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Creating a duplicate subscription for the exact same cohort returns 409 Conflict."""
    set_mock_auth(sample_user)
    payload = {
        "centre_id": sample_centre.centre_id,
        "level": "n1",
        "enrollment_month_year": "2027-03-01T00:00:00Z",
    }
    first_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert first_resp.status_code == 201

    duplicate_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert duplicate_resp.status_code == 409
    assert (
        duplicate_resp.json()["detail"]
        == "Watchlist subscription already exists for this centre, cohort level, and enrollment month"
    )


def test_create_watchlist_duplicate_after_normalization_returns_409(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Duplicate subscription detection works across different days within the same month."""
    set_mock_auth(sample_user)
    payload_1 = {
        "centre_id": sample_centre.centre_id,
        "level": "n2",
        "enrollment_month_year": "2027-04-01T00:00:00Z",
    }
    payload_2 = {
        "centre_id": sample_centre.centre_id,
        "level": "n2",
        "enrollment_month_year": "2027-04-20T18:00:00Z",
    }
    first_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload_1,
    )
    assert first_resp.status_code == 201

    second_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload_2,
    )
    assert second_resp.status_code == 409
    assert (
        second_resp.json()["detail"]
        == "Watchlist subscription already exists for this centre, cohort level, and enrollment month"
    )


def test_create_watchlist_distinct_parents_can_subscribe_to_same_cohort(
    client: TestClient,
    sample_centre: PreschoolCentre,
    sample_user: User,
    second_user: User,
):
    """Two distinct parents can subscribe to the same centre, level, and month without conflict."""
    payload = {
        "centre_id": sample_centre.centre_id,
        "level": "k1",
        "enrollment_month_year": "2027-05-01T00:00:00Z",
    }

    set_mock_auth(sample_user)
    resp_1 = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert resp_1.status_code == 201
    data_1 = resp_1.json()

    set_mock_auth(second_user)
    resp_2 = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert resp_2.status_code == 201
    data_2 = resp_2.json()

    assert data_1["watchlist_id"] != data_2["watchlist_id"]
    assert data_1["user_id"] == sample_user.user_id
    assert data_2["user_id"] == second_user.user_id


def test_create_watchlist_same_parent_different_levels_success(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """The same parent can subscribe to different cohort levels for the same centre and month."""
    set_mock_auth(sample_user)
    resp_infant = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-08-01T00:00:00Z",
        },
    )
    assert resp_infant.status_code == 201

    resp_pg = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "pg",
            "enrollment_month_year": "2027-08-01T00:00:00Z",
        },
    )
    assert resp_pg.status_code == 201
    assert resp_infant.json()["watchlist_id"] != resp_pg.json()["watchlist_id"]


def test_create_watchlist_same_parent_different_months_success(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """The same parent can subscribe to different enrollment months for the same centre and level."""
    set_mock_auth(sample_user)
    resp_mar = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "k2",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert resp_mar.status_code == 201

    resp_apr = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "k2",
            "enrollment_month_year": "2027-04-01T00:00:00Z",
        },
    )
    assert resp_apr.status_code == 201
    assert resp_mar.json()["watchlist_id"] != resp_apr.json()["watchlist_id"]


@pytest.fixture
def second_centre(test_engine) -> PreschoolCentre:
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_TEST_02",
            centre_name="Sunny Smiles Academy",
            postal="650456",
            address="456 Sunny Ridge",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        return centre


# ==============================================================================
# GET /api/v1/centres/{centre_id}/watchlist Tests (Ticket #36)
# ==============================================================================


def test_centre_watchlist_status_missing_auth_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Inspecting centre watchlist status without Authorization header returns 401."""
    response = client.get(f"/api/v1/centres/{sample_centre.centre_id}/watchlist")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_centre_watchlist_status_invalid_auth_scheme_returns_401(
    client: TestClient, sample_centre: PreschoolCentre
):
    """Inspecting centre watchlist status with non-Bearer auth scheme returns 401."""
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Basic not-a-bearer-token"},
    )
    assert response.status_code == 401


def test_centre_watchlist_status_suspended_user_returns_403(
    client: TestClient, sample_centre: PreschoolCentre, suspended_user: User
):
    """Inspecting centre watchlist status with suspended user returns 403."""
    set_mock_auth(suspended_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_centre_watchlist_status_centre_not_found_returns_404(
    client: TestClient, sample_user: User
):
    """Inspecting watchlist status for non-existent centre_id returns 404."""
    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/centres/99999/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_centre_watchlist_status_not_watched_returns_false_and_empty_list(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Returns is_watched: false and empty list when authenticated parent has no subscriptions."""
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["is_watched"] is False
    assert data["watched_entries"] == []


def test_centre_watchlist_status_other_user_watched_returns_false(
    client: TestClient,
    sample_centre: PreschoolCentre,
    sample_user: User,
    second_user: User,
):
    """Enforces user isolation: does not return is_watched: true if only another user watches."""
    # second_user subscribes to sample_centre
    set_mock_auth(second_user)
    post_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert post_resp.status_code == 201

    # sample_user inspects sample_centre
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["is_watched"] is False
    assert data["watched_entries"] == []


def test_centre_watchlist_status_watched_returns_true_and_populated_entries(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Returns is_watched: true and populated watched_entries when authenticated parent watches."""
    set_mock_auth(sample_user)
    create_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "pg",
            "enrollment_month_year": "2027-05-01T00:00:00Z",
        },
    )
    assert create_resp.status_code == 201
    created_entry = create_resp.json()

    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["is_watched"] is True
    assert len(data["watched_entries"]) == 1
    entry_item = data["watched_entries"][0]
    assert entry_item["watchlist_id"] == created_entry["watchlist_id"]
    assert entry_item["centre_id"] == sample_centre.centre_id
    assert entry_item["level"] == "pg"
    assert entry_item["enrollment_month_year"].startswith("2027-05-01")


def test_centre_watchlist_status_multiple_cohorts_returns_all_sorted_entries(
    client: TestClient, sample_centre: PreschoolCentre, sample_user: User
):
    """Returns all subscribed cohorts for the centre, sorted chronologically."""
    set_mock_auth(sample_user)
    # Add June entry first, then March entry
    resp_jun = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "n1",
            "enrollment_month_year": "2027-06-01T00:00:00Z",
        },
    )
    assert resp_jun.status_code == 201

    resp_mar = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert resp_mar.status_code == 201

    response = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["centre_id"] == sample_centre.centre_id
    assert data["is_watched"] is True
    assert len(data["watched_entries"]) == 2

    # Should be sorted chronologically: March before June
    assert data["watched_entries"][0]["watchlist_id"] == resp_mar.json()["watchlist_id"]
    assert data["watched_entries"][0]["level"] == "infant"
    assert data["watched_entries"][0]["enrollment_month_year"].startswith("2027-03-01")

    assert data["watched_entries"][1]["watchlist_id"] == resp_jun.json()["watchlist_id"]
    assert data["watched_entries"][1]["level"] == "n1"
    assert data["watched_entries"][1]["enrollment_month_year"].startswith("2027-06-01")


def test_centre_watchlist_status_scoped_to_queried_centre_only(
    client: TestClient,
    sample_centre: PreschoolCentre,
    second_centre: PreschoolCentre,
    sample_user: User,
):
    """Introspection only returns entries belonging to the specific queried centre."""
    set_mock_auth(sample_user)
    # Watch sample_centre
    resp_centre1 = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": sample_centre.centre_id,
            "level": "infant",
            "enrollment_month_year": "2027-03-01T00:00:00Z",
        },
    )
    assert resp_centre1.status_code == 201

    # Watch second_centre
    resp_centre2 = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json={
            "centre_id": second_centre.centre_id,
            "level": "k1",
            "enrollment_month_year": "2027-09-01T00:00:00Z",
        },
    )
    assert resp_centre2.status_code == 201

    # Check sample_centre: only sample_centre entries returned
    response1 = client.get(
        f"/api/v1/centres/{sample_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response1.status_code == 200
    data1 = response1.json()
    assert data1["centre_id"] == sample_centre.centre_id
    assert data1["is_watched"] is True
    assert len(data1["watched_entries"]) == 1
    assert data1["watched_entries"][0]["centre_id"] == sample_centre.centre_id
    assert data1["watched_entries"][0]["watchlist_id"] == resp_centre1.json()["watchlist_id"]

    # Check second_centre: only second_centre entries returned
    response2 = client.get(
        f"/api/v1/centres/{second_centre.centre_id}/watchlist",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["centre_id"] == second_centre.centre_id
    assert data2["is_watched"] is True
    assert len(data2["watched_entries"]) == 1
    assert data2["watched_entries"][0]["centre_id"] == second_centre.centre_id
    assert data2["watched_entries"][0]["watchlist_id"] == resp_centre2.json()["watchlist_id"]


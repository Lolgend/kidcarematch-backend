"""Tests for Cohort Watchlist Subscription API endpoints."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    PreschoolCentre,
    PreschoolLevel,
    User,
    UserRole,
    VacancyRecord,
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


@pytest.fixture
def sample_watchlist_entry(
    test_engine, sample_user: User, sample_centre: PreschoolCentre
) -> WatchlistEntry:
    with Session(test_engine) as session:
        assert sample_user.user_id is not None
        assert sample_centre.centre_id is not None
        entry = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry


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


# ==============================================================================
# DELETE /api/v1/watchlist/{watchlist_id} Tests (Ticket #34)
# ==============================================================================


def test_delete_watchlist_missing_auth_returns_401(
    client: TestClient, sample_watchlist_entry: WatchlistEntry
):
    """Deleting a watchlist subscription without Authorization header returns 401."""
    response = client.delete(f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_delete_watchlist_invalid_auth_scheme_returns_401(
    client: TestClient, sample_watchlist_entry: WatchlistEntry
):
    """Deleting a watchlist subscription with non-Bearer auth scheme returns 401."""
    response = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Basic not-a-bearer-token"},
    )
    assert response.status_code == 401


def test_delete_watchlist_suspended_user_returns_403(
    client: TestClient, sample_watchlist_entry: WatchlistEntry, suspended_user: User
):
    """Deleting a watchlist subscription as a suspended user returns 403."""
    set_mock_auth(suspended_user)
    response = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_delete_watchlist_not_found_returns_404(client: TestClient, sample_user: User):
    """Deleting a non-existent watchlist subscription returns 404."""
    set_mock_auth(sample_user)
    response = client.delete(
        "/api/v1/watchlist/non-existent-watchlist-id",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Watchlist entry not found"


def test_delete_watchlist_other_user_entry_returns_403(
    client: TestClient,
    test_engine,
    sample_watchlist_entry: WatchlistEntry,
    second_user: User,
):
    """Deleting another user's watchlist subscription returns 403 Forbidden."""
    set_mock_auth(second_user)
    response = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "You do not have permission to delete this watchlist entry"
    )

    # Verify entry is NOT deleted
    with Session(test_engine) as session:
        entry_in_db = session.get(WatchlistEntry, sample_watchlist_entry.watchlist_id)
        assert entry_in_db is not None


def test_delete_watchlist_success_returns_204_and_removes_from_db(
    client: TestClient,
    test_engine,
    sample_watchlist_entry: WatchlistEntry,
    sample_user: User,
):
    """Deleting an authored watchlist subscription permanently removes it and returns 204."""
    set_mock_auth(sample_user)
    response = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 204
    assert response.content == b""

    # Verify permanently deleted from database
    with Session(test_engine) as session:
        entry_in_db = session.get(WatchlistEntry, sample_watchlist_entry.watchlist_id)
        assert entry_in_db is None


def test_delete_watchlist_second_attempt_returns_404(
    client: TestClient, sample_watchlist_entry: WatchlistEntry, sample_user: User
):
    """Attempting to delete the same watchlist entry a second time returns 404 Not Found."""
    set_mock_auth(sample_user)
    first_resp = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert first_resp.status_code == 204

    second_resp = client.delete(
        f"/api/v1/watchlist/{sample_watchlist_entry.watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert second_resp.status_code == 404
    assert second_resp.json()["detail"] == "Watchlist entry not found"


def test_delete_watchlist_allows_resubscribing_without_409(
    client: TestClient,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """After deleting a subscription, the parent can re-subscribe without 409 Conflict."""
    set_mock_auth(sample_user)
    payload = {
        "centre_id": sample_centre.centre_id,
        "level": "infant",
        "enrollment_month_year": "2027-09-01T00:00:00Z",
    }
    create_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert create_resp.status_code == 201
    watchlist_id = create_resp.json()["watchlist_id"]

    del_resp = client.delete(
        f"/api/v1/watchlist/{watchlist_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert del_resp.status_code == 204

    re_create_resp = client.post(
        "/api/v1/watchlist",
        headers={"Authorization": "Bearer mock-token"},
        json=payload,
    )
    assert re_create_resp.status_code == 201
    assert re_create_resp.json()["watchlist_id"] != watchlist_id


# ==============================================================================
# GET /api/v1/watchlist/me Tests (Ticket #35)
# ==============================================================================


def test_get_my_watchlist_missing_auth_returns_401(client: TestClient):
    """Retrieving watchlist portfolio without Authorization header returns 401."""
    response = client.get("/api/v1/watchlist/me")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_my_watchlist_invalid_auth_scheme_returns_401(client: TestClient):
    """Retrieving watchlist portfolio with non-Bearer auth scheme returns 401."""
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Basic invalid-token"},
    )
    assert response.status_code == 401


def test_get_my_watchlist_suspended_user_returns_403(
    client: TestClient, suspended_user: User
):
    """Retrieving watchlist portfolio as a suspended user returns 403."""
    set_mock_auth(suspended_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended."


def test_get_my_watchlist_empty_returns_total_0(
    client: TestClient, sample_user: User
):
    """Parent with no active watchlist entries receives empty items and total=0."""
    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


def test_get_my_watchlist_user_isolation(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
    second_user: User,
):
    """Parents only see their own watchlist subscriptions, never other parents'."""
    assert sample_user.user_id is not None
    assert second_user.user_id is not None
    assert sample_centre.centre_id is not None
    with Session(test_engine) as session:
        # Create 2 entries for sample_user (Alice)
        entry1 = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 3, 1, tzinfo=timezone.utc),
        )
        entry2 = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.PG,
            enrollment_month_year=datetime(2027, 4, 1, tzinfo=timezone.utc),
        )
        # Create 1 entry for second_user (Bob)
        entry3 = WatchlistEntry(
            user_id=second_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.N1,
            enrollment_month_year=datetime(2027, 5, 1, tzinfo=timezone.utc),
        )
        session.add_all([entry1, entry2, entry3])
        session.commit()

    # Query as Alice
    set_mock_auth(sample_user)
    resp_alice = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_alice.status_code == 200
    alice_data = resp_alice.json()
    assert alice_data["total"] == 2
    assert len(alice_data["items"]) == 2
    alice_levels = {item["level"] for item in alice_data["items"]}
    assert alice_levels == {"infant", "pg"}

    # Query as Bob
    set_mock_auth(second_user)
    resp_bob = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_bob.status_code == 200
    bob_data = resp_bob.json()
    assert bob_data["total"] == 1
    assert len(bob_data["items"]) == 1
    assert bob_data["items"][0]["level"] == "n1"


def test_get_my_watchlist_enriched_centre_metadata(
    client: TestClient,
    test_engine,
    sample_user: User,
):
    """Returned items include enriched centre metadata and preschool flags."""
    assert sample_user.user_id is not None
    with Session(test_engine) as session:
        rich_centre = PreschoolCentre(
            centre_code="PC_RICH_01",
            centre_name="Sunny Hill Discovery",
            postal="123456",
            address="10 Orchard Boulevard #01-01",
            spark_certified=True,
            gov_subsidy=True,
        )
        session.add(rich_centre)
        session.commit()
        session.refresh(rich_centre)
        assert rich_centre.centre_id is not None

        entry = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=rich_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 6, 1, tzinfo=timezone.utc),
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        rich_centre_id = rich_centre.centre_id
        entry_id = entry.watchlist_id

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["watchlist_id"] == entry_id
    assert item["centre_id"] == rich_centre_id
    assert item["centre_name"] == "Sunny Hill Discovery"
    assert item["centre_code"] == "PC_RICH_01"
    assert item["postal"] == "123456"
    assert item["address"] == "10 Orchard Boulevard #01-01"
    assert item["spark_certified"] is True
    assert item["gov_subsidy"] is True
    assert item["level"] == "infant"
    assert item["enrollment_month_year"].startswith("2027-06-01")
    assert item["current_vacancy_status"] is None


def test_get_my_watchlist_with_matching_vacancy_status(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """current_vacancy_status is populated when matching VacancyRecord exists."""
    assert sample_user.user_id is not None
    assert sample_centre.centre_id is not None
    month = datetime(2027, 7, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        vacancy = VacancyRecord(
            record_id="vac_test_01",
            centre_id=sample_centre.centre_id,
            level_type=PreschoolLevel.N1,
            enrollment_month_year=month,
            vacancy_status="Immediate Vacancy",
        )
        entry = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.N1,
            enrollment_month_year=month,
        )
        session.add_all([vacancy, entry])
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["current_vacancy_status"] == "Immediate Vacancy"


def test_get_my_watchlist_without_vacancy_status_returns_null(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """current_vacancy_status is null when no VacancyRecord exists for that cohort."""
    assert sample_user.user_id is not None
    assert sample_centre.centre_id is not None
    with Session(test_engine) as session:
        entry = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.PG,
            enrollment_month_year=datetime(2027, 8, 1, tzinfo=timezone.utc),
        )
        session.add(entry)
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["current_vacancy_status"] is None


def test_get_my_watchlist_vacancy_mismatched_cohort_or_month_returns_null(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """VacancyRecord only matches when centre_id, level, AND month all match."""
    assert sample_user.user_id is not None
    assert sample_centre.centre_id is not None
    month = datetime(2027, 9, 1, tzinfo=timezone.utc)
    other_month = datetime(2027, 10, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        # Vacancy exists for PG in month 9, and Infant in month 10
        vac1 = VacancyRecord(
            record_id="vac_mismatch_01",
            centre_id=sample_centre.centre_id,
            level_type=PreschoolLevel.PG,
            enrollment_month_year=month,
            vacancy_status="Available PG",
        )
        vac2 = VacancyRecord(
            record_id="vac_mismatch_02",
            centre_id=sample_centre.centre_id,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=other_month,
            vacancy_status="Available Oct Infant",
        )
        # Entry is Infant in month 9 (level mismatch with vac1, month mismatch with vac2)
        entry = WatchlistEntry(
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=month,
        )
        session.add_all([vac1, vac2, entry])
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["current_vacancy_status"] is None


def test_get_my_watchlist_sorted_chronologically_by_month_and_id(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """Items are sorted by enrollment_month_year ASC, then watchlist_id ASC."""
    assert sample_user.user_id is not None
    assert sample_centre.centre_id is not None
    with Session(test_engine) as session:
        entry_late = WatchlistEntry(
            watchlist_id="wl_id_3_late",
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 12, 1, tzinfo=timezone.utc),
        )
        entry_early_b = WatchlistEntry(
            watchlist_id="wl_id_2_early_b",
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.PG,
            enrollment_month_year=datetime(2027, 2, 1, tzinfo=timezone.utc),
        )
        entry_early_a = WatchlistEntry(
            watchlist_id="wl_id_1_early_a",
            user_id=sample_user.user_id,
            centre_id=sample_centre.centre_id,
            level=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2027, 2, 1, tzinfo=timezone.utc),
        )
        session.add_all([entry_late, entry_early_b, entry_early_a])
        session.commit()

    set_mock_auth(sample_user)
    response = client.get(
        "/api/v1/watchlist/me",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    ids = [item["watchlist_id"] for item in data["items"]]
    assert ids == ["wl_id_1_early_a", "wl_id_2_early_b", "wl_id_3_late"]


def test_get_my_watchlist_pagination_limit_and_offset(
    client: TestClient,
    test_engine,
    sample_centre: PreschoolCentre,
    sample_user: User,
):
    """Pagination via limit and offset correctly pages through portfolio."""
    assert sample_user.user_id is not None
    assert sample_centre.centre_id is not None
    with Session(test_engine) as session:
        for month_idx in range(1, 6):
            entry = WatchlistEntry(
                watchlist_id=f"wl_page_{month_idx:02d}",
                user_id=sample_user.user_id,
                centre_id=sample_centre.centre_id,
                level=PreschoolLevel.INFANT,
                enrollment_month_year=datetime(2027, month_idx, 1, tzinfo=timezone.utc),
            )
            session.add(entry)
        session.commit()

    set_mock_auth(sample_user)

    # Page 1: limit=2, offset=0
    resp_p1 = client.get(
        "/api/v1/watchlist/me?limit=2&offset=0",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_p1.status_code == 200
    data_p1 = resp_p1.json()
    assert data_p1["total"] == 5
    assert data_p1["limit"] == 2
    assert data_p1["offset"] == 0
    assert len(data_p1["items"]) == 2
    assert [i["watchlist_id"] for i in data_p1["items"]] == [
        "wl_page_01",
        "wl_page_02",
    ]

    # Page 2: limit=2, offset=2
    resp_p2 = client.get(
        "/api/v1/watchlist/me?limit=2&offset=2",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_p2.status_code == 200
    data_p2 = resp_p2.json()
    assert data_p2["total"] == 5
    assert len(data_p2["items"]) == 2
    assert [i["watchlist_id"] for i in data_p2["items"]] == [
        "wl_page_03",
        "wl_page_04",
    ]

    # Page 3: limit=2, offset=4
    resp_p3 = client.get(
        "/api/v1/watchlist/me?limit=2&offset=4",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_p3.status_code == 200
    data_p3 = resp_p3.json()
    assert data_p3["total"] == 5
    assert len(data_p3["items"]) == 1
    assert [i["watchlist_id"] for i in data_p3["items"]] == ["wl_page_05"]

    # Offset beyond total
    resp_empty = client.get(
        "/api/v1/watchlist/me?limit=2&offset=5",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert resp_empty.status_code == 200
    data_empty = resp_empty.json()
    assert data_empty["total"] == 5
    assert len(data_empty["items"]) == 0


@pytest.mark.parametrize(
    ("limit", "offset"),
    [
        (0, 0),
        (-1, 0),
        (101, 0),
        (20, -1),
    ],
)
def test_get_my_watchlist_invalid_pagination_returns_422(
    client: TestClient,
    sample_user: User,
    limit: int,
    offset: int,
):
    """Invalid pagination parameters (limit < 1, limit > 100, offset < 0) return 422."""
    set_mock_auth(sample_user)
    response = client.get(
        f"/api/v1/watchlist/me?limit={limit}&offset={offset}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422

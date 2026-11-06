"""Tests for authenticated centre commute endpoint (Task 3)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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
from kidcarematch_backend.core.commute import (
    clear_commute_postal_cache,
    get_representative_sample_pair,
)
from kidcarematch_backend.db.db import (
    AccountStatus,
    CitizenshipType,
    FamilyPreferenceProfile,
    LocationPreference,
    PreschoolCentre,
    PreschoolLevel,
    User,
    UserRole,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app
from kidcarematch_backend.schemas.commute import Coordinate, RouteLeg


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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_cache():
    clear_commute_postal_cache()
    yield
    clear_commute_postal_cache()


@pytest.fixture
def parent_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="parent-commute-1",
            firebase_uid="firebase-parent-1",
            email="parent1@example.com",
            full_name="Parent One",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def other_parent_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="parent-commute-2",
            firebase_uid="firebase-parent-2",
            email="parent2@example.com",
            full_name="Parent Two",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def admin_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="admin-user-1",
            firebase_uid="firebase-admin-1",
            email="admin@example.com",
            full_name="Admin User",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.fixture
def suspended_parent(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="suspended-parent-1",
            firebase_uid="firebase-suspended-1",
            email="suspended@example.com",
            full_name="Suspended Parent",
            role=UserRole.PARENT,
            account_status=AccountStatus.SUSPENDED,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def set_auth(user: User):
    claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: claims
    app.dependency_overrides[get_optional_token_claims] = lambda: claims


def create_centre(
    test_engine, centre_id: int = 1, postal: str = "560123"
) -> PreschoolCentre:
    with Session(test_engine) as session:
        c = PreschoolCentre(
            centre_id=centre_id,
            centre_code=f"C{centre_id:03d}",
            centre_name=f"Preschool {centre_id}",
            postal=postal,
            address=f"{postal} Singapore",
        )
        session.add(c)
        session.commit()
        session.refresh(c)
        return c


def create_profile(
    test_engine,
    user_id: str,
    profile_id: str = "prof-1",
    home_postal: str = "120120",
    workplace_postal: str | None = None,
) -> FamilyPreferenceProfile:
    with Session(test_engine) as session:
        p = FamilyPreferenceProfile(
            profile_id=profile_id,
            user_id=user_id,
            profile_name="Test Profile",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code=home_postal,
            workplace_postal_code=workplace_postal,
            location_preference=LocationPreference.HOME
            if not workplace_postal
            else LocationPreference.EITHER,
            household_income=5000.0,
            household_member_count=3,
            budget_max=1500.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=0.4,
            cost_weight=0.3,
            travel_weight=0.3,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def test_commute_unauthenticated_returns_401(client: TestClient, test_engine):
    centre = create_centre(test_engine)
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": "prof-1", "mode": "drive"},
    )
    assert res.status_code == 401


def test_commute_suspended_parent_returns_403(
    client: TestClient, test_engine, suspended_parent
):
    set_auth(suspended_parent)
    centre = create_centre(test_engine)
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": "prof-1", "mode": "drive"},
    )
    assert res.status_code == 403


def test_commute_admin_user_returns_403(client: TestClient, test_engine, admin_user):
    set_auth(admin_user)
    centre = create_centre(test_engine)
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": "prof-1", "mode": "drive"},
    )
    assert res.status_code == 403


def test_commute_centre_not_found_returns_404(
    client: TestClient, test_engine, parent_user
):
    set_auth(parent_user)
    profile = create_profile(test_engine, parent_user.user_id)
    res = client.post(
        "/api/v1/centres/99999/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 404
    assert "centre" in res.json()["detail"].lower()


def test_commute_other_parent_profile_returns_404_without_disclosure(
    client: TestClient, test_engine, parent_user, other_parent_user
):
    set_auth(parent_user)
    centre = create_centre(test_engine)
    other_profile = create_profile(
        test_engine, other_parent_user.user_id, profile_id="other-prof"
    )

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": other_profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 404
    assert "profile" in res.json()["detail"].lower()


def test_commute_invalid_mode_returns_422(client: TestClient, test_engine, parent_user):
    set_auth(parent_user)
    centre = create_centre(test_engine)
    profile = create_profile(test_engine, parent_user.user_id)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "flying"},
    )
    assert res.status_code in (422, 400)


def test_commute_home_only_driving_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.3500",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {
                    "total_time": 600,
                    "total_distance": 5000,
                },
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["centre_id"] == 1
    assert data["profile_id"] == profile.profile_id
    assert data["mode"] == "drive"
    assert "home" in data["anchors"]
    assert "centre" in data["anchors"]
    assert "workplace" not in data["anchors"]
    assert len(data["samples"]) == 1
    sample = data["samples"][0]
    assert sample["kind"] == "single"
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "centre"
    assert leg["status"] == "available"
    assert leg["source"] == "onemap"
    assert leg["duration_seconds"] == 600.0
    assert leg["distance_meters"] == 5000.0
    assert len(leg["geometry"]) == 2
    assert sample["totals"]["duration_seconds"] == 600.0
    assert sample["totals"]["distance_meters"] == 5000.0


def test_commute_two_legs_driving_workplace_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000"
                        if postal == "120120"
                        else ("1.3500" if postal == "560123" else "1.2800"),
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            # Leg 1: home (1.3000) -> centre (1.3500)
            # Leg 2: centre (1.3500) -> workplace (1.2800)
            if start == "1.3,103.8":
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 5000},
                }
            else:
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 900, "total_distance": 7000},
                }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "workplace" in data["anchors"]
    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["from_anchor"] == "home"
    assert sample["legs"][0]["to_anchor"] == "centre"
    assert sample["legs"][1]["from_anchor"] == "centre"
    assert sample["legs"][1]["to_anchor"] == "workplace"
    assert sample["totals"]["duration_seconds"] == 1500.0  # 600 + 900
    assert sample["totals"]["distance_meters"] == 12000.0  # 5000 + 7000


def test_commute_partial_leg_failure_withholds_totals(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000"
                        if postal == "120120"
                        else ("1.3500" if postal == "560123" else "1.2800"),
                        "LONGITUDE": "103.8000",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            if "1.3000" in str(start) or start == "1.3,103.8":
                # Leg 1 succeeds
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 5000},
                }
                return mock_resp
            else:
                # Leg 2 fails
                raise httpx.ConnectTimeout("Routing timed out")

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][1]["status"] == "unavailable"
    assert sample["totals"] is None  # Withheld when any leg fails!


def test_commute_walking_home_and_workplace_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = (
                "1.3000"
                if postal == "120120"
                else ("1.3500" if postal == "560123" else "1.2800")
            )
            lng = (
                "103.8000"
                if postal == "120120"
                else ("103.8500" if postal == "560123" else "103.8600")
            )
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            assert params.get("routeType") == "walk"
            start = str(params.get("start", ""))
            if "1.3000" in start or start.startswith("1.3,"):
                # Leg 1: 1800s (30 mins), 2500m
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 1800, "total_distance": 2500},
                }
            else:
                # Leg 2: 2400s (40 mins), 3200m
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 2400, "total_distance": 3200},
                }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "walk"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "walk"
    sample = data["samples"][0]
    assert sample["kind"] == "single"
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][0]["duration_seconds"] == 1800.0
    assert sample["legs"][0]["distance_meters"] == 2500.0
    assert sample["legs"][1]["status"] == "available"
    assert sample["legs"][1]["duration_seconds"] == 2400.0
    assert sample["legs"][1]["distance_meters"] == 3200.0
    assert sample["totals"]["duration_seconds"] == 4200.0
    assert sample["totals"]["distance_meters"] == 5700.0


def test_commute_cycling_home_and_workplace_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = (
                "1.3000"
                if postal == "120120"
                else ("1.3500" if postal == "560123" else "1.2800")
            )
            lng = (
                "103.8000"
                if postal == "120120"
                else ("103.8500" if postal == "560123" else "103.8600")
            )
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            assert params.get("routeType") == "cycle"
            start = str(params.get("start", ""))
            if "1.3000" in start or start.startswith("1.3,"):
                # Leg 1: 600s (10 mins), 3000m
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 3000},
                }
            else:
                # Leg 2: 900s (15 mins), 4500m
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 900, "total_distance": 4500},
                }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "cycle"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "cycle"
    sample = data["samples"][0]
    assert sample["kind"] == "single"
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][0]["duration_seconds"] == 600.0
    assert sample["legs"][0]["distance_meters"] == 3000.0
    assert sample["legs"][1]["status"] == "available"
    assert sample["legs"][1]["duration_seconds"] == 900.0
    assert sample["legs"][1]["distance_meters"] == 4500.0
    assert sample["totals"]["duration_seconds"] == 1500.0
    assert sample["totals"]["distance_meters"] == 7500.0


def test_commute_walking_single_estimate_no_invented_day_differences(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": "Location",
                        "POSTAL": "120120",
                        "LATITUDE": "1.3000",
                        "LONGITUDE": "103.8000",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 1800, "total_distance": 2500},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "walk"},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["samples"]) == 1
    assert data["samples"][0]["kind"] == "single"
    assert data["samples"][0]["sampled_at"] is None


def test_commute_home_only_pt_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    pt_fixture = {
        "plan": {
            "date": 1762300800000,
            "itineraries": [
                {
                    "duration": 1500,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 300,
                            "distance": 250,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                        {
                            "mode": "BUS",
                            "route": "159",
                            "duration": 900,
                            "distance": 4500,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                        {
                            "mode": "WALK",
                            "duration": 300,
                            "distance": 250,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                    ],
                }
            ],
        }
    }

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.3500",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            assert params.get("routeType") == "pt"
            assert params.get("mode") == "TRANSIT"
            assert params.get("time") == "08:00:00"
            mock_resp.json.return_value = pt_fixture
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "pt"
    assert len(data["samples"]) == 2
    assert len(data["sample_pair"]) == 2

    # Weekday sample
    sample_wd = data["samples"][0]
    assert sample_wd["kind"] == "weekday"
    assert sample_wd["sampled_at"] is not None
    assert "08:00:00" in sample_wd["sampled_at"]
    assert len(sample_wd["legs"]) == 1
    leg1_wd = sample_wd["legs"][0]
    assert leg1_wd["from_anchor"] == "home"
    assert leg1_wd["to_anchor"] == "centre"
    assert leg1_wd["status"] == "available"
    assert leg1_wd["source"] == "onemap"
    assert leg1_wd["duration_seconds"] == 1500.0
    assert leg1_wd["distance_meters"] == 5000.0
    assert len(leg1_wd["geometry"]) == 6
    assert len(leg1_wd["transit_legs"]) == 3
    assert leg1_wd["transit_legs"][0]["mode"] == "WALK"
    assert leg1_wd["transit_legs"][1]["mode"] == "BUS"
    assert leg1_wd["transit_legs"][1]["route"] == "159"
    assert sample_wd["totals"]["duration_seconds"] == 1500.0
    assert sample_wd["totals"]["distance_meters"] == 5000.0

    # Weekend sample
    sample_we = data["samples"][1]
    assert sample_we["kind"] == "weekend"
    assert sample_we["sampled_at"] is not None
    assert "08:00:00" in sample_we["sampled_at"]
    assert len(sample_we["legs"]) == 1
    leg1_we = sample_we["legs"][0]
    assert leg1_we["from_anchor"] == "home"
    assert leg1_we["to_anchor"] == "centre"
    assert leg1_we["status"] == "available"
    assert leg1_we["duration_seconds"] == 1500.0
    assert sample_we["totals"]["duration_seconds"] == 1500.0


def test_commute_two_legs_pt_sequential_onward_timing(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    pt_calls = []
    include_end_time = True

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000"
                        if postal == "120120"
                        else ("1.3500" if postal == "560123" else "1.2800"),
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            assert params.get("routeType") == "pt"
            pt_calls.append(params)
            start = str(params.get("start", ""))

            if "1.3000" in start or start.startswith("1.3,"):
                # Leg 1: 1500s (25 mins = 08:00 to 08:25)
                departure = datetime.strptime(
                    f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 1500,
                                **(
                                    {
                                        "startTime": int(departure.timestamp() * 1000),
                                        "endTime": int(
                                            (
                                                departure + timedelta(seconds=1500)
                                            ).timestamp()
                                            * 1000
                                        ),
                                    }
                                    if include_end_time
                                    else {}
                                ),
                                "legs": [
                                    {
                                        "mode": "WALK",
                                        "duration": 300,
                                        "distance": 250,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    },
                                    {
                                        "mode": "BUS",
                                        "route": "159",
                                        "duration": 1200,
                                        "distance": 4750,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    },
                                ],
                            }
                        ]
                    }
                }
            else:
                # Leg 2: 1200s (20 mins)
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 1200,
                                "legs": [
                                    {
                                        "mode": "SUBWAY",
                                        "route": "NSL",
                                        "duration": 900,
                                        "distance": 4000,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    },
                                    {
                                        "mode": "WALK",
                                        "duration": 300,
                                        "distance": 200,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    },
                                ],
                            }
                        ]
                    }
                }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(pt_calls) == 4
    # Weekday Leg 1 departure is 08:00:00
    assert pt_calls[0]["time"] == "08:00:00"
    # Weekday Leg 2 onward departure is exactly 08:25:00 (first leg arrival + 0 drop-off duration)
    assert pt_calls[1]["time"] == "08:25:00"
    # Weekend Leg 1 departure is 08:00:00
    assert pt_calls[2]["time"] == "08:00:00"
    # Weekend Leg 2 onward departure is exactly 08:25:00
    assert pt_calls[3]["time"] == "08:25:00"

    assert len(data["samples"]) == 2
    sample_wd = data["samples"][0]
    assert sample_wd["kind"] == "weekday"
    assert len(sample_wd["legs"]) == 2
    assert sample_wd["legs"][0]["duration_seconds"] == 1500.0
    assert sample_wd["legs"][1]["duration_seconds"] == 1200.0
    assert sample_wd["totals"]["duration_seconds"] == 2700.0
    assert sample_wd["totals"]["distance_meters"] == 9200.0

    sample_we = data["samples"][1]
    assert sample_we["kind"] == "weekend"
    assert len(sample_we["legs"]) == 2
    assert sample_we["legs"][0]["duration_seconds"] == 1500.0
    assert sample_we["legs"][1]["duration_seconds"] == 1200.0
    assert sample_we["totals"]["duration_seconds"] == 2700.0

    # A successful PT itinerary without provider endTime remains visible, but cannot invent an onward departure.
    include_end_time = False
    pt_calls.clear()
    missing_arrival = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert missing_arrival.status_code == 200
    missing_arrival_sample = missing_arrival.json()["samples"][0]
    assert len(pt_calls) == 2
    assert missing_arrival_sample["legs"][0]["status"] == "available"
    assert missing_arrival_sample["legs"][0]["arrival_at"] is None
    assert missing_arrival_sample["legs"][1]["status"] == "unavailable"
    assert missing_arrival_sample["totals"] is None


def test_commute_pt_first_leg_failure_marks_onward_unavailable(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048581",
    )

    pt_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000"
                        if postal == "120120"
                        else ("1.3500" if postal == "560123" else "1.2800"),
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            pt_calls.append(params)
            # Leg 1 fails with no itineraries
            mock_resp.json.return_value = {"plan": {"itineraries": []}}
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert res.status_code == 200
    data = res.json()
    # Leg 2 must not be requested because Leg 1 arrival cannot be determined! (1 for weekday, 1 for weekend)
    assert len(pt_calls) == 2
    for sample in data["samples"]:
        assert sample["legs"][0]["status"] == "unavailable"
        assert sample["legs"][1]["status"] == "unavailable"
        assert "onward" in sample["legs"][1]["message"].lower()
        assert sample["totals"] is None


def test_commute_pt_expired_sample_pair_returns_409(
    client: TestClient, test_engine, parent_user
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id)

    # Expired sample in the past
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sample_pair": ["2020-01-01T08:00:00+08:00"],
        },
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "sample_expired"
    assert detail["message"] == "Representative sample pair expired"


def test_commute_pt_rejects_sample_pair_with_seconds_or_microseconds(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    from kidcarematch_backend.api.v1 import centres as centres_api

    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id)
    monkeypatch.setattr(
        centres_api,
        "resolve_commute_postal",
        lambda _postal: Coordinate(lat=1.3, lng=103.8),
    )
    monkeypatch.setattr(
        centres_api,
        "get_commute_leg",
        lambda **kwargs: RouteLeg(
            from_anchor=kwargs["from_anchor"],
            to_anchor=kwargs["to_anchor"],
            status="unavailable",
            message="test route unavailable",
        ),
    )
    wednesday, saturday = get_representative_sample_pair()
    for sample_pair in (
        [wednesday.replace(second=59).isoformat(), saturday.isoformat()],
        [wednesday.replace(microsecond=1).isoformat(), saturday.isoformat()],
    ):
        response = client.post(
            f"/api/v1/centres/{centre.centre_id}/commute",
            json={
                "profile_id": profile.profile_id,
                "mode": "pt",
                "sample_pair": sample_pair,
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "sample_expired"


def test_commute_pt_saturday_routing_and_sample_pair_reuse(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    from kidcarematch_backend.core.commute import get_representative_sample_pair

    wed, sat = get_representative_sample_pair()
    sample_pair = [wed.isoformat(), sat.isoformat()]

    pt_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.3500",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            pt_calls.append(params)
            mock_resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": 1800
                            if params.get("date") == sat.strftime("%m-%d-%Y")
                            else 1500,
                            "legs": [
                                {
                                    "mode": "WALK",
                                    "duration": 300,
                                    "distance": 250,
                                    "legGeometry": {"points": "sagGwhtxRsDkC"},
                                },
                                {
                                    "mode": "BUS",
                                    "route": "159",
                                    "duration": 1500
                                    if params.get("date") == sat.strftime("%m-%d-%Y")
                                    else 1200,
                                    "distance": 4750,
                                    "legGeometry": {"points": "sagGwhtxRsDkC"},
                                },
                            ],
                        }
                    ]
                }
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sample_pair": sample_pair,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["sample_pair"] == sample_pair
    assert len(pt_calls) == 2
    assert pt_calls[0]["date"] == wed.strftime("%m-%d-%Y")
    assert pt_calls[1]["date"] == sat.strftime("%m-%d-%Y")

    assert len(data["samples"]) == 2
    assert data["samples"][0]["kind"] == "weekday"
    assert data["samples"][0]["totals"]["duration_seconds"] == 1500.0
    assert data["samples"][1]["kind"] == "weekend"
    assert data["samples"][1]["totals"]["duration_seconds"] == 1800.0


def test_commute_single_estimate_modes_return_single_sample_with_sample_pair(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.3500",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {
                    "total_time": 600,
                    "total_distance": 5000,
                },
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    for mode in ("drive", "walk", "cycle"):
        res = client.post(
            f"/api/v1/centres/{centre.centre_id}/commute",
            json={"profile_id": profile.profile_id, "mode": mode},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["mode"] == mode
        assert len(data["samples"]) == 1
        sample = data["samples"][0]
        assert sample["kind"] == "single"
        assert sample["sampled_at"] is None
        assert sample["totals"]["duration_seconds"] == 600.0
        # Sample pair is present for session reuse
        assert len(data["sample_pair"]) == 2

    # Expired sample pair in drive mode raises 409
    res_expired = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": ["2020-01-01T08:00:00+08:00"],
        },
    )
    assert res_expired.status_code == 409
    assert res_expired.json()["detail"]["code"] == "sample_expired"

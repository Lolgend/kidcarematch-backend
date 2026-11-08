"""Tests for authenticated centre commute endpoint (Ticket #82)."""

from datetime import datetime, timezone
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
    clear_commute_caches,
    clear_commute_postal_cache,
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
    profile_name: str = "Test Profile",
    home_postal: str = "120120",
    workplace_postal: str | None = None,
) -> FamilyPreferenceProfile:
    with Session(test_engine) as session:
        p = FamilyPreferenceProfile(
            profile_id=profile_id,
            user_id=user_id,
            profile_name=profile_name,
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
            vacancy_weight=3,
            cost_weight=3,
            travel_weight=3,
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


def test_commute_profile_not_found_returns_404(
    client: TestClient, test_engine, parent_user
):
    set_auth(parent_user)
    centre = create_centre(test_engine)
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": "non-existent-profile", "mode": "drive"},
    )
    assert res.status_code == 404
    assert "profile" in res.json()["detail"].lower()


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


def test_commute_unsupported_mode_returns_422(
    client: TestClient, test_engine, parent_user
):
    set_auth(parent_user)
    centre = create_centre(test_engine)
    profile = create_profile(test_engine, parent_user.user_id)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "flying"},
    )
    assert res.status_code == 422


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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
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
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] == {"lat": 1.35, "lng": 103.85}
    assert "workplace" not in data["anchors"]
    assert len(data["samples"]) == 1
    sample = data["samples"][0]
    assert sample["kind"] == "single"
    assert sample["sampled_at"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "centre"
    assert leg["status"] == "available"
    assert leg["source"] == "onemap"
    assert leg["duration_seconds"] == 600.0
    assert leg["distance_meters"] == 5000.0
    assert len(leg["geometry"]) == 2
    assert leg["geometry"][0] == {"lat": 1.3521, "lng": 103.8198}
    assert sample["totals"]["duration_seconds"] == 600.0
    assert sample["totals"]["distance_meters"] == 5000.0
    assert len(data["sample_pair"]) == 2


def test_commute_default_mode_is_drive(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )

    routing_calls = []

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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 600, "total_distance": 5000},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["mode"] == "drive"
    assert routing_calls[0]["routeType"] == "drive"


def test_commute_walking_home_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )

    routing_calls = []

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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            assert params.get("routeType") == "walk"
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
    assert data["mode"] == "walk"
    sample = data["samples"][0]
    assert sample["kind"] == "single"
    assert sample["sampled_at"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["status"] == "available"
    assert leg["source"] == "onemap"
    assert leg["duration_seconds"] == 1800.0
    assert leg["distance_meters"] == 2500.0
    assert sample["totals"]["duration_seconds"] == 1800.0
    assert sample["totals"]["distance_meters"] == 2500.0


def test_commute_cycling_home_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )

    routing_calls = []

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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            assert params.get("routeType") == "cycle"
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 720, "total_distance": 3600},
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
    assert sample["sampled_at"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["status"] == "available"
    assert leg["source"] == "onemap"
    assert leg["duration_seconds"] == 720.0
    assert leg["distance_meters"] == 3600.0
    assert sample["totals"]["duration_seconds"] == 720.0
    assert sample["totals"]["distance_meters"] == 3600.0


def test_commute_unresolvable_home_postal_returns_unavailable_leg(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="999999", workplace_postal=None
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "560123":
                mock_resp.json.return_value = {
                    "found": 1,
                    "results": [
                        {
                            "SEARCHVAL": "Centre",
                            "POSTAL": "560123",
                            "LATITUDE": "1.3500",
                            "LONGITUDE": "103.8500",
                        }
                    ],
                }
            else:
                # Home postal not found
                mock_resp.json.return_value = {"found": 0, "results": []}
            return mock_resp

        raise RuntimeError(f"Unexpected routing call: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["anchors"]["home"] is None
    assert data["anchors"]["centre"] == {"lat": 1.35, "lng": 103.85}
    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "centre"
    assert leg["status"] == "unavailable"
    assert leg["message"] == "Home postal code could not be resolved"
    assert leg["duration_seconds"] is None
    assert leg["distance_meters"] is None
    assert leg["geometry"] is None


def test_commute_unresolvable_centre_postal_returns_unavailable_leg(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="888888")
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
            if postal == "120120":
                mock_resp.json.return_value = {
                    "found": 1,
                    "results": [
                        {
                            "SEARCHVAL": "Home",
                            "POSTAL": "120120",
                            "LATITUDE": "1.3000",
                            "LONGITUDE": "103.8000",
                        }
                    ],
                }
            else:
                # Centre postal not found
                mock_resp.json.return_value = {"found": 0, "results": []}
            return mock_resp

        raise RuntimeError(f"Unexpected routing call: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] is None
    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "centre"
    assert leg["status"] == "unavailable"
    assert leg["message"] == "Centre postal code could not be resolved"
    assert leg["duration_seconds"] is None
    assert leg["distance_meters"] is None
    assert leg["geometry"] is None


def test_commute_incomplete_profile_missing_home_postal_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")

    # Case 1: Empty string home postal
    profile_empty = create_profile(
        test_engine,
        parent_user.user_id,
        profile_id="prof-empty",
        home_postal="",
        workplace_postal=None,
    )
    res_empty = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile_empty.profile_id, "mode": "drive"},
    )
    assert res_empty.status_code == 422
    assert "home_postal_code is required" in res_empty.json()["detail"]

    # Case 2: None home postal
    profile_none = create_profile(
        test_engine,
        parent_user.user_id,
        profile_id="prof-none",
        profile_name="Test Profile None",
        home_postal="123456",
        workplace_postal=None,
    )
    real_session_get = Session.get

    def fake_get(self, model, ident):
        if model == FamilyPreferenceProfile and str(ident) == "prof-none":
            prof = real_session_get(self, model, ident)
            object.__setattr__(prof, "home_postal_code", None)
            return prof
        return real_session_get(self, model, ident)

    monkeypatch.setattr(Session, "get", fake_get)

    res_none = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile_none.profile_id, "mode": "drive"},
    )
    assert res_none.status_code == 422
    assert "home_postal_code is required" in res_none.json()["detail"]


def test_commute_routing_timeout_returns_unavailable_leg_without_haversine_fallback(
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            raise httpx.ConnectTimeout("Connection to OneMap routing timed out")

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] == {"lat": 1.35, "lng": 103.85}
    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 1
    leg = sample["legs"][0]
    assert leg["status"] == "unavailable"
    assert "timed out" in leg["message"].lower() or "timeout" in leg["message"].lower()
    assert leg["duration_seconds"] is None
    assert leg["distance_meters"] is None
    assert leg["geometry"] is None


def test_commute_missing_route_geometry_returns_unavailable_leg(
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            # Missing route_geometry
            mock_resp.json.return_value = {
                "status_message": "Found route",
                "route_summary": {"total_time": 600, "total_distance": 5000},
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
    sample = data["samples"][0]
    assert sample["totals"] is None
    leg = sample["legs"][0]
    assert leg["status"] == "unavailable"
    assert "geometry" in leg["message"].lower()


def test_commute_routing_reactive_401_retry_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    from kidcarematch_backend.core.onemap import onemap_token_manager

    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )

    invalidations = []
    original_invalidate = onemap_token_manager.invalidate_token

    def spy_invalidate():
        invalidations.append(True)
        original_invalidate()

    monkeypatch.setattr(onemap_token_manager, "invalidate_token", spy_invalidate)

    routing_attempt = 0

    def mock_get(self, url, *args, **kwargs):
        nonlocal routing_attempt
        url_str = str(url)
        mock_resp = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            routing_attempt += 1
            if routing_attempt == 1:
                # First attempt returns 401 Unauthorized
                mock_resp.status_code = 401
                return mock_resp
            # Second attempt succeeds after token invalidation & retry
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 600, "total_distance": 5000},
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
    assert len(invalidations) == 1
    assert routing_attempt == 2
    sample = data["samples"][0]
    assert sample["legs"][0]["status"] == "available"
    assert sample["totals"]["duration_seconds"] == 600.0


def test_commute_workplace_driving_two_legs_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )
    # Explicitly set location_preference to HOME to test independence
    with Session(test_engine) as session:
        db_prof = session.get(FamilyPreferenceProfile, profile.profile_id)
        assert db_prof is not None
        db_prof.location_preference = LocationPreference.HOME
        session.add(db_prof)
        session.commit()

    routing_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat_map = {"120120": "1.3000", "560123": "1.3500", "048624": "1.2800"}
            lng_map = {"120120": "103.8000", "560123": "103.8500", "048624": "103.8500"}
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat_map.get(postal, "1.3000"),
                        "LONGITUDE": lng_map.get(postal, "103.8000"),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            start = params.get("start")
            if start == "1.3,103.8":
                # Leg 1: home -> centre
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 5000},
                }
            else:
                # Leg 2: centre -> workplace
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 800, "total_distance": 7000},
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
    assert data["mode"] == "drive"
    assert "home" in data["anchors"]
    assert "centre" in data["anchors"]
    assert "workplace" in data["anchors"]
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] == {"lat": 1.35, "lng": 103.85}
    assert data["anchors"]["workplace"] == {"lat": 1.28, "lng": 103.85}

    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    leg1 = sample["legs"][0]
    leg2 = sample["legs"][1]

    assert leg1["from_anchor"] == "home"
    assert leg1["to_anchor"] == "centre"
    assert leg1["status"] == "available"
    assert leg1["duration_seconds"] == 600.0
    assert leg1["distance_meters"] == 5000.0

    assert leg2["from_anchor"] == "centre"
    assert leg2["to_anchor"] == "workplace"
    assert leg2["status"] == "available"
    assert leg2["duration_seconds"] == 800.0
    assert leg2["distance_meters"] == 7000.0

    assert sample["totals"]["duration_seconds"] == 1400.0
    assert sample["totals"]["distance_meters"] == 12000.0


def test_commute_workplace_walking_two_legs_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )

    routing_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat_map = {"120120": "1.3000", "560123": "1.3500", "048624": "1.2800"}
            lng_map = {"120120": "103.8000", "560123": "103.8500", "048624": "103.8500"}
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat_map.get(postal, "1.3000"),
                        "LONGITUDE": lng_map.get(postal, "103.8000"),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            assert params.get("routeType") == "walk"
            start = params.get("start")
            if start == "1.3,103.8":
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 1800, "total_distance": 2500},
                }
            else:
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
    assert len(routing_calls) == 2
    assert all(c["routeType"] == "walk" for c in routing_calls)
    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][0]["duration_seconds"] == 1800.0
    assert sample["legs"][1]["status"] == "available"
    assert sample["legs"][1]["duration_seconds"] == 2400.0
    assert sample["totals"]["duration_seconds"] == 4200.0
    assert sample["totals"]["distance_meters"] == 5700.0


def test_commute_workplace_cycling_two_legs_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )

    routing_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat_map = {"120120": "1.3000", "560123": "1.3500", "048624": "1.2800"}
            lng_map = {"120120": "103.8000", "560123": "103.8500", "048624": "103.8500"}
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat_map.get(postal, "1.3000"),
                        "LONGITUDE": lng_map.get(postal, "103.8000"),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            assert params.get("routeType") == "cycle"
            start = params.get("start")
            if start == "1.3,103.8":
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 700, "total_distance": 4000},
                }
            else:
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 900, "total_distance": 5000},
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
    assert len(routing_calls) == 2
    assert all(c["routeType"] == "cycle" for c in routing_calls)
    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][0]["duration_seconds"] == 700.0
    assert sample["legs"][1]["status"] == "available"
    assert sample["legs"][1]["duration_seconds"] == 900.0
    assert sample["totals"]["duration_seconds"] == 1600.0
    assert sample["totals"]["distance_meters"] == 9000.0


def test_commute_workplace_unresolvable_returns_unavailable_leg2_and_preserves_leg1(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="999999",
    )

    routing_calls = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "999999":
                mock_resp.json.return_value = {"found": 0, "results": []}
                return mock_resp
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 600, "total_distance": 5000},
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
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] == {"lat": 1.35, "lng": 103.85}
    assert "workplace" in data["anchors"]
    assert data["anchors"]["workplace"] is None

    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 2

    leg1 = sample["legs"][0]
    assert leg1["from_anchor"] == "home"
    assert leg1["to_anchor"] == "centre"
    assert leg1["status"] == "available"
    assert leg1["duration_seconds"] == 600.0

    leg2 = sample["legs"][1]
    assert leg2["from_anchor"] == "centre"
    assert leg2["to_anchor"] == "workplace"
    assert leg2["status"] == "unavailable"
    assert leg2["message"] == "Workplace postal code could not be resolved"
    assert leg2["duration_seconds"] is None
    assert leg2["distance_meters"] is None
    assert leg2["geometry"] is None


def test_commute_centre_unresolvable_with_workplace_marks_both_legs_unavailable(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="999999")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "999999":
                mock_resp.json.return_value = {"found": 0, "results": []}
                return mock_resp
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.2800",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.8500",
                    }
                ],
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
    assert data["anchors"]["home"] == {"lat": 1.3, "lng": 103.8}
    assert data["anchors"]["centre"] is None
    assert data["anchors"]["workplace"] == {"lat": 1.28, "lng": 103.85}

    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 2

    leg1 = sample["legs"][0]
    assert leg1["from_anchor"] == "home"
    assert leg1["to_anchor"] == "centre"
    assert leg1["status"] == "unavailable"
    assert leg1["message"] == "Centre postal code could not be resolved"

    leg2 = sample["legs"][1]
    assert leg2["from_anchor"] == "centre"
    assert leg2["to_anchor"] == "workplace"
    assert leg2["status"] == "unavailable"
    assert leg2["message"] == "Centre postal code could not be resolved"


def test_commute_workplace_leg2_routing_failure_preserves_leg1(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat_map = {"120120": "1.3000", "560123": "1.3500", "048624": "1.2800"}
            lng_map = {"120120": "103.8000", "560123": "103.8500", "048624": "103.8500"}
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat_map.get(postal, "1.3000"),
                        "LONGITUDE": lng_map.get(postal, "103.8000"),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            if start == "1.3,103.8":
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 5000},
                }
                return mock_resp
            # Leg 2 times out
            raise httpx.ConnectTimeout("OneMap routing timed out for leg 2")

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive"},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 2

    leg1 = sample["legs"][0]
    assert leg1["status"] == "available"
    assert leg1["duration_seconds"] == 600.0

    leg2 = sample["legs"][1]
    assert leg2["status"] == "unavailable"
    assert (
        "timed out" in leg2["message"].lower() or "timeout" in leg2["message"].lower()
    )


def test_commute_workplace_leg1_routing_failure_preserves_leg2(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="048624",
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat_map = {"120120": "1.3000", "560123": "1.3500", "048624": "1.2800"}
            lng_map = {"120120": "103.8000", "560123": "103.8500", "048624": "103.8500"}
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat_map.get(postal, "1.3000"),
                        "LONGITUDE": lng_map.get(postal, "103.8000"),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            if start == "1.3,103.8":
                # Leg 1 times out
                raise httpx.ConnectTimeout("OneMap routing timed out for leg 1")
            # Leg 2 succeeds
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 800, "total_distance": 7000},
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
    sample = data["samples"][0]
    assert sample["totals"] is None
    assert len(sample["legs"]) == 2

    leg1 = sample["legs"][0]
    assert leg1["status"] == "unavailable"
    assert (
        "timed out" in leg1["message"].lower() or "timeout" in leg1["message"].lower()
    )

    leg2 = sample["legs"][1]
    assert leg2["status"] == "available"
    assert leg2["duration_seconds"] == 800.0


def test_commute_workplace_whitespace_omits_workplace_anchor(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="   ",
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 600, "total_distance": 5000},
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
    assert "workplace" not in data["anchors"]
    sample = data["samples"][0]
    assert len(sample["legs"]) == 1
    assert sample["legs"][0]["from_anchor"] == "home"
    assert sample["legs"][0]["to_anchor"] == "centre"
    assert sample["totals"]["duration_seconds"] == 600.0


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

    routing_calls = []

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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            assert params.get("routeType") == "pt"
            assert params.get("mode") == "TRANSIT"
            assert params.get("time") == "08:00:00"
            assert params.get("maxWalkDistance") == 1000
            assert params.get("numItineraries") == 1
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
    assert len(routing_calls) == 2

    # Weekday sample
    sample_wd = data["samples"][0]
    assert sample_wd["kind"] == "weekday"
    assert sample_wd["sampled_at"] is not None
    assert "+08:00" in sample_wd["sampled_at"]
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
    assert leg1_wd["transit_legs"][0]["instruction"] == "Walk (250 m)"
    assert leg1_wd["transit_legs"][1]["mode"] == "BUS"
    assert leg1_wd["transit_legs"][1]["route"] == "159"
    assert leg1_wd["transit_legs"][1]["instruction"] == "Take Bus 159"
    assert sample_wd["totals"]["duration_seconds"] == 1500.0
    assert sample_wd["totals"]["distance_meters"] == 5000.0

    # Weekend sample
    sample_we = data["samples"][1]
    assert sample_we["kind"] == "weekend"
    assert sample_we["sampled_at"] is not None
    assert "+08:00" in sample_we["sampled_at"]
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
                mock_resp.json.return_value = {
                    "plan": {
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
    assert sample_wd["legs"][0]["arrival_at"] is not None
    assert "08:25:00" in sample_wd["legs"][0]["arrival_at"]
    assert sample_wd["legs"][1]["duration_seconds"] == 1200.0
    assert sample_wd["totals"]["duration_seconds"] == 2700.0
    assert sample_wd["totals"]["distance_meters"] == 9200.0


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
            # Leg 1 returns no itineraries
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
    # Leg 2 must not be requested because Leg 1 arrival cannot be determined (1 call for weekday leg 1, 1 call for weekend leg 1)
    assert len(pt_calls) == 2
    for sample in data["samples"]:
        assert sample["totals"] is None
        assert len(sample["legs"]) == 2
        assert sample["legs"][0]["status"] == "unavailable"
        assert sample["legs"][1]["status"] == "unavailable"
        assert (
            sample["legs"][1]["message"]
            == "Onward leg departure cannot be determined because first leg arrival is unavailable"
        )


def test_commute_pt_second_leg_failure_preserves_leg1(
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
            start = str(params.get("start", ""))
            if "1.3000" in start or start.startswith("1.3,"):
                # Leg 1 succeeds
                mock_resp.json.return_value = {
                    "plan": {
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
                                        "duration": 1200,
                                        "distance": 4750,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    },
                                ],
                            }
                        ]
                    }
                }
                return mock_resp
            # Leg 2 times out
            raise httpx.ConnectTimeout("OneMap PT routing timed out for leg 2")

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(pt_calls) == 4
    for sample in data["samples"]:
        assert sample["totals"] is None
        assert len(sample["legs"]) == 2
        assert sample["legs"][0]["status"] == "available"
        assert sample["legs"][0]["duration_seconds"] == 1500.0
        assert sample["legs"][1]["status"] == "unavailable"
        assert (
            "timed out" in sample["legs"][1]["message"].lower()
            or "timeout" in sample["legs"][1]["message"].lower()
        )


def test_commute_pt_subleg_missing_geometry_marks_leg_unavailable(
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
            "itineraries": [
                {
                    "duration": 600,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 600,
                            "distance": 500,
                            "legGeometry": {"points": ""},  # empty polyline
                        }
                    ],
                }
            ]
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
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
    for sample in data["samples"]:
        assert sample["totals"] is None
        assert sample["legs"][0]["status"] == "unavailable"
        assert "geometry unavailable" in sample["legs"][0]["message"].lower()


def test_commute_pt_subleg_missing_metrics_marks_leg_unavailable(
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
            "itineraries": [
                {
                    "duration": 600,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": -10,  # invalid negative duration
                            "distance": 500,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        }
                    ],
                }
            ]
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
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
    for sample in data["samples"]:
        assert sample["totals"] is None
        assert sample["legs"][0]["status"] == "unavailable"


def test_commute_pt_missing_plan_marks_leg_unavailable(
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            # OneMap returns response without plan
            mock_resp.json.return_value = {"status": "error"}
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "pt"},
    )
    assert res.status_code == 200
    data = res.json()
    for sample in data["samples"]:
        assert sample["totals"] is None
        assert sample["legs"][0]["status"] == "unavailable"
        assert sample["legs"][0]["message"] == "Public transport plan unavailable"


def test_commute_pt_default_instruction_formats(
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
            "itineraries": [
                {
                    "duration": 2500,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 900,
                            "distance": 1500,  # >= 1000m -> "Walk (1.5 km)"
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                        {
                            "mode": "SUBWAY",
                            "routeShortName": "NS",
                            "duration": 600,
                            "distance": 3000,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                        {
                            "mode": "BUS",
                            # no route
                            "duration": 1000,
                            "distance": 2000,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                    ],
                }
            ]
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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
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
    sample = data["samples"][0]
    tlegs = sample["legs"][0]["transit_legs"]
    assert tlegs[0]["instruction"] == "Walk (1.5 km)"
    assert tlegs[1]["instruction"] == "Take Subway NS"
    assert tlegs[2]["instruction"] == "Take Bus"


def test_commute_expired_sample_pair_returns_409_with_exact_detail(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id)

    def mock_get(self, url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "found": 1,
            "results": [
                {
                    "SEARCHVAL": "Location",
                    "POSTAL": "560123",
                    "LATITUDE": "1.3500",
                    "LONGITUDE": "103.8500",
                }
            ],
        }
        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    from datetime import timedelta

    from kidcarematch_backend.core.commute import SGT

    past_wed = (datetime.now(SGT) - timedelta(days=7)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    past_sat = (datetime.now(SGT) - timedelta(days=4)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": [past_wed.isoformat(), past_sat.isoformat()],
        },
    )
    assert res.status_code == 409
    assert res.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }


@pytest.mark.parametrize(
    "invalid_pair",
    [
        [],
        ["2026-11-11T08:00:00+08:00"],
        [
            "2026-11-11T08:00:00+08:00",
            "2026-11-14T08:00:00+08:00",
            "2026-11-18T08:00:00+08:00",
        ],
    ],
)
def test_commute_empty_or_wrong_length_sample_pair_returns_409(
    client: TestClient, test_engine, parent_user, monkeypatch, invalid_pair
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id)

    def mock_get(self, url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "found": 1,
            "results": [
                {
                    "SEARCHVAL": "Location",
                    "POSTAL": "560123",
                    "LATITUDE": "1.3500",
                    "LONGITUDE": "103.8500",
                }
            ],
        }
        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": invalid_pair,
        },
    )
    assert res.status_code == 409
    assert res.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }


def test_commute_sample_pair_different_cycle_returns_409(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id)

    def mock_get(self, url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "found": 1,
            "results": [
                {
                    "SEARCHVAL": "Location",
                    "POSTAL": "560123",
                    "LATITUDE": "1.3500",
                    "LONGITUDE": "103.8500",
                }
            ],
        }
        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    from datetime import timedelta

    from kidcarematch_backend.core.commute import get_representative_sample_pair

    wed, sat = get_representative_sample_pair()

    # Next week's cycle (+7 days ahead)
    future_cycle = [
        (wed + timedelta(days=7)).isoformat(),
        (sat + timedelta(days=7)).isoformat(),
    ]
    res_future = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": future_cycle,
        },
    )
    assert res_future.status_code == 409
    assert res_future.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }

    # Swapped order [sat, wed]
    swapped = [sat.isoformat(), wed.isoformat()]
    res_swapped = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": swapped,
        },
    )
    assert res_swapped.status_code == 409
    assert res_swapped.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }

    # Malformed strings
    res_malformed = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": ["invalid-date-1", "invalid-date-2"],
        },
    )
    assert res_malformed.status_code == 409
    assert res_malformed.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }

    # Non-zero seconds
    res_seconds = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sample_pair": [
                wed.replace(second=15).isoformat(),
                sat.isoformat(),
            ],
        },
    )
    assert res_seconds.status_code == 409
    assert res_seconds.json()["detail"] == {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }


def test_commute_sample_pair_all_modes_and_reuse(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre1 = create_centre(test_engine, centre_id=1, postal="560123")
    centre2 = create_centre(test_engine, centre_id=2, postal="560456")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    from kidcarematch_backend.core.commute import get_representative_sample_pair

    wed, sat = get_representative_sample_pair()
    valid_pair = [wed.isoformat(), sat.isoformat()]

    pt_date_calls = []

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
                        "LATITUDE": (
                            "1.3000"
                            if postal == "120120"
                            else ("1.3500" if postal == "560123" else "1.3600")
                        ),
                        "LONGITUDE": (
                            "103.8000"
                            if postal == "120120"
                            else ("103.8500" if postal == "560123" else "103.8600")
                        ),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            route_type = params.get("routeType")
            if route_type == "pt":
                pt_date_calls.append(params.get("date"))
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 1500,
                                "legs": [
                                    {
                                        "mode": "BUS",
                                        "route": "159",
                                        "duration": 1500,
                                        "distance": 4500,
                                        "legGeometry": {"points": "sagGwhtxRsDkC"},
                                    }
                                ],
                            }
                        ]
                    }
                }
            else:
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "sagGwhtxRsDkC",
                    "route_summary": {"total_time": 600, "total_distance": 5000},
                }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    # 1. Test all modes return sample_pair without explicit input
    for mode in ("drive", "pt", "walk", "cycle"):
        res = client.post(
            f"/api/v1/centres/{centre1.centre_id}/commute",
            json={"profile_id": profile.profile_id, "mode": mode},
        )
        assert res.status_code == 200
        data = res.json()
        assert "sample_pair" in data
        assert isinstance(data["sample_pair"], list)
        assert len(data["sample_pair"]) == 2
        assert data["sample_pair"] == valid_pair

    # 2. Test all modes accept and reuse valid sample_pair across centres
    clear_commute_caches()
    for centre in (centre1, centre2):
        for mode in ("drive", "pt", "walk", "cycle"):
            res = client.post(
                f"/api/v1/centres/{centre.centre_id}/commute",
                json={
                    "profile_id": profile.profile_id,
                    "mode": mode,
                    "sample_pair": valid_pair,
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert data["sample_pair"] == valid_pair

    # 3. Verify PT OneMap routing calls used the exact dates from valid_pair
    # Total PT calls: 1 call from step 1 (2 requests for wd and we) = 2,
    # plus step 2 (2 centres * 2 requests for wd and we) = 4 -> total 6 calls
    assert len(pt_date_calls) == 6
    expected_wed_date = wed.strftime("%m-%d-%Y")
    expected_sat_date = sat.strftime("%m-%d-%Y")
    for i in range(0, 6, 2):
        assert pt_date_calls[i] == expected_wed_date
        assert pt_date_calls[i + 1] == expected_sat_date

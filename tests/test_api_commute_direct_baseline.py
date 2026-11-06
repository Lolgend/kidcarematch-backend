"""Tests for opt-in direct commute baseline route and failure survival (Ticket #90)."""

import re
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
    SGT,
    build_commute_reason,
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
from kidcarematch_backend.schemas.commute import FailureClass, ReasonCode

EXPECTED_REASON_MAPPINGS: dict[ReasonCode, tuple[FailureClass, bool]] = {
    # Sample dependent
    "no_itinerary": ("sample_dependent", True),
    # Conditions
    "direct_unavailable": ("conditions", False),
    "via_centre_incomplete": ("conditions", False),
    "timing_unverified": ("conditions", False),
    "conditions_mismatch": ("conditions", False),
    # Location
    "home_unresolved": ("location", False),
    "centre_unresolved": ("location", False),
    "workplace_unresolved": ("location", False),
    # Not applicable
    "workplace_not_configured": ("not_applicable", False),
    # Service - retryable
    "provider_timeout": ("service", True),
    "provider_unavailable": ("service", True),
    "provider_auth_failed": ("service", True),
    "evaluation_budget_exhausted": ("service", True),
    # Service - non-retryable
    "invalid_provider_payload": ("service", False),
    "geometry_unavailable": ("service", False),
}


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
            user_id="parent-db-1",
            firebase_uid="firebase-parent-db-1",
            email="parent_db@example.com",
            full_name="Parent Baseline",
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
    profile_id: str = "prof-db-1",
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


def mock_onemap_routing(monkeypatch, route_time=600, route_dist=5000, polyline="sagGwhtxRsDkC"):
    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            offset = int(postal) % 1000 / 10000.0
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(1.3000 + offset),
                        "LONGITUDE": str(103.8000 + offset),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": polyline,
                "route_summary": {
                    "total_time": route_time,
                    "total_distance": route_dist,
                },
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)


def test_all_15_reason_codes_mapped_accurately():
    assert len(EXPECTED_REASON_MAPPINGS) == 15
    for code, (expected_class, expected_retryable) in EXPECTED_REASON_MAPPINGS.items():
        reason = build_commute_reason(code)
        assert reason.code == code
        assert reason.failure_class == expected_class
        assert reason.retryable is expected_retryable


def test_commute_legacy_request_omits_negotiated_fields(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(test_engine, parent_user.user_id, home_postal="120120")
    mock_onemap_routing(monkeypatch)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": False},
    )
    assert res.status_code == 200
    data = res.json()
    assert "comparison_contract" not in data
    assert "evaluation" not in data
    assert "workplace" not in data["anchors"]
    sample = data["samples"][0]
    assert "direct_route" not in sample
    assert "comparison" not in sample
    assert "via_status" not in sample
    assert "conditions" not in sample


def test_commute_opt_in_direct_baseline_returns_negotiated_response_and_metadata(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre1 = create_centre(test_engine, centre_id=1, postal="560123")
    centre2 = create_centre(test_engine, centre_id=2, postal="650321")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal=None
    )
    mock_onemap_routing(monkeypatch)

    res1 = client.post(
        f"/api/v1/centres/{centre1.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res1.status_code == 200
    data1 = res1.json()

    assert data1["comparison_contract"] == "direct_baseline_v1"
    assert "workplace" in data1["anchors"]
    assert data1["anchors"]["workplace"] is None

    # Check evaluation metadata
    eval1 = data1["evaluation"]
    assert re.match(r"^[0-9a-f-]{36}$", eval1["evaluation_id"])
    assert "evaluated_at" in eval1
    assert len(eval1["profile_revision"]) == 64
    assert len(eval1["anchor_revision"]) == 64
    # Zero plaintext coordinates or postal codes in revisions
    assert "120120" not in eval1["profile_revision"]
    assert "560123" not in eval1["anchor_revision"]

    # Evaluate centre2 for the same profile
    res2 = client.post(
        f"/api/v1/centres/{centre2.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    eval2 = data2["evaluation"]

    # Profile revision must be shared across centre evaluations for the same profile state
    assert eval2["profile_revision"] == eval1["profile_revision"]
    # Anchor revision must differ because centre2 has different coordinates
    assert eval2["anchor_revision"] != eval1["anchor_revision"]


def test_commute_direct_baseline_no_workplace_not_applicable(
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
                        "LATITUDE": "1.3000",
                        "LONGITUDE": "103.8000",
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
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert len(routing_calls) == 1  # Only home -> centre, zero direct route calls
    assert sample["via_status"] == "available"

    direct = sample["direct_route"]
    assert direct["status"] == "not_applicable"
    assert direct["legs"] == []
    assert direct["totals"] is None
    assert len(direct["reasons"]) == 1
    assert direct["reasons"][0]["code"] == "workplace_not_configured"
    assert direct["reasons"][0]["failure_class"] == "not_applicable"
    assert direct["reasons"][0]["retryable"] is False

    comp = sample["comparison"]
    assert comp["status"] == "not_applicable"
    assert comp["certification"] is None
    assert len(comp["reasons"]) == 1
    assert comp["reasons"][0]["code"] == "workplace_not_configured"


def test_commute_direct_baseline_unresolvable_workplace_unavailable(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="999999"
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
                "route_summary": {"total_time": 600, "total_distance": 5000},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["via_status"] == "partial"
    assert sample["totals"] is None

    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert len(direct["legs"]) == 1
    leg = direct["legs"][0]
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "workplace"
    assert leg["status"] == "unavailable"
    assert leg["reasons"][0]["code"] == "workplace_unresolved"
    assert leg["reasons"][0]["failure_class"] == "location"
    assert leg["reasons"][0]["retryable"] is False

    assert direct["totals"] is None
    assert direct["reasons"][0]["code"] == "workplace_unresolved"

    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    assert comp["certification"] is None
    comp_codes = [r["code"] for r in comp["reasons"]]
    assert "direct_unavailable" in comp_codes
    assert "workplace_unresolved" in comp_codes


def test_commute_direct_baseline_unresolvable_home_unavailable(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="888888", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "888888":
                mock_resp.json.return_value = {"found": 0, "results": []}
                return mock_resp
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
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
                "route_summary": {"total_time": 600, "total_distance": 5000},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert len(direct["legs"]) == 1
    assert direct["legs"][0]["reasons"][0]["code"] == "home_unresolved"
    assert direct["reasons"][0]["code"] == "home_unresolved"
    assert direct["totals"] is None


def test_commute_direct_baseline_driving_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    routes_called = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            routes_called.append((start, end))

            # Direct route home -> workplace (1.3, 103.8 -> 1.4, 103.9)
            if start == "1.3,103.8" and end == "1.4,103.9":
                dur = 1200
                dist = 15000
            elif start == "1.3,103.8" and end == "1.35,103.85":  # Home -> Centre
                dur = 600
                dist = 6000
            else:  # Centre -> Workplace
                dur = 800
                dist = 10000

            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Verify 3 routes called: home->centre, centre->workplace, home->workplace (direct)
    assert len(routes_called) == 3
    assert ("1.3,103.8", "1.4,103.9") in routes_called

    # Via route assertions
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 1400.0
    assert sample["totals"]["distance_meters"] == 16000.0

    # Direct route assertions
    direct = sample["direct_route"]
    assert direct["status"] == "available"
    assert direct["totals"]["duration_seconds"] == 1200.0
    assert direct["totals"]["distance_meters"] == 15000.0
    assert direct["reasons"] == []

    assert len(direct["legs"]) == 1
    d_leg = direct["legs"][0]
    assert d_leg["from_anchor"] == "home"
    assert d_leg["to_anchor"] == "workplace"
    assert d_leg["status"] == "available"
    assert len(d_leg["geometry"]) == 2  # decoded polyline
    assert d_leg["timing"]["duration_basis"] == "provider_default"
    assert d_leg["timing"]["arrival_source"] == "not_applicable"
    assert d_leg["timing"]["initial_wait_included"] is None
    assert d_leg["reasons"] == []

    # Comparison assertions
    comp = sample["comparison"]
    assert comp["status"] == "available"
    assert comp["reasons"] == []
    assert comp["certification"]["duration_basis"] == "provider_default"
    assert comp["certification"]["provider_options_id"] == "onemap_drive_default"


def test_commute_direct_baseline_pt_elapsed_timing_success(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            date_str = params.get("date", "06-10-2026")
            time_str = params.get("time", "08:00:00")
            dep_dt = datetime.strptime(f"{date_str} {time_str}", "%m-%d-%Y %H:%M:%S").replace(
                tzinfo=SGT
            )
            # End time 15 minutes (900s) after requested departure (with 10 min transit duration)
            end_ms = int((dep_dt.timestamp() + 900) * 1000)

            mock_resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": 600,  # 10 min itinerary
                            "endTime": end_ms,  # 15 min elapsed
                            "legs": [
                                {
                                    "mode": "WALK",
                                    "duration": 180,
                                    "distance": 200,
                                    "legGeometry": {"points": "sagGwhtxRsDkC"},
                                },
                                {
                                    "mode": "BUS",
                                    "duration": 420,
                                    "distance": 3000,
                                    "route": "147",
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
        json={"profile_id": profile.profile_id, "mode": "pt", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["samples"]) == 2  # Weekday and weekend in legacy pair policy

    for sample in data["samples"]:
        assert sample["via_status"] == "available"
        assert sample["direct_route"]["status"] == "available"
        assert sample["direct_route"]["totals"]["duration_seconds"] == 900.0  # elapsed
        d_leg = sample["direct_route"]["legs"][0]
        assert d_leg["departure_at"] is not None
        assert d_leg["timing"]["duration_basis"] == "elapsed_from_requested_departure"
        assert d_leg["timing"]["arrival_source"] == "provider_end_time"
        assert d_leg["timing"]["initial_wait_included"] is True

        assert sample["comparison"]["status"] == "available"
        assert sample["comparison"]["certification"]["duration_basis"] == "elapsed_from_requested_departure"


def test_commute_direct_baseline_pt_duration_derived_timing_unverified(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
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
                        "LATITUDE": "1.3000",
                        "LONGITUDE": "103.8000",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            # Missing endTime -> duration derived
            mock_resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": 600,
                            "legs": [
                                {
                                    "mode": "WALK",
                                    "duration": 600,
                                    "distance": 500,
                                    "legGeometry": {"points": "sagGwhtxRsDkC"},
                                }
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
        json={"profile_id": profile.profile_id, "mode": "pt", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["direct_route"]["status"] == "available"
    d_leg = sample["direct_route"]["legs"][0]
    assert d_leg["timing"]["duration_basis"] == "itinerary_duration"
    assert d_leg["timing"]["arrival_source"] == "duration_derived"
    assert d_leg["timing"]["initial_wait_included"] is False

    # Comparison should be unavailable with timing_unverified
    assert sample["comparison"]["status"] == "unavailable"
    assert sample["comparison"]["certification"] is None
    comp_codes = [r["code"] for r in sample["comparison"]["reasons"]]
    assert "timing_unverified" in comp_codes


def test_commute_unresolvable_centre_preserves_available_direct_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    # Centre with unresolvable postal "000000"
    centre = create_centre(test_engine, centre_id=1, postal="000000")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "000000":
                mock_resp.json.return_value = {"found": 0, "results": []}
                return mock_resp
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": "1.3000" if postal == "120120" else "1.4000",
                        "LONGITUDE": "103.8000" if postal == "120120" else "103.9000",
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 1200, "total_distance": 15000},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Via legs fail because centre is unresolvable
    assert sample["via_status"] == "unavailable"
    assert sample["totals"] is None
    for v_leg in sample["legs"]:
        assert v_leg["status"] == "unavailable"
        assert v_leg["reasons"][0]["code"] == "centre_unresolved"

    # Direct route STILL succeeds independently!
    direct = sample["direct_route"]
    assert direct["status"] == "available"
    assert direct["totals"]["duration_seconds"] == 1200.0
    assert direct["totals"]["distance_meters"] == 15000.0
    assert len(direct["legs"]) == 1
    assert direct["legs"][0]["status"] == "available"
    assert len(direct["legs"][0]["geometry"]) == 2

    # Comparison unavailable due to via_centre_incomplete
    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    comp_codes = [r["code"] for r in comp["reasons"]]
    assert "via_centre_incomplete" in comp_codes


def test_commute_direct_route_timeout_preserves_via_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            # If direct route query (1.3, 103.8 -> 1.4, 103.9), raise TimeoutException
            if start == "1.3,103.8" and end == "1.4,103.9":
                raise httpx.ReadTimeout("Direct routing timed out")

            # Via route succeeds
            dur = 600 if end == "1.35,103.85" else 800
            dist = 5000 if end == "1.35,103.85" else 7000
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Via route and totals are preserved completely intact!
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 1400.0
    assert sample["totals"]["distance_meters"] == 12000.0
    for leg in sample["legs"]:
        assert leg["status"] == "available"

    # Direct route fails with provider_timeout
    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert direct["totals"] is None
    assert len(direct["reasons"]) == 1
    assert direct["reasons"][0]["code"] == "provider_timeout"
    assert direct["reasons"][0]["failure_class"] == "service"
    assert direct["reasons"][0]["retryable"] is True
    assert direct["legs"][0]["reasons"][0]["code"] == "provider_timeout"

    # Comparison is unavailable with direct_unavailable and provider_timeout
    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    comp_codes = [r["code"] for r in comp["reasons"]]
    assert "direct_unavailable" in comp_codes
    assert "provider_timeout" in comp_codes


def test_commute_direct_route_401_auth_exhaustion_preserves_via_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            # If direct route query (1.3, 103.8 -> 1.4, 103.9), simulate 401 exhaustion
            if start == "1.3,103.8" and end == "1.4,103.9":
                resp_401 = MagicMock()
                resp_401.status_code = 401
                resp_401.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "401 Unauthorized", request=MagicMock(), response=resp_401
                )
                return resp_401

            # Via route succeeds
            dur = 600 if end == "1.35,103.85" else 800
            dist = 5000 if end == "1.35,103.85" else 7000
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Via route and totals are preserved completely intact!
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 1400.0

    # Direct route fails with provider_auth_failed
    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert direct["reasons"][0]["code"] == "provider_auth_failed"
    assert direct["reasons"][0]["failure_class"] == "service"
    assert direct["reasons"][0]["retryable"] is True

    comp_codes = [r["code"] for r in sample["comparison"]["reasons"]]
    assert "direct_unavailable" in comp_codes
    assert "provider_auth_failed" in comp_codes


def test_commute_direct_route_500_provider_unavailable_preserves_via_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            # If direct route query (1.3, 103.8 -> 1.4, 103.9), simulate 500 error
            if start == "1.3,103.8" and end == "1.4,103.9":
                resp_500 = MagicMock()
                resp_500.status_code = 500
                resp_500.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "500 Internal Server Error", request=MagicMock(), response=resp_500
                )
                return resp_500

            # Via route succeeds
            dur = 600 if end == "1.35,103.85" else 800
            dist = 5000 if end == "1.35,103.85" else 7000
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 1400.0

    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert direct["reasons"][0]["code"] == "provider_unavailable"
    assert direct["reasons"][0]["failure_class"] == "service"
    assert direct["reasons"][0]["retryable"] is True


def test_commute_direct_route_geometry_unavailable_preserves_via_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            # If direct route query, return empty route_geometry
            if start == "1.3,103.8" and end == "1.4,103.9":
                mock_resp.json.return_value = {
                    "status_message": "Found route between points",
                    "route_geometry": "",
                    "route_summary": {"total_time": 1200, "total_distance": 15000},
                }
                return mock_resp

            # Via route succeeds
            dur = 600 if end == "1.35,103.85" else 800
            dist = 5000 if end == "1.35,103.85" else 7000
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 1400.0

    direct = sample["direct_route"]
    assert direct["status"] == "unavailable"
    assert direct["reasons"][0]["code"] == "geometry_unavailable"
    assert direct["reasons"][0]["failure_class"] == "service"
    assert direct["reasons"][0]["retryable"] is False


def test_commute_via_leg2_failure_preserves_available_direct_route(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="650321"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.4000)
            lng = 103.8000 if postal == "120120" else (103.8500 if postal == "560123" else 103.9000)
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": f"Location {postal}",
                        "POSTAL": postal,
                        "LATITUDE": str(lat),
                        "LONGITUDE": str(lng),
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start = params.get("start")
            end = params.get("end")
            # If Leg 2 (centre -> workplace: 1.35, 103.85 -> 1.4, 103.9), timeout
            if start == "1.35,103.85" and end == "1.4,103.9":
                raise httpx.ReadTimeout("Centre to workplace timed out")

            dur = 600 if end == "1.35,103.85" else 1200
            dist = 5000 if end == "1.35,103.85" else 15000
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": dur, "total_distance": dist},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={"profile_id": profile.profile_id, "mode": "drive", "include_direct_baseline": True},
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Leg 1 succeeded, Leg 2 failed -> via_status is partial, totals is None, leg 1 preserved
    assert sample["via_status"] == "partial"
    assert sample["totals"] is None
    assert sample["legs"][0]["status"] == "available"
    assert sample["legs"][1]["status"] == "unavailable"
    assert sample["legs"][1]["reasons"][0]["code"] == "provider_timeout"

    # Direct route STILL succeeds with available status, decoded geometry, and totals!
    direct = sample["direct_route"]
    assert direct["status"] == "available"
    assert direct["totals"]["duration_seconds"] == 1200.0
    assert direct["totals"]["distance_meters"] == 15000.0
    assert direct["reasons"] == []

    # Comparison is unavailable with via_centre_incomplete
    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    comp_codes = [r["code"] for r in comp["reasons"]]
    assert "via_centre_incomplete" in comp_codes


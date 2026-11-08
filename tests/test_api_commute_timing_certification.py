"""Tests for elapsed timing certification and fail-closed commute comparison (Ticket #91)."""

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
    CommuteAnchors,
    Coordinate,
    build_negotiated_commute_sample,
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
            user_id="parent-cert-1",
            firebase_uid="firebase-parent-cert-1",
            email="parent_cert@example.com",
            full_name="Parent Timing Cert",
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
    profile_id: str = "prof-cert-1",
    home_postal: str = "120120",
    workplace_postal: str | None = "048581",
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
            vacancy_weight=3,
            cost_weight=3,
            travel_weight=3,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def test_commute_pt_initial_wait_included_and_timing_certified(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """Verify PT initial wait inclusion:

    Request: 08:00 SGT.
    Leg 1: 10-min itinerary (600s), arrival at 08:15 SGT (endTime) -> elapsed 900s.
    Onward leg: departs at exactly 08:15:00 SGT, 15-min itinerary (900s), arrival 08:35 SGT -> elapsed 1200s.
    Direct leg: departs at 08:00 SGT, 20-min itinerary (1200s), arrival 08:25 SGT -> elapsed 1500s.
    All legs certified with duration_basis="elapsed_from_requested_departure",
    arrival_source="provider_end_time", initial_wait_included=True.
    Comparison certified available with provider_options_id="onemap_pt_default".
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    recorded_pt_calls: list[dict] = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            recorded_pt_calls.append(dict(params))
            start = str(params.get("start", ""))

            # Distinguish legs based on start coords
            if "1.3" in start and "103.8" in start and "1.35" not in start and "1.28" not in start:
                # Could be Leg 1 (home -> centre) or Direct Leg (home -> workplace)
                end = str(params.get("end", ""))
                if "1.28" in end:
                    # Direct leg: home -> workplace. Departs 08:00, arrival 08:25 (1500s elapsed, 1200s itinerary)
                    dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)
                    end_ms = int((dep_dt.timestamp() + 1500) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 1200,
                                    "endTime": end_ms,
                                    "legs": [
                                        {
                                            "mode": "BUS",
                                            "route": "10",
                                            "duration": 1200,
                                            "distance": 8000,
                                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                else:
                    # Leg 1: home -> centre. Departs 08:00, arrival 08:15 (900s elapsed, 600s itinerary)
                    dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)
                    end_ms = int((dep_dt.timestamp() + 900) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 600,
                                    "endTime": end_ms,
                                    "legs": [
                                        {
                                            "mode": "WALK",
                                            "duration": 600,
                                            "distance": 800,
                                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
            else:
                # Leg 2: centre -> workplace. Departs at Leg 1 arrival (08:15:00)
                dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)
                end_ms = int((dep_dt.timestamp() + 1200) * 1000)
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 900,
                                "endTime": end_ms,
                                "legs": [
                                    {
                                        "mode": "SUBWAY",
                                        "route": "NSL",
                                        "duration": 900,
                                        "distance": 5000,
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
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["samples"]) == 2

    sample = data["samples"][0]  # Wednesday sample
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 900.0 + 1200.0  # 2100.0s elapsed sum

    # Leg 1 verification
    leg1 = sample["legs"][0]
    assert leg1["from_anchor"] == "home"
    assert leg1["to_anchor"] == "centre"
    assert leg1["status"] == "available"
    assert leg1["duration_seconds"] == 900.0  # includes initial wait
    assert leg1["departure_at"] is not None
    assert leg1["arrival_at"] is not None
    assert leg1["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert leg1["timing"]["arrival_source"] == "provider_end_time"
    assert leg1["timing"]["initial_wait_included"] is True

    # Leg 2 onward departure verification: must depart at exact Leg 1 arrival
    leg2 = sample["legs"][1]
    assert leg2["from_anchor"] == "centre"
    assert leg2["to_anchor"] == "workplace"
    assert leg2["status"] == "available"
    assert leg2["duration_seconds"] == 1200.0  # elapsed
    assert leg2["departure_at"] == leg1["arrival_at"]
    assert leg2["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert leg2["timing"]["arrival_source"] == "provider_end_time"
    assert leg2["timing"]["initial_wait_included"] is True

    # Direct route verification
    direct = sample["direct_route"]
    assert direct["status"] == "available"
    assert direct["totals"]["duration_seconds"] == 1500.0
    direct_leg = direct["legs"][0]
    assert direct_leg["from_anchor"] == "home"
    assert direct_leg["to_anchor"] == "workplace"
    assert direct_leg["duration_seconds"] == 1500.0
    assert direct_leg["departure_at"] == leg1["departure_at"]  # same initial departure
    assert direct_leg["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert direct_leg["timing"]["arrival_source"] == "provider_end_time"
    assert direct_leg["timing"]["initial_wait_included"] is True

    # Comparison certification verification
    comp = sample["comparison"]
    assert comp["status"] == "available"
    assert comp["reasons"] == []
    assert comp["certification"] is not None
    assert comp["certification"]["duration_basis"] == "elapsed_from_requested_departure"
    assert comp["certification"]["provider_options_id"] == "onemap_pt_default"


def test_commute_pt_sequential_onward_departure_midnight_rollover(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """Verify sequential onward PT leg rolling across midnight:

    Leg 1 departs at 23:50:00 SGT on 06-10-2026.
    Leg 1 endTime is 00:15:00 SGT on 06-11-2026 (arrival on next calendar day).
    Leg 2 onward OneMap query must rollover date to '06-11-2026' and time to '00:15:00'.
    Leg 2 departs at 00:15:00 SGT with zero drop-off dwell.
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    # Mock representative sample pair so Wednesday departs at 23:50:00 SGT
    wed_late = datetime(2026, 6, 10, 23, 50, 0, tzinfo=SGT)
    sat_late = datetime(2026, 6, 13, 23, 50, 0, tzinfo=SGT)
    monkeypatch.setattr(
        "kidcarematch_backend.api.v1.centres.validate_sample_pair",
        lambda *args, **kwargs: (wed_late, sat_late),
    )

    recorded_pt_calls: list[dict] = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            recorded_pt_calls.append(dict(params))
            start = str(params.get("start", ""))
            end = str(params.get("end", ""))

            if "1.3" in start and "103.8" in start and "1.35" not in start and "1.28" not in start:
                if "1.28" in end:
                    # Direct leg: departs 23:50:00 on 06-10-2026, arrives 00:25:00 on 06-11-2026 (2100s elapsed)
                    arrival_dt = datetime(2026, 6, 11, 0, 25, 0, tzinfo=SGT)
                    end_ms = int(arrival_dt.timestamp() * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 1800,
                                    "endTime": end_ms,
                                    "legs": [
                                        {
                                            "mode": "BUS",
                                            "route": "10",
                                            "duration": 1800,
                                            "distance": 8000,
                                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                else:
                    # Leg 1: departs 23:50:00 on 06-10-2026, arrives 00:15:00 on 06-11-2026 (1500s elapsed, 600s itinerary)
                    arrival_dt = datetime(2026, 6, 11, 0, 15, 0, tzinfo=SGT)
                    end_ms = int(arrival_dt.timestamp() * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 600,
                                    "endTime": end_ms,
                                    "legs": [
                                        {
                                            "mode": "WALK",
                                            "duration": 600,
                                            "distance": 800,
                                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
            else:
                # Leg 2: centre -> workplace
                # Arrival is 00:35:00 on 06-11-2026
                arrival_dt = datetime(2026, 6, 11, 0, 35, 0, tzinfo=SGT)
                end_ms = int(arrival_dt.timestamp() * 1000)
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 900,
                                "endTime": end_ms,
                                "legs": [
                                    {
                                        "mode": "SUBWAY",
                                        "route": "NSL",
                                        "duration": 900,
                                        "distance": 5000,
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
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Verify Leg 1 arrival is on next day (06-11-2026 00:15:00 SGT)
    leg1 = sample["legs"][0]
    assert leg1["departure_at"] == "2026-06-10T23:50:00+08:00"
    assert leg1["arrival_at"] == "2026-06-11T00:15:00+08:00"
    assert leg1["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert leg1["duration_seconds"] == 1500.0  # 23:50 to 00:15

    # Verify Leg 2 requested departure rolls over date to 06-11-2026 and time to 00:15:00
    leg2 = sample["legs"][1]
    assert leg2["departure_at"] == "2026-06-11T00:15:00+08:00"
    assert leg2["arrival_at"] == "2026-06-11T00:35:00+08:00"
    assert leg2["duration_seconds"] == 1200.0  # 00:15 to 00:35

    # Find the OneMap call for Leg 2 (start from centre 1.3500)
    leg2_calls = [c for c in recorded_pt_calls if "1.35" in str(c.get("start", ""))]
    assert len(leg2_calls) > 0
    leg2_first_call = leg2_calls[0]
    assert leg2_first_call["date"] == "06-11-2026"  # Rolled over to next calendar day!
    assert leg2_first_call["time"] == "00:15:00"    # Rolled over departure time!

    # Certified comparison is available across midnight
    assert sample["comparison"]["status"] == "available"
    assert sample["comparison"]["certification"]["duration_basis"] == "elapsed_from_requested_departure"


def test_commute_pt_missing_endtime_on_leg1_preserves_routes_fails_closed_comparison(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """Missing endTime on Leg 1:

    - Leg 1 duration_seconds == itinerary_duration (600s).
    - Leg 1 timing: duration_basis='itinerary_duration', arrival_source='duration_derived', initial_wait_included=False.
    - Leg 1 and Leg 2 remain status='available'.
    - Via route totals remain populated!
    - Direct route remains status='available' with totals populated.
    - Comparison fails closed: status='unavailable', reasons=[timing_unverified], certification=None.
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            start = str(params.get("start", ""))
            end = str(params.get("end", ""))
            dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)

            if "1.3" in start and "103.8" in start and "1.35" not in start and "1.28" not in start:
                if "1.28" in end:
                    # Direct leg: verified with endTime
                    end_ms = int((dep_dt.timestamp() + 1500) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 1200,
                                    "endTime": end_ms,
                                    "legs": [
                                        {"mode": "BUS", "route": "10", "duration": 1200, "distance": 8000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
                else:
                    # Leg 1: MISSING endTime!
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 600,
                                    # NO endTime!
                                    "legs": [
                                        {"mode": "WALK", "duration": 600, "distance": 800, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
            else:
                # Leg 2: verified with endTime
                end_ms = int((dep_dt.timestamp() + 1200) * 1000)
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 900,
                                "endTime": end_ms,
                                "legs": [
                                    {"mode": "SUBWAY", "route": "NSL", "duration": 900, "distance": 5000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
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
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Leg 1: available, duration-derived
    leg1 = sample["legs"][0]
    assert leg1["status"] == "available"
    assert leg1["duration_seconds"] == 600.0
    assert leg1["timing"]["duration_basis"] == "itinerary_duration"
    assert leg1["timing"]["arrival_source"] == "duration_derived"
    assert leg1["timing"]["initial_wait_included"] is False

    # Leg 2: departs at synthesized leg1 arrival, available
    leg2 = sample["legs"][1]
    assert leg2["status"] == "available"
    assert leg2["departure_at"] == leg1["arrival_at"]

    # Via totals intact and available!
    assert sample["via_status"] == "available"
    assert sample["totals"] is not None
    assert sample["totals"]["duration_seconds"] == 600.0 + 1200.0

    # Direct route intact and available!
    assert sample["direct_route"]["status"] == "available"
    assert sample["direct_route"]["totals"]["duration_seconds"] == 1500.0

    # Comparison fails closed
    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    assert comp["certification"] is None
    assert len(comp["reasons"]) == 1
    assert comp["reasons"][0]["code"] == "timing_unverified"
    assert comp["reasons"][0]["failure_class"] == "conditions"
    assert comp["reasons"][0]["retryable"] is False


@pytest.mark.parametrize(
    "bad_case",
    [
        True,
        "not-a-number",
        -5000,
        "elapsed_underflow",  # will produce elapsed < duration
        "elapsed_overflow",   # will produce elapsed > 24h
    ],
)
def test_commute_pt_invalid_endtime_variants_fail_closed_to_duration_derived(
    client: TestClient, test_engine, parent_user, monkeypatch, bad_case
):
    """Verify various invalid endTime payloads on Leg 2:

    Cases:
    - Boolean: endTime=True
    - Non-numeric string: endTime="invalid"
    - Negative: endTime=-1000
    - Elapsed < itinerary_duration: endTime 300s after departure when itinerary is 600s
    - Elapsed > 24 hours: endTime 25 hours after departure
    All must synthesize duration-derived arrival, keep status='available',
    and fail closed comparison with timing_unverified.
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            start = str(params.get("start", ""))
            end = str(params.get("end", ""))
            dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)

            if "1.3" in start and "103.8" in start and "1.35" not in start and "1.28" not in start:
                if "1.28" in end:
                    # Direct leg: verified
                    end_ms = int((dep_dt.timestamp() + 1500) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 1200,
                                    "endTime": end_ms,
                                    "legs": [
                                        {"mode": "BUS", "route": "10", "duration": 1200, "distance": 8000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
                else:
                    # Leg 1: verified
                    end_ms = int((dep_dt.timestamp() + 900) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 600,
                                    "endTime": end_ms,
                                    "legs": [
                                        {"mode": "WALK", "duration": 600, "distance": 800, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
            else:
                # Leg 2: INVALID endTime
                if bad_case == "elapsed_underflow":
                    leg2_end = int((dep_dt.timestamp() + 300) * 1000)  # 300s elapsed < 900s itinerary
                elif bad_case == "elapsed_overflow":
                    leg2_end = int((dep_dt.timestamp() + 25 * 3600) * 1000)  # 25 hours elapsed > 24h
                else:
                    leg2_end = bad_case

                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 900,
                                "endTime": leg2_end,
                                "legs": [
                                    {"mode": "SUBWAY", "route": "NSL", "duration": 900, "distance": 5000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
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
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    leg2 = sample["legs"][1]
    assert leg2["status"] == "available", f"Failed for bad_case: {bad_case}"
    assert leg2["duration_seconds"] == 900.0  # itinerary duration synthesized
    assert leg2["timing"]["duration_basis"] == "itinerary_duration"
    assert leg2["timing"]["arrival_source"] == "duration_derived"
    assert leg2["timing"]["initial_wait_included"] is False

    # Via totals remain available
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 900.0 + 900.0

    # Comparison fails closed
    assert sample["comparison"]["status"] == "unavailable"
    assert sample["comparison"]["certification"] is None
    assert any(r["code"] == "timing_unverified" for r in sample["comparison"]["reasons"])


def test_commute_drive_walk_cycle_timing_certification_and_comparison(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """Verify drive, walk, and cycle modes:

    All legs: duration_basis='provider_default', arrival_source='not_applicable',
              initial_wait_included=None, departure_at=None, arrival_at=None.
    Comparison: status='available', reasons=[], certification.duration_basis='provider_default',
                provider_options_id='onemap_{mode}_default'.
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    for mode in ("drive", "walk", "cycle"):
        def mock_get(self, url, *args, **kwargs):
            url_str = str(url)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()

            if "elastic/search" in url_str:
                params = kwargs.get("params", {})
                postal = str(params.get("searchVal", ""))
                lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
                lng = 103.8000 if postal == "120120" else 103.8500
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
            json={
                "profile_id": profile.profile_id,
                "mode": mode,
                "include_direct_baseline": True,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert len(data["samples"]) == 1
        sample = data["samples"][0]
        assert sample["via_status"] == "available"
        assert sample["direct_route"]["status"] == "available"

        # Check legs
        all_legs = sample["legs"] + sample["direct_route"]["legs"]
        for leg in all_legs:
            assert leg["timing"]["duration_basis"] == "provider_default"
            assert leg["timing"]["arrival_source"] == "not_applicable"
            assert leg["timing"]["initial_wait_included"] is None
            assert leg["departure_at"] is None
            assert leg["arrival_at"] is None

        # Check comparison
        comp = sample["comparison"]
        assert comp["status"] == "available"
        assert comp["reasons"] == []
        assert comp["certification"] is not None
        assert comp["certification"]["duration_basis"] == "provider_default"
        assert comp["certification"]["provider_options_id"] == f"onemap_{mode}_default"


def test_commute_conditions_mismatch_fails_closed_comparison(
    monkeypatch
):
    """If conditions or provider_options_id is mismatched, comparison fails closed with conditions_mismatch."""
    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            mock_resp.json.return_value = {
                "status_message": "Found route between points",
                "route_geometry": "sagGwhtxRsDkC",
                "route_summary": {"total_time": 600, "total_distance": 5000},
            }
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    anchors = CommuteAnchors(
        home=Coordinate(lat=1.3000, lng=103.8000),
        centre=Coordinate(lat=1.3500, lng=103.8500),
        workplace=Coordinate(lat=1.2800, lng=103.8500),
        has_workplace=True,
    )
    # If conditions has compatible provider_options_id
    sample = build_negotiated_commute_sample(
        kind="single",
        sampled_at=None,
        mode="drive",
        anchors=anchors,
        include_direct_baseline=True,
        conditions_id="drive_default_conditions_v1",
    )
    assert sample.comparison is not None
    assert sample.comparison.status == "available"

    # Now if provider_options_id does not match expected
    sample_mismatched = build_negotiated_commute_sample(
        kind="single",
        sampled_at=None,
        mode="drive",
        anchors=anchors,
        include_direct_baseline=True,
        conditions_id="drive_default_conditions_v1",
        provider_options_id="mismatched_options_id_v2",
    )
    assert sample_mismatched.comparison is not None
    assert sample_mismatched.comparison.status == "unavailable"
    assert sample_mismatched.comparison.certification is None
    assert len(sample_mismatched.comparison.reasons) == 1
    assert sample_mismatched.comparison.reasons[0].code == "conditions_mismatch"


def test_commute_pt_missing_endtime_on_direct_leg_preserves_routes_fails_closed_comparison(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """Missing endTime on Direct Leg:

    - Direct leg duration_seconds == itinerary_duration (1200s).
    - Direct leg timing: duration_basis='itinerary_duration', arrival_source='duration_derived', initial_wait_included=False.
    - Direct route status == 'available'.
    - Via route status == 'available' with certified elapsed timing.
    - Comparison fails closed: status='unavailable', reasons=[timing_unverified], certification=None.
    """
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            start = str(params.get("start", ""))
            end = str(params.get("end", ""))
            dep_dt = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SGT)

            if "1.3" in start and "103.8" in start and "1.35" not in start and "1.28" not in start:
                if "1.28" in end:
                    # Direct leg: MISSING endTime!
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 1200,
                                    "legs": [
                                        {"mode": "BUS", "route": "10", "duration": 1200, "distance": 8000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
                else:
                    # Leg 1: verified with endTime
                    end_ms = int((dep_dt.timestamp() + 900) * 1000)
                    mock_resp.json.return_value = {
                        "plan": {
                            "itineraries": [
                                {
                                    "duration": 600,
                                    "endTime": end_ms,
                                    "legs": [
                                        {"mode": "WALK", "duration": 600, "distance": 800, "legGeometry": {"points": "sagGwhtxRsDkC"}}
                                    ],
                                }
                            ]
                        }
                    }
            else:
                # Leg 2: verified with endTime
                end_ms = int((dep_dt.timestamp() + 1200) * 1000)
                mock_resp.json.return_value = {
                    "plan": {
                        "itineraries": [
                            {
                                "duration": 900,
                                "endTime": end_ms,
                                "legs": [
                                    {"mode": "SUBWAY", "route": "NSL", "duration": 900, "distance": 5000, "legGeometry": {"points": "sagGwhtxRsDkC"}}
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
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    # Via route: available and elapsed
    assert sample["via_status"] == "available"
    assert sample["legs"][0]["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert sample["legs"][1]["timing"]["duration_basis"] == "elapsed_from_requested_departure"

    # Direct route: available and duration-derived
    direct = sample["direct_route"]
    assert direct["status"] == "available"
    assert direct["totals"]["duration_seconds"] == 1200.0  # itinerary duration
    direct_leg = direct["legs"][0]
    assert direct_leg["timing"]["duration_basis"] == "itinerary_duration"
    assert direct_leg["timing"]["arrival_source"] == "duration_derived"
    assert direct_leg["timing"]["initial_wait_included"] is False

    # Comparison fails closed
    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    assert comp["certification"] is None
    assert any(r["code"] == "timing_unverified" for r in comp["reasons"])


def test_commute_incomplete_via_route_fails_closed_comparison_with_via_centre_incomplete(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """When via route is incomplete/partial (e.g. Leg 2 fails), comparison fails closed with via_centre_incomplete."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120", workplace_postal="048581"
    )

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = 1.3000 if postal == "120120" else (1.3500 if postal == "560123" else 1.2800)
            lng = 103.8000 if postal == "120120" else 103.8500
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
            start = str(params.get("start", ""))
            if "1.35" in start:
                # Leg 2 fails with timeout
                raise httpx.TimeoutException("Route timed out")
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
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["via_status"] == "partial"
    assert sample["direct_route"]["status"] == "available"

    comp = sample["comparison"]
    assert comp["status"] == "unavailable"
    assert comp["certification"] is None
    codes = [r["code"] for r in comp["reasons"]]
    assert "via_centre_incomplete" in codes
    assert "provider_timeout" in codes


def test_commute_no_workplace_configured_sets_not_applicable_comparison(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """When workplace is not configured on profile: direct_route and comparison are not_applicable."""
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
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "include_direct_baseline": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    sample = data["samples"][0]

    assert sample["direct_route"]["status"] == "not_applicable"
    assert sample["direct_route"]["reasons"][0]["code"] == "workplace_not_configured"
    assert sample["comparison"]["status"] == "not_applicable"
    assert sample["comparison"]["certification"] is None
    assert sample["comparison"]["reasons"][0]["code"] == "workplace_not_configured"



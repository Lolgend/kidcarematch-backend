"""Tests for bounded commute concurrency, aggregate deadlines, and telemetry (Ticket #92)."""

import os
import threading
import time
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
    COMMUTE_CALL_TIMEOUT_SECONDS,
    COMMUTE_EVALUATION_DEADLINE_SECONDS,
    COMMUTE_MAX_WORKERS,
    clear_commute_postal_cache,
)
from kidcarematch_backend.core.config import settings
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
            user_id="parent-conc-1",
            firebase_uid="firebase-parent-conc-1",
            email="parent_conc@example.com",
            full_name="Parent Concurrency",
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


def create_centre(test_engine, centre_id: int = 1, postal: str = "560123") -> PreschoolCentre:
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
    profile_id: str = "prof-conc-1",
    home_postal: str = "120120",
    workplace_postal: str | None = "048581",
) -> FamilyPreferenceProfile:
    with Session(test_engine) as session:
        p = FamilyPreferenceProfile(
            profile_id=profile_id,
            user_id=user_id,
            profile_name="Concurrency Profile",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code=home_postal,
            workplace_postal_code=workplace_postal,
            location_preference=LocationPreference.EITHER if workplace_postal else LocationPreference.HOME,
            household_income=6000.0,
            household_member_count=3,
            budget_max=1600.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
            vacancy_weight=3,
            cost_weight=3,
            travel_weight=3,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def test_commute_operational_constants_values():
    """Verify operational budget constants defined for bounded concurrency and deadlines."""
    assert COMMUTE_CALL_TIMEOUT_SECONDS == 6.0
    assert COMMUTE_EVALUATION_DEADLINE_SECONDS == 12.0
    assert COMMUTE_MAX_WORKERS == 4
    assert settings.COMMUTE_CALL_TIMEOUT_SECONDS == 6.0
    assert settings.COMMUTE_EVALUATION_DEADLINE_SECONDS == 12.0
    assert settings.COMMUTE_MAX_WORKERS == 4


def test_pt_mode_concurrent_dispatch_home_to_centre_and_direct(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """In PT mode with baseline, Leg 1 and Direct Leg are dispatched concurrently at 08:00 SGT."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=101, postal="560123")
    create_profile(test_engine, user_id=parent_user.user_id, profile_id="prof-pt-1")

    call_records: list[dict] = []
    lock = threading.Lock()
    active_threads: set[int] = set()
    max_active_threads = 0

    def mock_get(self, url, *args, **kwargs):
        nonlocal max_active_threads
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = (
                "1.300000"
                if postal == "120120"
                else ("1.350000" if postal == "560123" else "1.280000")
            )
            lng = (
                "103.800000"
                if postal == "120120"
                else ("103.850000" if postal == "560123" else "103.820000")
            )
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                        "X": "28000",
                        "Y": "30000",
                        "ADDRESS": f"{postal} Singapore",
                    }
                ],
            }
            return mock_resp

        if "route" in url_str:
            tid = threading.get_ident()
            t_start = time.perf_counter()
            with lock:
                active_threads.add(tid)
                max_active_threads = max(max_active_threads, len(active_threads))

            # Simulate network latency of 100ms
            time.sleep(0.1)

            t_end = time.perf_counter()
            with lock:
                active_threads.discard(tid)
                call_records.append(
                    {
                        "thread_id": tid,
                        "start": t_start,
                        "end": t_end,
                        "params": kwargs.get("params", {}),
                    }
                )

            mock_resp.json.return_value = {
                "status": 200,
                "plan": {
                    "itineraries": [
                        {
                            "duration": 600,
                            "startTime": 1773273600000,
                            "endTime": 1773274200000,
                            "legs": [
                                {
                                    "mode": "BUS",
                                    "duration": 600,
                                    "distance": 4000,
                                    "route": "100",
                                    "legGeometry": {"points": "sagGwhtxRsDkC"},
                                }
                            ],
                        }
                    ]
                },
            }
            return mock_resp

        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    payload = {
        "profile_id": "prof-pt-1",
        "mode": "pt",
        "include_direct_baseline": True,
        "sampling": {
            "policy": "weekday_0800_wed_thu_fri_v1",
            "cycle_id": None,
            "sample_index": 0,
        },
    }

    resp = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["comparison_contract"] == "direct_baseline_v1"
    assert data["samples"][0]["direct_route"]["status"] == "available"
    assert data["samples"][0]["via_status"] == "available"

    # There were 3 routing calls: Leg 1, Leg 2, and Direct Leg
    assert len(call_records) == 3

    # Direct Leg and Leg 1 were dispatched concurrently!
    # Check that at least 2 threads were concurrently executing
    assert max_active_threads >= 2
    assert max_active_threads <= COMMUTE_MAX_WORKERS


def test_non_pt_mode_concurrent_dispatch_all_three_legs(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """In drive/walk/cycle mode with baseline, Leg 1, Leg 2, and Direct Leg execute concurrently."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=102, postal="560123")
    create_profile(test_engine, user_id=parent_user.user_id, profile_id="prof-drive-1")

    call_records: list[dict] = []
    lock = threading.Lock()
    active_threads: set[int] = set()
    max_active_threads = 0

    def mock_get(self, url, *args, **kwargs):
        nonlocal max_active_threads
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = (
                "1.300000"
                if postal == "120120"
                else ("1.350000" if postal == "560123" else "1.280000")
            )
            lng = (
                "103.800000"
                if postal == "120120"
                else ("103.850000" if postal == "560123" else "103.820000")
            )
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                        "X": "28000",
                        "Y": "30000",
                        "ADDRESS": f"{postal} Singapore",
                    }
                ],
            }
            return mock_resp

        if "route" in url_str:
            tid = threading.get_ident()
            t_start = time.perf_counter()
            with lock:
                active_threads.add(tid)
                max_active_threads = max(max_active_threads, len(active_threads))

            time.sleep(0.1)

            t_end = time.perf_counter()
            with lock:
                active_threads.discard(tid)
                call_records.append(
                    {
                        "thread_id": tid,
                        "start": t_start,
                        "end": t_end,
                        "params": kwargs.get("params", {}),
                    }
                )

            mock_resp.json.return_value = {
                "route_summary": {
                    "total_time": 450,
                    "total_distance": 3200,
                },
                "route_geometry": "sagGwhtxRsDkC",
            }
            return mock_resp

        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    payload = {
        "profile_id": "prof-drive-1",
        "mode": "drive",
        "include_direct_baseline": True,
    }

    resp = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["samples"][0]["direct_route"]["status"] == "available"
    assert data["samples"][0]["via_status"] == "available"

    # All 3 legs executed
    assert len(call_records) == 3

    # All 3 ran concurrently: max active threads >= 2 (typically 3)
    assert max_active_threads >= 2
    assert max_active_threads <= COMMUTE_MAX_WORKERS

    # Total elapsed routing time is significantly faster than sequential 3 * 100ms = 300ms
    routing_duration = max(r["end"] for r in call_records) - min(r["start"] for r in call_records)
    assert routing_duration < 0.20


def test_individual_call_timeout_returns_provider_timeout_when_deadline_intact(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """When an individual OneMap call raises TimeoutException before deadline, returns provider_timeout."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=103, postal="560123")
    create_profile(test_engine, user_id=parent_user.user_id, profile_id="prof-timeout-1")

    observed_timeouts: list[float] = []

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            lat = (
                "1.300000"
                if postal == "120120"
                else ("1.350000" if postal == "560123" else "1.280000")
            )
            lng = (
                "103.800000"
                if postal == "120120"
                else ("103.850000" if postal == "560123" else "103.820000")
            )
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                        "X": "28000",
                        "Y": "30000",
                        "ADDRESS": f"{postal} Singapore",
                    }
                ],
            }
            return mock_resp

        if "route" in url_str:
            timeout_arg = kwargs.get("timeout")
            observed_timeouts.append(timeout_arg)
            # Raise TimeoutException while aggregate deadline is not exceeded
            raise httpx.ReadTimeout("Simulated OneMap read timeout")

        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    payload = {
        "profile_id": "prof-timeout-1",
        "mode": "drive",
        "include_direct_baseline": True,
    }

    resp = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # Verify call timeout requested was COMMUTE_CALL_TIMEOUT_SECONDS (6.0)
    assert observed_timeouts[0] == COMMUTE_CALL_TIMEOUT_SECONDS

    sample = data["samples"][0]
    assert sample["direct_route"]["status"] == "unavailable"
    assert sample["direct_route"]["reasons"][0]["code"] == "provider_timeout"
    assert sample["direct_route"]["reasons"][0]["failure_class"] == "service"
    assert sample["direct_route"]["reasons"][0]["retryable"] is True
    assert sample["comparison"]["status"] == "unavailable"


def test_aggregate_deadline_preserves_completed_direct_leg_when_via_exceeds(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """When aggregate deadline expires while via leg hangs, Direct Leg is preserved intact."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=104, postal="560123")
    create_profile(test_engine, user_id=parent_user.user_id, profile_id="prof-dead-1")

    # Set deadline budget to 0.25 seconds for fast test
    monkeypatch.setattr(settings, "COMMUTE_EVALUATION_DEADLINE_SECONDS", 0.25)

    # Distinguish coordinates by postal code
    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "120120":
                lat, lng = "1.300000", "103.800000"
            elif postal == "560123":
                lat, lng = "1.350000", "103.850000"
            else:
                lat, lng = "1.280000", "103.840000"  # workplace 048581

            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                        "X": "28000",
                        "Y": "30000",
                        "ADDRESS": f"{postal} Singapore",
                    }
                ],
            }
            return mock_resp

        if "route" in url_str:
            params = kwargs.get("params", {})
            end = params.get("end")

            # Direct leg ends at workplace (1.28, 103.84)
            if "1.28" in str(end):
                # Quick response for direct leg
                time.sleep(0.02)
                mock_resp.json.return_value = {
                    "route_summary": {
                        "total_time": 500,
                        "total_distance": 4000,
                    },
                    "route_geometry": "sagGwhtxRsDkC",
                }
                return mock_resp
            else:
                # Leg 1 (home -> centre) hangs
                time.sleep(1.5)
                mock_resp.json.return_value = {
                    "route_summary": {
                        "total_time": 800,
                        "total_distance": 6000,
                    },
                    "route_geometry": "sagGwhtxRsDkC",
                }
                return mock_resp

        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    payload = {
        "profile_id": "prof-dead-1",
        "mode": "drive",
        "include_direct_baseline": True,
    }

    t0 = time.perf_counter()
    resp = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    data = resp.json()
    sample = data["samples"][0]

    # Direct Route completed and is PRESERVED!
    assert sample["direct_route"]["status"] == "available"
    assert sample["direct_route"]["totals"]["duration_seconds"] == 500
    assert sample["direct_route"]["totals"]["distance_meters"] == 4000
    assert len(sample["direct_route"]["legs"]) == 1
    assert sample["direct_route"]["legs"][0]["status"] == "available"

    # Via leg timed out on aggregate deadline!
    assert sample["via_status"] in ("unavailable", "partial")
    via_reasons = [r["code"] for r in sample["reasons"]]
    assert "evaluation_budget_exhausted" in via_reasons

    # Comparison is unavailable due to incomplete via centre and evaluation budget exhausted
    assert sample["comparison"]["status"] == "unavailable"
    comp_codes = [r["code"] for r in sample["comparison"]["reasons"]]
    assert "via_centre_incomplete" in comp_codes
    assert "evaluation_budget_exhausted" in comp_codes

    # Ensure executor shutdown with wait=False did not block for 1.5s
    assert elapsed < 0.90


def test_aggregate_deadline_preserves_completed_via_legs_when_direct_exceeds(
    client: TestClient, test_engine, parent_user, monkeypatch
):
    """When aggregate deadline expires while Direct Leg hangs, completed Via legs are preserved."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=105, postal="560123")
    create_profile(test_engine, user_id=parent_user.user_id, profile_id="prof-dead-2")

    monkeypatch.setattr(settings, "COMMUTE_EVALUATION_DEADLINE_SECONDS", 0.25)

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", ""))
            if postal == "120120":
                lat, lng = "1.300000", "103.800000"
            elif postal == "560123":
                lat, lng = "1.350000", "103.850000"
            else:
                lat, lng = "1.280000", "103.840000"  # workplace

            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                        "X": "28000",
                        "Y": "30000",
                        "ADDRESS": f"{postal} Singapore",
                    }
                ],
            }
            return mock_resp

        if "route" in url_str:
            params = kwargs.get("params", {})
            end = params.get("end")

            # Direct leg ends at workplace (1.28) and starts at home (1.30, not centre 1.35)
            start = params.get("start")
            if "1.28" in str(end) and "1.35" not in str(start):
                # Direct leg hangs
                time.sleep(1.5)
                mock_resp.json.return_value = {
                    "route_summary": {
                        "total_time": 500,
                        "total_distance": 4000,
                    },
                    "route_geometry": "sagGwhtxRsDkC",
                }
                return mock_resp
            else:
                # Via legs (home -> centre, and centre -> workplace) complete quickly (0.02s)
                time.sleep(0.02)
                mock_resp.json.return_value = {
                    "route_summary": {
                        "total_time": 300,
                        "total_distance": 2500,
                    },
                    "route_geometry": "sagGwhtxRsDkC",
                }
                return mock_resp

        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    payload = {
        "profile_id": "prof-dead-2",
        "mode": "drive",
        "include_direct_baseline": True,
    }

    t0 = time.perf_counter()
    resp = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    data = resp.json()
    sample = data["samples"][0]

    # Via legs are PRESERVED and available!
    assert sample["via_status"] == "available"
    assert sample["totals"]["duration_seconds"] == 600  # 300 + 300
    assert sample["totals"]["distance_meters"] == 5000  # 2500 + 2500
    assert len(sample["legs"]) == 2
    assert all(l["status"] == "available" for l in sample["legs"])

    # Direct Route timed out on aggregate deadline!
    assert sample["direct_route"]["status"] == "unavailable"
    assert sample["direct_route"]["totals"] is None
    direct_codes = [r["code"] for r in sample["direct_route"]["reasons"]]
    assert "evaluation_budget_exhausted" in direct_codes

    # Comparison is unavailable with direct_unavailable and evaluation_budget_exhausted
    assert sample["comparison"]["status"] == "unavailable"
    comp_codes = [r["code"] for r in sample["comparison"]["reasons"]]
    assert "direct_unavailable" in comp_codes
    assert "evaluation_budget_exhausted" in comp_codes

    # Non-blocking executor shutdown did not wait for 1.5s
    assert elapsed < 0.90


def test_adr_0010_exists_and_covers_required_architectural_decisions():
    """Verify ADR-0010 exists and documents capabilities, tokens, timing certification, guardrails."""
    adr_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "docs",
        "adr",
        "0010-direct-commute-baseline-and-weekday-sampling.md",
    )
    assert os.path.isfile(adr_path), f"ADR-0010 not found at {adr_path}"

    with open(adr_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Required topics
    assert "direct_baseline_v1" in content
    assert "weekday_0800_wed_thu_fri_v1" in content
    assert "HMAC-SHA256" in content
    assert "primary Wednesday 08:00 SGT" in content
    assert "timing_unverified" in content
    assert "ThreadPoolExecutor" in content
    assert "COMMUTE_CALL_TIMEOUT_SECONDS" in content or "6.0s" in content or "6.0" in content
    assert "COMMUTE_EVALUATION_DEADLINE_SECONDS" in content or "12.0s" in content or "12.0" in content
    assert "COMMUTE_MAX_WORKERS" in content or "4-worker" in content or "4" in content
    assert "ADR-0009" in content
    assert "ADR-0007" in content


def test_commute_capabilities_endpoint_release_gate_open_by_default(
    client: TestClient, parent_user: User
):
    """Release operational hold open: GET /api/v1/commute/capabilities returns both capabilities by default."""
    set_auth(parent_user)
    response = client.get("/api/v1/commute/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "direct_baseline_contract": "direct_baseline_v1",
        "sampling_contract": "weekday_0800_wed_thu_fri_v1",
    }

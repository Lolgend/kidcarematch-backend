"""API integration tests for OneMap route caching, in-flight coalescing, and verified postal cache warming (Ticket #101)."""

import concurrent.futures
import threading
import time
from collections.abc import Generator
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
)
from kidcarematch_backend.core.onemap import clear_onemap_caches, onemap_token_manager
from kidcarematch_backend.db.db import (
    AccountStatus,
    CitizenshipType,
    FamilyPreferenceProfile,
    LocationPreference,
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
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(test_engine) -> Generator[TestClient, None, None]:
    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def parent_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="parent-cache-1",
            firebase_uid="firebase-parent-cache-1",
            email="parent_cache@example.com",
            full_name="Parent Cache",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def set_auth(user: User) -> None:
    claims = FirebaseTokenClaims(uid=user.firebase_uid, email=user.email)
    app.dependency_overrides[get_token_claims] = lambda: claims
    app.dependency_overrides[get_optional_token_claims] = lambda: claims


def seed_centres_and_vacancies(test_engine) -> list[PreschoolCentre]:
    """Seed 6 centres (C001-C006) with infant vacancies and SC fees."""
    centres = []
    with Session(test_engine) as session:
        target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
        postals = ["560123", "560124", "560125", "560126", "560127", "560128"]
        for i, postal in enumerate(postals, start=1):
            c = PreschoolCentre(
                centre_id=i,
                centre_code=f"C{i:03d}",
                centre_name=f"Preschool {i}",
                postal=postal,
                address=f"{postal} Singapore",
                spark_certified=True,
                gov_subsidy=True,
            )
            centres.append(c)
            session.add(c)
            s = PreschoolService(
                centre_id=i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=1000.0 + (i * 50.0),
                citizenship_type=CitizenshipType.SC,
            )
            session.add(s)
            v = VacancyRecord(
                record_id=f"VR{i:03d}",
                centre_id=i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Available",
            )
            session.add(v)
        session.commit()
        for c in centres:
            session.refresh(c)
    return centres


def create_profile(
    test_engine,
    user_id: str,
    profile_id: str = "prof-cache-1",
    home_postal: str = "120120",
    workplace_postal: str | None = "048581",
) -> FamilyPreferenceProfile:
    with Session(test_engine) as session:
        p = FamilyPreferenceProfile(
            profile_id=profile_id,
            user_id=user_id,
            profile_name=f"Profile {profile_id}",
            level=PreschoolLevel.INFANT,
            citizenship_status=CitizenshipType.SC,
            service_required="Full Day",
            home_postal_code=home_postal,
            workplace_postal_code=workplace_postal,
            location_preference=(
                LocationPreference.EITHER if workplace_postal else LocationPreference.HOME
            ),
            household_income=6000.0,
            household_member_count=3,
            budget_max=1600.0,
            enrollment_month_year=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


def setup_tracking_onemap_mock(
    monkeypatch: pytest.MonkeyPatch,
    coords_table: dict[str, tuple[float, float]] | None = None,
    failing_routes_flag: list[bool] | None = None,
    empty_itineraries_flag: list[bool] | None = None,
):
    """Setup OneMap mock recording search and route requests with flag controls."""
    onemap_token_manager.invalidate_token()

    default_coords = {
        "560123": (1.3691, 103.8512),
        "560124": (1.3695, 103.8520),
        "560125": (1.3780, 103.8550),
        "560126": (1.3800, 103.8560),
        "560127": (1.3820, 103.8570),
        "560128": (1.3840, 103.8580),
        "048581": (1.2840, 103.8510),
        "120120": (1.3100, 103.7600),
    }
    coords = {**default_coords, **(coords_table or {})}

    searches: list[str] = []
    routes: list[tuple[str, str, str]] = []  # (start, end, mode_or_type)
    lock = threading.Lock()

    original_get = httpx.Client.get
    original_post = httpx.Client.post

    def mock_post(self, url, *args, **kwargs):
        if "onemap.gov.sg/api/auth/post/getToken" in str(url):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "mock-token-jwt",
                "expiry_timestamp": time.time() + 86400,
            }
            resp.raise_for_status = MagicMock()
            return resp
        return original_post(self, url, *args, **kwargs)

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        params = kwargs.get("params", {})

        if "onemap.gov.sg/api/common/elastic/search" in url_str:
            postal = str(params.get("searchVal", "")).strip()
            with lock:
                searches.append(postal)

            if postal in coords:
                lat, lon = coords[postal]
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "found": 1,
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
                resp.raise_for_status = MagicMock()
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"found": 0, "results": []}
            resp.raise_for_status = MagicMock()
            return resp

        if "onemap.gov.sg/api/public/routingsvc/route" in url_str:
            start_str = str(params.get("start", ""))
            end_str = str(params.get("end", ""))
            route_type = str(params.get("routeType", ""))
            with lock:
                routes.append((start_str, end_str, route_type))

            if failing_routes_flag is not None and failing_routes_flag[0]:
                raise httpx.ConnectTimeout("OneMap connection timed out")

            if empty_itineraries_flag is not None and empty_itineraries_flag[0]:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"plan": {"itineraries": []}}
                resp.raise_for_status = MagicMock()
                return resp

            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": 1500,  # 25 mins
                            "legs": [
                                {
                                    "mode": "BUS",
                                    "route": "166",
                                    "duration": 1500,
                                    "distance": 4000,
                                    "legGeometry": {"points": "_{feAmwvy@o_@_t@"},
                                    "instruction": "Take Bus 166",
                                }
                            ],
                        }
                    ]
                }
            }
            resp.raise_for_status = MagicMock()
            return resp

        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    monkeypatch.setattr(httpx.Client, "post", mock_post)

    return searches, routes


# ==============================================================================
# 1. Repeat POST /api/v1/centres/match reuses cached route durations (0 new calls)
# ==============================================================================


def test_repeat_match_runs_reuse_cached_onemap_routes(
    client: TestClient, test_engine, monkeypatch: pytest.MonkeyPatch
):
    """Adjusting Importance Ratings reuses cached routes; 0 duplicate route calls on warm cache."""
    clear_onemap_caches()
    clear_commute_caches()
    seed_centres_and_vacancies(test_engine)
    _searches, routes = setup_tracking_onemap_mock(monkeypatch)

    payload_run1 = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "weights": {"vacancy_weight": 5, "cost_weight": 3, "commute_weight": 2},
    }

    resp1 = client.post("/api/v1/centres/match", json=payload_run1)
    assert resp1.status_code == 200
    initial_routes_count = len(routes)
    assert initial_routes_count > 0

    # Repeat matching run with adjusted importance rating weights for same home/workplace postals
    payload_run2 = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "weights": {"vacancy_weight": 1, "cost_weight": 5, "commute_weight": 4},
    }

    resp2 = client.post("/api/v1/centres/match", json=payload_run2)
    assert resp2.status_code == 200
    # Exactly ZERO additional OneMap routing calls on the second match run!
    assert len(routes) == initial_routes_count


# ==============================================================================
# 2. Degraded / failing routing calls in /match fall back without poisoning cache
# ==============================================================================


def test_match_routing_failure_falls_back_without_poisoning_cache(
    client: TestClient, test_engine, monkeypatch: pytest.MonkeyPatch
):
    """Degraded OneMap route calls fall back to Haversine; subsequent call retries OneMap."""
    clear_onemap_caches()
    clear_commute_caches()
    seed_centres_and_vacancies(test_engine)

    fail_routes = [True]
    _searches, routes = setup_tracking_onemap_mock(
        monkeypatch, failing_routes_flag=fail_routes
    )

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
    }

    # First run: routing fails, engine falls back to straight-line travel fit
    resp1 = client.post("/api/v1/centres/match", json=payload)
    assert resp1.status_code == 200
    assert resp1.json()["total_candidates"] == 6
    assert len(routes) > 0
    first_attempts = len(routes)

    # Second run: OneMap route service recovers
    fail_routes[0] = False
    resp2 = client.post("/api/v1/centres/match", json=payload)
    assert resp2.status_code == 200
    # Must have retried OneMap (routes increased) rather than returning stale unpoisoned cache!
    assert len(routes) > first_attempts


# ==============================================================================
# 3. 6 concurrent /commute requests coalesce postal lookups and direct baseline
# ==============================================================================


def test_concurrent_commute_requests_coalesce_postals_and_direct_baseline(
    client: TestClient, test_engine, parent_user: User, monkeypatch: pytest.MonkeyPatch
):
    """6 concurrent /commute requests coalesce home/workplace postal lookups and direct baseline route."""
    clear_onemap_caches()
    clear_commute_caches()
    centres = seed_centres_and_vacancies(test_engine)
    set_auth(parent_user)

    assert parent_user.user_id is not None
    profile = create_profile(
        test_engine,
        user_id=parent_user.user_id,
        profile_id="prof-cache-1",
        home_postal="120120",
        workplace_postal="048581",
    )

    searches, routes = setup_tracking_onemap_mock(monkeypatch)

    barrier = threading.Barrier(6)
    results = []

    def fetch_commute_card(centre_id: int):
        barrier.wait()
        payload = {
            "profile_id": profile.profile_id,
            "mode": "pt",
            "include_direct_baseline": True,
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        }
        with TestClient(app) as thread_client:
            res = thread_client.post(f"/api/v1/centres/{centre_id}/commute", json=payload)
        results.append(res)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(fetch_commute_card, int(centres[i].centre_id))  # type: ignore[arg-type]
            for i in range(6)
        ]
        concurrent.futures.wait(futures)

    assert len(results) == 6
    assert all(r.status_code == 200 for r in results)

    # 1. Coalescing: home postal (120120) and workplace postal (048581) queried exactly ONCE each!
    home_searches = [s for s in searches if s == "120120"]
    workplace_searches = [s for s in searches if s == "048581"]
    assert len(home_searches) == 1
    assert len(workplace_searches) == 1

    # 2. Coalescing: direct baseline home -> workplace route leg evaluated exactly ONCE across all 6 requests!
    # Home coord is 1.3100, 103.7600; Workplace coord is 1.2840, 103.8510
    direct_baseline_routes = [
        r for r in routes if "1.31" in r[0] and "1.284" in r[1]
    ]
    assert len(direct_baseline_routes) == 1


# ==============================================================================
# 4. Failed / unavailable /commute legs are not cached and recover immediately
# ==============================================================================


def test_commute_leg_unavailable_not_cached_and_recovers_immediately(
    client: TestClient, test_engine, parent_user: User, monkeypatch: pytest.MonkeyPatch
):
    """Unavailable commute legs are never cached and recover immediately when OneMap succeeds."""
    clear_onemap_caches()
    clear_commute_caches()
    centres = seed_centres_and_vacancies(test_engine)
    set_auth(parent_user)

    assert parent_user.user_id is not None
    profile = create_profile(
        test_engine,
        user_id=parent_user.user_id,
        profile_id="prof-cache-2",
        home_postal="120120",
        workplace_postal="048581",
    )

    empty_itins = [True]
    _searches, routes = setup_tracking_onemap_mock(
        monkeypatch, empty_itineraries_flag=empty_itins
    )

    payload = {
        "profile_id": profile.profile_id,
        "mode": "pt",
        "include_direct_baseline": True,
        "sampling": {
            "policy": "weekday_0800_wed_thu_fri_v1",
            "cycle_id": None,
            "sample_index": 0,
        },
    }

    # First attempt: leg returns unavailable
    res1 = client.post(f"/api/v1/centres/{centres[0].centre_id}/commute", json=payload)
    assert res1.status_code == 200
    assert res1.json()["samples"][0]["via_status"] == "unavailable"
    assert len(routes) > 0
    first_attempts = len(routes)

    # Second attempt: must NOT be cached! Must retry OneMap and succeed
    empty_itins[0] = False
    res2 = client.post(f"/api/v1/centres/{centres[0].centre_id}/commute", json=payload)
    assert res2.status_code == 200
    assert res2.json()["samples"][0]["via_status"] == "available"
    assert len(routes) > first_attempts


# ==============================================================================
# 5. POST /match warms verified postal cache; /commute & /location skip search
# ==============================================================================


def test_match_warms_verified_postal_cache_for_commute_and_location(
    client: TestClient, test_engine, parent_user: User, monkeypatch: pytest.MonkeyPatch
):
    """POST /match warms verified postal cache so subsequent /commute and /location skip OneMap Search."""
    clear_onemap_caches()
    clear_commute_caches()
    centres = seed_centres_and_vacancies(test_engine)
    set_auth(parent_user)

    assert parent_user.user_id is not None
    profile = create_profile(
        test_engine,
        user_id=parent_user.user_id,
        profile_id="prof-cache-3",
        home_postal="560123",
        workplace_postal="048581",
    )

    searches, _ = setup_tracking_onemap_mock(monkeypatch)

    match_payload = {
        "profile_id": profile.profile_id,
        "level": "infant",
        "enrollment_month_year": "2026-06",
    }

    # Step 1: Run matching
    match_resp = client.post("/api/v1/centres/match", json=match_payload)
    assert match_resp.status_code == 200
    searches_after_match = len(searches)
    assert searches_after_match > 0

    # Step 2: Call /location for one of the matched centres (C001, postal 560123)
    loc_resp = client.get(f"/api/v1/centres/{centres[0].centre_id}/location")
    assert loc_resp.status_code == 200
    assert loc_resp.json()["status"] == "available"

    # Search calls must NOT have increased!
    assert len(searches) == searches_after_match

    # Step 3: Call /commute for C001
    commute_payload = {
        "profile_id": profile.profile_id,
        "mode": "pt",
        "include_direct_baseline": True,
    }
    commute_resp = client.post(
        f"/api/v1/centres/{centres[0].centre_id}/commute", json=commute_payload
    )
    assert commute_resp.status_code == 200

    # Search calls must STILL NOT have increased!
    assert len(searches) == searches_after_match

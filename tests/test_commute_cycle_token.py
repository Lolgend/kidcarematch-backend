"""Tests for stateless cycle token lifecycle and selected weekday sampling (Ticket #89, Spec #87)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
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
    clear_commute_postal_cache,
    create_cycle_token,
    decode_cycle_token,
    get_primary_wednesday,
    validate_cycle_token,
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
            vacancy_weight=0.4,
            cost_weight=0.3,
            travel_weight=0.3,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


MOCK_PT_FIXTURE = {
    "plan": {
        "date": 1762300800000,
        "itineraries": [
            {
                "duration": 1500,
                "endTime": 1762302300000,
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


def setup_mock_onemap(monkeypatch, routing_calls: list[dict]):
    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        if "elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = params.get("searchVal", "")
            lat = "1.3000" if postal == "120120" else ("1.3500" if postal == "560123" else "1.2800")
            lng = "103.8000" if postal == "120120" else ("103.8500" if postal == "560123" else "103.8200")
            mock_resp.json.return_value = {
                "found": 1,
                "total_pages": 1,
                "results": [
                    {
                        "SEARCHVAL": f"BLOCK {postal}",
                        "POSTAL": postal,
                        "LATITUDE": lat,
                        "LONGITUDE": lng,
                    }
                ],
            }
            return mock_resp

        if "routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            routing_calls.append(params)
            date_str = params.get("date")
            time_str = params.get("time")
            if date_str and time_str:
                dep_dt = datetime.strptime(
                    f"{date_str} {time_str}", "%m-%d-%Y %H:%M:%S"
                ).replace(tzinfo=SGT)
                end_ms = int((dep_dt.timestamp() + 1500) * 1000)
                mock_resp.json.return_value = {
                    "plan": {
                        "date": int(dep_dt.timestamp() * 1000),
                        "itineraries": [
                            {
                                "duration": 1500,
                                "endTime": end_ms,
                                "legs": MOCK_PT_FIXTURE["plan"]["itineraries"][0]["legs"],
                            }
                        ],
                    }
                }
            else:
                mock_resp.json.return_value = MOCK_PT_FIXTURE
            return mock_resp

        raise RuntimeError(f"Unexpected URL: {url_str}")

    monkeypatch.setattr(httpx.Client, "get", mock_get)


def test_primary_wednesday_tuesday_night() -> None:
    """Tuesday 23:59:59 SGT maps to the next upcoming Wednesday at 08:00:00 SGT."""
    # 2026-11-10 is Tuesday
    tuesday_night = datetime(2026, 11, 10, 23, 59, 59, tzinfo=SGT)
    expected_wed = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    assert get_primary_wednesday(tuesday_night) == expected_wed


def test_primary_wednesday_wednesday_before_0800() -> None:
    """Wednesday strictly before 08:00:00 SGT maps to the same day at 08:00:00 SGT."""
    # 2026-11-11 is Wednesday
    wed_morning = datetime(2026, 11, 11, 7, 59, 59, tzinfo=SGT)
    expected_wed = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    assert get_primary_wednesday(wed_morning) == expected_wed


def test_primary_wednesday_wednesday_at_0800_rolls_over() -> None:
    """Wednesday at exactly 08:00:00 SGT rolls to next week's Wednesday at 08:00:00 SGT."""
    wed_at_eight = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    expected_next_wed = datetime(2026, 11, 18, 8, 0, 0, tzinfo=SGT)
    assert get_primary_wednesday(wed_at_eight) == expected_next_wed


def test_primary_wednesday_wednesday_after_0800() -> None:
    """Wednesday after 08:00:00 SGT rolls to next week's Wednesday at 08:00:00 SGT."""
    wed_after_eight = datetime(2026, 11, 11, 8, 0, 1, tzinfo=SGT)
    expected_next_wed = datetime(2026, 11, 18, 8, 0, 0, tzinfo=SGT)
    assert get_primary_wednesday(wed_after_eight) == expected_next_wed


def test_primary_wednesday_thursday() -> None:
    """Thursday 14:00:00 SGT rolls to next week's Wednesday at 08:00:00 SGT."""
    # 2026-11-12 is Thursday
    thursday = datetime(2026, 11, 12, 14, 0, 0, tzinfo=SGT)
    expected_wed = datetime(2026, 11, 18, 8, 0, 0, tzinfo=SGT)
    assert get_primary_wednesday(thursday) == expected_wed


def test_cycle_token_creation_and_decoding() -> None:
    """A signed cycle token encodes policy, primary_wed, cycle_expires_at, and verifies HMAC."""
    primary_wed = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    cycle_expires_at = primary_wed  # Strictly expires at primary Wednesday 08:00 SGT

    token = create_cycle_token(primary_wed=primary_wed, cycle_expires_at=cycle_expires_at)
    assert isinstance(token, str)
    assert len(token) > 20

    payload = decode_cycle_token(token)
    assert payload["policy"] == "weekday_0800_wed_thu_fri_v1"
    assert payload["primary_wed_iso"] == primary_wed.isoformat()
    assert payload["cycle_expires_at_iso"] == cycle_expires_at.isoformat()
    assert "salt" in payload


def test_cycle_token_tampering_rejected() -> None:
    """Tampered token fails HMAC verification with 422 Unprocessable Entity."""
    primary_wed = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    token = create_cycle_token(primary_wed=primary_wed, cycle_expires_at=primary_wed)

    # Corrupt the token characters
    tampered_token = token[:-4] + "ABCD"
    with pytest.raises(HTTPException) as exc_info:
        decode_cycle_token(tampered_token)
    assert exc_info.value.status_code == 422


def test_cycle_token_malformed_rejected() -> None:
    """Malformed non-base64 or invalid JSON string raises 422 Unprocessable Entity."""
    with pytest.raises(HTTPException) as exc_info:
        decode_cycle_token("this-is-not-a-valid-token!!!")
    assert exc_info.value.status_code == 422


def test_validate_cycle_token_new_cycle_issuance() -> None:
    """cycle_id=None and sample_index=0 issues a new active cycle token."""
    server_now = datetime(2026, 11, 10, 10, 0, 0, tzinfo=SGT)
    res = validate_cycle_token(
        cycle_id=None,
        sample_index=0,
        policy="weekday_0800_wed_thu_fri_v1",
        now=server_now,
    )
    assert res.selected_index == 0
    assert res.primary_wed == datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    assert res.cycle_expires_at == datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    assert len(res.allowed_samples) == 3
    assert res.allowed_samples[0].sample_index == 0
    assert res.allowed_samples[0].departure_at == datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    assert res.allowed_samples[1].sample_index == 1
    assert res.allowed_samples[1].departure_at == datetime(2026, 11, 12, 8, 0, 0, tzinfo=SGT)
    assert res.allowed_samples[2].sample_index == 2
    assert res.allowed_samples[2].departure_at == datetime(2026, 11, 13, 8, 0, 0, tzinfo=SGT)
    assert isinstance(res.cycle_id, str)
    assert res.selected_departure_at == datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)


def test_validate_cycle_token_reuse_index_1_and_2() -> None:
    """Reusing active cycle_id with sample_index=1 (Thu) or 2 (Fri) succeeds and retains cycle."""
    server_now = datetime(2026, 11, 10, 10, 0, 0, tzinfo=SGT)
    # Issue initial cycle
    initial = validate_cycle_token(
        cycle_id=None,
        sample_index=0,
        policy="weekday_0800_wed_thu_fri_v1",
        now=server_now,
    )

    # Reuse with index 1 (Thursday)
    thu_res = validate_cycle_token(
        cycle_id=initial.cycle_id,
        sample_index=1,
        policy="weekday_0800_wed_thu_fri_v1",
        now=server_now + timedelta(hours=1),
    )
    assert thu_res.selected_index == 1
    assert thu_res.cycle_id == initial.cycle_id
    assert thu_res.selected_departure_at == datetime(2026, 11, 12, 8, 0, 0, tzinfo=SGT)

    # Reuse with index 2 (Friday)
    fri_res = validate_cycle_token(
        cycle_id=initial.cycle_id,
        sample_index=2,
        policy="weekday_0800_wed_thu_fri_v1",
        now=server_now + timedelta(hours=2),
    )
    assert fri_res.selected_index == 2
    assert fri_res.cycle_id == initial.cycle_id
    assert fri_res.selected_departure_at == datetime(2026, 11, 13, 8, 0, 0, tzinfo=SGT)


def test_validate_cycle_token_null_cycle_with_nonzero_index_rejected() -> None:
    """cycle_id=None with sample_index=1 or 2 raises 422 Unprocessable Entity."""
    server_now = datetime(2026, 11, 10, 10, 0, 0, tzinfo=SGT)
    with pytest.raises(HTTPException) as exc_info:
        validate_cycle_token(
            cycle_id=None,
            sample_index=1,
            policy="weekday_0800_wed_thu_fri_v1",
            now=server_now,
        )
    assert exc_info.value.status_code == 422

    with pytest.raises(HTTPException) as exc_info:
        validate_cycle_token(
            cycle_id=None,
            sample_index=2,
            policy="weekday_0800_wed_thu_fri_v1",
            now=server_now,
        )
    assert exc_info.value.status_code == 422


def test_validate_cycle_token_expired_cycle_returns_409() -> None:
    """When server_now >= cycle_expires_at, returns 409 Conflict with sample_expired."""
    server_now = datetime(2026, 11, 10, 10, 0, 0, tzinfo=SGT)
    initial = validate_cycle_token(
        cycle_id=None,
        sample_index=0,
        policy="weekday_0800_wed_thu_fri_v1",
        now=server_now,
    )
    # Expiry is exactly primary Wednesday 08:00:00 SGT (2026-11-11 08:00:00 SGT)
    expired_now = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    with pytest.raises(HTTPException) as exc_info:
        validate_cycle_token(
            cycle_id=initial.cycle_id,
            sample_index=1,
            policy="weekday_0800_wed_thu_fri_v1",
            now=expired_now,
        )
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "sample_expired"


def test_api_commute_sampling_new_cycle_issuance(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """POST /centres/{centre_id}/commute with new sampling attempt (cycle_id=null, sample_index=0)."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    routing_calls: list[dict] = []
    setup_mock_onemap(monkeypatch, routing_calls)

    payload = {
        "profile_id": profile.profile_id,
        "mode": "pt",
        "sampling": {
            "policy": "weekday_0800_wed_thu_fri_v1",
            "cycle_id": None,
            "sample_index": 0,
        },
    }

    res = client.post(f"/api/v1/centres/{centre.centre_id}/commute", json=payload)
    assert res.status_code == 200
    data = res.json()

    # Invariants for NegotiatedCommuteResponse
    assert data["sample_pair"] is None
    assert data["sampling_contract"] == "weekday_0800_wed_thu_fri_v1"
    assert data["comparison_contract"] is None
    assert data["mode"] == "pt"
    assert data["centre_id"] == 1

    # Sampling metadata
    sampling = data["sampling"]
    assert sampling is not None
    assert sampling["policy"] == "weekday_0800_wed_thu_fri_v1"
    assert isinstance(sampling["cycle_id"], str)
    assert len(sampling["cycle_id"]) > 20
    assert sampling["selected_index"] == 0
    assert len(sampling["allowed_samples"]) == 3
    assert sampling["allowed_samples"][0]["sample_index"] == 0
    assert sampling["allowed_samples"][1]["sample_index"] == 1
    assert sampling["allowed_samples"][2]["sample_index"] == 2

    # Evaluation metadata
    evaluation = data["evaluation"]
    assert evaluation is not None
    assert evaluation["evaluation_id"].startswith("eval_")
    assert len(evaluation["profile_revision"]) == 64  # SHA-256 hex
    assert len(evaluation["anchor_revision"]) == 64  # SHA-256 hex
    assert "120120" not in evaluation["profile_revision"]  # No plaintext PII

    # Exactly 1 sample evaluated (NO Saturday query!)
    assert len(data["samples"]) == 1
    assert len(routing_calls) == 1  # Only 1 provider route call
    sample = data["samples"][0]
    assert sample["kind"] == "weekday"
    assert sample["sampled_at"] == sampling["allowed_samples"][0]["departure_at"]
    assert sample["via_status"] == "available"
    assert sample["reasons"] == []
    assert sample["conditions"]["timing_basis"] == "scheduled_departure"
    assert sample["conditions"]["conditions_id"] == sampling["allowed_samples"][0]["conditions_id"]

    # Leg verification
    leg = sample["legs"][0]
    assert leg["status"] == "available"
    assert leg["from_anchor"] == "home"
    assert leg["to_anchor"] == "centre"
    assert leg["departure_at"] == sample["sampled_at"]
    assert leg["timing"]["duration_basis"] == "elapsed_from_requested_departure"
    assert leg["timing"]["arrival_source"] == "provider_end_time"
    assert leg["timing"]["initial_wait_included"] is True
    assert leg["reasons"] == []


def test_api_commute_sampling_cycle_reuse_indices_1_and_2(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Reusing issued cycle_id with sample_index=1 (Thu) and 2 (Fri) succeeds and retains cycle."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    routing_calls: list[dict] = []
    setup_mock_onemap(monkeypatch, routing_calls)

    # Step 1: Initial cycle request (index 0)
    init_res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert init_res.status_code == 200
    cycle_id = init_res.json()["sampling"]["cycle_id"]
    allowed = init_res.json()["sampling"]["allowed_samples"]

    # Step 2: Request Thursday (index 1) with cycle_id
    thu_res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": cycle_id,
                "sample_index": 1,
            },
        },
    )
    assert thu_res.status_code == 200
    thu_data = thu_res.json()
    assert thu_data["sampling"]["cycle_id"] == cycle_id
    assert thu_data["sampling"]["selected_index"] == 1
    assert thu_data["samples"][0]["sampled_at"] == allowed[1]["departure_at"]

    # Step 3: Request Friday (index 2) with cycle_id
    fri_res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": cycle_id,
                "sample_index": 2,
            },
        },
    )
    assert fri_res.status_code == 200
    fri_data = fri_res.json()
    assert fri_data["sampling"]["cycle_id"] == cycle_id
    assert fri_data["sampling"]["selected_index"] == 2
    assert fri_data["samples"][0]["sampled_at"] == allowed[2]["departure_at"]


def test_api_commute_sampling_null_cycle_with_index_1_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Sending cycle_id=None with sample_index=1 returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 1,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_tampered_token_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Sending tampered or malformed cycle_id returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": "tampered_cycle_token_xyz",
                "sample_index": 1,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_unrecognized_policy_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Sending unrecognized sampling policy returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "unsupported_v2",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_invalid_sample_index_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Sending sample_index not in {0, 1, 2} returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 5,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_mutual_exclusion_with_sample_pair_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Supplying both sampling and sample_pair returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sample_pair": [
                "2026-11-11T08:00:00+08:00",
                "2026-11-14T08:00:00+08:00",
            ],
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_non_pt_mode_returns_422(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Supplying sampling with mode='drive' returns 422 Unprocessable Entity."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "drive",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert res.status_code == 422


def test_api_commute_sampling_expired_cycle_returns_409(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """When a cycle token has expired, returns 409 Conflict with sample_expired."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine, parent_user.user_id, home_postal="120120"
    )

    setup_mock_onemap(monkeypatch, [])

    # Create token expired in the past
    past_wed = datetime(2026, 11, 4, 8, 0, 0, tzinfo=SGT)
    expired_token = create_cycle_token(
        primary_wed=past_wed, cycle_expires_at=past_wed
    )

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": expired_token,
                "sample_index": 1,
            },
        },
    )
    assert res.status_code == 409
    data = res.json()
    detail = data["detail"]
    assert isinstance(detail, dict)
    assert detail.get("code") == "sample_expired"


def test_api_commute_sampling_with_workplace_evaluates_onward_leg(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """When workplace is configured, negotiated sampling evaluates both home->centre and centre->workplace."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="018989",
    )

    routing_calls: list[dict] = []
    setup_mock_onemap(monkeypatch, routing_calls)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert res.status_code == 200
    data = res.json()

    # Both legs evaluated
    assert "workplace" in data["anchors"]
    assert len(data["samples"]) == 1
    sample = data["samples"][0]
    assert len(sample["legs"]) == 2
    assert sample["legs"][0]["from_anchor"] == "home"
    assert sample["legs"][0]["to_anchor"] == "centre"
    assert sample["legs"][1]["from_anchor"] == "centre"
    assert sample["legs"][1]["to_anchor"] == "workplace"
    assert sample["totals"]["duration_seconds"] == 3000.0  # 1500 * 2


def test_api_commute_legacy_request_preserves_shape(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """Legacy request (without sampling or baseline) returns legacy CommuteResponse shape."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal=None,
    )

    routing_calls: list[dict] = []
    setup_mock_onemap(monkeypatch, routing_calls)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
        },
    )
    assert res.status_code == 200
    data = res.json()

    # Legacy shape: 2 samples, 2 sample_pairs, no sampling or evaluation fields
    assert "sample_pair" in data
    assert len(data["sample_pair"]) == 2
    assert len(data["samples"]) == 2
    assert "evaluation" not in data
    assert "sampling" not in data
    assert "sampling_contract" not in data
    assert "comparison_contract" not in data
    assert len(routing_calls) == 2  # Wednesday + Saturday


def test_api_commute_sampling_with_direct_baseline_combined(
    client: TestClient, test_engine, parent_user, monkeypatch
) -> None:
    """When both sampling and include_direct_baseline=True are passed, both contracts and objects are present."""
    set_auth(parent_user)
    centre = create_centre(test_engine, centre_id=1, postal="560123")
    profile = create_profile(
        test_engine,
        parent_user.user_id,
        home_postal="120120",
        workplace_postal="018989",
    )

    routing_calls: list[dict] = []
    setup_mock_onemap(monkeypatch, routing_calls)

    res = client.post(
        f"/api/v1/centres/{centre.centre_id}/commute",
        json={
            "profile_id": profile.profile_id,
            "mode": "pt",
            "include_direct_baseline": True,
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "cycle_id": None,
                "sample_index": 0,
            },
        },
    )
    assert res.status_code == 200
    data = res.json()

    # Top-level contracts
    assert data["sample_pair"] is None
    assert data["sampling_contract"] == "weekday_0800_wed_thu_fri_v1"
    assert data["comparison_contract"] == "direct_baseline_v1"

    # Sampling metadata block
    assert data["sampling"] is not None
    assert data["sampling"]["selected_index"] == 0

    # Evaluation metadata block
    assert data["evaluation"] is not None
    assert data["evaluation"]["evaluation_id"].startswith("eval_")

    # Sample block contains single selected sample with direct route + comparison
    assert len(data["samples"]) == 1
    sample = data["samples"][0]
    assert sample["kind"] == "weekday"
    assert sample["via_status"] == "available"
    assert len(sample["legs"]) == 2

    # Direct baseline route
    assert sample["direct_route"] is not None
    assert sample["direct_route"]["status"] == "available"
    assert len(sample["direct_route"]["legs"]) == 1
    assert sample["direct_route"]["legs"][0]["from_anchor"] == "home"
    assert sample["direct_route"]["legs"][0]["to_anchor"] == "workplace"

    # Commute comparison
    assert sample["comparison"] is not None
    assert sample["comparison"]["status"] == "available"
    assert sample["comparison"]["certification"] is not None


def test_build_evaluation_metadata():
    from kidcarematch_backend.core.commute import (
        CommuteAnchors,
        build_evaluation_metadata,
        compute_anchor_revision,
        compute_profile_revision,
    )
    from kidcarematch_backend.schemas.commute import Coordinate

    home = Coordinate(lat=1.35, lng=103.82)
    centre = Coordinate(lat=1.36, lng=103.83)
    workplace = Coordinate(lat=1.30, lng=103.85)
    anchors = CommuteAnchors(
        home=home, centre=centre, workplace=workplace, has_workplace=True
    )

    explicit_time = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)
    meta = build_evaluation_metadata("120120", "560123", anchors, now=explicit_time)

    assert meta.evaluation_id.startswith("eval_")
    assert len(meta.evaluation_id) == 29  # "eval_" (5) + 24 hex chars
    assert meta.evaluated_at == explicit_time
    assert meta.profile_revision == compute_profile_revision("120120", "560123")
    assert meta.anchor_revision == compute_anchor_revision(home, centre, workplace)

    # Defaults to now if now is None
    meta_default = build_evaluation_metadata("120120", None, anchors)
    assert meta_default.evaluation_id.startswith("eval_")
    assert meta_default.evaluated_at.tzinfo == timezone.utc



"""Tests for commute capability discovery endpoint (Ticket #88)."""

from unittest.mock import patch

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
from kidcarematch_backend.db.db import (
    AccountStatus,
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


@pytest.fixture
def parent_user(test_engine) -> User:
    with Session(test_engine) as session:
        user = User(
            user_id="parent-cap-1",
            firebase_uid="firebase-parent-cap-1",
            email="parent_cap@example.com",
            full_name="Parent Cap",
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
            user_id="admin-cap-1",
            firebase_uid="firebase-admin-cap-1",
            email="admin_cap@example.com",
            full_name="Admin Cap",
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
            user_id="suspended-parent-cap-1",
            firebase_uid="firebase-suspended-cap-1",
            email="suspended_cap@example.com",
            full_name="Suspended Parent Cap",
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


def test_commute_capabilities_unauthenticated_returns_401(client: TestClient):
    """Unauthenticated request must return 401."""
    response = client.get("/api/v1/commute/capabilities")
    assert response.status_code == 401


def test_commute_capabilities_admin_user_returns_403(
    client: TestClient, admin_user: User
):
    """Admin user (non-parent) must be rejected with 403."""
    set_auth(admin_user)
    response = client.get("/api/v1/commute/capabilities")
    assert response.status_code == 403
    assert "Parent privileges required" in response.json()["detail"]


def test_commute_capabilities_suspended_parent_returns_403(
    client: TestClient, suspended_parent: User
):
    """Suspended parent must be rejected with 403."""
    set_auth(suspended_parent)
    response = client.get("/api/v1/commute/capabilities")
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_commute_capabilities_flags_disabled_returns_null_contracts(
    client: TestClient, parent_user: User
):
    """When both feature flags are False, capabilities return null values."""
    set_auth(parent_user)
    with patch.object(settings, "COMMUTE_ENABLE_DIRECT_BASELINE", False), patch.object(
        settings, "COMMUTE_ENABLE_WEEKDAY_SAMPLING", False
    ):
        response = client.get("/api/v1/commute/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "direct_baseline_contract": None,
            "sampling_contract": None,
        }


def test_commute_capabilities_direct_baseline_enabled_returns_contract(
    client: TestClient, parent_user: User
):
    """When direct baseline is enabled and weekday sampling is disabled."""
    set_auth(parent_user)
    with patch.object(settings, "COMMUTE_ENABLE_DIRECT_BASELINE", True), patch.object(
        settings, "COMMUTE_ENABLE_WEEKDAY_SAMPLING", False
    ):
        response = client.get("/api/v1/commute/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "direct_baseline_contract": "direct_baseline_v1",
            "sampling_contract": None,
        }


def test_commute_capabilities_weekday_sampling_enabled_returns_contract(
    client: TestClient, parent_user: User
):
    """When weekday sampling is enabled and direct baseline is disabled."""
    set_auth(parent_user)
    with patch.object(settings, "COMMUTE_ENABLE_DIRECT_BASELINE", False), patch.object(
        settings, "COMMUTE_ENABLE_WEEKDAY_SAMPLING", True
    ):
        response = client.get("/api/v1/commute/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "direct_baseline_contract": None,
            "sampling_contract": "weekday_0800_wed_thu_fri_v1",
        }


def test_commute_capabilities_both_flags_enabled_returns_both_contracts(
    client: TestClient, parent_user: User
):
    """When both feature flags are independently enabled, return both contracts."""
    set_auth(parent_user)
    with patch.object(settings, "COMMUTE_ENABLE_DIRECT_BASELINE", True), patch.object(
        settings, "COMMUTE_ENABLE_WEEKDAY_SAMPLING", True
    ):
        response = client.get("/api/v1/commute/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "direct_baseline_contract": "direct_baseline_v1",
            "sampling_contract": "weekday_0800_wed_thu_fri_v1",
        }


def test_commute_config_default_settings():
    """Verify settings defaults for commute feature flags and cycle secret key."""
    assert hasattr(settings, "COMMUTE_ENABLE_DIRECT_BASELINE")
    assert settings.COMMUTE_ENABLE_DIRECT_BASELINE is False
    assert hasattr(settings, "COMMUTE_ENABLE_WEEKDAY_SAMPLING")
    assert settings.COMMUTE_ENABLE_WEEKDAY_SAMPLING is False
    assert hasattr(settings, "COMMUTE_CYCLE_SECRET_KEY")
    assert settings.COMMUTE_CYCLE_SECRET_KEY == "kidcarematch-commute-cycle-secret-key-v1"


def test_commute_extensible_schemas_instantiation():
    """Verify all extensible contract types can be instantiated and validated."""
    from datetime import datetime, timezone

    from kidcarematch_backend.schemas.commute import (
        AllowedSampleDefinition,
        CommuteCapabilitiesResponse,
        CommuteComparison,
        CommuteReason,
        CommuteSamplingResponse,
        ComparisonCertification,
        Coordinate,
        DirectRoute,
        EvaluationMetadata,
        LegTiming,
        NegotiatedCommuteResponse,
        NegotiatedCommuteSample,
        NegotiatedRouteLeg,
        RouteTotals,
        SampleConditions,
    )

    now = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)

    # 1. Capabilities
    cap = CommuteCapabilitiesResponse(
        direct_baseline_contract="direct_baseline_v1",
        sampling_contract="weekday_0800_wed_thu_fri_v1",
    )
    assert cap.direct_baseline_contract == "direct_baseline_v1"
    assert cap.sampling_contract == "weekday_0800_wed_thu_fri_v1"

    # 2. Reason & Timing
    reason = CommuteReason(
        code="no_itinerary",
        failure_class="sample_dependent",
        retryable=True,
    )
    assert reason.code == "no_itinerary"
    assert reason.failure_class == "sample_dependent"
    assert reason.retryable is True

    timing = LegTiming(
        duration_basis="elapsed_from_requested_departure",
        arrival_source="provider_end_time",
        initial_wait_included=True,
    )
    assert timing.duration_basis == "elapsed_from_requested_departure"

    # 3. Negotiated RouteLeg
    leg = NegotiatedRouteLeg(
        from_anchor="home",
        to_anchor="workplace",
        status="available",
        source="onemap_pt",
        duration_seconds=1800.0,
        distance_meters=4500.0,
        geometry=[Coordinate(lat=1.3, lng=103.8), Coordinate(lat=1.35, lng=103.85)],
        departure_at=now,
        timing=timing,
        reasons=[],
    )
    assert leg.timing.duration_basis == "elapsed_from_requested_departure"
    assert leg.departure_at == now

    # 4. DirectRoute
    direct = DirectRoute(
        status="available",
        legs=[leg],
        totals=RouteTotals(duration_seconds=1800.0, distance_meters=4500.0),
        reasons=[],
    )
    assert direct.status == "available"
    assert len(direct.legs) == 1

    # 5. Comparison
    comp = CommuteComparison(
        status="available",
        reasons=[],
        certification=ComparisonCertification(
            duration_basis="elapsed_from_requested_departure",
            provider_options_id="test-pt-options",
        ),
    )
    assert comp.status == "available"
    assert comp.certification is not None
    assert comp.certification.provider_options_id == "test-pt-options"

    # 6. Conditions & Sample
    cond = SampleConditions(
        conditions_id="cond-wed-0800",
        timing_basis="scheduled_departure",
        initial_departure_at=now,
        provider_options_id="test-pt-options",
    )
    sample = NegotiatedCommuteSample(
        kind="weekday",
        sampled_at=now,
        legs=[leg],
        totals=RouteTotals(duration_seconds=2500.0, distance_meters=6000.0),
        conditions=cond,
        via_status="available",
        reasons=[],
        direct_route=direct,
        comparison=comp,
    )
    assert sample.via_status == "available"
    assert sample.direct_route is not None

    # 7. Evaluation & Sampling response
    eval_meta = EvaluationMetadata(
        evaluation_id="eval-001",
        evaluated_at=now,
        profile_revision="hash-prof-rev",
        anchor_revision="hash-anchor-rev",
    )
    sampling_resp = CommuteSamplingResponse(
        policy="weekday_0800_wed_thu_fri_v1",
        cycle_id="cycle-token-xyz",
        cycle_expires_at=now,
        selected_index=0,
        allowed_samples=[
            AllowedSampleDefinition(sample_index=0, conditions_id="c0", departure_at=now),
            AllowedSampleDefinition(sample_index=1, conditions_id="c1", departure_at=now),
            AllowedSampleDefinition(sample_index=2, conditions_id="c2", departure_at=now),
        ],
    )
    assert sampling_resp.selected_index == 0
    assert len(sampling_resp.allowed_samples) == 3

    # 8. Full Negotiated Response
    neg_resp = NegotiatedCommuteResponse(
        centre_id=1,
        profile_id="prof-1",
        mode="pt",
        anchors={"home": Coordinate(lat=1.3, lng=103.8), "centre": None, "workplace": None},
        sample_pair=None,
        samples=[sample],
        comparison_contract="direct_baseline_v1",
        sampling_contract="weekday_0800_wed_thu_fri_v1",
        evaluation=eval_meta,
        sampling=sampling_resp,
    )
    assert neg_resp.comparison_contract == "direct_baseline_v1"
    assert neg_resp.sampling_contract == "weekday_0800_wed_thu_fri_v1"


def test_commute_sampling_request_validation():
    """Verify CommuteSamplingRequest validates policy and sample_index."""
    import pydantic

    from kidcarematch_backend.schemas.commute import CommuteSamplingRequest

    # Valid
    req = CommuteSamplingRequest(
        policy="weekday_0800_wed_thu_fri_v1",
        sample_index=0,
    )
    assert req.policy == "weekday_0800_wed_thu_fri_v1"
    assert req.sample_index == 0
    assert req.cycle_id is None

    # Valid with cycle_id
    req_cycle = CommuteSamplingRequest(
        policy="weekday_0800_wed_thu_fri_v1",
        cycle_id="token-abc",
        sample_index=2,
    )
    assert req_cycle.sample_index == 2

    # Invalid policy
    with pytest.raises(pydantic.ValidationError):
        CommuteSamplingRequest(
            policy="invalid_policy",  # type: ignore[arg-type]
            sample_index=0,
        )

    # Invalid sample_index
    with pytest.raises(pydantic.ValidationError):
        CommuteSamplingRequest(
            policy="weekday_0800_wed_thu_fri_v1",
            sample_index=3,  # type: ignore[arg-type]
        )


def test_commute_request_mutually_exclusive_sampling_and_sample_pair():
    """Verify CommuteRequest rejects simultaneous sample_pair and sampling."""
    import pydantic

    from kidcarematch_backend.schemas.commute import (
        CommuteRequest,
        CommuteSamplingRequest,
    )

    # Valid: neither
    req1 = CommuteRequest(profile_id=1)
    assert req1.include_direct_baseline is False
    assert req1.sampling is None

    # Valid: sampling with sample_pair=None
    req2 = CommuteRequest(
        profile_id=1,
        sampling=CommuteSamplingRequest(
            policy="weekday_0800_wed_thu_fri_v1",
            sample_index=0,
        ),
    )
    assert req2.sampling is not None

    # Valid: sample_pair with sampling=None
    req3 = CommuteRequest(
        profile_id=1,
        sample_pair=["2026-11-11T08:00:00+08:00", "2026-11-14T08:00:00+08:00"],
    )
    assert req3.sample_pair is not None

    # Invalid: both non-null
    with pytest.raises(pydantic.ValidationError) as exc_info:
        CommuteRequest(
            profile_id=1,
            sample_pair=["2026-11-11T08:00:00+08:00", "2026-11-14T08:00:00+08:00"],
            sampling=CommuteSamplingRequest(
                policy="weekday_0800_wed_thu_fri_v1",
                sample_index=0,
            ),
        )
    assert "mutually exclusive" in str(exc_info.value).lower()


def test_commute_legacy_response_exact_shape_preserved(
    client: TestClient, parent_user: User, test_engine
):
    """Verify legacy requests return exact legacy JSON shape with no additive metadata."""
    from kidcarematch_backend.db.db import (
        CitizenshipType,
        FamilyPreferenceProfile,
        LocationPreference,
        PreschoolCentre,
        PreschoolLevel,
    )

    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_id=101,
            centre_code="C101",
            centre_name="Legacy Preschool",
            postal="560123",
            address="560123 Singapore",
        )
        from datetime import datetime, timezone

        assert parent_user.user_id is not None
        profile = FamilyPreferenceProfile(
            profile_name="Test Legacy Profile",
            user_id=parent_user.user_id,
            level=PreschoolLevel.INFANT,
            service_required="infant",
            citizenship_status=CitizenshipType.SC,
            location_preference=LocationPreference.HOME,
            home_postal_code="560123",
            workplace_postal_code=None,
            household_income=5000.0,
            household_member_count=3,
            budget_max=1500.0,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            travel_weight=0.34,
            cost_weight=0.33,
            vacancy_weight=0.33,
        )
        session.add(centre)
        session.add(profile)
        session.commit()
        session.refresh(profile)

    set_auth(parent_user)
    with patch(
        "kidcarematch_backend.api.v1.centres.resolve_commute_postal",
        return_value={"lat": 1.35, "lng": 103.85},
    ), patch(
        "kidcarematch_backend.api.v1.centres.build_commute_samples"
    ) as mock_samples:
        from kidcarematch_backend.schemas.commute import (
            CommuteSample,
            Coordinate,
            RouteLeg,
            RouteTotals,
        )

        mock_samples.return_value = [
            CommuteSample(
                kind="single",
                sampled_at=None,
                legs=[
                    RouteLeg(
                        from_anchor="home",
                        to_anchor="centre",
                        status="available",
                        source="onemap_drive",
                        duration_seconds=600.0,
                        distance_meters=2000.0,
                        geometry=[Coordinate(lat=1.35, lng=103.85)],
                    )
                ],
                totals=RouteTotals(duration_seconds=600.0, distance_meters=2000.0),
            )
        ]

        response = client.post(
            "/api/v1/centres/101/commute",
            json={
                "profile_id": profile.profile_id,
                "mode": "drive",
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Exact legacy top-level keys
        assert set(data.keys()) == {
            "centre_id",
            "profile_id",
            "mode",
            "sample_pair",
            "anchors",
            "samples",
        }
        # Verify additive metadata keys do NOT exist in legacy response
        assert "comparison_contract" not in data
        assert "sampling_contract" not in data
        assert "evaluation" not in data
        assert "sampling" not in data

        # Exact legacy sample keys
        sample_data = data["samples"][0]
        assert set(sample_data.keys()) == {
            "kind",
            "sampled_at",
            "legs",
            "totals",
        }
        assert "conditions" not in sample_data
        assert "via_status" not in sample_data
        assert "reasons" not in sample_data
        assert "direct_route" not in sample_data
        assert "comparison" not in sample_data

        # Exact legacy leg keys
        leg_data = sample_data["legs"][0]
        assert set(leg_data.keys()) == {
            "from_anchor",
            "to_anchor",
            "status",
            "source",
            "duration_seconds",
            "arrival_at",
            "distance_meters",
            "geometry",
            "message",
            "transit_legs",
        }
        assert "departure_at" not in leg_data
        assert "timing" not in leg_data
        assert "reasons" not in leg_data


def test_commute_endpoint_rejects_mutually_exclusive_sampling_and_sample_pair(
    client: TestClient, parent_user: User
):
    """POST /centres/{centre_id}/commute returns 422 if both sampling and sample_pair provided."""
    set_auth(parent_user)
    response = client.post(
        "/api/v1/centres/1/commute",
        json={
            "profile_id": 1,
            "mode": "pt",
            "sample_pair": [
                "2026-11-11T08:00:00+08:00",
                "2026-11-14T08:00:00+08:00",
            ],
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "sample_index": 0,
            },
        },
    )
    assert response.status_code == 422


def test_commute_endpoint_rejects_invalid_sampling_payload(
    client: TestClient, parent_user: User
):
    """POST /centres/{centre_id}/commute returns 422 for invalid sampling policy or index."""
    set_auth(parent_user)

    # Invalid policy
    resp_pol = client.post(
        "/api/v1/centres/1/commute",
        json={
            "profile_id": 1,
            "mode": "pt",
            "sampling": {
                "policy": "invalid_policy",
                "sample_index": 0,
            },
        },
    )
    assert resp_pol.status_code == 422

    # Invalid index
    resp_idx = client.post(
        "/api/v1/centres/1/commute",
        json={
            "profile_id": 1,
            "mode": "pt",
            "sampling": {
                "policy": "weekday_0800_wed_thu_fri_v1",
                "sample_index": 3,
            },
        },
    )
    assert resp_idx.status_code == 422


"""Tests for Multi-Criteria Centre Matching Core Engine (Spec #66, ADR-0007).

Validates:
- Data Clump encapsulation into MatchCriteria
- Location preference routing alignment (HOME, WORKPLACE, EITHER, default dual-anchor)
- Candidate pool non-discarding: all cohort centres scored on Vacancy & Cost Fit
- Top-30 Stage 2 OneMap routing with straight-line transit time estimation for remainder
- Clean math.inf semantics replacing magic 999999.0 sentinels
- FeeBreakdownItem remarks deduplication
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.core.matching import (
    MatchCriteria,
    TravelRoutingMode,
    calculate_cost_fit,
    evaluate_government_subsidy,
    map_level_to_care_type,
    resolve_published_programme_fee,
    resolve_travel_routing_mode,
    score_vacancy_fit,
)
from kidcarematch_backend.db.db import (
    ApplicantStatus,
    CareType,
    CitizenshipType,
    LocationPreference,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    SubsidyMetric,
    SubsidyRule,
    VacancyRecord,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app
from kidcarematch_backend.schemas.match import FeeBreakdownItem


@pytest.fixture
def test_engine():
    """Create isolated in-memory SQLite database engine."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(test_engine):
    """FastAPI TestClient with overridden database session."""

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def setup_matching_onemap_mock(
    monkeypatch,
    postal_coords: dict[str, tuple[float, float]] | None = None,
    route_durations: dict[str, int] | None = None,
):
    """Setup mock for OneMap search and routing endpoints."""
    default_coords = {
        "560123": (1.3691, 103.8512),  # Ang Mo Kio (Home)
        "560124": (1.3695, 103.8520),  # Close to Home
        "048581": (1.2840, 103.8510),  # Raffles Place (Workplace)
        "600123": (1.3320, 103.7430),  # Jurong East (Far outlier)
    }
    coords_table = {**default_coords, **(postal_coords or {})}

    original_get = httpx.Client.get

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if "onemap.gov.sg/api/common/elastic/search" in url_str:
            params = kwargs.get("params", {})
            postal = str(params.get("searchVal", "")).strip()
            if postal in coords_table:
                lat, lon = coords_table[postal]
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "found": 1,
                    "totalNumPages": 1,
                    "pageNum": 1,
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
                mock_resp.raise_for_status = MagicMock()
                return mock_resp
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"found": 0, "results": []}
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        if "onemap.gov.sg/api/public/routingsvc/route" in url_str:
            params = kwargs.get("params", {})
            start_str = str(params.get("start", ""))
            end_str = str(params.get("end", ""))

            def _norm(s: str) -> str:
                parts = s.split(",")
                if len(parts) == 2:
                    return f"{float(parts[0]):.4f},{float(parts[1]):.4f}"
                return s

            norm_durations = {
                f"{_norm(k.split('->')[0])}->{_norm(k.split('->')[1])}": v
                for k, v in (route_durations or {}).items()
            }
            norm_key = f"{_norm(start_str)}->{_norm(end_str)}"
            duration_secs = norm_durations.get(norm_key, 1200)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "plan": {
                    "itineraries": [
                        {
                            "duration": duration_secs,
                        }
                    ]
                }
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)


# ==============================================================================
# Spec Fix 1: Candidate Pool Evaluation when travel_weight == 0.0
# ==============================================================================


def test_zero_travel_weight_retains_distant_high_fit_centre(
    client: TestClient, test_engine, monkeypatch
):
    """When candidate pool > 30 centres and parent sets travel_weight = 0.0,

    a distant centre (> 20 km away) with Available vacancy and 100% Cost Fit
    is NOT discarded and ranks #1.
    """
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {"560123": (1.3691, 103.8512)}  # Home

    with Session(test_engine) as session:
        # Create 34 near centres with Full vacancy (0% Vacancy Fit) and expensive fee ($2000)
        for i in range(1, 35):
            p_code = f"56{i:04d}"
            coords_dict[p_code] = (1.3691 + (i * 0.001), 103.8512 + (i * 0.001))
            c = PreschoolCentre(
                centre_id=1000 + i,
                centre_code=f"C_NEAR_EXP_{i:02d}",
                centre_name=f"Near Expensive Centre {i:02d}",
                postal=p_code,
            )
            s = PreschoolService(
                centre_id=1000 + i,
                type_of_service="Full Day",
                level=PreschoolLevel.INFANT,
                fee=2000.0,
                citizenship_type=CitizenshipType.SC,
            )
            v = VacancyRecord(
                record_id=f"v_near_exp_{i}",
                centre_id=1000 + i,
                level_type=PreschoolLevel.INFANT,
                enrollment_month_year=target_month,
                vacancy_status="Full",
            )
            session.add_all([c, s, v])

        # Create 1 distant outlier centre (e.g. Tuas > 25 km away) with Available vacancy (100%) and $400 fee (100% Cost Fit)
        coords_dict["639999"] = (1.3100, 103.6500)
        c_distant = PreschoolCentre(
            centre_id=2000,
            centre_code="C_DISTANT_PERFECT",
            centre_name="Distant Perfect Match Centre",
            postal="639999",
        )
        s_distant = PreschoolService(
            centre_id=2000,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=400.0,
            citizenship_type=CitizenshipType.SC,
        )
        v_distant = VacancyRecord(
            record_id="v_distant_perfect",
            centre_id=2000,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c_distant, s_distant, v_distant])
        session.commit()

    setup_matching_onemap_mock(monkeypatch, postal_coords=coords_dict)

    # Parent prioritizes vacancy and cost: travel_weight = 0.0, vacancy_weight = 0.50, cost_weight = 0.50, budget = 500
    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "budget_max": 500.0,
        "vacancy_weight": 0.50,
        "cost_weight": 0.50,
        "travel_weight": 0.0,
        "top_n": 5,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["total_candidates"] == 35
    assert data["returned_count"] == 5

    # Distant Perfect Match Centre MUST be #1 despite being #35 spatially!
    top_centre = data["results"][0]
    assert top_centre["centre_code"] == "C_DISTANT_PERFECT"
    assert top_centre["vacancy_fit_score"] == 100.0
    assert top_centre["cost_fit_score"] == 100.0
    assert top_centre["overall_fit_score"] == 100.0


# ==============================================================================
# Spec Fix 2: Location Preference Routing Alignment
# ==============================================================================


def test_location_preference_home_with_dual_anchors_evaluates_home_single_anchor(
    client: TestClient, test_engine, monkeypatch
):
    """When both home and workplace are provided, and location_preference is HOME,

    evaluate single-anchor Home routing (distance to Home, no detour deviation).
    """
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {
        "560123": (1.3691, 103.8512),  # Home
        "048581": (1.2840, 103.8510),  # Workplace
        "560124": (1.3695, 103.8520),  # Centre near Home (approx 0.1 km)
    }

    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=3001,
            centre_code="C_PREF_HOME_01",
            centre_name="Home Preferred Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=3001,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=800.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_pref_home_1",
            centre_id=3001,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    setup_matching_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "location_preference": "HOME",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    tb = item["travel_breakdown"]
    assert tb is not None
    # Single-anchor routing: commute_deviation_mins and baseline_* are None
    assert tb["commute_deviation_mins"] is None
    assert tb["baseline_duration_mins"] is None
    assert tb["baseline_distance_km"] is None
    # Commute distance is physical distance from Home (approx < 1.0 km)
    assert tb["commute_distance_km"] is not None
    assert tb["commute_distance_km"] < 1.0


def test_location_preference_workplace_with_dual_anchors_evaluates_workplace_single_anchor(
    client: TestClient, test_engine, monkeypatch
):
    """When both home and workplace are provided, and location_preference is WORKPLACE,

    evaluate single-anchor Workplace routing (distance to Workplace, no detour deviation).
    """
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {
        "560123": (1.3691, 103.8512),  # Home (Ang Mo Kio)
        "048581": (1.2840, 103.8510),  # Workplace (Raffles Place)
        "048582": (1.2845, 103.8515),  # Centre near Workplace (approx 0.1 km)
    }

    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=3002,
            centre_code="C_PREF_WORK_01",
            centre_name="Workplace Preferred Centre",
            postal="048582",
        )
        s1 = PreschoolService(
            centre_id=3002,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=800.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_pref_work_1",
            centre_id=3002,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    setup_matching_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "location_preference": "WORKPLACE",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    tb = item["travel_breakdown"]
    assert tb is not None
    # Single-anchor routing: commute_deviation_mins and baseline_* are None
    assert tb["commute_deviation_mins"] is None
    assert tb["baseline_duration_mins"] is None
    assert tb["baseline_distance_km"] is None
    # Commute distance is physical distance from Workplace (approx < 1.0 km)
    assert tb["commute_distance_km"] is not None
    assert tb["commute_distance_km"] < 1.0


def test_location_preference_either_with_dual_anchors_evaluates_dual_corridor_detour(
    client: TestClient, test_engine, monkeypatch
):
    """When both home and workplace are provided, and location_preference is EITHER,

    evaluate dual-anchor corridor detour routing (commute_deviation_mins present).
    """
    target_month = datetime(2026, 6, 1, tzinfo=timezone.utc)
    coords_dict = {
        "560123": (1.3691, 103.8512),  # Home
        "048581": (1.2840, 103.8510),  # Workplace
        "560124": (1.3695, 103.8520),  # Centre
    }

    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_id=3003,
            centre_code="C_PREF_EITHER_01",
            centre_name="Either Preferred Centre",
            postal="560124",
        )
        s1 = PreschoolService(
            centre_id=3003,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=800.0,
            citizenship_type=CitizenshipType.SC,
        )
        v1 = VacancyRecord(
            record_id="v_pref_either_1",
            centre_id=3003,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=target_month,
            vacancy_status="Available",
        )
        session.add_all([c1, s1, v1])
        session.commit()

    setup_matching_onemap_mock(monkeypatch, postal_coords=coords_dict)

    payload = {
        "level": "infant",
        "enrollment_month_year": "2026-06",
        "citizenship_status": "SC",
        "home_postal_code": "560123",
        "workplace_postal_code": "048581",
        "location_preference": "EITHER",
        "top_n": 1,
    }

    response = client.post("/api/v1/centres/match", json=payload)
    assert response.status_code == 200
    data = response.json()
    item = data["results"][0]

    tb = item["travel_breakdown"]
    assert tb is not None
    # Dual-anchor detour routing: commute_deviation_mins and baseline_* are populated
    assert tb["commute_deviation_mins"] is not None
    assert tb["baseline_duration_mins"] is not None
    assert tb["baseline_distance_km"] is not None


# ==============================================================================
# Standards Fix: Data Clump and FeeBreakdownItem Deduplication
# ==============================================================================


def test_fee_breakdown_item_remarks_deduplication():
    """FeeBreakdownItem auto-synchronizes remarks and incidental_fee_remarks."""
    item1 = FeeBreakdownItem(
        published_fee=1000.0,
        estimated_subsidy=300.0,
        estimated_monthly_payable_fee=700.0,
        remarks="Registration Fee: $50",
    )
    assert item1.remarks == "Registration Fee: $50"
    assert item1.incidental_fee_remarks == "Registration Fee: $50"

    item2 = FeeBreakdownItem(
        published_fee=1000.0,
        estimated_subsidy=300.0,
        estimated_monthly_payable_fee=700.0,
        incidental_fee_remarks="Deposit: 1 month",
    )
    assert item2.remarks == "Deposit: 1 month"
    assert item2.incidental_fee_remarks == "Deposit: 1 month"


def test_match_criteria_data_structure():
    """MatchCriteria cleanly encapsulates all matching parameters."""
    criteria = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        citizenship_status=CitizenshipType.SC,
        household_income=5000.0,
        household_member_count=4,
        budget_max=600.0,
        home_postal_code="560123",
        workplace_postal_code="048581",
        location_preference=LocationPreference.EITHER,
        vacancy_weight=0.40,
        cost_weight=0.30,
        travel_weight=0.30,
        applicant_status=ApplicantStatus.WORKING_APPLICANT,
    )
    assert criteria.level == PreschoolLevel.INFANT
    assert criteria.enrollment_month_year == "2026-06"
    assert criteria.household_income == 5000.0
    assert criteria.budget_max == 600.0
    assert criteria.location_preference == LocationPreference.EITHER


def test_resolve_travel_routing_mode_rules():
    """resolve_travel_routing_mode correctly evaluates all combinations."""
    # 1. Dual coords + WORKPLACE -> SINGLE_WORKPLACE
    c_work = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        location_preference=LocationPreference.WORKPLACE,
    )
    assert (
        resolve_travel_routing_mode(c_work, has_home_coords=True, has_workplace_coords=True)
        == TravelRoutingMode.SINGLE_WORKPLACE
    )

    # 2. Dual coords + EITHER -> DUAL_DETOUR
    c_either = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        location_preference=LocationPreference.EITHER,
    )
    assert (
        resolve_travel_routing_mode(c_either, has_home_coords=True, has_workplace_coords=True)
        == TravelRoutingMode.DUAL_DETOUR
    )

    # 3. Dual coords + HOME with default preference -> DUAL_DETOUR
    c_default = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        location_preference=LocationPreference.HOME,
        is_default_location_preference=True,
    )
    assert (
        resolve_travel_routing_mode(c_default, has_home_coords=True, has_workplace_coords=True)
        == TravelRoutingMode.DUAL_DETOUR
    )

    # 4. Dual coords + HOME explicitly specified -> SINGLE_HOME
    c_home_explicit = MatchCriteria(
        level=PreschoolLevel.INFANT,
        enrollment_month_year="2026-06",
        location_preference=LocationPreference.HOME,
        is_default_location_preference=False,
    )
    assert (
        resolve_travel_routing_mode(c_home_explicit, has_home_coords=True, has_workplace_coords=True)
        == TravelRoutingMode.SINGLE_HOME
    )

    # 5. Only home coords -> SINGLE_HOME
    assert (
        resolve_travel_routing_mode(c_default, has_home_coords=True, has_workplace_coords=False)
        == TravelRoutingMode.SINGLE_HOME
    )

    # 6. Only workplace coords -> SINGLE_WORKPLACE
    assert (
        resolve_travel_routing_mode(c_default, has_home_coords=False, has_workplace_coords=True)
        == TravelRoutingMode.SINGLE_WORKPLACE
    )

    # 7. Neither -> NONE
    assert (
        resolve_travel_routing_mode(c_default, has_home_coords=False, has_workplace_coords=False)
        == TravelRoutingMode.NONE
    )


def test_core_matching_pure_functions(test_engine):
    """Direct unit tests for core matching functions."""
    assert score_vacancy_fit("Available") == 100.0
    assert score_vacancy_fit("Immediate") == 100.0
    assert score_vacancy_fit("Limited") == 50.0
    assert score_vacancy_fit("Full") == 0.0
    assert score_vacancy_fit(None) == 0.0

    assert map_level_to_care_type(PreschoolLevel.INFANT) == CareType.INFANT_CARE
    assert map_level_to_care_type(PreschoolLevel.N1) == CareType.CHILD_CARE

    assert calculate_cost_fit(400.0, 500.0) == 100.0
    assert calculate_cost_fit(1000.0, 500.0) == 50.0
    assert calculate_cost_fit(500.0, None) is None

    services = [
        PreschoolService(
            centre_id=1,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=950.0,
            citizenship_type=CitizenshipType.SC,
            remarks="Registration: $50",
        )
    ]
    fee, remarks = resolve_published_programme_fee(
        services=services,
        requested_level=PreschoolLevel.INFANT,
        service_required="Full Day",
        citizenship_status=CitizenshipType.SC,
    )
    assert fee == 950.0
    assert remarks == "Registration: $50"

    with Session(test_engine) as session:
        rule = SubsidyRule(
            rule_id="r1",
            care_type=CareType.INFANT_CARE,
            citizenship=CitizenshipType.SC,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
            subsidy_name="Basic Subsidy",
            subsidy_amount=600.0,
            metric=SubsidyMetric.FIXED,
        )
        session.add(rule)
        session.commit()

        subsidy = evaluate_government_subsidy(
            session=session,
            care_type=CareType.INFANT_CARE,
            citizenship=CitizenshipType.SC,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
            household_income=6000.0,
            household_member_count=3,
        )
        assert subsidy == 600.0

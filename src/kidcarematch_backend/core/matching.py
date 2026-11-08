"""Multi-Criteria Preschool Centre Matching Engine (ADR-0007, Spec #66).

Encapsulates cohort filtering, government subsidy evaluation, OneMap spatial detour
routing, multi-factor scoring (Vacancy Fit, Cost Fit, Travel Fit), and AI explanation.
"""

import logging
import math
from collections import defaultdict
from enum import Enum

import httpx
from fastapi import HTTPException, status
from sqlmodel import Session, col, or_, select

from kidcarematch_backend.core.explanation import (
    MatchCriteria,
    generate_fit_explanation,
)
from kidcarematch_backend.core.onemap import (
    STAGE1_MAX_CANDIDATES,
    estimate_transit_duration_mins,
    get_route_duration_and_distance,
    haversine_distance_km,
    normalize_travel_fit,
    resolve_coordinates_batch,
)
from kidcarematch_backend.db.db import (
    ApplicantStatus,
    CareType,
    CitizenshipType,
    FamilyPreferenceProfile,
    LocationPreference,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    SubsidyMetric,
    SubsidyRule,
    User,
    VacancyRecord,
)
from kidcarematch_backend.schemas.match import (
    CentreMatchRequest,
    CentreMatchResponse,
    FeeBreakdownItem,
    MatchCandidateItem,
    TravelBreakdownItem,
)
from kidcarematch_backend.schemas.profile import parse_year_month

logger = logging.getLogger(__name__)

__all__ = [
    "MatchCriteria",
    "TravelRoutingMode",
    "calculate_cost_fit",
    "evaluate_government_subsidy",
    "execute_multi_criteria_match",
    "map_level_to_care_type",
    "resolve_match_criteria",
    "resolve_published_programme_fee",
    "resolve_travel_routing_mode",
    "score_vacancy_fit",
]


class TravelRoutingMode(str, Enum):
    """Travel routing evaluation strategy based on anchor postal codes and parental preference."""

    DUAL_DETOUR = "DUAL_DETOUR"
    SINGLE_HOME = "SINGLE_HOME"
    SINGLE_WORKPLACE = "SINGLE_WORKPLACE"
    NONE = "NONE"


def score_vacancy_fit(status: str | None) -> float:
    """Score Vacancy Fit based on ECDA vacancy status for the target enrollment month.

    - Available / Immediate: 100.0%
    - Limited: 50.0%
    - Full / Not Applicable / unrecorded / other: 0.0%
    """
    if not status:
        return 0.0
    normalized = status.strip().lower()
    if normalized in ("available", "immediate"):
        return 100.0
    if normalized == "limited":
        return 50.0
    return 0.0


def map_level_to_care_type(level: PreschoolLevel) -> CareType:
    """Map developmental preschool cohort level to ECDA subsidy CareType category."""
    if level == PreschoolLevel.INFANT:
        return CareType.INFANT_CARE
    return CareType.CHILD_CARE


def resolve_published_programme_fee(
    services: list[PreschoolService],
    requested_level: PreschoolLevel,
    service_required: str,
    citizenship_status: CitizenshipType,
) -> tuple[float, str | None]:
    """Resolve centre published programme fee matching requested level, service type, and citizenship.

    Priority and fallback logic:
    - Service Type: exact match (case-insensitive) -> fallback to "Full Day" -> fallback to any available service type.
    - Citizenship: exact match -> fallback to OTHERS -> fallback to any available citizenship tier.
    Returns (published_fee, remarks).
    """
    level_services = [s for s in services if s.level == requested_level]
    if not level_services:
        return 0.0, None

    req_clean = service_required.strip().lower()
    type_matches = [
        s for s in level_services if s.type_of_service.strip().lower() == req_clean
    ]
    if not type_matches and req_clean != "full day":
        type_matches = [
            s
            for s in level_services
            if s.type_of_service.strip().lower() == "full day"
        ]
    if not type_matches:
        type_matches = level_services

    cit_match = next(
        (s for s in type_matches if s.citizenship_type == citizenship_status),
        None,
    )
    if cit_match is None and citizenship_status != CitizenshipType.OTHERS:
        cit_match = next(
            (
                s
                for s in type_matches
                if s.citizenship_type == CitizenshipType.OTHERS
            ),
            None,
        )
    if cit_match is None:
        cit_match = type_matches[0]

    return cit_match.fee, cit_match.remarks


def evaluate_government_subsidy(
    session: Session,
    care_type: CareType,
    citizenship: CitizenshipType,
    applicant_status: ApplicantStatus,
    household_income: float,
    household_member_count: int,
) -> float:
    """Evaluate eligible government early childhood subsidies according to ECDA means-tested policy.

    - Fixed subsidies (e.g. Basic Subsidy) are summed universally.
    - Means-tested tiered subsidies (e.g. Additional Subsidy) evaluate both Gross Household Income (HHI)
      and Per-Capita Income (PCI = HHI / household_member_count), taking the maximum eligible benefit.
    """
    statement = (
        select(SubsidyRule)
        .where(
            col(SubsidyRule.care_type) == care_type,
            col(SubsidyRule.citizenship) == citizenship,
            or_(
                col(SubsidyRule.applicant_status) == applicant_status,
                col(SubsidyRule.applicant_status).is_(None),
            ),
        )
    )
    rules = session.exec(statement).all()
    if not rules:
        return 0.0

    member_count = max(1, household_member_count)
    pci = household_income / member_count

    fixed_subsidy = 0.0
    tiered_rules_by_name: dict[str, list[SubsidyRule]] = defaultdict(list)

    for r in rules:
        if r.metric == SubsidyMetric.FIXED:
            fixed_subsidy += r.subsidy_amount
        elif r.metric in (
            SubsidyMetric.HOUSEHOLD_INCOME,
            SubsidyMetric.PER_CAPITA_INCOME,
        ):
            tiered_rules_by_name[r.subsidy_name].append(r)

    total_tiered_subsidy = 0.0
    for sub_rules in tiered_rules_by_name.values():
        hhi_benefit = 0.0
        pci_benefit = 0.0
        for r in sub_rules:
            if (
                r.metric == SubsidyMetric.HOUSEHOLD_INCOME
                and (r.income_min is None or household_income >= r.income_min)
                and (r.income_max is None or household_income <= r.income_max)
            ):
                hhi_benefit = max(hhi_benefit, r.subsidy_amount)
            elif (
                r.metric == SubsidyMetric.PER_CAPITA_INCOME
                and (r.income_min is None or pci >= r.income_min)
                and (r.income_max is None or pci <= r.income_max)
            ):
                pci_benefit = max(pci_benefit, r.subsidy_amount)
        total_tiered_subsidy += max(hhi_benefit, pci_benefit)

    return round(fixed_subsidy + total_tiered_subsidy, 2)


def calculate_cost_fit(
    estimated_payable_fee: float,
    budget_max: float | None,
) -> float | None:
    """Calculate Cost Fit percentage (0% - 100%) comparing net payable fee against budget ceiling.

    - 100% if payable fee <= budget_max (or <= 0.0)
    - min(100.0, (budget_max / payable_fee) * 100.0) otherwise
    - None if budget_max is None
    """
    if budget_max is None:
        return None
    if estimated_payable_fee <= budget_max or estimated_payable_fee <= 0.0:
        return 100.0
    return round(min(100.0, (budget_max / estimated_payable_fee) * 100.0), 2)


def resolve_match_criteria(
    request: CentreMatchRequest,
    session: Session,
    current_user: User | None = None,
) -> MatchCriteria:
    """Resolve and encapsulate matching parameters from saved profile or inline request."""
    if request.profile_id is not None:
        if current_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required when profile_id is provided",
                headers={"WWW-Authenticate": "Bearer"},
            )
        profile = session.exec(
            select(FamilyPreferenceProfile).where(
                FamilyPreferenceProfile.profile_id == request.profile_id,
                FamilyPreferenceProfile.user_id == current_user.user_id,
            )
        ).first()
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Family preference profile not found",
            )
        enrollment_month_str = profile.enrollment_month_year.strftime("%Y-%m")
        return MatchCriteria(
            level=profile.level,
            enrollment_month_year=enrollment_month_str,
            citizenship_status=profile.citizenship_status,
            service_required=profile.service_required,
            household_income=profile.household_income,
            household_member_count=profile.household_member_count,
            budget_max=profile.budget_max,
            applicant_status=profile.applicant_status,
            vacancy_weight=profile.vacancy_weight,
            cost_weight=profile.cost_weight,
            travel_weight=profile.travel_weight,
            home_postal_code=profile.home_postal_code,
            workplace_postal_code=profile.workplace_postal_code,
            location_preference=profile.location_preference,
            is_default_location_preference=False,
            top_n=request.top_n,
            profile_id=request.profile_id,
        )

    assert request.level is not None
    assert request.enrollment_month_year is not None
    is_default_loc = "location_preference" not in request.model_fields_set

    return MatchCriteria(
        level=request.level,
        enrollment_month_year=request.enrollment_month_year,
        citizenship_status=request.citizenship_status,
        service_required=request.service_required,
        household_income=request.household_income,
        household_member_count=request.household_member_count,
        budget_max=request.budget_max,
        applicant_status=request.applicant_status,
        vacancy_weight=request.vacancy_weight,
        cost_weight=request.cost_weight,
        travel_weight=request.travel_weight,
        home_postal_code=request.home_postal_code,
        workplace_postal_code=request.workplace_postal_code,
        location_preference=request.location_preference,
        is_default_location_preference=is_default_loc,
        top_n=request.top_n,
        profile_id=None,
    )


def resolve_travel_routing_mode(
    criteria: MatchCriteria,
    has_home_coords: bool,
    has_workplace_coords: bool,
) -> TravelRoutingMode:
    """Determine the travel routing mode based on resolved postal coordinates and parental preference."""
    if has_home_coords and has_workplace_coords:
        if criteria.location_preference == LocationPreference.WORKPLACE:
            return TravelRoutingMode.SINGLE_WORKPLACE
        if criteria.location_preference == LocationPreference.EITHER:
            return TravelRoutingMode.DUAL_DETOUR
        if criteria.location_preference == LocationPreference.HOME:
            if criteria.is_default_location_preference:
                return TravelRoutingMode.DUAL_DETOUR
            return TravelRoutingMode.SINGLE_HOME
        return TravelRoutingMode.DUAL_DETOUR
    if has_home_coords:
        return TravelRoutingMode.SINGLE_HOME
    if has_workplace_coords:
        return TravelRoutingMode.SINGLE_WORKPLACE
    return TravelRoutingMode.NONE


def execute_multi_criteria_match(
    session: Session,
    criteria: MatchCriteria,
) -> CentreMatchResponse:
    """Execute complete multi-criteria preschool matching algorithm (Spec #66)."""
    target_dt = parse_year_month(criteria.enrollment_month_year)

    # 1. Strict cohort filtering: candidates must offer requested level
    candidate_centres = session.exec(
        select(PreschoolCentre)
        .where(
            col(PreschoolCentre.centre_id).in_(
                select(PreschoolService.centre_id).where(
                    col(PreschoolService.level) == criteria.level
                )
            )
        )
        .order_by(col(PreschoolCentre.centre_name).asc())
    ).all()

    total_candidates = len(candidate_centres)
    if total_candidates == 0:
        empty_explanation = generate_fit_explanation(
            criteria=criteria,
            total_candidates=0,
            candidates=[],
        )
        return CentreMatchResponse(
            total_candidates=0,
            returned_count=0,
            level=criteria.level,
            enrollment_month_year=criteria.enrollment_month_year,
            results=[],
            explanation=empty_explanation,
            profile_id=criteria.profile_id,
        )

    # 2. Consolidated Coordinate Batch Prefetching
    postals_to_resolve: list[str] = []
    if criteria.home_postal_code and len(criteria.home_postal_code.strip()) == 6:
        postals_to_resolve.append(criteria.home_postal_code.strip())
    if criteria.workplace_postal_code and len(criteria.workplace_postal_code.strip()) == 6:
        postals_to_resolve.append(criteria.workplace_postal_code.strip())
    for c in candidate_centres:
        if c.postal and len(c.postal.strip()) == 6:
            postals_to_resolve.append(c.postal.strip())

    travel_dev_map: dict[int, float] = {}
    travel_fit_map: dict[int, float] = {}
    travel_breakdown_map: dict[int, TravelBreakdownItem | None] = {}
    has_travel_anchors = False

    with httpx.Client(timeout=5.0) as http_client:
        coords_map = resolve_coordinates_batch(
            postals_to_resolve, session=session, client=http_client
        )
        home_coords = (
            coords_map.get(criteria.home_postal_code.strip())
            if criteria.home_postal_code
            else None
        )
        workplace_coords = (
            coords_map.get(criteria.workplace_postal_code.strip())
            if criteria.workplace_postal_code
            else None
        )

        routing_mode = resolve_travel_routing_mode(
            criteria=criteria,
            has_home_coords=home_coords is not None,
            has_workplace_coords=workplace_coords is not None,
        )
        has_travel_anchors = routing_mode != TravelRoutingMode.NONE

        if has_travel_anchors:
            spatial_dev_map: dict[int, float] = {}
            base_dur = 0.0
            base_dist = 0.0

            if routing_mode == TravelRoutingMode.DUAL_DETOUR:
                assert home_coords is not None and workplace_coords is not None
                d_hw = haversine_distance_km(
                    home_coords[0],
                    home_coords[1],
                    workplace_coords[0],
                    workplace_coords[1],
                )
                base_dur, base_dist = get_route_duration_and_distance(
                    home_coords, workplace_coords, client=http_client
                )

                for c in candidate_centres:
                    if c.centre_id is None:
                        continue
                    c_pt = coords_map.get(c.postal) if c.postal else None
                    if c_pt:
                        d_hc = haversine_distance_km(
                            home_coords[0], home_coords[1], c_pt[0], c_pt[1]
                        )
                        d_cw = haversine_distance_km(
                            c_pt[0], c_pt[1], workplace_coords[0], workplace_coords[1]
                        )
                        spatial_dev_map[c.centre_id] = max(0.0, d_hc + d_cw - d_hw)
                    else:
                        spatial_dev_map[c.centre_id] = math.inf

            elif routing_mode in (
                TravelRoutingMode.SINGLE_HOME,
                TravelRoutingMode.SINGLE_WORKPLACE,
            ):
                anchor_coords = (
                    home_coords
                    if routing_mode == TravelRoutingMode.SINGLE_HOME
                    else workplace_coords
                )
                assert anchor_coords is not None

                for c in candidate_centres:
                    if c.centre_id is None:
                        continue
                    c_pt = coords_map.get(c.postal) if c.postal else None
                    if c_pt:
                        spatial_dev_map[c.centre_id] = haversine_distance_km(
                            anchor_coords[0], anchor_coords[1], c_pt[0], c_pt[1]
                        )
                    else:
                        spatial_dev_map[c.centre_id] = math.inf

            # Identify Stage 2 candidates: top STAGE1_MAX_CANDIDATES (or scaled to criteria.top_n) by straight-line spatial deviation
            effective_stage1_cap = max(STAGE1_MAX_CANDIDATES, criteria.top_n)
            valid_spatial_centres = [
                c
                for c in candidate_centres
                if c.centre_id is not None
                and c.centre_id in spatial_dev_map
                and math.isfinite(spatial_dev_map[c.centre_id])
            ]
            valid_spatial_centres.sort(key=lambda c: spatial_dev_map[c.centre_id])  # type: ignore[index]
            stage2_centre_ids = {
                c.centre_id for c in valid_spatial_centres[:effective_stage1_cap]
            }

            # Evaluate Travel Fit for all candidates (Stage 2 routing for top 30, straight-line estimation for remainder)
            if routing_mode == TravelRoutingMode.DUAL_DETOUR:
                assert home_coords is not None and workplace_coords is not None
                for c in candidate_centres:
                    if c.centre_id is None:
                        continue
                    c_pt = coords_map.get(c.postal) if c.postal else None
                    if not c_pt or not math.isfinite(spatial_dev_map.get(c.centre_id, math.inf)):
                        travel_dev_map[c.centre_id] = math.inf
                        travel_breakdown_map[c.centre_id] = None
                        continue

                    if c.centre_id in stage2_centre_ids:
                        t_hc, d_hc = get_route_duration_and_distance(
                            home_coords, c_pt, client=http_client
                        )
                        t_cw, d_cw = get_route_duration_and_distance(
                            c_pt, workplace_coords, client=http_client
                        )
                        tot_dur = t_hc + t_cw
                        tot_dist = d_hc + d_cw
                        delta_time = max(0.0, tot_dur - base_dur)
                        travel_dev_map[c.centre_id] = delta_time
                        travel_breakdown_map[c.centre_id] = TravelBreakdownItem(
                            commute_duration_mins=round(tot_dur, 1),
                            commute_deviation_mins=round(delta_time, 1),
                            commute_distance_km=round(tot_dist, 2),
                            baseline_duration_mins=round(base_dur, 1),
                            baseline_distance_km=round(base_dist, 2),
                        )
                    else:
                        d_hc = haversine_distance_km(
                            home_coords[0], home_coords[1], c_pt[0], c_pt[1]
                        )
                        d_cw = haversine_distance_km(
                            c_pt[0], c_pt[1], workplace_coords[0], workplace_coords[1]
                        )
                        t_hc = estimate_transit_duration_mins(d_hc)
                        t_cw = estimate_transit_duration_mins(d_cw)
                        tot_dur = t_hc + t_cw
                        tot_dist = d_hc + d_cw
                        delta_time = max(0.0, tot_dur - base_dur)
                        travel_dev_map[c.centre_id] = delta_time
                        travel_breakdown_map[c.centre_id] = TravelBreakdownItem(
                            commute_duration_mins=round(tot_dur, 1),
                            commute_deviation_mins=round(delta_time, 1),
                            commute_distance_km=round(tot_dist, 2),
                            baseline_duration_mins=round(base_dur, 1),
                            baseline_distance_km=round(base_dist, 2),
                        )

            elif routing_mode in (
                TravelRoutingMode.SINGLE_HOME,
                TravelRoutingMode.SINGLE_WORKPLACE,
            ):
                anchor_coords = (
                    home_coords
                    if routing_mode == TravelRoutingMode.SINGLE_HOME
                    else workplace_coords
                )
                assert anchor_coords is not None
                for c in candidate_centres:
                    if c.centre_id is None:
                        continue
                    c_pt = coords_map.get(c.postal) if c.postal else None
                    if not c_pt or not math.isfinite(spatial_dev_map.get(c.centre_id, math.inf)):
                        travel_dev_map[c.centre_id] = math.inf
                        travel_breakdown_map[c.centre_id] = None
                        continue

                    if c.centre_id in stage2_centre_ids:
                        dur, dist = get_route_duration_and_distance(
                            anchor_coords, c_pt, client=http_client
                        )
                        travel_dev_map[c.centre_id] = dist
                        travel_breakdown_map[c.centre_id] = TravelBreakdownItem(
                            commute_duration_mins=round(dur, 1),
                            commute_deviation_mins=None,
                            commute_distance_km=round(dist, 2),
                            baseline_duration_mins=None,
                            baseline_distance_km=None,
                        )
                    else:
                        dist = spatial_dev_map[c.centre_id]
                        dur = estimate_transit_duration_mins(dist)
                        travel_dev_map[c.centre_id] = dist
                        travel_breakdown_map[c.centre_id] = TravelBreakdownItem(
                            commute_duration_mins=round(dur, 1),
                            commute_deviation_mins=None,
                            commute_distance_km=round(dist, 2),
                            baseline_duration_mins=None,
                            baseline_distance_km=None,
                        )

            # Pool-relative Travel Fit normalization across all valid candidate centres
            valid_centres = [
                c
                for c in candidate_centres
                if c.centre_id is not None
                and c.centre_id in travel_dev_map
                and math.isfinite(travel_dev_map[c.centre_id])
            ]
            if valid_centres:
                valid_devs = [
                    travel_dev_map[c.centre_id]
                    for c in valid_centres
                    if c.centre_id is not None
                ]
                norm_scores = normalize_travel_fit(valid_devs)
                for c, score in zip(valid_centres, norm_scores):
                    if c.centre_id is not None:
                        travel_fit_map[c.centre_id] = score

    # 3. Prefetch vacancy and service records for ALL candidate centres
    candidate_ids = [
        c.centre_id for c in candidate_centres if c.centre_id is not None
    ]
    vacancies = session.exec(
        select(VacancyRecord).where(
            col(VacancyRecord.centre_id).in_(candidate_ids),
            col(VacancyRecord.level_type) == criteria.level,
        )
    ).all()

    vacancy_map: dict[int, VacancyRecord] = {}
    for v in vacancies:
        if (
            v.centre_id is not None
            and v.enrollment_month_year is not None
            and v.enrollment_month_year.year == target_dt.year
            and v.enrollment_month_year.month == target_dt.month
        ):
            vacancy_map[v.centre_id] = v

    services = session.exec(
        select(PreschoolService).where(
            col(PreschoolService.centre_id).in_(candidate_ids),
            col(PreschoolService.level) == criteria.level,
        )
    ).all()

    services_map: dict[int, list[PreschoolService]] = defaultdict(list)
    for s in services:
        if s.centre_id is not None:
            services_map[s.centre_id].append(s)

    care_type = map_level_to_care_type(criteria.level)
    estimated_subsidy = evaluate_government_subsidy(
        session=session,
        care_type=care_type,
        citizenship=criteria.citizenship_status,
        applicant_status=criteria.applicant_status,
        household_income=criteria.household_income,
        household_member_count=criteria.household_member_count,
    )

    # 4. Multi-Factor Scoring for each candidate centre
    scored_candidates: list[MatchCandidateItem] = []
    for centre in candidate_centres:
        v_rec = (
            vacancy_map.get(centre.centre_id) if centre.centre_id is not None else None
        )
        v_status = v_rec.vacancy_status if v_rec else None
        v_score = score_vacancy_fit(v_status)

        centre_services = (
            services_map.get(centre.centre_id, [])
            if centre.centre_id is not None
            else []
        )
        published_fee, remarks = resolve_published_programme_fee(
            services=centre_services,
            requested_level=criteria.level,
            service_required=criteria.service_required,
            citizenship_status=criteria.citizenship_status,
        )
        payable_fee = max(0.0, round(published_fee - estimated_subsidy, 2))
        cost_fit = calculate_cost_fit(payable_fee, criteria.budget_max)

        fee_breakdown = FeeBreakdownItem(
            published_fee=published_fee,
            estimated_subsidy=estimated_subsidy,
            estimated_monthly_payable_fee=payable_fee,
            cost_fit=cost_fit,
            remarks=remarks,
            incidental_fee_remarks=remarks,
        )

        travel_fit = (
            travel_fit_map.get(centre.centre_id)
            if has_travel_anchors and centre.centre_id is not None
            else None
        )
        travel_breakdown = (
            travel_breakdown_map.get(centre.centre_id)
            if has_travel_anchors and centre.centre_id is not None
            else None
        )

        if cost_fit is not None and travel_fit is not None:
            overall_fit_score = round(
                (v_score * criteria.vacancy_weight)
                + (cost_fit * criteria.cost_weight)
                + (travel_fit * criteria.travel_weight),
                2,
            )
        elif cost_fit is not None and travel_fit is None:
            active_weight = criteria.vacancy_weight + criteria.cost_weight
            overall_fit_score = (
                round(
                    ((v_score * criteria.vacancy_weight) + (cost_fit * criteria.cost_weight))
                    / active_weight,
                    2,
                )
                if active_weight > 0
                else 0.0
            )
        elif cost_fit is None and travel_fit is not None:
            active_weight = criteria.vacancy_weight + criteria.travel_weight
            overall_fit_score = (
                round(
                    ((v_score * criteria.vacancy_weight) + (travel_fit * criteria.travel_weight))
                    / active_weight,
                    2,
                )
                if active_weight > 0
                else 0.0
            )
        else:
            overall_fit_score = v_score

        candidate_item = MatchCandidateItem(
            centre_id=centre.centre_id,  # type: ignore[arg-type]
            centre_code=centre.centre_code,
            centre_name=centre.centre_name,
            contact=centre.contact,
            email=centre.email,
            postal=centre.postal,
            website_url=centre.website_url,
            food_offered=centre.food_offered,
            address=centre.address,
            second_languages=centre.second_languages,
            spark_certified=centre.spark_certified,
            weekday_full_day_hours=centre.weekday_full_day_hours,
            saturday_hours=centre.saturday_hours,
            extended_operating_hours=centre.extended_operating_hours,
            transport_provided=centre.transport_provided,
            gov_subsidy=centre.gov_subsidy,
            last_updated=centre.last_updated,
            level=criteria.level,
            enrollment_month_year=criteria.enrollment_month_year,
            vacancy_status=v_status,
            vacancy_fit_score=v_score,
            cost_fit_score=cost_fit,
            travel_fit_score=travel_fit,
            overall_fit_score=overall_fit_score,
            fee_breakdown=fee_breakdown,
            travel_breakdown=travel_breakdown,
            highlight=None,
        )
        scored_candidates.append(candidate_item)

    # 5. Global Rank: overall_fit desc, vacancy_fit desc, cost_fit desc, travel_fit desc, centre_name asc, centre_id asc
    scored_candidates.sort(
        key=lambda item: (
            -item.overall_fit_score,
            -item.vacancy_fit_score,
            -(item.cost_fit_score or 0.0),
            -(item.travel_fit_score or 0.0),
            item.centre_name,
            item.centre_id,
        )
    )

    # 6. Top N candidates
    top_results = scored_candidates[: criteria.top_n]

    # 7. Synthesized Gemini AI Explanation with Resilient Fallback
    explanation = generate_fit_explanation(
        criteria=criteria,
        total_candidates=total_candidates,
        candidates=top_results,
    )

    for i, item in enumerate(top_results):
        if i < len(explanation.candidate_highlights):
            item.highlight = explanation.candidate_highlights[i]

    return CentreMatchResponse(
        total_candidates=total_candidates,
        returned_count=len(top_results),
        level=criteria.level,
        enrollment_month_year=criteria.enrollment_month_year,
        results=top_results,
        explanation=explanation,
        profile_id=criteria.profile_id,
    )

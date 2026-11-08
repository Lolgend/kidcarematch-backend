"""Gemini AI Fit Explanation Service with resilient fallback (Ticket #71, Spec #66)."""

import json
import logging
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import (
    ApplicantStatus,
    CitizenshipType,
    LocationPreference,
    PreschoolLevel,
)
from kidcarematch_backend.schemas.match import (
    FitExplanationResponse,
    MatchCandidateItem,
)

logger = logging.getLogger(__name__)


@dataclass
class MatchCriteria:
    """Encapsulated criteria and parameters for multi-criteria preschool matching (ADR-0007)."""

    level: PreschoolLevel
    enrollment_month_year: str
    citizenship_status: CitizenshipType = CitizenshipType.SC
    service_required: str = "Full Day"
    household_income: float = 0.0
    household_member_count: int = 1
    budget_max: float | None = None
    home_postal_code: str | None = None
    workplace_postal_code: str | None = None
    location_preference: LocationPreference = LocationPreference.HOME
    is_default_location_preference: bool = False
    vacancy_weight: float = 1.0 / 3.0
    cost_weight: float = 1.0 / 3.0
    travel_weight: float = 1.0 / 3.0
    applicant_status: ApplicantStatus = ApplicantStatus.WORKING_APPLICANT
    top_n: int = 5
    profile_id: str | None = None

    def __post_init__(self) -> None:
        total = float(self.vacancy_weight + self.cost_weight + self.travel_weight)
        if total > 0:
            self.vacancy_weight = float(self.vacancy_weight) / total
            self.cost_weight = float(self.cost_weight) / total
            self.travel_weight = float(self.travel_weight) / total


def build_explanation_prompt(
    criteria: MatchCriteria | None = None,
    total_candidates: int = 0,
    candidates: list[MatchCandidateItem] | None = None,
    *,
    level: PreschoolLevel | None = None,
    enrollment_month_year: str | None = None,
    household_income: float = 0.0,
    household_member_count: int = 1,
    budget_max: float | None = None,
    home_postal_code: str | None = None,
    workplace_postal_code: str | None = None,
    vacancy_weight: float = 1.0 / 3.0,
    cost_weight: float = 1.0 / 3.0,
    travel_weight: float = 1.0 / 3.0,
    applicant_status: ApplicantStatus = ApplicantStatus.WORKING_APPLICANT,
    **kwargs: Any,
) -> str:
    """Build structured prompt for Gemini AI model (gemini-2.5-flash)."""
    if candidates is None:
        candidates = []
    if criteria is None:
        if level is None or enrollment_month_year is None:
            raise ValueError(
                "Either criteria or (level, enrollment_month_year) must be provided"
            )
        criteria = MatchCriteria(
            level=level,
            enrollment_month_year=enrollment_month_year,
            household_income=household_income,
            household_member_count=household_member_count,
            budget_max=budget_max,
            home_postal_code=home_postal_code,
            workplace_postal_code=workplace_postal_code,
            vacancy_weight=vacancy_weight,
            cost_weight=cost_weight,
            travel_weight=travel_weight,
            applicant_status=applicant_status,
        )

    candidates_lines: list[str] = []
    for rank, cand in enumerate(candidates, start=1):
        line = [
            f"Candidate #{rank}: {cand.centre_name} (Code: {cand.centre_code})",
            f"  - Overall Fit Score: {cand.overall_fit_score:.1f}%",
            f"  - Vacancy Fit Score: {cand.vacancy_fit_score:.1f}% (Status: {cand.vacancy_status or 'Unrecorded'})",
        ]
        if cand.fee_breakdown and cand.cost_fit_score is not None:
            line.append(
                f"  - Cost Fit Score: {cand.cost_fit_score:.1f}% "
                f"(Published Fee: ${cand.fee_breakdown.published_fee:.2f}, "
                f"Estimated Subsidy: ${cand.fee_breakdown.estimated_subsidy:.2f}, "
                f"Payable Fee: ${cand.fee_breakdown.estimated_monthly_payable_fee:.2f}/month)"
            )
        else:
            line.append("  - Cost Fit Score: Not evaluated (no budget or fee data)")

        if cand.travel_breakdown and cand.travel_fit_score is not None:
            dev_str = (
                f", Detour Deviation: {cand.travel_breakdown.commute_deviation_mins:.1f} mins"
                if cand.travel_breakdown.commute_deviation_mins is not None
                else ""
            )
            dur_str = (
                f", Commute Duration: {cand.travel_breakdown.commute_duration_mins:.1f} mins"
                if cand.travel_breakdown.commute_duration_mins is not None
                else ""
            )
            dist_str = (
                f", Commute Distance: {cand.travel_breakdown.commute_distance_km:.2f} km"
                if cand.travel_breakdown.commute_distance_km is not None
                else ""
            )
            line.append(
                f"  - Travel Fit Score: {cand.travel_fit_score:.1f}% ({dev_str.lstrip(', ')}{dur_str}{dist_str})"
            )
        else:
            line.append("  - Travel Fit Score: Not evaluated (no postal anchors provided)")

        if cand.spark_certified:
            line.append("  - SPARK Certified: Yes")
        if cand.gov_subsidy:
            line.append("  - Government Subsidy Eligible: Yes")

        candidates_lines.append("\n".join(line))

    candidates_text = "\n\n".join(candidates_lines)

    budget_text = (
        f"${criteria.budget_max:.2f}/month"
        if criteria.budget_max is not None
        else "No ceiling specified"
    )
    applicant_status_str = (
        criteria.applicant_status.value
        if hasattr(criteria.applicant_status, "value")
        else str(criteria.applicant_status)
    )

    prompt = f"""You are KidCareMatch's expert preschool recommendation advisor in Singapore.
Your role is to explain multi-criteria preschool matching recommendations to parents in clear, transparent, and empathetic natural language based on quantitative scoring.

Parent Search Profile & Criteria:
- Developmental Level: {criteria.level.value.upper()}
- Target Enrollment Month: {criteria.enrollment_month_year}
- Monthly Household Income: ${criteria.household_income:.2f} (Household Size: {criteria.household_member_count})
- Applicant Employment Status: {applicant_status_str}
- Preferred Maximum Budget: {budget_text}
- Anchor Locations: Home Postal: {criteria.home_postal_code or 'Not specified'}, Workplace Postal: {criteria.workplace_postal_code or 'Not specified'}
- Preference Weights: Vacancy Weight: {round(criteria.vacancy_weight * 100)}%, Cost Weight: {round(criteria.cost_weight * 100)}%, Travel Weight: {round(criteria.travel_weight * 100)}%
- Total Eligible Cohort Centres Evaluated: {total_candidates}

Top Candidate Recommendations:
{candidates_text}

Instructions:
Generate a structured JSON response matching the following schema:
1. "summary_narrative": A clear 2-3 sentence overview explaining how the parent's trade-off weights shaped the overall candidate ranking, highlighting key factors (e.g., vacancy availability vs affordability vs commute logistics).
2. "top_centre_rationale": A detailed 2-4 sentence rationale explaining why the #1 ranked centre achieved top placement, explicitly referencing its vacancy status, net out-of-pocket payable fee (after subsidies), and commute detour metrics.
3. "ranking_rationale": A 2-4 sentence comparative explanation explaining why the shortlisted candidates ({f"#1 through #{len(candidates)}" if len(candidates) > 1 else "#1"}) are ordered in their specific sequence based on Overall Fit, Vacancy Fit, Cost Fit (net payable fee), and Travel Fit (commute detour or distance), grounded in candidate data and Importance Ratings.
4. "candidate_highlights": A list of exactly {len(candidates)} concise 1-sentence highlights, one for each candidate centre in rank order, capturing its specific advantage or notable feature.
"""
    return prompt


def generate_deterministic_fallback(
    criteria: MatchCriteria | None = None,
    total_candidates: int = 0,
    candidates: list[MatchCandidateItem] | None = None,
    *,
    level: PreschoolLevel | None = None,
    enrollment_month_year: str | None = None,
    household_income: float = 0.0,
    household_member_count: int = 1,
    budget_max: float | None = None,
    home_postal_code: str | None = None,
    workplace_postal_code: str | None = None,
    vacancy_weight: float = 1.0 / 3.0,
    cost_weight: float = 1.0 / 3.0,
    travel_weight: float = 1.0 / 3.0,
    applicant_status: ApplicantStatus = ApplicantStatus.WORKING_APPLICANT,
    **kwargs: Any,
) -> FitExplanationResponse:
    """Generate structured deterministic explanation template when Gemini AI is unavailable."""
    if candidates is None:
        candidates = []
    if criteria is None:
        if level is None or enrollment_month_year is None:
            raise ValueError(
                "Either criteria or (level, enrollment_month_year) must be provided"
            )
        criteria = MatchCriteria(
            level=level,
            enrollment_month_year=enrollment_month_year,
            household_income=household_income,
            household_member_count=household_member_count,
            budget_max=budget_max,
            home_postal_code=home_postal_code,
            workplace_postal_code=workplace_postal_code,
            vacancy_weight=vacancy_weight,
            cost_weight=cost_weight,
            travel_weight=travel_weight,
            applicant_status=applicant_status,
        )

    level_name = criteria.level.value.upper()

    if not candidates:
        return FitExplanationResponse(
            summary_narrative=(
                f"No preschool centres offering {level_name} cohort were found "
                f"for enrollment in {criteria.enrollment_month_year} matching your criteria."
            ),
            top_centre_rationale="No candidate centres available to recommend.",
            ranking_rationale="No candidate centres available to rank.",
            candidate_highlights=[],
            fallback_used=True,
        )

    v_pct = round(criteria.vacancy_weight * 100)
    c_pct = round(criteria.cost_weight * 100)
    t_pct = round(criteria.travel_weight * 100)
    budget_desc = (
        f" with a preferred budget ceiling of ${criteria.budget_max:.2f}/month"
        if criteria.budget_max is not None
        else ""
    )

    summary_narrative = (
        f"Evaluated {total_candidates} candidate centres offering {level_name} cohort "
        f"for enrollment in {criteria.enrollment_month_year}{budget_desc}. "
        f"Rankings reflect your preference weights of {v_pct}% Vacancy Fit, "
        f"{c_pct}% Cost Fit, and {t_pct}% Travel Fit."
    )

    top = candidates[0]
    v_status = top.vacancy_status or "unrecorded"
    fee_part = ""
    if top.fee_breakdown and top.cost_fit_score is not None:
        fee_part = (
            f", an estimated net payable fee of ${top.fee_breakdown.estimated_monthly_payable_fee:.2f}/month "
            f"({top.cost_fit_score:.1f}% Cost Fit)"
        )

    travel_part = ""
    if top.travel_breakdown and top.travel_fit_score is not None:
        if top.travel_breakdown.commute_deviation_mins is not None:
            travel_part = (
                f", and a commute detour deviation of {top.travel_breakdown.commute_deviation_mins:.1f} mins "
                f"({top.travel_fit_score:.1f}% Travel Fit)"
            )
        elif top.travel_breakdown.commute_distance_km is not None:
            travel_part = (
                f", and a commute distance of {top.travel_breakdown.commute_distance_km:.2f} km "
                f"({top.travel_fit_score:.1f}% Travel Fit)"
            )

    top_centre_rationale = (
        f"{top.centre_name} achieved the top recommendation with an Overall Fit of {top.overall_fit_score:.1f}%. "
        f"It offers {v_status} vacancy ({top.vacancy_fit_score:.1f}% Vacancy Fit)"
        f"{fee_part}{travel_part}."
    )

    if len(candidates) == 1:
        fee_info = (
            f", net fee ${top.fee_breakdown.estimated_monthly_payable_fee:.2f}/month ({top.cost_fit_score:.1f}% Cost Fit)"
            if top.fee_breakdown and top.cost_fit_score is not None
            else ""
        )
        travel_info = ""
        if top.travel_breakdown and top.travel_fit_score is not None:
            if top.travel_breakdown.commute_deviation_mins is not None:
                travel_info = f", and {top.travel_breakdown.commute_deviation_mins:.1f}m commute detour ({top.travel_fit_score:.1f}% Travel Fit)"
            elif top.travel_breakdown.commute_distance_km is not None:
                travel_info = f", and {top.travel_breakdown.commute_distance_km:.2f}km commute distance ({top.travel_fit_score:.1f}% Travel Fit)"

        ranking_rationale = (
            f"{top.centre_name} is the sole candidate holding rank #1 with an Overall Fit of {top.overall_fit_score:.1f}% "
            f"(Vacancy Fit: {top.vacancy_fit_score:.1f}% [{v_status}]{fee_info}{travel_info})."
        )
    else:
        rank_items: list[str] = []
        for idx, cand in enumerate(candidates, start=1):
            details = [f"#{idx} {cand.centre_name} ({cand.overall_fit_score:.1f}% Overall Fit, {cand.vacancy_fit_score:.1f}% Vacancy"]
            if cand.fee_breakdown and cand.cost_fit_score is not None:
                details.append(f"${cand.fee_breakdown.estimated_monthly_payable_fee:.2f}/mo net fee [{cand.cost_fit_score:.1f}% Cost]")
            if cand.travel_breakdown and cand.travel_fit_score is not None:
                if cand.travel_breakdown.commute_deviation_mins is not None:
                    details.append(f"{cand.travel_breakdown.commute_deviation_mins:.1f}m detour [{cand.travel_fit_score:.1f}% Travel]")
                elif cand.travel_breakdown.commute_distance_km is not None:
                    details.append(f"{cand.travel_breakdown.commute_distance_km:.2f}km [{cand.travel_fit_score:.1f}% Travel]")
            rank_items.append(", ".join(details) + ")")

        progression = "; ".join(rank_items)
        ranking_rationale = (
            f"Candidates are ranked from #1 to #{len(candidates)} based on composite Overall Fit weighted by your preference priorities: "
            f"{progression}. "
            f"{candidates[0].centre_name} achieved the top rank ahead of subsequent centres primarily due to its higher composite fit across the prioritized criteria."
        )

    candidate_highlights: list[str] = []
    for cand in candidates:
        status_text = cand.vacancy_status or "unrecorded"
        segments = [
            f"{cand.centre_name}: Overall Fit {cand.overall_fit_score:.1f}%",
            f"{status_text} vacancy ({cand.vacancy_fit_score:.1f}%)",
        ]
        if cand.fee_breakdown and cand.cost_fit_score is not None:
            segments.append(
                f"Net fee ${cand.fee_breakdown.estimated_monthly_payable_fee:.2f}/mo ({cand.cost_fit_score:.1f}% Cost Fit)"
            )
        if cand.travel_breakdown and cand.travel_fit_score is not None:
            if cand.travel_breakdown.commute_deviation_mins is not None:
                segments.append(
                    f"{cand.travel_breakdown.commute_deviation_mins:.1f}m detour ({cand.travel_fit_score:.1f}% Travel Fit)"
                )
            elif cand.travel_breakdown.commute_distance_km is not None:
                segments.append(
                    f"{cand.travel_breakdown.commute_distance_km:.2f}km distance ({cand.travel_fit_score:.1f}% Travel Fit)"
                )
        candidate_highlights.append(" | ".join(segments) + ".")

    return FitExplanationResponse(
        summary_narrative=summary_narrative,
        top_centre_rationale=top_centre_rationale,
        ranking_rationale=ranking_rationale,
        candidate_highlights=candidate_highlights,
        fallback_used=True,
    )


def _parse_gemini_response(
    response: Any,
    expected_count: int,
) -> FitExplanationResponse | None:
    """Parse Gemini response into FitExplanationResponse safely."""
    try:
        if hasattr(response, "parsed") and isinstance(
            response.parsed, FitExplanationResponse
        ):
            return response.parsed

        text = getattr(response, "text", None)
        if not text:
            return None

        raw = text.strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        raw = raw.removesuffix("```")
        raw = raw.strip()

        data = json.loads(raw)
        if isinstance(data, dict) and (
            "ranking_rationale" not in data or data["ranking_rationale"] is None
        ):
            data["ranking_rationale"] = ""
        return FitExplanationResponse.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse Gemini explanation response: %s", exc)
        return None


def generate_fit_explanation(
    criteria: MatchCriteria | None = None,
    total_candidates: int = 0,
    candidates: list[MatchCandidateItem] | None = None,
    *,
    level: PreschoolLevel | None = None,
    enrollment_month_year: str | None = None,
    household_income: float = 0.0,
    household_member_count: int = 1,
    budget_max: float | None = None,
    home_postal_code: str | None = None,
    workplace_postal_code: str | None = None,
    vacancy_weight: float = 1.0 / 3.0,
    cost_weight: float = 1.0 / 3.0,
    travel_weight: float = 1.0 / 3.0,
    applicant_status: ApplicantStatus = ApplicantStatus.WORKING_APPLICANT,
    **kwargs: Any,
) -> FitExplanationResponse:
    """Generate synthesized Gemini AI explanation with resilient fallback."""
    if candidates is None:
        candidates = []
    if criteria is None:
        if level is None or enrollment_month_year is None:
            raise ValueError(
                "Either criteria or (level, enrollment_month_year) must be provided"
            )
        criteria = MatchCriteria(
            level=level,
            enrollment_month_year=enrollment_month_year,
            household_income=household_income,
            household_member_count=household_member_count,
            budget_max=budget_max,
            home_postal_code=home_postal_code,
            workplace_postal_code=workplace_postal_code,
            vacancy_weight=vacancy_weight,
            cost_weight=cost_weight,
            travel_weight=travel_weight,
            applicant_status=applicant_status,
        )

    fallback = generate_deterministic_fallback(
        criteria=criteria,
        total_candidates=total_candidates,
        candidates=candidates,
    )

    if not candidates:
        return fallback

    if not settings.GEMINI_API_KEY:
        logger.info(
            "GEMINI_API_KEY not configured; using deterministic fallback explanation."
        )
        return fallback

    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        prompt = build_explanation_prompt(
            criteria=criteria,
            total_candidates=total_candidates,
            candidates=candidates,
        )

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FitExplanationResponse,
            temperature=0.2,
        )

        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
            config=config,
        )

        explanation = _parse_gemini_response(response, len(candidates))
        if explanation is not None:
            if (
                not explanation.ranking_rationale
                or not explanation.ranking_rationale.strip()
            ):
                explanation.ranking_rationale = fallback.ranking_rationale
            if len(explanation.candidate_highlights) < len(candidates):
                explanation.candidate_highlights.extend(
                    fallback.candidate_highlights[
                        len(explanation.candidate_highlights) :
                    ]
                )
            explanation.fallback_used = False
            return explanation

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Gemini AI explanation call failed (%s: %s); falling back to deterministic template.",
            type(exc).__name__,
            exc,
        )

    return fallback

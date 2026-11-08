"""Schemas for Multi-Criteria Centre Matching (Spec #66, ADR-0007)."""

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from kidcarematch_backend.db.db import (
    ApplicantStatus,
    CitizenshipType,
    LocationPreference,
    PreschoolLevel,
)
from kidcarematch_backend.schemas.centre import PreschoolCentreItem
from kidcarematch_backend.schemas.profile import (
    validate_enrollment_month_format,
    validate_non_empty_str,
    validate_singapore_postal_code,
)


class FeeBreakdownItem(BaseModel):
    """Estimated fee and subsidy calculation breakdown (Ticket #69)."""

    published_fee: float = Field(description="Centre published programme fee in SGD")
    estimated_subsidy: float = Field(
        description="Estimated government subsidy benefit in SGD"
    )
    estimated_monthly_payable_fee: float = Field(
        description="Net out-of-pocket estimated monthly payable fee in SGD"
    )
    cost_fit: float | None = Field(
        default=None,
        description="Quantitative Cost Fit percentage score (0-100%)",
    )
    remarks: str | None = Field(
        default=None,
        description="Incidental and registration fee remarks or notes",
    )
    incidental_fee_remarks: str | None = Field(
        default=None,
        description="Incidental and registration fee remarks or notes",
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_remarks(cls, data: object) -> object:
        if isinstance(data, dict):
            if "remarks" in data and "incidental_fee_remarks" not in data:
                data["incidental_fee_remarks"] = data["remarks"]
            elif "incidental_fee_remarks" in data and "remarks" not in data:
                data["remarks"] = data["incidental_fee_remarks"]
        return data


class TravelBreakdownItem(BaseModel):
    """Commute route and detour travel metrics (Ticket #70)."""

    commute_duration_mins: float | None = Field(
        default=None,
        description="Total estimated morning journey travel time in minutes",
    )
    commute_deviation_mins: float | None = Field(
        default=None,
        description="Detour time deviation relative to direct baseline route in minutes",
    )
    commute_distance_km: float | None = Field(
        default=None,
        description="Total journey or detour distance in kilometers",
    )
    baseline_duration_mins: float | None = Field(
        default=None,
        description="Direct baseline morning commute duration in minutes",
    )
    baseline_distance_km: float | None = Field(
        default=None,
        description="Direct baseline morning commute distance in kilometers",
    )


class FitExplanationResponse(BaseModel):
    """Synthesized Gemini AI matching narrative and highlights (Ticket #71)."""

    summary_narrative: str = Field(
        description="Synthesized narrative explaining multi-criteria search trade-offs",
    )
    top_centre_rationale: str = Field(
        description="Deep-dive rationale explaining why the top-ranked centre was recommended",
    )
    candidate_highlights: list[str] = Field(
        default_factory=list,
        description="Concise 1-sentence highlight for each candidate in top results",
    )


class MatchCandidateItem(PreschoolCentreItem):
    """Ranked candidate preschool centre matching result."""

    level: PreschoolLevel = Field(
        description="Evaluated preschool cohort developmental level",
    )
    enrollment_month_year: str = Field(
        description="Target enrollment month and year in YYYY-MM format",
    )
    vacancy_status: str | None = Field(
        default=None,
        description="Reported ECDA admission vacancy status for target cohort and month",
    )
    vacancy_fit_score: float = Field(
        ge=0.0,
        le=100.0,
        description="Normalized quantitative Vacancy Fit percentage score (0-100%)",
    )
    cost_fit_score: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Normalized quantitative Cost Fit percentage score (0-100%)",
    )
    travel_fit_score: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Normalized quantitative Travel Fit percentage score (0-100%)",
    )
    overall_fit_score: float = Field(
        ge=0.0,
        le=100.0,
        description="Composite weighted Overall Fit percentage score (0-100%)",
    )
    fee_breakdown: FeeBreakdownItem | None = Field(
        default=None,
        description="Itemized fee and subsidy calculation breakdown (Ticket #69)",
    )
    travel_breakdown: TravelBreakdownItem | None = Field(
        default=None,
        description="Commute detour travel metrics (Ticket #70)",
    )
    highlight: str | None = Field(
        default=None,
        description="Concise 1-sentence AI highlight for this candidate (Ticket #71)",
    )


class CentreMatchRequest(BaseModel):
    """Payload for multi-criteria preschool matching endpoint."""

    profile_id: str | None = Field(
        default=None,
        description="Optional saved FamilyPreferenceProfile ID. If provided, criteria are loaded from profile and authentication is required.",
    )
    level: PreschoolLevel | None = Field(
        default=None,
        description="Preschool cohort developmental age level",
    )
    citizenship_status: CitizenshipType = Field(
        default=CitizenshipType.SC,
        description="Child's citizenship status for fee and subsidy determination",
    )
    service_required: str = Field(
        default="Full Day",
        min_length=1,
        max_length=100,
        description="Required service offering type (e.g. Full Day, Half Day)",
    )
    home_postal_code: str | None = Field(
        default=None,
        description="Optional 6-digit Singapore home postal code",
    )
    workplace_postal_code: str | None = Field(
        default=None,
        description="Optional 6-digit Singapore workplace postal code",
    )
    location_preference: LocationPreference = Field(
        default=LocationPreference.HOME,
        description="Proximity anchor prioritization preference (HOME, WORKPLACE, EITHER)",
    )
    household_income: float = Field(
        default=0.0,
        ge=0.0,
        description="Gross monthly household income in SGD",
    )
    household_member_count: int = Field(
        default=1,
        ge=1,
        description="Total number of household members for per-capita income calculation",
    )
    budget_max: float | None = Field(
        default=None,
        gt=0.0,
        description="Maximum monthly net preschool fee budget ceiling in SGD",
    )
    enrollment_month_year: str | None = Field(
        default=None,
        description="Target enrollment month and year in YYYY-MM format (e.g. 2026-06)",
    )
    vacancy_weight: int = Field(
        default=3,
        ge=1,
        le=5,
        strict=True,
        description="Importance rating assigned to vacancy factor (1 to 5, default 3)",
    )
    cost_weight: int = Field(
        default=3,
        ge=1,
        le=5,
        strict=True,
        description="Importance rating assigned to cost factor (1 to 5, default 3)",
    )
    travel_weight: int = Field(
        default=3,
        ge=1,
        le=5,
        strict=True,
        description="Importance rating assigned to travel factor (1 to 5, default 3)",
    )
    applicant_status: ApplicantStatus = Field(
        default=ApplicantStatus.WORKING_APPLICANT,
        description="Parent employment status for ECDA subsidy eligibility tier determination",
    )
    top_n: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum number of top-ranked candidate centres to return (1 to 20, default 6)",
    )

    @field_validator("service_required")
    @classmethod
    def validate_service_required(cls, v: str) -> str:
        return validate_non_empty_str(v, "service_required")

    @field_validator("home_postal_code")
    @classmethod
    def validate_home_postal(cls, v: str | None) -> str | None:
        return validate_singapore_postal_code(v, "home_postal_code", allow_none=True)

    @field_validator("workplace_postal_code")
    @classmethod
    def validate_workplace_postal(cls, v: str | None) -> str | None:
        return validate_singapore_postal_code(
            v, "workplace_postal_code", allow_none=True
        )

    @field_validator("enrollment_month_year")
    @classmethod
    def validate_enrollment_month(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_enrollment_month_format(v, "enrollment_month_year")
        return None

    @model_validator(mode="after")
    def validate_request_invariants(self) -> "CentreMatchRequest":
        if self.profile_id is None:
            if self.level is None:
                raise ValueError("level is required when profile_id is not provided")
            if self.enrollment_month_year is None:
                raise ValueError(
                    "enrollment_month_year is required when profile_id is not provided"
                )
            if (
                self.location_preference
                in (LocationPreference.WORKPLACE, LocationPreference.EITHER)
                and not self.workplace_postal_code
            ):
                raise ValueError(
                    "Workplace postal code is required when location preference is WORKPLACE or EITHER"
                )

        return self


class CentreMatchResponse(BaseModel):
    """Response containing ranked multi-criteria preschool matching candidate centres."""

    total_candidates: int = Field(
        description="Total candidate centres offering the requested developmental cohort",
    )
    returned_count: int = Field(
        description="Number of candidate centres returned in this response",
    )
    level: PreschoolLevel = Field(
        description="Preschool cohort developmental level evaluated",
    )
    enrollment_month_year: str = Field(
        description="Target enrollment month and year in YYYY-MM format",
    )
    results: list[MatchCandidateItem] = Field(
        description="Ranked candidate preschool centres",
    )
    explanation: FitExplanationResponse | None = Field(
        default=None,
        description="Gemini AI synthesized narrative and candidate highlights (Ticket #71)",
    )
    profile_id: str | None = Field(
        default=None,
        description="Source FamilyPreferenceProfile ID if profile-linked matching was executed",
    )

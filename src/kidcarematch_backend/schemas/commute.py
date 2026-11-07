"""Pydantic schemas for OneMap commute display and routing comparisons."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

AnchorName = Literal["home", "centre", "workplace"]
LegStatus = Literal["available", "unavailable"]
CommuteMode = Literal["drive", "pt", "walk", "cycle"]
SampleKind = Literal["single", "weekday", "weekend"]
DirectBaselineContract = Literal["direct_baseline_v1"]
SamplingContract = Literal["weekday_0800_wed_thu_fri_v1"]
SamplingPolicy = Literal["weekday_0800_wed_thu_fri_v1"]
SampleIndex = Literal[0, 1, 2]

ReasonCode = Literal[
    "no_itinerary",
    "direct_unavailable",
    "via_centre_incomplete",
    "home_unresolved",
    "centre_unresolved",
    "workplace_unresolved",
    "workplace_not_configured",
    "timing_unverified",
    "conditions_mismatch",
    "provider_timeout",
    "provider_unavailable",
    "provider_auth_failed",
    "evaluation_budget_exhausted",
    "invalid_provider_payload",
    "geometry_unavailable",
]

FailureClass = Literal[
    "sample_dependent",
    "authentication",
    "service",
    "location",
    "conditions",
    "not_applicable",
]

DurationBasis = Literal[
    "elapsed_from_requested_departure",
    "itinerary_duration",
    "provider_default",
    "unknown",
]

ArrivalSource = Literal[
    "provider_end_time",
    "duration_derived",
    "not_applicable",
    "unknown",
]

DirectRouteStatus = Literal["available", "unavailable", "not_applicable"]
ComparisonStatus = Literal["available", "unavailable", "not_applicable"]
ComparisonDurationBasis = Literal[
    "elapsed_from_requested_departure", "provider_default"
]
TimingBasis = Literal["scheduled_departure", "provider_default"]
ViaStatus = Literal["available", "partial", "unavailable"]


class CommuteCapabilitiesResponse(BaseModel):
    """Available commute comparison capability contracts."""

    direct_baseline_contract: DirectBaselineContract | None = None
    sampling_contract: SamplingContract | None = None


class Coordinate(BaseModel):
    """Geographic coordinate representation."""

    lat: float
    lng: float


class RouteTotals(BaseModel):
    """Aggregated metrics across completed journey legs."""

    duration_seconds: float
    distance_meters: float


class TransitLeg(BaseModel):
    """Public transport itinerary sub-leg representation."""

    mode: str
    duration_seconds: float | None = None
    distance_meters: float | None = None
    route: str | None = None
    instruction: str | None = None
    geometry: list[Coordinate] | None = None


class RouteLeg(BaseModel):
    """Individual Journey Leg between geographic anchors in a Commute Route."""

    from_anchor: AnchorName
    to_anchor: AnchorName
    status: LegStatus
    source: str | None = None
    duration_seconds: float | None = None
    arrival_at: datetime | None = None
    distance_meters: float | None = None
    geometry: list[Coordinate] | None = None
    message: str | None = None
    transit_legs: list[TransitLeg] | None = None


class CommuteReason(BaseModel):
    """Structured diagnostics for route component failure or ineligibility."""

    code: ReasonCode
    failure_class: FailureClass
    retryable: bool


class LegTiming(BaseModel):
    """Provenance and semantics of journey leg travel duration."""

    duration_basis: DurationBasis
    arrival_source: ArrivalSource
    initial_wait_included: bool | None = None


class NegotiatedRouteLeg(RouteLeg):
    """Journey Leg with timing provenance and failure reasons for negotiated contracts."""

    departure_at: datetime | None = None
    timing: LegTiming
    reasons: list[CommuteReason] = Field(default_factory=list)


class DirectRoute(BaseModel):
    """Direct home to workplace alternative route baseline."""

    status: DirectRouteStatus
    legs: list[NegotiatedRouteLeg] = Field(default_factory=list)
    totals: RouteTotals | None = None
    reasons: list[CommuteReason] = Field(default_factory=list)


class ComparisonCertification(BaseModel):
    """Certified duration comparability metadata."""

    duration_basis: ComparisonDurationBasis
    provider_options_id: str


class CommuteComparison(BaseModel):
    """Commute route comparison eligibility and certification."""

    status: ComparisonStatus
    reasons: list[CommuteReason] = Field(default_factory=list)
    certification: ComparisonCertification | None = None


class SampleConditions(BaseModel):
    """Identifies departure timing basis and provider options for a sample."""

    conditions_id: str
    timing_basis: TimingBasis
    initial_departure_at: datetime | None = None
    provider_options_id: str


class CommuteSample(BaseModel):
    """Commute evaluation sample for a representative time or Commute Route."""

    kind: SampleKind
    sampled_at: datetime | None = None
    legs: list[RouteLeg] = Field(default_factory=list)
    totals: RouteTotals | None = None


class NegotiatedCommuteSample(CommuteSample):
    """Sample evaluation with conditions, via status, direct route, and comparison."""

    legs: list[NegotiatedRouteLeg] = Field(default_factory=list)  # type: ignore[assignment]
    conditions: SampleConditions
    via_status: ViaStatus
    reasons: list[CommuteReason] = Field(default_factory=list)
    direct_route: DirectRoute | None = None
    comparison: CommuteComparison | None = None


class CommuteSamplingRequest(BaseModel):
    """Negotiated public transport sampling request parameters."""

    policy: SamplingPolicy
    cycle_id: str | None = None
    sample_index: SampleIndex


class CommuteRequest(BaseModel):
    """Request payload for personalised centre commute comparison."""

    profile_id: int | str
    mode: CommuteMode = "drive"
    sample_pair: list[str] | None = None
    include_direct_baseline: bool = False
    sampling: CommuteSamplingRequest | None = None

    @model_validator(mode="after")
    def validate_sampling_mutual_exclusion(self) -> "CommuteRequest":
        if self.sampling is not None and self.sample_pair is not None:
            raise ValueError(
                "sampling and sample_pair are mutually exclusive"
            )
        return self


class EvaluationMetadata(BaseModel):
    """Evaluation provenance, execution timestamp, and state revisions."""

    evaluation_id: str
    evaluated_at: datetime
    profile_revision: str
    anchor_revision: str


class AllowedSampleDefinition(BaseModel):
    """Definition of an allowed sample condition in a sampling cycle."""

    sample_index: SampleIndex
    conditions_id: str
    departure_at: datetime


class CommuteSamplingResponse(BaseModel):
    """Negotiated sampling metadata with stateless cycle tokens and allowed samples."""

    policy: SamplingPolicy
    cycle_id: str
    cycle_expires_at: datetime
    selected_index: SampleIndex
    allowed_samples: list[AllowedSampleDefinition]


class CommuteResponse(BaseModel):
    """Response payload for personalised centre commute comparison."""

    centre_id: int
    profile_id: int | str
    mode: CommuteMode
    sample_pair: list[str] | None = None
    anchors: dict[str, Coordinate | None] = Field(default_factory=dict)
    samples: list[CommuteSample] = Field(default_factory=list)


class NegotiatedCommuteResponse(BaseModel):
    """Full response payload for negotiated commute comparisons."""

    centre_id: int
    profile_id: int | str
    mode: CommuteMode
    anchors: dict[str, Coordinate | None] = Field(default_factory=dict)
    sample_pair: list[str] | None = None
    samples: list[NegotiatedCommuteSample] = Field(default_factory=list)
    comparison_contract: DirectBaselineContract | None = None
    sampling_contract: SamplingContract | None = None
    evaluation: EvaluationMetadata
    sampling: CommuteSamplingResponse | None = None

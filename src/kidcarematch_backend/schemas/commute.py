"""Pydantic schemas for OneMap commute display and routing comparisons."""

from datetime import datetime

from pydantic import BaseModel, Field


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
    """Individual journey leg between geographic anchors."""

    from_anchor: str
    to_anchor: str
    status: str
    source: str | None = None
    duration_seconds: float | None = None
    arrival_at: datetime | None = None
    distance_meters: float | None = None
    geometry: list[Coordinate] | None = None
    message: str | None = None
    transit_legs: list[TransitLeg] | None = None


class CommuteSample(BaseModel):
    """Commute evaluation sample for a representative time or single trip."""

    kind: str
    sampled_at: datetime | None = None
    legs: list[RouteLeg] = Field(default_factory=list)
    totals: RouteTotals | None = None


class CommuteRequest(BaseModel):
    """Request payload for personalised centre commute comparison."""

    profile_id: int | str
    mode: str = "drive"
    sample_pair: list[str] | None = None


class CommuteResponse(BaseModel):
    """Response payload for personalised centre commute comparison."""

    centre_id: int
    profile_id: int | str
    mode: str
    sample_pair: list[str] | None = None
    anchors: dict[str, Coordinate | None] = Field(default_factory=dict)
    samples: list[CommuteSample] = Field(default_factory=list)

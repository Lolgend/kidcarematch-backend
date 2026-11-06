"""Pydantic schemas for OneMap commute display and routing comparisons."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AnchorName = Literal["home", "centre", "workplace"]
LegStatus = Literal["available", "unavailable"]
CommuteMode = Literal["drive", "pt", "walk", "cycle"]
SampleKind = Literal["single", "weekday", "weekend"]


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


class CommuteSample(BaseModel):
    """Commute evaluation sample for a representative time or Commute Route."""

    kind: SampleKind
    sampled_at: datetime | None = None
    legs: list[RouteLeg] = Field(default_factory=list)
    totals: RouteTotals | None = None


class CommuteRequest(BaseModel):
    """Request payload for personalised centre commute comparison."""

    profile_id: int | str
    mode: CommuteMode = "drive"
    sample_pair: list[str] | None = None


class CommuteResponse(BaseModel):
    """Response payload for personalised centre commute comparison."""

    centre_id: int
    profile_id: int | str
    mode: CommuteMode
    sample_pair: list[str] | None = None
    anchors: dict[str, Coordinate | None] = Field(default_factory=dict)
    samples: list[CommuteSample] = Field(default_factory=list)

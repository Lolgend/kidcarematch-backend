"""Pydantic schemas for OneMap commute display and routing comparisons."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AnchorName = Literal["home", "centre", "workplace"]
LegStatus = Literal["available", "unavailable"]
CommuteMode = Literal["drive", "pt", "walk", "cycle"]
SampleKind = Literal["single", "weekday", "weekend"]


class Coordinate(BaseModel):
    lat: float
    lng: float


class RouteTotals(BaseModel):
    duration_seconds: float
    distance_meters: float


class TransitLeg(BaseModel):
    mode: str
    duration_seconds: float | None = None
    distance_meters: float | None = None
    route: str | None = None
    instruction: str | None = None
    geometry: list[Coordinate] | None = None


class RouteLeg(BaseModel):
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
    kind: str
    sampled_at: datetime | None = None
    legs: list[RouteLeg] = Field(default_factory=list)
    totals: RouteTotals | None = None


class CommuteRequest(BaseModel):
    profile_id: int | str
    mode: str = "drive"
    sample_pair: list[str] | None = None


class CommuteResponse(BaseModel):
    centre_id: int
    profile_id: int | str
    mode: str
    sample_pair: list[str] | None = None
    anchors: dict[str, Coordinate | None] = Field(default_factory=dict)
    samples: list[CommuteSample] = Field(default_factory=list)

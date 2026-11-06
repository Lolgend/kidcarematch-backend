"""Pydantic schemas for OneMap commute display and routing comparisons."""

from pydantic import BaseModel


class Coordinate(BaseModel):
    """Geographic coordinate representation."""

    lat: float
    lng: float

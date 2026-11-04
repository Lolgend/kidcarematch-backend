"""OneMap geospatial adapter for coordinate resolution, caching, and detour routing (ADR-0007, Ticket #70)."""

import logging
import math
from collections.abc import Sequence
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, col, select

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import PostalCoordinateCache

logger = logging.getLogger(__name__)

STAGE1_MAX_CANDIDATES = 30
EARTH_RADIUS_KM = 6371.0
AVERAGE_TRANSIT_SPEED_KMH = 25.0


def haversine_distance_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Calculate great-circle straight-line distance between two coordinates in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(EARTH_RADIUS_KM * c, 3)


def estimate_transit_duration_mins(distance_km: float) -> float:
    """Estimate public transit commute travel duration in minutes based on straight-line distance."""
    if distance_km <= 0.0:
        return 0.0
    duration_mins = (distance_km / AVERAGE_TRANSIT_SPEED_KMH) * 60.0
    return max(1.0, round(duration_mins, 1))


def resolve_postal_coordinates(
    postal: str | None,
    session: Session,
    client: httpx.Client | None = None,
) -> tuple[float, float] | None:
    """Resolve a 6-digit Singapore postal code to (latitude, longitude) coordinates.

    Checks persistent database cache (PostalCoordinateCache) first.
    If not cached, queries OneMap search API and persists the resolved coordinates.
    """
    if not postal:
        return None
    clean_postal = postal.strip()
    if len(clean_postal) != 6 or not clean_postal.isdigit():
        return None

    # 1. Query persistent database cache
    cached = session.get(PostalCoordinateCache, clean_postal)
    if cached is not None:
        return cached.latitude, cached.longitude

    # 2. Query external OneMap Search API
    params: dict[str, str | int] = {
        "searchVal": clean_postal,
        "returnGeom": "Y",
        "getAddrDetails": "Y",
        "pageNum": 1,
    }

    try:
        if client is not None:
            response = client.get(
                settings.ONEMAP_API_URL, params=params, timeout=5.0
            )
            response.raise_for_status()
            data = response.json()
        else:
            with httpx.Client(timeout=5.0) as http_client:
                response = http_client.get(
                    settings.ONEMAP_API_URL, params=params
                )
                response.raise_for_status()
                data = response.json()

        if data.get("found", 0) > 0 and data.get("results"):
            first = data["results"][0]
            lat = float(first["LATITUDE"])
            lon = float(first["LONGITUDE"])
            x_val = (
                float(first["X"])
                if first.get("X") and str(first["X"]).upper() != "NIL"
                else None
            )
            y_val = (
                float(first["Y"])
                if first.get("Y") and str(first["Y"]).upper() != "NIL"
                else None
            )
            address = first.get("ADDRESS")

            cache_entry = PostalCoordinateCache(
                postal_code=clean_postal,
                latitude=lat,
                longitude=lon,
                x_coord=x_val,
                y_coord=y_val,
                address=address,
            )
            session.add(cache_entry)
            session.commit()
            session.refresh(cache_entry)
            return lat, lon
    except (
        httpx.HTTPError,
        httpx.TimeoutException,
        ValueError,
        KeyError,
        OSError,
    ) as exc:
        logger.warning(
            "Failed to resolve coordinates for postal %s via OneMap: %s",
            clean_postal,
            exc,
        )

    return None


def resolve_coordinates_batch(
    postals: Sequence[str | None],
    session: Session,
    client: httpx.Client | None = None,
) -> dict[str, tuple[float, float]]:
    """Batch resolve coordinates for a list of postal codes, utilizing persistent cache."""
    valid_postals = {
        p.strip()
        for p in postals
        if p and len(p.strip()) == 6 and p.strip().isdigit()
    }
    if not valid_postals:
        return {}

    # Query existing records from persistent cache
    cached_records = session.exec(
        select(PostalCoordinateCache).where(
            col(PostalCoordinateCache.postal_code).in_(valid_postals)
        )
    ).all()

    result_map: dict[str, tuple[float, float]] = {
        rec.postal_code: (rec.latitude, rec.longitude)
        for rec in cached_records
    }

    # Fetch missing postals
    missing_postals = valid_postals - set(result_map.keys())
    for postal in missing_postals:
        coords = resolve_postal_coordinates(postal, session=session, client=client)
        if coords is not None:
            result_map[postal] = coords

    return result_map


def get_route_duration_and_distance(
    start: tuple[float, float],
    end: tuple[float, float],
    client: httpx.Client | None = None,
) -> tuple[float, float]:
    """Calculate commute route travel duration (minutes) and distance (km) between two coordinates.

    Calls OneMap Public Transport Routing API (`routeType=pt`).
    Falls back gracefully to straight-line distance and estimated transit time if external API fails.
    """
    straight_dist_km = haversine_distance_km(start[0], start[1], end[0], end[1])
    if straight_dist_km < 0.001:
        return 0.0, 0.0

    params: dict[str, str | int] = {
        "start": f"{start[0]},{start[1]}",
        "end": f"{end[0]},{end[1]}",
        "routeType": "pt",
        "date": datetime.now(timezone.utc).strftime("%m-%d-%Y"),
        "time": "08:00:00",
        "mode": "transit",
        "maxWalkDistance": 1000,
        "numItineraries": 1,
    }
    headers: dict[str, str] = {}
    if settings.ONEMAP_API_TOKEN:
        headers["Authorization"] = f"Bearer {settings.ONEMAP_API_TOKEN}"

    try:
        if client is not None:
            response = client.get(
                settings.ONEMAP_ROUTING_API_URL,
                params=params,
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            data = response.json()
        else:
            with httpx.Client(timeout=5.0) as http_client:
                response = http_client.get(
                    settings.ONEMAP_ROUTING_API_URL,
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

        # Parse Public Transport itinerary response
        plan = data.get("plan")
        if plan and plan.get("itineraries"):
            first_itinerary = plan["itineraries"][0]
            duration_secs = first_itinerary.get("duration", 0)
            duration_mins = duration_secs / 60.0
            return round(duration_mins, 1), round(straight_dist_km, 2)

        # Parse Alternative route_summary format
        summary = data.get("route_summary")
        if summary:
            duration_secs = summary.get("total_time", 0)
            dist_meters = summary.get("total_distance", 0)
            return round(duration_secs / 60.0, 1), round(dist_meters / 1000.0, 2)

    except (
        httpx.HTTPError,
        httpx.TimeoutException,
        ValueError,
        KeyError,
        OSError,
    ) as exc:
        logger.warning(
            "OneMap Routing API call failed (%s -> %s), falling back to straight-line: %s",
            start,
            end,
            exc,
        )

    # Graceful fallback: straight-line estimation
    est_duration_mins = estimate_transit_duration_mins(straight_dist_km)
    return round(est_duration_mins, 1), round(straight_dist_km, 2)


def normalize_travel_fit(deviations: list[float]) -> list[float]:
    """Pool-relative Travel Fit normalization (ADR-0007).

    Travel Fit = 100.0 * (1.0 - (dev - dev_min) / (dev_max - dev_min))
    - If dev_max == dev_min: all candidates receive 100.0%
    - Lowest deviation receives 100.0%
    - Largest deviation receives 0.0%
    - Intermediate candidates scale linearly
    """
    if not deviations:
        return []

    dev_min = min(deviations)
    dev_max = max(deviations)

    if math.isclose(dev_max, dev_min, abs_tol=1e-5):
        return [100.0] * len(deviations)

    spread = dev_max - dev_min
    return [
        round(max(0.0, min(100.0, 100.0 * (1.0 - (dev - dev_min) / spread))), 2)
        for dev in deviations
    ]

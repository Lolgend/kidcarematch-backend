"""Strict OneMap geospatial adapter for commute route display (separate from matching)."""

import logging
from datetime import datetime

import httpx

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.schemas.commute import Coordinate, RouteLeg

logger = logging.getLogger(__name__)

# Bounded in-memory cache for verified exact-postcode resolutions
_COMMUTE_POSTAL_CACHE: dict[str, Coordinate] = {}


def clear_commute_postal_cache() -> None:
    """Clear in-memory cache of verified postal resolutions (useful for test isolation)."""
    _COMMUTE_POSTAL_CACHE.clear()


def decode_polyline(polyline_str: str) -> list[Coordinate]:
    """Decode Google encoded polyline string (factor 1e5) into a list of Coordinate(lat, lng).

    Returns an empty list if polyline_str is empty or malformed.
    """
    if not polyline_str or not isinstance(polyline_str, str):
        return []

    points: list[Coordinate] = []
    index = 0
    lat = 0
    lng = 0
    length = len(polyline_str)

    try:
        while index < length:
            # Decode latitude difference
            shift = 0
            result = 0
            while index < length:
                byte = ord(polyline_str[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            dlat = ~(result >> 1) if (result & 1) else (result >> 1)
            lat += dlat

            # Decode longitude difference
            shift = 0
            result = 0
            while index < length:
                byte = ord(polyline_str[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            dlng = ~(result >> 1) if (result & 1) else (result >> 1)
            lng += dlng

            lat_deg = round(lat / 1e5, 6)
            lng_deg = round(lng / 1e5, 6)
            if not (-90.0 <= lat_deg <= 90.0 and -180.0 <= lng_deg <= 180.0):
                logger.warning(
                    "Decoded coordinate out of bounds: lat=%s, lng=%s",
                    lat_deg,
                    lng_deg,
                )
                return []

            points.append(
                Coordinate(
                    lat=lat_deg,
                    lng=lng_deg,
                )
            )
    except (IndexError, ValueError, KeyError, TypeError, OSError) as exc:
        logger.warning("Failed to decode polyline: %s", exc)
        return []

    return points


def resolve_commute_postal(
    postal: str | None,
    client: httpx.Client | None = None,
) -> Coordinate | None:
    """Resolve a 6-digit Singapore postal code to Coordinate via OneMap search.

    Strict contract: requires exact match on returned POSTAL attribute.
    Relevance-ranked fuzzy matches with different postals are discarded.
    """
    if not postal:
        return None
    clean_postal = postal.strip()
    if len(clean_postal) != 6 or not clean_postal.isdigit():
        return None

    if clean_postal in _COMMUTE_POSTAL_CACHE:
        return _COMMUTE_POSTAL_CACHE[clean_postal]

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
            for result in data["results"]:
                if str(result.get("POSTAL", "")).strip() == clean_postal:
                    lat = float(result["LATITUDE"])
                    lng = float(result["LONGITUDE"])
                    coord = Coordinate(lat=lat, lng=lng)
                    _COMMUTE_POSTAL_CACHE[clean_postal] = coord
                    return coord
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
    ) as exc:
        logger.warning(
            "Failed to resolve commute postal %s via OneMap: %s",
            clean_postal,
            exc,
        )

    return None


def get_commute_leg(
    from_anchor: str,
    to_anchor: str,
    start: Coordinate,
    end: Coordinate,
    mode: str = "drive",
    departure: datetime | None = None,
    client: httpx.Client | None = None,
) -> RouteLeg:
    """Query OneMap routing endpoint for a single commute journey leg.

    Strict contract:
    - Normalizes route geometry, duration in seconds, distance in meters.
    - If routing fails or geometry is missing, returns status='unavailable' with null metrics.
    - NEVER returns straight-line fallback.
    """
    params: dict[str, str] = {
        "start": f"{start.lat},{start.lng}",
        "end": f"{end.lat},{end.lng}",
        "routeType": mode,
    }

    token = settings.ONEMAP_API_TOKEN or settings.ONEMAP_API_KEY
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = (
            token if token.startswith("Bearer ") else f"Bearer {token}"
        )

    try:
        if client is not None:
            response = client.get(
                settings.ONEMAP_ROUTING_API_URL,
                params=params,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
        else:
            with httpx.Client(timeout=10.0) as http_client:
                response = http_client.get(
                    settings.ONEMAP_ROUTING_API_URL,
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

        route_geometry = data.get("route_geometry")
        if not route_geometry or not isinstance(route_geometry, str):
            return RouteLeg(
                from_anchor=from_anchor,
                to_anchor=to_anchor,
                status="unavailable",
                source=None,
                duration_seconds=None,
                distance_meters=None,
                geometry=None,
                message="Route geometry unavailable",
            )

        coords = decode_polyline(route_geometry)
        if not coords:
            return RouteLeg(
                from_anchor=from_anchor,
                to_anchor=to_anchor,
                status="unavailable",
                source=None,
                duration_seconds=None,
                distance_meters=None,
                geometry=None,
                message="Decoded route geometry is empty",
            )

        summary = data.get("route_summary")
        if not summary or not isinstance(summary, dict):
            return RouteLeg(
                from_anchor=from_anchor,
                to_anchor=to_anchor,
                status="unavailable",
                source=None,
                duration_seconds=None,
                distance_meters=None,
                geometry=None,
                message="Route summary unavailable",
            )

        duration_secs = float(summary.get("total_time", 0))
        dist_meters = float(summary.get("total_distance", 0))

        return RouteLeg(
            from_anchor=from_anchor,
            to_anchor=to_anchor,
            status="available",
            source="onemap",
            duration_seconds=duration_secs,
            distance_meters=dist_meters,
            geometry=coords,
            message=None,
        )

    except httpx.TimeoutException as exc:
        logger.warning(
            "OneMap routing timeout (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        return RouteLeg(
            from_anchor=from_anchor,
            to_anchor=to_anchor,
            status="unavailable",
            source=None,
            duration_seconds=None,
            distance_meters=None,
            geometry=None,
            message="Route request timed out",
        )
    except (
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
    ) as exc:
        logger.warning(
            "OneMap routing failed (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        return RouteLeg(
            from_anchor=from_anchor,
            to_anchor=to_anchor,
            status="unavailable",
            source=None,
            duration_seconds=None,
            distance_meters=None,
            geometry=None,
            message="Route unavailable",
        )

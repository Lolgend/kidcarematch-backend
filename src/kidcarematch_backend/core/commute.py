"""Strict OneMap geospatial adapter for commute route display (separate from matching)."""

import logging
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import httpx

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.schemas.commute import Coordinate, RouteLeg, TransitLeg

logger = logging.getLogger(__name__)

SGT: tzinfo
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        SGT = ZoneInfo("Asia/Singapore")
    except (ZoneInfoNotFoundError, OSError, KeyError, ValueError):
        SGT = timezone(timedelta(hours=8))
except ImportError:
    SGT = timezone(timedelta(hours=8))

# Bounded in-memory cache for verified exact-postcode resolutions
_COMMUTE_POSTAL_CACHE: dict[str, Coordinate] = {}


def get_next_representative_wednesday(now: datetime | None = None) -> datetime:
    """Calculate the next upcoming Wednesday at 08:00 Asia/Singapore.

    - If today is Wednesday before 08:00 SGT, use today 08:00 SGT.
    - If today is Wednesday at or after 08:00 SGT, use next week Wednesday 08:00 SGT.
    - If today is any other day, use the next upcoming Wednesday at 08:00 SGT.
    """
    if now is None:
        now_sgt = datetime.now(SGT)
    elif now.tzinfo is None:
        now_sgt = now.replace(tzinfo=SGT)
    else:
        now_sgt = now.astimezone(SGT)

    target_weekday = 2  # Wednesday (Monday=0, Tuesday=1, Wednesday=2, ...)
    current_weekday = now_sgt.weekday()

    if current_weekday == target_weekday:
        if (now_sgt.hour, now_sgt.minute, now_sgt.second, now_sgt.microsecond) < (8, 0, 0, 0):
            days_ahead = 0
        else:
            days_ahead = 7
    else:
        days_ahead = (target_weekday - current_weekday) % 7

    target_date = (now_sgt + timedelta(days=days_ahead)).date()
    return datetime.combine(target_date, time(8, 0, 0), tzinfo=SGT)


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
    if mode == "pt":
        dep = departure or get_next_representative_wednesday()
        params: dict[str, str | int] = {
            "start": f"{start.lat},{start.lng}",
            "end": f"{end.lat},{end.lng}",
            "routeType": "pt",
            "date": dep.strftime("%m-%d-%Y"),
            "time": dep.strftime("%H:%M:%S"),
            "mode": "TRANSIT",
            "maxWalkDistance": 1000,
            "numItineraries": 1,
        }
    else:
        params = {
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

        # Handle Public Transport plan / itinerary structure
        if "plan" in data:
            plan = data.get("plan")
            itineraries = plan.get("itineraries") if plan else None
            if not itineraries:
                return RouteLeg(
                    from_anchor=from_anchor,
                    to_anchor=to_anchor,
                    status="unavailable",
                    source=None,
                    duration_seconds=None,
                    distance_meters=None,
                    geometry=None,
                    message="No public transport itinerary found",
                )
            itinerary = itineraries[0]
            raw_legs = itinerary.get("legs") or []
            transit_legs: list[TransitLeg] = []
            all_coords: list[Coordinate] = []

            for raw_leg in raw_legs:
                sub_mode = str(raw_leg.get("mode", "TRANSIT")).upper()
                sub_duration = float(raw_leg.get("duration", 0))
                sub_distance = float(raw_leg.get("distance", 0))
                sub_route = (
                    raw_leg.get("route")
                    or raw_leg.get("routeShortName")
                    or raw_leg.get("routeLongName")
                )
                if sub_route:
                    sub_route = str(sub_route)

                geom_val = raw_leg.get("legGeometry")
                if isinstance(geom_val, dict):
                    geom_poly = geom_val.get("points") or ""
                elif isinstance(geom_val, str):
                    geom_poly = geom_val
                else:
                    geom_poly = ""

                sub_coords = decode_polyline(geom_poly) if geom_poly else []
                if sub_coords:
                    all_coords.extend(sub_coords)

                instruction = raw_leg.get("instruction")
                if not instruction:
                    if sub_mode == "WALK":
                        dist_fmt = (
                            f"{int(sub_distance)} m"
                            if sub_distance < 1000
                            else f"{sub_distance / 1000:.1f} km"
                        )
                        instruction = f"Walk ({dist_fmt})"
                    elif sub_route:
                        instruction = f"Take {sub_mode.title()} {sub_route}"
                    else:
                        instruction = f"Take {sub_mode.title()}"

                transit_legs.append(
                    TransitLeg(
                        mode=sub_mode,
                        duration_seconds=sub_duration,
                        distance_meters=sub_distance,
                        route=sub_route,
                        instruction=instruction,
                        geometry=sub_coords if sub_coords else None,
                    )
                )

            if not all_coords:
                top_poly = itinerary.get("route_geometry") or data.get(
                    "route_geometry"
                )
                if top_poly and isinstance(top_poly, str):
                    all_coords = decode_polyline(top_poly)

            if not all_coords:
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

            duration_secs = float(
                itinerary.get(
                    "duration",
                    sum(t.duration_seconds or 0 for t in transit_legs),
                )
            )
            total_dist = sum(t.distance_meters or 0 for t in transit_legs)
            if total_dist == 0 and "walkDistance" in itinerary:
                total_dist = float(itinerary.get("walkDistance", 0))

            return RouteLeg(
                from_anchor=from_anchor,
                to_anchor=to_anchor,
                status="available",
                source="onemap",
                duration_seconds=duration_secs,
                distance_meters=total_dist,
                geometry=all_coords,
                message=None,
                transit_legs=transit_legs if transit_legs else None,
            )

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

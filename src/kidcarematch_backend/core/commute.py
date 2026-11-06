"""Strict OneMap geospatial adapter for commute route and centre location display (ADR-0008, ADR-0009)."""

import logging
import math
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.core.onemap import onemap_token_manager
from kidcarematch_backend.db.db import PostalCoordinateCache
from kidcarematch_backend.schemas.commute import Coordinate, RouteLeg

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
_COMMUTE_POSTAL_CACHE_MAX = 512
_COMMUTE_POSTAL_CACHE: dict[str, Coordinate] = {}


def clear_commute_postal_cache() -> None:
    """Clear in-memory cache of verified postal resolutions (useful for test isolation)."""
    _COMMUTE_POSTAL_CACHE.clear()


def _get_auth_headers(client: httpx.Client | None = None) -> dict[str, str]:
    """Retrieve auth headers from onemap_token_manager with static fallback."""
    headers = onemap_token_manager.get_auth_headers(client=client)
    if not headers:
        static_token = settings.ONEMAP_API_TOKEN or getattr(
            settings, "ONEMAP_API_KEY", None
        )
        if static_token:
            clean = static_token.removeprefix("Bearer ").strip()
            headers = {"Authorization": f"Bearer {clean}"}
    return headers


def _fetch_onemap(
    url: str,
    params: dict[str, Any],
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    """Execute OneMap API request with dynamic auth headers and reactive 401 retry."""
    headers = _get_auth_headers(client=client)
    if client is not None:
        response = client.get(url, params=params, headers=headers, timeout=timeout)
        if response.status_code == 401:
            onemap_token_manager.invalidate_token()
            retry_headers = _get_auth_headers(client=client)
            response = client.get(
                url, params=params, headers=retry_headers, timeout=timeout
            )
        return response

    with httpx.Client(timeout=timeout) as http_client:
        response = http_client.get(url, params=params, headers=headers)
        if response.status_code == 401:
            onemap_token_manager.invalidate_token()
            retry_headers = _get_auth_headers(client=http_client)
            response = http_client.get(url, params=params, headers=retry_headers)
        return response


def resolve_commute_postal(
    postal: str | None,
    session: Session | None = None,
    client: httpx.Client | None = None,
) -> Coordinate | None:
    """Resolve a 6-digit Singapore postal code to Coordinate via OneMap search.

    Strict contract: requires exact match on returned POSTAL attribute.
    Approximate search results with differing postals are rejected.
    Verified coordinates are cached in _COMMUTE_POSTAL_CACHE and upserted into
    database PostalCoordinateCache if session is provided.
    Legacy unverified database cache rows are not trusted on cache misses.
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
        response = _fetch_onemap(settings.ONEMAP_API_URL, params=params, client=client)
        response.raise_for_status()
        data = response.json()

        if data.get("found", 0) > 0 and data.get("results"):
            for result in data["results"]:
                if str(result.get("POSTAL", "")).strip() == clean_postal:
                    lat = float(result["LATITUDE"])
                    lng = float(result["LONGITUDE"])
                    if not (
                        math.isfinite(lat)
                        and math.isfinite(lng)
                        and -90 <= lat <= 90
                        and -180 <= lng <= 180
                    ):
                        raise ValueError("OneMap returned invalid WGS84 coordinates")
                    coord = Coordinate(lat=lat, lng=lng)
                    _COMMUTE_POSTAL_CACHE[clean_postal] = coord
                    if len(_COMMUTE_POSTAL_CACHE) > _COMMUTE_POSTAL_CACHE_MAX:
                        _COMMUTE_POSTAL_CACHE.pop(next(iter(_COMMUTE_POSTAL_CACHE)))

                    if session is not None:
                        try:
                            db_entry = session.get(PostalCoordinateCache, clean_postal)
                            x_val = (
                                float(result["X"])
                                if result.get("X") and str(result["X"]).upper() != "NIL"
                                else None
                            )
                            y_val = (
                                float(result["Y"])
                                if result.get("Y") and str(result["Y"]).upper() != "NIL"
                                else None
                            )
                            address_val = result.get("ADDRESS")
                            if db_entry is not None:
                                db_entry.latitude = lat
                                db_entry.longitude = lng
                                db_entry.x_coord = x_val
                                db_entry.y_coord = y_val
                                db_entry.address = address_val
                                db_entry.last_updated = datetime.now(timezone.utc)
                                session.add(db_entry)
                            else:
                                db_entry = PostalCoordinateCache(
                                    postal_code=clean_postal,
                                    latitude=lat,
                                    longitude=lng,
                                    x_coord=x_val,
                                    y_coord=y_val,
                                    address=address_val,
                                )
                                session.add(db_entry)
                            session.commit()
                        except (SQLAlchemyError, OSError) as exc:
                            session.rollback()
                            logger.warning(
                                "Failed to upsert PostalCoordinateCache for %s: %s",
                                clean_postal,
                                exc,
                            )

                    return coord
    except (
        httpx.HTTPError,
        httpx.TimeoutException,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
    ) as exc:
        logger.warning(
            "Failed to resolve coordinates for postal %s via OneMap: %s",
            clean_postal,
            exc,
        )

    return None


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

    def read_value() -> int:
        nonlocal index
        shift = 0
        result = 0
        while index < length:
            byte = ord(polyline_str[index]) - 63
            index += 1
            if byte < 0 or byte > 0x3F:
                raise ValueError("Invalid encoded polyline character")
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                return ~(result >> 1) if result & 1 else result >> 1
        raise ValueError("Truncated encoded polyline")

    try:
        while index < length:
            lat += read_value()
            lng += read_value()

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


def get_representative_day(
    target_weekday: int, now: datetime | None = None
) -> datetime:
    """Calculate the next upcoming target weekday at 08:00 Asia/Singapore."""
    if now is None:
        now_sgt = datetime.now(SGT)
    elif now.tzinfo is None:
        now_sgt = now.replace(tzinfo=SGT)
    else:
        now_sgt = now.astimezone(SGT)

    current_weekday = now_sgt.weekday()

    if current_weekday == target_weekday:
        if (now_sgt.hour, now_sgt.minute, now_sgt.second, now_sgt.microsecond) < (
            8,
            0,
            0,
            0,
        ):
            days_ahead = 0
        else:
            days_ahead = 7
    else:
        days_ahead = (target_weekday - current_weekday) % 7

    target_date = (now_sgt + timedelta(days=days_ahead)).date()
    return datetime.combine(target_date, time(8, 0, 0), tzinfo=SGT)


def get_next_representative_wednesday(now: datetime | None = None) -> datetime:
    """Calculate the next upcoming Wednesday at 08:00 Asia/Singapore."""
    return get_representative_day(2, now)


def get_next_representative_saturday(now: datetime | None = None) -> datetime:
    """Calculate the next upcoming Saturday at 08:00 Asia/Singapore."""
    return get_representative_day(5, now)


def get_representative_sample_pair(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Calculate representative sample pair: next upcoming Wednesday at 08:00 SGT and Saturday at 08:00 SGT."""
    return (
        get_next_representative_wednesday(now),
        get_next_representative_saturday(now),
    )


def validate_sample_pair(
    sample_pair: list[str] | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Validate client-provided sample_pair or return fresh representative pair.

    If sample_pair is provided:
    - Must be a 2-element list of ISO timestamp strings.
    - Timestamps must correspond to Wednesday and Saturday at 08:00 SGT.
    - Neither timestamp can be in the past relative to now_sgt.
    - Both timestamps must be within the upcoming 7-day window.
    Raises HTTPException 409 if expired, stale, or malformed.
    """
    if now is None:
        now_sgt = datetime.now(SGT)
    elif now.tzinfo is None:
        now_sgt = now.replace(tzinfo=SGT)
    else:
        now_sgt = now.astimezone(SGT)

    if not sample_pair:
        return get_representative_sample_pair(now_sgt)

    if not isinstance(sample_pair, (list, tuple)) or len(sample_pair) != 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "sample_expired",
                "message": "Representative sample pair expired",
            },
        )

    try:
        dt1 = datetime.fromisoformat(sample_pair[0])
        dt2 = datetime.fromisoformat(sample_pair[1])
        dt1_sgt = dt1.replace(tzinfo=SGT) if dt1.tzinfo is None else dt1.astimezone(SGT)
        dt2_sgt = dt2.replace(tzinfo=SGT) if dt2.tzinfo is None else dt2.astimezone(SGT)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "sample_expired",
                "message": "Representative sample pair expired",
            },
        )

    # Check expired (in past)
    if dt1_sgt < now_sgt or dt2_sgt < now_sgt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "sample_expired",
                "message": "Representative sample pair expired",
            },
        )

    # Check stale (beyond 7 days window)
    max_window = now_sgt + timedelta(days=7, hours=1)
    if dt1_sgt > max_window or dt2_sgt > max_window:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "sample_expired",
                "message": "Representative sample pair expired",
            },
        )

    # Check Wednesday (weekday 2) and Saturday (weekday 5) at 08:00:00.000000
    if (
        dt1_sgt.weekday() != 2
        or dt1_sgt.hour != 8
        or dt1_sgt.minute != 0
        or dt1_sgt.second != 0
        or dt1_sgt.microsecond != 0
        or dt2_sgt.weekday() != 5
        or dt2_sgt.hour != 8
        or dt2_sgt.minute != 0
        or dt2_sgt.second != 0
        or dt2_sgt.microsecond != 0
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "sample_expired",
                "message": "Representative sample pair expired",
            },
        )

    return dt1_sgt, dt2_sgt


def _required_metric(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if value is None or isinstance(value, bool):
        raise ValueError(f"Missing route metric: {key}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Invalid route metric: {key}")
    return number


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
    params: dict[str, Any] = {
        "start": f"{start.lat},{start.lng}",
        "end": f"{end.lat},{end.lng}",
        "routeType": mode,
    }

    try:
        response = _fetch_onemap(
            settings.ONEMAP_ROUTING_API_URL,
            params=params,
            client=client,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise TypeError("Malformed OneMap routing response")

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

        duration_secs = _required_metric(summary, "total_time")
        dist_meters = _required_metric(summary, "total_distance")

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
        AttributeError,
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

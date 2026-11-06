"""Strict OneMap geospatial adapter for commute route and centre location display (ADR-0008, ADR-0009)."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.core.onemap import onemap_token_manager
from kidcarematch_backend.db.db import PostalCoordinateCache
from kidcarematch_backend.schemas.commute import (
    AnchorName,
    CommuteMode,
    CommuteSample,
    Coordinate,
    RouteLeg,
    RouteTotals,
    SampleKind,
    TransitLeg,
)

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


def _execute_get_with_retry(
    http_client: httpx.Client,
    url: str,
    params: dict[str, Any],
    timeout: float,
) -> httpx.Response:
    """Execute GET request with OneMap auth headers, invalidating and retrying once on 401."""
    headers = onemap_token_manager.get_auth_headers(client=http_client)
    response = http_client.get(url, params=params, headers=headers, timeout=timeout)
    if response.status_code == 401:
        onemap_token_manager.invalidate_token()
        retry_headers = onemap_token_manager.get_auth_headers(client=http_client)
        response = http_client.get(
            url, params=params, headers=retry_headers, timeout=timeout
        )
    return response


def _fetch_onemap(
    url: str,
    params: dict[str, Any],
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    """Execute OneMap API request with dynamic auth headers and reactive 401 retry."""
    if client is not None:
        return _execute_get_with_retry(client, url, params, timeout)

    with httpx.Client(timeout=timeout) as http_client:
        return _execute_get_with_retry(http_client, url, params, timeout)


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

    if sample_pair is None:
        return get_representative_sample_pair(now_sgt)

    error_detail = {
        "code": "sample_expired",
        "message": "Representative sample pair has expired",
    }

    if not isinstance(sample_pair, (list, tuple)) or len(sample_pair) != 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail,
        )

    try:
        dt1 = datetime.fromisoformat(sample_pair[0])
        dt2 = datetime.fromisoformat(sample_pair[1])
        dt1_sgt = dt1.replace(tzinfo=SGT) if dt1.tzinfo is None else dt1.astimezone(SGT)
        dt2_sgt = dt2.replace(tzinfo=SGT) if dt2.tzinfo is None else dt2.astimezone(SGT)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail,
        )

    # Check expired (in past)
    if dt1_sgt < now_sgt or dt2_sgt < now_sgt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail,
        )

    # Must match the active upcoming Wednesday and Saturday 08:00 SGT sampling window
    expected_wed, expected_sat = get_representative_sample_pair(now_sgt)
    if dt1_sgt != expected_wed or dt2_sgt != expected_sat:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail,
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


def _unavailable_leg(
    from_anchor: AnchorName, to_anchor: AnchorName, message: str
) -> RouteLeg:
    return RouteLeg(
        from_anchor=from_anchor,
        to_anchor=to_anchor,
        status="unavailable",
        source=None,
        duration_seconds=None,
        distance_meters=None,
        geometry=None,
        message=message,
    )


def _itinerary_arrival(
    itinerary: dict[str, Any], departure: datetime | None, duration_seconds: float
) -> tuple[datetime | None, float | None]:
    depart = None
    if departure is not None:
        depart = (
            departure.replace(tzinfo=SGT)
            if departure.tzinfo is None
            else departure.astimezone(SGT)
        )
    raw_end = itinerary.get("endTime")
    if raw_end is not None and not isinstance(raw_end, bool):
        try:
            end_ms = float(raw_end)
            if math.isfinite(end_ms) and end_ms > 0:
                arrival = datetime.fromtimestamp(
                    end_ms / 1000, tz=timezone.utc
                ).astimezone(SGT)
                if depart is None:
                    return arrival, None
                elapsed = (arrival - depart).total_seconds()
                if duration_seconds <= elapsed <= 24 * 60 * 60:
                    return arrival, elapsed
        except (OverflowError, OSError, TypeError, ValueError):
            pass

    if depart is not None:
        arrival = depart + timedelta(seconds=duration_seconds)
        return arrival, duration_seconds
    return None, None


def get_commute_leg(
    from_anchor: AnchorName,
    to_anchor: AnchorName,
    start: Coordinate,
    end: Coordinate,
    mode: CommuteMode = "drive",
    departure: datetime | None = None,
    client: httpx.Client | None = None,
) -> RouteLeg:
    """Query OneMap routing endpoint for a single commute journey leg.

    Strict contract:
    - Normalizes route geometry, duration in seconds, distance in meters.
    - If routing fails or geometry is missing, returns status='unavailable' with null metrics.
    - NEVER returns straight-line fallback.
    """
    effective_departure: datetime | None = departure
    if mode == "pt":
        dep = departure or get_next_representative_wednesday()
        effective_departure = dep
        params: dict[str, Any] = {
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

        if mode == "pt" and "plan" not in data:
            return _unavailable_leg(
                from_anchor, to_anchor, "Public transport plan unavailable"
            )

        if "plan" in data:
            plan = data.get("plan")
            if not isinstance(plan, dict):
                raise TypeError("Malformed public transport plan")
            itineraries = plan.get("itineraries")
            if not isinstance(itineraries, list):
                raise ValueError("Malformed public transport itineraries")
            if not itineraries:
                return _unavailable_leg(
                    from_anchor, to_anchor, "No public transport itinerary found"
                )
            itinerary = itineraries[0]
            if not isinstance(itinerary, dict):
                raise TypeError("Malformed public transport itinerary")
            raw_legs = itinerary.get("legs")
            if not isinstance(raw_legs, list) or not raw_legs:
                raise ValueError("Public transport itinerary legs unavailable")

            transit_legs: list[TransitLeg] = []
            all_coords: list[Coordinate] = []

            for raw_leg in raw_legs:
                if not isinstance(raw_leg, dict):
                    raise TypeError("Malformed public transport sub-leg")
                sub_mode = str(raw_leg.get("mode", "TRANSIT")).upper()
                sub_duration = _required_metric(raw_leg, "duration")
                sub_distance = _required_metric(raw_leg, "distance")
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
                if not sub_coords:
                    return _unavailable_leg(
                        from_anchor,
                        to_anchor,
                        "Route geometry unavailable for transit sub-leg",
                    )
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
                return _unavailable_leg(
                    from_anchor, to_anchor, "Route geometry unavailable"
                )

            duration_secs = _required_metric(itinerary, "duration")
            total_dist = sum(t.distance_meters or 0 for t in transit_legs)
            arrival_at, elapsed = _itinerary_arrival(
                itinerary, effective_departure, duration_secs
            )
            if elapsed is not None:
                duration_secs = elapsed

            return RouteLeg(
                from_anchor=from_anchor,
                to_anchor=to_anchor,
                status="available",
                source="onemap",
                duration_seconds=duration_secs,
                arrival_at=arrival_at,
                distance_meters=total_dist,
                geometry=all_coords,
                message=None,
                transit_legs=transit_legs if transit_legs else None,
            )

        route_geometry = data.get("route_geometry")
        if not route_geometry or not isinstance(route_geometry, str):
            return _unavailable_leg(
                from_anchor, to_anchor, "Route geometry unavailable"
            )

        coords = decode_polyline(route_geometry)
        if not coords:
            return _unavailable_leg(
                from_anchor, to_anchor, "Decoded route geometry is empty"
            )

        summary = data.get("route_summary")
        if not summary or not isinstance(summary, dict):
            return _unavailable_leg(from_anchor, to_anchor, "Route summary unavailable")

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
        return _unavailable_leg(from_anchor, to_anchor, "Route request timed out")
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
        return _unavailable_leg(from_anchor, to_anchor, "Route unavailable")


@dataclass
class CommuteAnchors:
    """Geographic anchors for commute route comparison."""

    home: Coordinate | None
    centre: Coordinate | None
    workplace: Coordinate | None = None
    has_workplace: bool = False

    def __post_init__(self) -> None:
        if self.workplace is not None and not self.has_workplace:
            self.has_workplace = True

    def to_dict(self) -> dict[str, Coordinate | None]:
        res: dict[str, Coordinate | None] = {
            "home": self.home,
            "centre": self.centre,
        }
        if self.has_workplace:
            res["workplace"] = self.workplace
        return res


def build_commute_sample(
    kind: SampleKind,
    sampled_at: datetime | None,
    mode: CommuteMode,
    anchors: CommuteAnchors,
    client: httpx.Client | None = None,
) -> CommuteSample:
    """Build a single CommuteSample with independent routing, failure handling, and totals."""
    legs: list[RouteLeg] = []
    leg1_departure = sampled_at if mode == "pt" else None

    # Leg 1: home -> centre
    if not anchors.home:
        leg1 = _unavailable_leg(
            "home",
            "centre",
            "Home postal code could not be resolved",
        )
    elif not anchors.centre:
        leg1 = _unavailable_leg(
            "home",
            "centre",
            "Centre postal code could not be resolved",
        )
    else:
        leg1 = get_commute_leg(
            from_anchor="home",
            to_anchor="centre",
            start=anchors.home,
            end=anchors.centre,
            mode=mode,
            departure=leg1_departure,
            client=client,
        )
    legs.append(leg1)

    # Leg 2: centre -> workplace (if workplace postal provided)
    if anchors.has_workplace:
        if not anchors.centre:
            leg2 = _unavailable_leg(
                "centre",
                "workplace",
                "Centre postal code could not be resolved",
            )
        elif not anchors.workplace:
            leg2 = _unavailable_leg(
                "centre",
                "workplace",
                "Workplace postal code could not be resolved",
            )
        elif mode == "pt" and (leg1.status != "available" or leg1.arrival_at is None):
            leg2 = _unavailable_leg(
                "centre",
                "workplace",
                "Onward leg departure cannot be determined because first leg arrival is unavailable",
            )
        else:
            leg2_departure = None
            if mode == "pt" and leg1.arrival_at is not None:
                leg2_departure = leg1.arrival_at

            leg2 = get_commute_leg(
                from_anchor="centre",
                to_anchor="workplace",
                start=anchors.centre,
                end=anchors.workplace,
                mode=mode,
                departure=leg2_departure,
                client=client,
            )
        legs.append(leg2)

    totals: RouteTotals | None = None
    if all(
        leg.status == "available"
        and leg.duration_seconds is not None
        and leg.distance_meters is not None
        for leg in legs
    ):
        totals = RouteTotals(
            duration_seconds=sum(
                leg.duration_seconds for leg in legs if leg.duration_seconds is not None
            ),
            distance_meters=sum(
                leg.distance_meters for leg in legs if leg.distance_meters is not None
            ),
        )

    return CommuteSample(
        kind=kind,
        sampled_at=sampled_at,
        legs=legs,
        totals=totals,
    )


def build_commute_samples(
    mode: CommuteMode,
    anchors: CommuteAnchors,
    sample_pair: tuple[datetime, datetime],
    client: httpx.Client | None = None,
) -> list[CommuteSample]:
    """Orchestrate commute samples based on travel mode and sample pair."""
    wednesday_dt, saturday_dt = sample_pair
    if mode == "pt":
        sample_weekday = build_commute_sample(
            kind="weekday",
            sampled_at=wednesday_dt,
            mode="pt",
            anchors=anchors,
            client=client,
        )
        sample_weekend = build_commute_sample(
            kind="weekend",
            sampled_at=saturday_dt,
            mode="pt",
            anchors=anchors,
            client=client,
        )
        return [sample_weekday, sample_weekend]

    sample_single = build_commute_sample(
        kind="single",
        sampled_at=None,
        mode=mode,
        anchors=anchors,
        client=client,
    )
    return [sample_single]


__all__ = [
    "SGT",
    "CommuteAnchors",
    "build_commute_sample",
    "build_commute_samples",
    "clear_commute_postal_cache",
    "decode_polyline",
    "get_commute_leg",
    "get_next_representative_saturday",
    "get_next_representative_wednesday",
    "get_representative_day",
    "get_representative_sample_pair",
    "resolve_commute_postal",
    "validate_sample_pair",
]

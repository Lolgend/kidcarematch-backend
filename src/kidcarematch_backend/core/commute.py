"""Strict OneMap geospatial adapter for commute route and centre location display (ADR-0008, ADR-0009)."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import secrets
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
    AllowedSampleDefinition,
    AnchorName,
    ArrivalSource,
    CommuteComparison,
    CommuteMode,
    CommuteReason,
    CommuteSample,
    ComparisonCertification,
    Coordinate,
    DirectRoute,
    DurationBasis,
    FailureClass,
    LegTiming,
    NegotiatedCommuteSample,
    NegotiatedRouteLeg,
    ReasonCode,
    RouteLeg,
    RouteTotals,
    SampleConditions,
    SampleKind,
    TransitLeg,
    ViaStatus,
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

REASON_METADATA: dict[ReasonCode, tuple[FailureClass, bool]] = {
    "no_itinerary": ("sample_dependent", True),
    "direct_unavailable": ("conditions", False),
    "via_centre_incomplete": ("conditions", False),
    "home_unresolved": ("location", False),
    "centre_unresolved": ("location", False),
    "workplace_unresolved": ("location", False),
    "workplace_not_configured": ("not_applicable", False),
    "timing_unverified": ("conditions", False),
    "conditions_mismatch": ("conditions", False),
    "provider_timeout": ("service", True),
    "provider_unavailable": ("service", True),
    "provider_auth_failed": ("service", True),
    "evaluation_budget_exhausted": ("service", True),
    "invalid_provider_payload": ("service", False),
    "geometry_unavailable": ("service", False),
}


def build_commute_reason(code: ReasonCode) -> CommuteReason:
    """Construct structured diagnostic Reason mapping accurately to FailureClass and retryable."""
    failure_class, retryable = REASON_METADATA[code]
    return CommuteReason(
        code=code,
        failure_class=failure_class,
        retryable=retryable,
    )


def compute_profile_revision(
    home_postal: str | None,
    workplace_postal: str | None,
) -> str:
    """Compute opaque SHA-256 hash of saved routing-relevant profile state without plaintext PII."""
    h = (home_postal or "").strip()
    w = (workplace_postal or "").strip()
    raw = f"home:{h}|workplace:{w}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_anchor_revision(
    home: Coordinate | None,
    centre: Coordinate | None,
    workplace: Coordinate | None,
) -> str:
    """Compute opaque SHA-256 hash of resolved coordinate snapshot without plaintext PII."""
    parts = [
        f"home:{home.lat:.6f},{home.lng:.6f}" if home else "home:none",
        f"centre:{centre.lat:.6f},{centre.lng:.6f}" if centre else "centre:none",
        f"workplace:{workplace.lat:.6f},{workplace.lng:.6f}"
        if workplace
        else "workplace:none",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def get_primary_wednesday(now: datetime | None = None) -> datetime:
    """Calculate the primary Wednesday at 08:00 Asia/Singapore for selected weekday sampling."""
    return get_next_representative_wednesday(now)


def build_cycle_conditions_id(departure_at: datetime) -> str:
    """Generate deterministic conditions identifier for a scheduled PT sample condition."""
    return f"pt_{departure_at.strftime('%Y%m%d_%H%M%S')}_onemap_v1"


def build_allowed_samples(primary_wed: datetime) -> list[AllowedSampleDefinition]:
    """Build the three allowed sample definitions for a sampling cycle (Wed=0, Thu=1, Fri=2)."""
    allowed: list[AllowedSampleDefinition] = []
    for idx in (0, 1, 2):
        dep = primary_wed + timedelta(days=idx)
        allowed.append(
            AllowedSampleDefinition(
                sample_index=idx,  # type: ignore[arg-type]
                conditions_id=build_cycle_conditions_id(dep),
                departure_at=dep,
            )
        )
    return allowed


def create_cycle_token(
    primary_wed: datetime,
    cycle_expires_at: datetime,
    policy: str = "weekday_0800_wed_thu_fri_v1",
    salt: str | None = None,
) -> str:
    """Encode and HMAC-sign a stateless URL-safe base64 cycle token."""
    if salt is None:
        salt = secrets.token_hex(8)
    payload = {
        "policy": policy,
        "primary_wed_iso": primary_wed.isoformat(),
        "cycle_expires_at_iso": cycle_expires_at.isoformat(),
        "salt": salt,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    sig = hmac.new(
        settings.COMMUTE_CYCLE_SECRET_KEY.encode("utf-8"),
        payload_json,
        hashlib.sha256,
    ).hexdigest()
    envelope = {
        "payload": payload,
        "signature": sig,
    }
    envelope_json = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(envelope_json).decode("ascii").rstrip("=")


def decode_cycle_token(token: str) -> dict[str, Any]:
    """Decode and verify HMAC signature on a stateless cycle token."""
    if not token or not isinstance(token, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid or malformed cycle token",
        )
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError("Token body is not a JSON object")
        if "payload" not in data or "signature" not in data:
            raise ValueError("Missing payload or signature in token")
        payload = data["payload"]
        signature = data["signature"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise TypeError("Malformed payload or signature")

        payload_json = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        expected_sig = hmac.new(
            settings.COMMUTE_CYCLE_SECRET_KEY.encode("utf-8"),
            payload_json,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            raise ValueError("HMAC signature mismatch")
        return payload
    except (
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid or tampered cycle token",
        ) from exc


@dataclass
class CycleValidationResult:
    """Result of cycle token lifecycle resolution and sample validation."""

    cycle_id: str
    policy: str
    primary_wed: datetime
    cycle_expires_at: datetime
    selected_index: int
    selected_departure_at: datetime
    allowed_samples: list[AllowedSampleDefinition]


def validate_cycle_token(
    cycle_id: str | None,
    sample_index: int,
    policy: str = "weekday_0800_wed_thu_fri_v1",
    now: datetime | None = None,
) -> CycleValidationResult:
    """Validate client-supplied cycle parameters or issue a new cycle for primary index 0."""
    if now is None:
        now_sgt = datetime.now(SGT)
    elif now.tzinfo is None:
        now_sgt = now.replace(tzinfo=SGT)
    else:
        now_sgt = now.astimezone(SGT)

    if policy != "weekday_0800_wed_thu_fri_v1":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported sampling policy: {policy}",
        )

    if sample_index not in (0, 1, 2):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid sample_index: must be 0, 1, or 2",
        )

    if cycle_id is None:
        if sample_index != 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="cycle_id is required for sample_index 1 or 2",
            )
        primary_wed = get_primary_wednesday(now_sgt)
        cycle_expires_at = primary_wed
        active_token = create_cycle_token(
            primary_wed=primary_wed, cycle_expires_at=cycle_expires_at, policy=policy
        )
    else:
        payload = decode_cycle_token(cycle_id)
        if payload.get("policy") != policy:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Policy mismatch in cycle token",
            )
        try:
            primary_wed = datetime.fromisoformat(payload["primary_wed_iso"]).astimezone(
                SGT
            )
            cycle_expires_at = datetime.fromisoformat(
                payload["cycle_expires_at_iso"]
            ).astimezone(SGT)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Malformed timestamps in cycle token",
            ) from exc

        if now_sgt >= cycle_expires_at:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "sample_expired",
                    "message": "Commute sampling cycle has expired",
                },
            )
        active_token = cycle_id

    allowed_samples = build_allowed_samples(primary_wed)
    selected_departure = primary_wed + timedelta(days=sample_index)

    return CycleValidationResult(
        cycle_id=active_token,
        policy=policy,
        primary_wed=primary_wed,
        cycle_expires_at=cycle_expires_at,
        selected_index=sample_index,
        selected_departure_at=selected_departure,
        allowed_samples=allowed_samples,
    )


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
) -> tuple[datetime | None, float | None, bool]:
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
                    return arrival, None, True
                elapsed = (arrival - depart).total_seconds()
                if duration_seconds <= elapsed <= 24 * 60 * 60:
                    return arrival, elapsed, True
        except (OverflowError, OSError, TypeError, ValueError):
            pass

    if depart is not None:
        arrival = depart + timedelta(seconds=duration_seconds)
        return arrival, duration_seconds, False
    return None, None, False


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
        dep_sgt = (
            dep.replace(tzinfo=SGT)
            if dep.tzinfo is None
            else dep.astimezone(SGT)
        )
        effective_departure = dep_sgt
        params: dict[str, Any] = {
            "start": f"{start.lat},{start.lng}",
            "end": f"{end.lat},{end.lng}",
            "routeType": "pt",
            "date": dep_sgt.strftime("%m-%d-%Y"),
            "time": dep_sgt.strftime("%H:%M:%S"),
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
            arrival_at, elapsed, is_provider_end_time = _itinerary_arrival(
                itinerary, effective_departure, duration_secs
            )
            if is_provider_end_time and elapsed is not None:
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

    def to_dict(
        self, include_workplace_key: bool = False
    ) -> dict[str, Coordinate | None]:
        res: dict[str, Coordinate | None] = {
            "home": self.home,
            "centre": self.centre,
        }
        if self.has_workplace or include_workplace_key:
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


def _unavailable_negotiated_leg(
    from_anchor: AnchorName,
    to_anchor: AnchorName,
    message: str,
    reason: CommuteReason,
    departure_at: datetime | None = None,
) -> NegotiatedRouteLeg:
    return NegotiatedRouteLeg(
        from_anchor=from_anchor,
        to_anchor=to_anchor,
        status="unavailable",
        source=None,
        duration_seconds=None,
        distance_meters=None,
        geometry=None,
        arrival_at=None,
        message=message,
        transit_legs=None,
        departure_at=departure_at,
        timing=LegTiming(
            duration_basis="unknown",
            arrival_source="unknown",
            initial_wait_included=None,
        ),
        reasons=[reason],
    )


def get_negotiated_commute_leg(
    from_anchor: AnchorName,
    to_anchor: AnchorName,
    start: Coordinate,
    end: Coordinate,
    mode: CommuteMode = "drive",
    departure: datetime | None = None,
    client: httpx.Client | None = None,
) -> NegotiatedRouteLeg:
    """Query OneMap routing endpoint returning NegotiatedRouteLeg with classified diagnostics."""
    effective_departure: datetime | None = departure
    if mode == "pt":
        dep = departure or get_next_representative_wednesday()
        dep_sgt = (
            dep.replace(tzinfo=SGT)
            if dep.tzinfo is None
            else dep.astimezone(SGT)
        )
        effective_departure = dep_sgt
        params: dict[str, Any] = {
            "start": f"{start.lat},{start.lng}",
            "end": f"{end.lat},{end.lng}",
            "routeType": "pt",
            "date": dep_sgt.strftime("%m-%d-%Y"),
            "time": dep_sgt.strftime("%H:%M:%S"),
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
        if response.status_code == 401:
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Provider authentication failed",
                build_commute_reason("provider_auth_failed"),
                departure_at=effective_departure if mode == "pt" else None,
            )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Malformed OneMap routing response",
                build_commute_reason("invalid_provider_payload"),
                departure_at=effective_departure if mode == "pt" else None,
            )

        if mode == "pt":
            if "plan" not in data:
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Public transport plan unavailable",
                    build_commute_reason("no_itinerary"),
                    departure_at=effective_departure,
                )
            plan = data.get("plan")
            if not isinstance(plan, dict):
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Malformed public transport plan",
                    build_commute_reason("invalid_provider_payload"),
                    departure_at=effective_departure,
                )
            itineraries = plan.get("itineraries")
            if not isinstance(itineraries, list):
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Malformed public transport itineraries",
                    build_commute_reason("invalid_provider_payload"),
                    departure_at=effective_departure,
                )
            if not itineraries:
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "No public transport itinerary found",
                    build_commute_reason("no_itinerary"),
                    departure_at=effective_departure,
                )
            itinerary = itineraries[0]
            if not isinstance(itinerary, dict):
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Malformed public transport itinerary",
                    build_commute_reason("invalid_provider_payload"),
                    departure_at=effective_departure,
                )
            raw_legs = itinerary.get("legs")
            if not isinstance(raw_legs, list) or not raw_legs:
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Public transport itinerary legs unavailable",
                    build_commute_reason("invalid_provider_payload"),
                    departure_at=effective_departure,
                )

            transit_legs: list[TransitLeg] = []
            all_coords: list[Coordinate] = []

            for raw_leg in raw_legs:
                if not isinstance(raw_leg, dict):
                    return _unavailable_negotiated_leg(
                        from_anchor,
                        to_anchor,
                        "Malformed public transport sub-leg",
                        build_commute_reason("invalid_provider_payload"),
                        departure_at=effective_departure,
                    )
                try:
                    sub_duration = _required_metric(raw_leg, "duration")
                    sub_distance = _required_metric(raw_leg, "distance")
                except ValueError:
                    return _unavailable_negotiated_leg(
                        from_anchor,
                        to_anchor,
                        "Missing sub-leg metric",
                        build_commute_reason("invalid_provider_payload"),
                        departure_at=effective_departure,
                    )
                sub_mode = str(raw_leg.get("mode", "TRANSIT")).upper()
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
                    return _unavailable_negotiated_leg(
                        from_anchor,
                        to_anchor,
                        "Route geometry unavailable for transit sub-leg",
                        build_commute_reason("geometry_unavailable"),
                        departure_at=effective_departure,
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
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Route geometry unavailable",
                    build_commute_reason("geometry_unavailable"),
                    departure_at=effective_departure,
                )

            try:
                duration_secs = _required_metric(itinerary, "duration")
            except ValueError:
                return _unavailable_negotiated_leg(
                    from_anchor,
                    to_anchor,
                    "Missing itinerary duration",
                    build_commute_reason("invalid_provider_payload"),
                    departure_at=effective_departure,
                )
            total_dist = sum(t.distance_meters or 0 for t in transit_legs)
            arrival_at, elapsed, is_provider_end_time = _itinerary_arrival(
                itinerary, effective_departure, duration_secs
            )
            initial_wait_included = False
            duration_basis: DurationBasis = "itinerary_duration"
            arrival_source: ArrivalSource = "duration_derived"
            if is_provider_end_time and elapsed is not None:
                duration_secs = elapsed
                initial_wait_included = True
                duration_basis = "elapsed_from_requested_departure"
                arrival_source = "provider_end_time"

            timing = LegTiming(
                duration_basis=duration_basis,
                arrival_source=arrival_source,
                initial_wait_included=initial_wait_included,
            )

            return NegotiatedRouteLeg(
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
                departure_at=effective_departure,
                timing=timing,
                reasons=[],
            )

        # Drive / walk / cycle modes
        route_geometry = data.get("route_geometry")
        if not route_geometry or not isinstance(route_geometry, str):
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Route geometry unavailable",
                build_commute_reason("geometry_unavailable"),
                departure_at=None,
            )

        coords = decode_polyline(route_geometry)
        if not coords:
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Decoded route geometry is empty",
                build_commute_reason("geometry_unavailable"),
                departure_at=None,
            )

        summary = data.get("route_summary")
        if not summary or not isinstance(summary, dict):
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Route summary unavailable",
                build_commute_reason("invalid_provider_payload"),
                departure_at=None,
            )

        try:
            duration_secs = _required_metric(summary, "total_time")
            dist_meters = _required_metric(summary, "total_distance")
        except ValueError:
            return _unavailable_negotiated_leg(
                from_anchor,
                to_anchor,
                "Route summary metrics invalid",
                build_commute_reason("invalid_provider_payload"),
                departure_at=None,
            )

        timing = LegTiming(
            duration_basis="provider_default",
            arrival_source="not_applicable",
            initial_wait_included=None,
        )

        return NegotiatedRouteLeg(
            from_anchor=from_anchor,
            to_anchor=to_anchor,
            status="available",
            source="onemap",
            duration_seconds=duration_secs,
            distance_meters=dist_meters,
            geometry=coords,
            message=None,
            departure_at=None,
            timing=timing,
            reasons=[],
        )

    except httpx.TimeoutException as exc:
        logger.warning(
            "OneMap routing timeout (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        return _unavailable_negotiated_leg(
            from_anchor,
            to_anchor,
            "Route request timed out",
            build_commute_reason("provider_timeout"),
            departure_at=effective_departure if mode == "pt" else None,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "OneMap routing HTTP error (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        reason_code: ReasonCode = (
            "provider_auth_failed"
            if exc.response.status_code == 401
            else "provider_unavailable"
        )
        msg = (
            "Provider authentication failed"
            if exc.response.status_code == 401
            else "Route service unavailable"
        )
        return _unavailable_negotiated_leg(
            from_anchor,
            to_anchor,
            msg,
            build_commute_reason(reason_code),
            departure_at=effective_departure if mode == "pt" else None,
        )
    except (
        httpx.HTTPError,
        OSError,
    ) as exc:
        logger.warning(
            "OneMap routing failed (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        return _unavailable_negotiated_leg(
            from_anchor,
            to_anchor,
            "Route unavailable",
            build_commute_reason("provider_unavailable"),
            departure_at=effective_departure if mode == "pt" else None,
        )
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.warning(
            "OneMap routing parsing failed (%s -> %s): %s", from_anchor, to_anchor, exc
        )
        return _unavailable_negotiated_leg(
            from_anchor,
            to_anchor,
            "Invalid provider response",
            build_commute_reason("invalid_provider_payload"),
            departure_at=effective_departure if mode == "pt" else None,
        )

def build_negotiated_commute_sample(
    kind: SampleKind,
    sampled_at: datetime | None,
    mode: CommuteMode,
    anchors: CommuteAnchors,
    include_direct_baseline: bool = True,
    conditions_id: str | None = None,
    provider_options_id: str | None = None,
    client: httpx.Client | None = None,
) -> NegotiatedCommuteSample:
    """Build a NegotiatedCommuteSample with conditions, via_status, direct_route, and comparison."""
    legs: list[NegotiatedRouteLeg] = []
    leg1_departure = sampled_at if mode == "pt" else None

    # Leg 1: home -> centre
    if not anchors.home:
        leg1 = _unavailable_negotiated_leg(
            from_anchor="home",
            to_anchor="centre",
            message="Home postal code could not be resolved",
            reason=build_commute_reason("home_unresolved"),
            departure_at=leg1_departure,
        )
    elif not anchors.centre:
        leg1 = _unavailable_negotiated_leg(
            from_anchor="home",
            to_anchor="centre",
            message="Centre postal code could not be resolved",
            reason=build_commute_reason("centre_unresolved"),
            departure_at=leg1_departure,
        )
    else:
        leg1 = get_negotiated_commute_leg(
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
            leg2 = _unavailable_negotiated_leg(
                from_anchor="centre",
                to_anchor="workplace",
                message="Centre postal code could not be resolved",
                reason=build_commute_reason("centre_unresolved"),
                departure_at=None,
            )
        elif not anchors.workplace:
            leg2 = _unavailable_negotiated_leg(
                from_anchor="centre",
                to_anchor="workplace",
                message="Workplace postal code could not be resolved",
                reason=build_commute_reason("workplace_unresolved"),
                departure_at=None,
            )
        elif mode == "pt" and (leg1.status != "available" or leg1.arrival_at is None):
            leg2 = _unavailable_negotiated_leg(
                from_anchor="centre",
                to_anchor="workplace",
                message="Onward leg departure cannot be determined because first leg arrival is unavailable",
                reason=build_commute_reason("timing_unverified"),
                departure_at=None,
            )
        else:
            leg2_departure = leg1.arrival_at if mode == "pt" else None
            leg2 = get_negotiated_commute_leg(
                from_anchor="centre",
                to_anchor="workplace",
                start=anchors.centre,
                end=anchors.workplace,
                mode=mode,
                departure=leg2_departure,
                client=client,
            )
        legs.append(leg2)

    # Via totals and status
    required_legs = legs
    available_legs = [l for l in required_legs if l.status == "available"]
    if len(available_legs) == len(required_legs):
        via_status: ViaStatus = "available"
        totals: RouteTotals | None = RouteTotals(
            duration_seconds=sum(
                l.duration_seconds
                for l in required_legs
                if l.duration_seconds is not None
            ),
            distance_meters=sum(
                l.distance_meters
                for l in required_legs
                if l.distance_meters is not None
            ),
        )
    elif len(available_legs) > 0:
        via_status = "partial"
        totals = None
    else:
        via_status = "unavailable"
        totals = None

    # Via sample reasons
    sample_reasons: list[CommuteReason] = []
    seen_codes: set[str] = set()
    for l in required_legs:
        for r in l.reasons:
            if r.code not in seen_codes:
                seen_codes.add(r.code)
                sample_reasons.append(r)
    if via_status != "available" and not sample_reasons:
        sample_reasons.append(build_commute_reason("via_centre_incomplete"))

    # Conditions
    if conditions_id is None:
        if mode == "pt":
            conditions_id = (
                f"pt:{sampled_at.isoformat() if sampled_at else 'default'}"
            )
        else:
            conditions_id = f"{mode}:provider_default"

    expected_options_id = (
        "onemap_pt_default" if mode == "pt" else f"onemap_{mode}_default"
    )
    effective_options_id = provider_options_id or expected_options_id

    conditions = SampleConditions(
        conditions_id=conditions_id,
        timing_basis="scheduled_departure" if mode == "pt" else "provider_default",
        initial_departure_at=sampled_at if mode == "pt" else None,
        provider_options_id=effective_options_id,
    )

    # Direct Route
    direct_route: DirectRoute | None = None
    comparison: CommuteComparison | None = None

    if include_direct_baseline:
        if not anchors.has_workplace:
            direct_route = DirectRoute(
                status="not_applicable",
                legs=[],
                totals=None,
                reasons=[build_commute_reason("workplace_not_configured")],
            )
            comparison = CommuteComparison(
                status="not_applicable",
                reasons=[build_commute_reason("workplace_not_configured")],
                certification=None,
            )
        elif not anchors.home:
            direct_leg = _unavailable_negotiated_leg(
                from_anchor="home",
                to_anchor="workplace",
                message="Home postal code could not be resolved",
                reason=build_commute_reason("home_unresolved"),
                departure_at=sampled_at if mode == "pt" else None,
            )
            direct_route = DirectRoute(
                status="unavailable",
                legs=[direct_leg],
                totals=None,
                reasons=[build_commute_reason("home_unresolved")],
            )
        elif not anchors.workplace:
            direct_leg = _unavailable_negotiated_leg(
                from_anchor="home",
                to_anchor="workplace",
                message="Workplace postal code could not be resolved",
                reason=build_commute_reason("workplace_unresolved"),
                departure_at=sampled_at if mode == "pt" else None,
            )
            direct_route = DirectRoute(
                status="unavailable",
                legs=[direct_leg],
                totals=None,
                reasons=[build_commute_reason("workplace_unresolved")],
            )
        else:
            # Home and workplace both resolve!
            direct_leg = get_negotiated_commute_leg(
                from_anchor="home",
                to_anchor="workplace",
                start=anchors.home,
                end=anchors.workplace,
                mode=mode,
                departure=sampled_at if mode == "pt" else None,
                client=client,
            )
            if direct_leg.status == "available":
                direct_route = DirectRoute(
                    status="available",
                    legs=[direct_leg],
                    totals=RouteTotals(
                        duration_seconds=direct_leg.duration_seconds or 0,
                        distance_meters=direct_leg.distance_meters or 0,
                    ),
                    reasons=[],
                )
            else:
                direct_route = DirectRoute(
                    status="unavailable",
                    legs=[direct_leg],
                    totals=None,
                    reasons=list(direct_leg.reasons),
                )

        if comparison is None and direct_route is not None:
            if direct_route.status == "unavailable" or via_status != "available":
                comp_reasons: list[CommuteReason] = []
                comp_seen: set[str] = set()
                if direct_route.status == "unavailable":
                    r_dir = build_commute_reason("direct_unavailable")
                    comp_reasons.append(r_dir)
                    comp_seen.add(r_dir.code)
                    for r in direct_route.reasons:
                        if r.code not in comp_seen:
                            comp_reasons.append(r)
                            comp_seen.add(r.code)
                if via_status != "available":
                    r_via = build_commute_reason("via_centre_incomplete")
                    if r_via.code not in comp_seen:
                        comp_reasons.append(r_via)
                        comp_seen.add(r_via.code)
                    for r in sample_reasons:
                        if r.code not in comp_seen:
                            comp_reasons.append(r)
                            comp_seen.add(r.code)
                comparison = CommuteComparison(
                    status="unavailable",
                    reasons=comp_reasons,
                    certification=None,
                )
            else:
                # Both available
                if conditions.provider_options_id != expected_options_id:
                    comparison = CommuteComparison(
                        status="unavailable",
                        reasons=[build_commute_reason("conditions_mismatch")],
                        certification=None,
                    )
                elif mode == "pt":
                    all_legs_to_verify = list(direct_route.legs) + list(legs)
                    all_elapsed = (
                        len(all_legs_to_verify) >= 2
                        and all(
                            l.timing.duration_basis
                            == "elapsed_from_requested_departure"
                            and l.timing.arrival_source
                            == "provider_end_time"
                            and l.timing.initial_wait_included is True
                            for l in all_legs_to_verify
                        )
                    )
                    if all_elapsed:
                        comparison = CommuteComparison(
                            status="available",
                            reasons=[],
                            certification=ComparisonCertification(
                                duration_basis="elapsed_from_requested_departure",
                                provider_options_id=conditions.provider_options_id,
                            ),
                        )
                    else:
                        comparison = CommuteComparison(
                            status="unavailable",
                            reasons=[build_commute_reason("timing_unverified")],
                            certification=None,
                        )
                else:
                    all_legs_to_verify = list(direct_route.legs) + list(legs)
                    all_default_verified = (
                        len(all_legs_to_verify) >= 2
                        and all(
                            l.timing.duration_basis == "provider_default"
                            and l.timing.arrival_source == "not_applicable"
                            and l.departure_at is None
                            and l.arrival_at is None
                            for l in all_legs_to_verify
                        )
                    )
                    if all_default_verified:
                        comparison = CommuteComparison(
                            status="available",
                            reasons=[],
                            certification=ComparisonCertification(
                                duration_basis="provider_default",
                                provider_options_id=conditions.provider_options_id,
                            ),
                        )
                    else:
                        comparison = CommuteComparison(
                            status="unavailable",
                            reasons=[build_commute_reason("timing_unverified")],
                            certification=None,
                        )

    return NegotiatedCommuteSample(
        kind=kind,
        sampled_at=sampled_at,
        legs=legs,
        totals=totals,
        conditions=conditions,
        via_status=via_status,
        reasons=sample_reasons,
        direct_route=direct_route,
        comparison=comparison,
    )


def build_negotiated_commute_samples(
    mode: CommuteMode,
    anchors: CommuteAnchors,
    sample_pair: tuple[datetime, datetime],
    include_direct_baseline: bool = True,
    client: httpx.Client | None = None,
) -> list[NegotiatedCommuteSample]:
    """Orchestrate negotiated commute samples based on travel mode, anchors, and direct baseline."""
    wednesday_dt, saturday_dt = sample_pair
    if mode == "pt":
        sample_weekday = build_negotiated_commute_sample(
            kind="weekday",
            sampled_at=wednesday_dt,
            mode="pt",
            anchors=anchors,
            include_direct_baseline=include_direct_baseline,
            client=client,
        )
        sample_weekend = build_negotiated_commute_sample(
            kind="weekend",
            sampled_at=saturday_dt,
            mode="pt",
            anchors=anchors,
            include_direct_baseline=include_direct_baseline,
            client=client,
        )
        return [sample_weekday, sample_weekend]

    sample_single = build_negotiated_commute_sample(
        kind="single",
        sampled_at=None,
        mode=mode,
        anchors=anchors,
        include_direct_baseline=include_direct_baseline,
        client=client,
    )
    return [sample_single]


__all__ = [
    "REASON_METADATA",
    "SGT",
    "CommuteAnchors",
    "CycleValidationResult",
    "build_allowed_samples",
    "build_commute_reason",
    "build_commute_sample",
    "build_commute_samples",
    "build_cycle_conditions_id",
    "build_negotiated_commute_sample",
    "build_negotiated_commute_samples",
    "clear_commute_postal_cache",
    "compute_anchor_revision",
    "compute_profile_revision",
    "create_cycle_token",
    "decode_cycle_token",
    "decode_polyline",
    "get_commute_leg",
    "get_negotiated_commute_leg",
    "get_next_representative_saturday",
    "get_next_representative_wednesday",
    "get_primary_wednesday",
    "get_representative_day",
    "get_representative_sample_pair",
    "resolve_commute_postal",
    "validate_cycle_token",
    "validate_sample_pair",
]

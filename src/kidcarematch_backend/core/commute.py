"""Strict OneMap geospatial adapter for commute route and centre location display (ADR-0008, ADR-0009)."""

import logging
import math
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.core.onemap import onemap_token_manager
from kidcarematch_backend.db.db import PostalCoordinateCache
from kidcarematch_backend.schemas.commute import Coordinate

logger = logging.getLogger(__name__)

# Bounded in-memory cache for verified exact-postcode resolutions
_COMMUTE_POSTAL_CACHE_MAX = 512
_COMMUTE_POSTAL_CACHE: dict[str, Coordinate] = {}


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
) -> httpx.Response:
    """Execute OneMap API request with dynamic auth headers and reactive 401 retry."""
    headers = _get_auth_headers(client=client)
    if client is not None:
        response = client.get(url, params=params, headers=headers, timeout=5.0)
        if response.status_code == 401:
            onemap_token_manager.invalidate_token()
            retry_headers = _get_auth_headers(client=client)
            response = client.get(
                url, params=params, headers=retry_headers, timeout=5.0
            )
        return response

    with httpx.Client(timeout=5.0) as http_client:
        response = http_client.get(url, params=params, headers=headers)
        if response.status_code == 401:
            onemap_token_manager.invalidate_token()
            retry_headers = _get_auth_headers(client=http_client)
            response = http_client.get(
                url, params=params, headers=retry_headers
            )
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
                                if result.get("X")
                                and str(result["X"]).upper() != "NIL"
                                else None
                            )
                            y_val = (
                                float(result["Y"])
                                if result.get("Y")
                                and str(result["Y"]).upper() != "NIL"
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

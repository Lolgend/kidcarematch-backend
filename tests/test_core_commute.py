"""Tests for strict OneMap commute display adapter (Task 2)."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from kidcarematch_backend.core.commute import (
    SGT,
    clear_commute_postal_cache,
    decode_polyline,
    get_commute_leg,
    get_next_representative_wednesday,
    resolve_commute_postal,
)
from kidcarematch_backend.schemas.commute import Coordinate

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "onemap"


@pytest.fixture(autouse=True)
def reset_cache():
    clear_commute_postal_cache()
    yield
    clear_commute_postal_cache()


def test_decode_polyline_valid():
    # Encoded polyline for (1.3521, 103.8198) and (1.3530, 103.8205)
    poly = "sagGwhtxRsDkC"
    coords = decode_polyline(poly)
    assert len(coords) == 2
    assert coords[0].lat == pytest.approx(1.3521, abs=1e-4)
    assert coords[0].lng == pytest.approx(103.8198, abs=1e-4)
    assert coords[1].lat == pytest.approx(1.3530, abs=1e-4)
    assert coords[1].lng == pytest.approx(103.8205, abs=1e-4)


def test_decode_polyline_empty_and_invalid():
    assert decode_polyline("") == []
    assert decode_polyline("???invalid???") == []


def test_resolve_commute_postal_exact_match(monkeypatch):
    data = json.loads((FIXTURES_DIR / "search_560123_exact.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_resp

    coord = resolve_commute_postal("560123", client=mock_client)
    assert coord is not None
    assert coord.lat == 1.369145
    assert coord.lng == 103.851234


def test_resolve_commute_postal_fuzzy_diff_first_matches_exact_second(monkeypatch):
    """When OneMap relevance ranking places a non-exact match first, find exact match in results."""
    data = json.loads((FIXTURES_DIR / "search_560123_fuzzy_diff_first.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    coord = resolve_commute_postal("560123", client=mock_client)
    assert coord is not None
    assert coord.lat == 1.369145
    assert coord.lng == 103.851234


def test_resolve_commute_postal_no_exact_match_returns_none(monkeypatch):
    data = json.loads((FIXTURES_DIR / "search_560123_no_exact_match.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    coord = resolve_commute_postal("560123", client=mock_client)
    assert coord is None


def test_resolve_commute_postal_invalid_format():
    assert resolve_commute_postal("12345") is None
    assert resolve_commute_postal("ABCDEF") is None
    assert resolve_commute_postal("") is None
    assert resolve_commute_postal(None) is None


def test_get_commute_leg_drive_success():
    data = json.loads((FIXTURES_DIR / "driving_route_success.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="drive",
        client=mock_client,
    )

    assert leg.from_anchor == "home"
    assert leg.to_anchor == "centre"
    assert leg.status == "available"
    assert leg.source == "onemap"
    assert leg.duration_seconds == 840.0
    assert leg.distance_meters == 6800.0
    assert leg.geometry is not None
    assert len(leg.geometry) == 2


def test_get_commute_leg_missing_geometry_fails_without_fallback():
    data = json.loads((FIXTURES_DIR / "driving_route_missing_geometry.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="drive",
        client=mock_client,
    )

    assert leg.status == "unavailable"
    assert leg.source is None
    assert leg.duration_seconds is None
    assert leg.distance_meters is None
    assert leg.geometry is None
    assert leg.message is not None


def test_get_commute_leg_timeout_fails_without_fallback():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = httpx.ConnectTimeout("OneMap timeout")

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="centre",
        to_anchor="workplace",
        start=start,
        end=end,
        mode="drive",
        client=mock_client,
    )

    assert leg.status == "unavailable"
    assert leg.source is None
    assert leg.duration_seconds is None
    assert leg.distance_meters is None
    assert leg.geometry is None
    assert any(term in (leg.message or "").lower() for term in ("timed out", "timeout", "unavailable"))


def test_get_next_representative_wednesday_boundaries():
    # 1. Monday before Wednesday -> upcoming Wednesday 08:00 SGT
    mon = datetime(2026, 11, 9, 10, 30, tzinfo=SGT)
    wed1 = get_next_representative_wednesday(mon)
    assert wed1 == datetime(2026, 11, 11, 8, 0, tzinfo=SGT)

    # 2. Wednesday before 08:00 SGT -> today Wednesday 08:00 SGT
    wed_early = datetime(2026, 11, 11, 7, 59, 59, tzinfo=SGT)
    wed2 = get_next_representative_wednesday(wed_early)
    assert wed2 == datetime(2026, 11, 11, 8, 0, tzinfo=SGT)

    # 3. Wednesday at 08:00:00 SGT -> next week Wednesday 08:00 SGT
    wed_at_8 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=SGT)
    wed3 = get_next_representative_wednesday(wed_at_8)
    assert wed3 == datetime(2026, 11, 18, 8, 0, tzinfo=SGT)

    # 4. Wednesday afternoon -> next week Wednesday 08:00 SGT
    wed_pm = datetime(2026, 11, 11, 14, 0, tzinfo=SGT)
    wed4 = get_next_representative_wednesday(wed_pm)
    assert wed4 == datetime(2026, 11, 18, 8, 0, tzinfo=SGT)

    # 5. Thursday after Wednesday -> next week Wednesday 08:00 SGT
    thu = datetime(2026, 11, 12, 9, 0, tzinfo=SGT)
    wed5 = get_next_representative_wednesday(thu)
    assert wed5 == datetime(2026, 11, 18, 8, 0, tzinfo=SGT)

    # 6. Sunday -> next week Wednesday 08:00 SGT
    sun = datetime(2026, 11, 15, 12, 0, tzinfo=SGT)
    wed6 = get_next_representative_wednesday(sun)
    assert wed6 == datetime(2026, 11, 18, 8, 0, tzinfo=SGT)


def test_get_commute_leg_pt_success():
    data = json.loads((FIXTURES_DIR / "pt_route_success.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)
    dep_time = datetime(2026, 11, 11, 8, 0, tzinfo=SGT)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="pt",
        departure=dep_time,
        client=mock_client,
    )

    assert leg.from_anchor == "home"
    assert leg.to_anchor == "centre"
    assert leg.status == "available"
    assert leg.source == "onemap"
    assert leg.duration_seconds == 1500.0
    assert leg.distance_meters == 5000.0
    assert leg.geometry is not None
    assert len(leg.geometry) == 6
    assert leg.transit_legs is not None
    assert len(leg.transit_legs) == 3
    assert leg.transit_legs[0].mode == "WALK"
    assert leg.transit_legs[0].duration_seconds == 300.0
    assert leg.transit_legs[1].mode == "BUS"
    assert leg.transit_legs[1].route == "159"
    assert leg.transit_legs[1].duration_seconds == 900.0
    assert leg.transit_legs[2].mode == "WALK"

    # Verify query parameters sent to OneMap
    call_args = mock_client.get.call_args
    assert call_args is not None
    params = call_args[1].get("params", {})
    assert params["routeType"] == "pt"
    assert params["date"] == "11-11-2026"
    assert params["time"] == "08:00:00"
    assert params["mode"] == "TRANSIT"


def test_get_commute_leg_pt_missing_geometry_fails_without_fallback():
    data = json.loads((FIXTURES_DIR / "pt_route_missing_geometry.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="pt",
        client=mock_client,
    )

    assert leg.status == "unavailable"
    assert leg.source is None
    assert leg.duration_seconds is None
    assert leg.distance_meters is None
    assert leg.geometry is None
    assert leg.message is not None


def test_get_commute_leg_pt_no_itinerary_fails_without_fallback():
    data = json.loads((FIXTURES_DIR / "pt_route_no_itinerary.json").read_text(encoding="utf-8"))

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="pt",
        client=mock_client,
    )

    assert leg.status == "unavailable"
    assert leg.source is None
    assert leg.duration_seconds is None
    assert leg.distance_meters is None
    assert leg.geometry is None
    assert "no public transport itinerary" in leg.message.lower()


def test_get_commute_leg_pt_timeout_fails_without_fallback():
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.get.side_effect = httpx.ConnectTimeout("OneMap PT timeout")

    start = Coordinate(lat=1.3691, lng=103.8512)
    end = Coordinate(lat=1.3521, lng=103.8198)

    leg = get_commute_leg(
        from_anchor="home",
        to_anchor="centre",
        start=start,
        end=end,
        mode="pt",
        client=mock_client,
    )

    assert leg.status == "unavailable"
    assert leg.source is None
    assert leg.duration_seconds is None
    assert leg.distance_meters is None
    assert leg.geometry is None
    assert "timed out" in leg.message.lower()

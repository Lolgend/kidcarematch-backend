"""Tests for strict OneMap commute display adapter (Task 2)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from kidcarematch_backend.core.commute import (
    clear_commute_postal_cache,
    decode_polyline,
    get_commute_leg,
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

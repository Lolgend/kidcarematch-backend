"""Tests for strict OneMap commute display adapter (Task 2)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from kidcarematch_backend.core import commute
from kidcarematch_backend.core.commute import (
    SGT,
    clear_commute_postal_cache,
    decode_polyline,
    get_commute_leg,
    get_next_representative_saturday,
    get_next_representative_wednesday,
    get_representative_sample_pair,
    resolve_commute_postal,
    validate_sample_pair,
)
from kidcarematch_backend.core.config import settings
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
    assert decode_polyline("sagGwhtxRsDk") == []  # truncated longitude


def test_resolve_commute_postal_exact_match(monkeypatch):
    data = json.loads(
        (FIXTURES_DIR / "search_560123_exact.json").read_text(encoding="utf-8")
    )

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_resp

    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", "test-token")
    coord = resolve_commute_postal("560123", client=mock_client)
    assert coord is not None
    assert coord.lat == 1.369145
    assert coord.lng == 103.851234
    assert mock_client.get.call_args.kwargs["headers"]["Authorization"] == "test-token"


def test_commute_search_cache_is_bounded(monkeypatch):
    """Verified postal resolutions must not grow process memory without a bound."""
    monkeypatch.setattr(commute, "_COMMUTE_POSTAL_CACHE_MAX", 2)
    client = MagicMock(spec=httpx.Client)

    def get_response(*args, **kwargs):
        postal = kwargs["params"]["searchVal"]
        response = MagicMock()
        response.json.return_value = {
            "found": 1,
            "results": [{"POSTAL": postal, "LATITUDE": "1.3", "LONGITUDE": "103.8"}],
        }
        response.raise_for_status = MagicMock()
        return response

    client.get.side_effect = get_response
    for postal in ("560123", "560124", "560125"):
        assert resolve_commute_postal(postal, client=client) is not None
    assert len(commute._COMMUTE_POSTAL_CACHE) == 2


def test_pt_requires_geometry_and_valid_metrics_for_every_sub_leg():
    response = MagicMock()
    response.json.return_value = {
        "plan": {
            "itineraries": [
                {
                    "duration": 300,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 100,
                            "distance": 20,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                        {
                            "mode": "BUS",
                            "duration": 200,
                            "distance": 100,
                            "legGeometry": {"points": ""},
                        },
                    ],
                }
            ]
        }
    }
    response.raise_for_status = MagicMock()
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = response
    leg = get_commute_leg(
        "home",
        "centre",
        Coordinate(lat=1.3, lng=103.8),
        Coordinate(lat=1.4, lng=103.9),
        mode="pt",
        client=client,
    )
    assert leg.status == "unavailable"
    assert leg.geometry is None


@pytest.mark.parametrize("bad_value", [None, -1, float("nan"), float("inf")])
def test_pt_rejects_missing_negative_or_nonfinite_required_metrics(bad_value):
    response = MagicMock()
    response.json.return_value = {
        "plan": {
            "itineraries": [
                {
                    "duration": 300,
                    "legs": [
                        {
                            "mode": "WALK",
                            "duration": 100,
                            "distance": bad_value,
                            "legGeometry": {"points": "sagGwhtxRsDkC"},
                        },
                    ],
                }
            ]
        }
    }
    response.raise_for_status = MagicMock()
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = response
    leg = get_commute_leg(
        "home",
        "centre",
        Coordinate(lat=1.3, lng=103.8),
        Coordinate(lat=1.4, lng=103.9),
        mode="pt",
        client=client,
    )
    assert leg.status == "unavailable"
    assert leg.duration_seconds is None
    assert leg.distance_meters is None


def test_pt_exposes_provider_end_time_for_sequential_timing():
    data = json.loads(
        (FIXTURES_DIR / "pt_route_with_initial_wait.json").read_text(encoding="utf-8")
    )
    response = MagicMock()
    response.json.return_value = data
    response.raise_for_status = MagicMock()
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = response
    departure = datetime.fromtimestamp(1762744740, tz=timezone.utc).astimezone(SGT)
    leg = get_commute_leg(
        "home",
        "centre",
        Coordinate(lat=1.3, lng=103.8),
        Coordinate(lat=1.4, lng=103.9),
        mode="pt",
        departure=departure,
        client=client,
    )
    assert leg.arrival_at is not None
    assert leg.arrival_at.timestamp() == pytest.approx(1762747229)
    assert leg.duration_seconds == pytest.approx(
        leg.arrival_at.timestamp() - departure.timestamp()
    )
    assert leg.duration_seconds == pytest.approx(
        2489
    )  # 251s schedule wait + 2238s itinerary


@pytest.mark.parametrize(
    "provider_data",
    [
        [],
        {"plan": "not-an-object"},
        {
            "route_geometry": "sagGwhtxRsDkC",
            "route_summary": {"total_time": 60, "total_distance": 500},
        },
    ],
)
def test_pt_rejects_malformed_or_non_pt_response(provider_data):
    response = MagicMock()
    response.json.return_value = provider_data
    response.raise_for_status = MagicMock()
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = response
    leg = get_commute_leg(
        "home",
        "centre",
        Coordinate(lat=1.3, lng=103.8),
        Coordinate(lat=1.4, lng=103.9),
        mode="pt",
        client=client,
    )
    assert leg.status == "unavailable"
    assert leg.duration_seconds is None
    assert leg.geometry is None


def test_resolve_commute_postal_fuzzy_diff_first_matches_exact_second(monkeypatch):
    """When OneMap relevance ranking places a non-exact match first, find exact match in results."""
    data = json.loads(
        (FIXTURES_DIR / "search_560123_fuzzy_diff_first.json").read_text(
            encoding="utf-8"
        )
    )

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
    data = json.loads(
        (FIXTURES_DIR / "search_560123_no_exact_match.json").read_text(encoding="utf-8")
    )

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


def test_get_commute_leg_drive_success(monkeypatch):
    data = json.loads(
        (FIXTURES_DIR / "driving_route_success.json").read_text(encoding="utf-8")
    )

    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_client.get.return_value = mock_resp
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", "test-routing-token")

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
    assert (
        mock_client.get.call_args.kwargs["headers"]["Authorization"]
        == "test-routing-token"
    )


def test_get_commute_leg_missing_geometry_fails_without_fallback():
    data = json.loads(
        (FIXTURES_DIR / "driving_route_missing_geometry.json").read_text(
            encoding="utf-8"
        )
    )

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
    assert any(
        term in (leg.message or "").lower()
        for term in ("timed out", "timeout", "unavailable")
    )


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


def test_get_next_representative_saturday_boundaries():
    # 1. Monday before Saturday -> upcoming Saturday 08:00 SGT
    mon = datetime(2026, 11, 9, 10, 30, tzinfo=SGT)
    sat1 = get_next_representative_saturday(mon)
    assert sat1 == datetime(2026, 11, 14, 8, 0, tzinfo=SGT)

    # 2. Saturday before 08:00 SGT -> today Saturday 08:00 SGT
    sat_early = datetime(2026, 11, 14, 7, 59, 59, tzinfo=SGT)
    sat2 = get_next_representative_saturday(sat_early)
    assert sat2 == datetime(2026, 11, 14, 8, 0, tzinfo=SGT)

    # 3. Saturday at 08:00:00 SGT -> next week Saturday 08:00 SGT
    sat_at_8 = datetime(2026, 11, 14, 8, 0, 0, tzinfo=SGT)
    sat3 = get_next_representative_saturday(sat_at_8)
    assert sat3 == datetime(2026, 11, 21, 8, 0, tzinfo=SGT)

    # 4. Sunday -> upcoming Saturday 08:00 SGT
    sun = datetime(2026, 11, 15, 12, 0, tzinfo=SGT)
    sat4 = get_next_representative_saturday(sun)
    assert sat4 == datetime(2026, 11, 21, 8, 0, tzinfo=SGT)


def test_get_representative_sample_pair():
    mon = datetime(2026, 11, 9, 10, 0, tzinfo=SGT)
    wed, sat = get_representative_sample_pair(mon)
    assert wed == datetime(2026, 11, 11, 8, 0, tzinfo=SGT)
    assert sat == datetime(2026, 11, 14, 8, 0, tzinfo=SGT)


def test_validate_sample_pair_valid():
    mon = datetime(2026, 11, 9, 10, 0, tzinfo=SGT)
    pair = [
        "2026-11-11T08:00:00+08:00",
        "2026-11-14T08:00:00+08:00",
    ]
    w, s = validate_sample_pair(pair, now=mon)
    assert w == datetime(2026, 11, 11, 8, 0, tzinfo=SGT)
    assert s == datetime(2026, 11, 14, 8, 0, tzinfo=SGT)


def test_validate_sample_pair_expired_or_invalid():
    now = datetime(2026, 11, 12, 10, 0, tzinfo=SGT)

    # 1. Past timestamp
    with pytest.raises(Exception) as exc_info:
        validate_sample_pair(
            ["2026-11-11T08:00:00+08:00", "2026-11-14T08:00:00+08:00"], now=now
        )
    assert "sample_expired" in str(exc_info.value)

    # 2. Too far in the future (> 7 days)
    with pytest.raises(Exception) as exc_info:
        validate_sample_pair(
            ["2026-11-25T08:00:00+08:00", "2026-11-28T08:00:00+08:00"], now=now
        )
    assert "sample_expired" in str(exc_info.value)

    # 3. Not 2 items
    with pytest.raises(Exception) as exc_info:
        validate_sample_pair(["2026-11-18T08:00:00+08:00"], now=now)
    assert "sample_expired" in str(exc_info.value)

    # 4. Wrong weekday or time
    with pytest.raises(Exception) as exc_info:
        validate_sample_pair(
            ["2026-11-17T08:00:00+08:00", "2026-11-14T08:00:00+08:00"], now=now
        )
    assert "sample_expired" in str(exc_info.value)


def test_get_commute_leg_pt_success():
    data = json.loads(
        (FIXTURES_DIR / "pt_route_success.json").read_text(encoding="utf-8")
    )

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
    data = json.loads(
        (FIXTURES_DIR / "pt_route_missing_geometry.json").read_text(encoding="utf-8")
    )

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
    data = json.loads(
        (FIXTURES_DIR / "pt_route_no_itinerary.json").read_text(encoding="utf-8")
    )

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

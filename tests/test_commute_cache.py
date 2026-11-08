import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from kidcarematch_backend.core.commute import (
    clear_commute_caches,
    clear_commute_route_cache,
    get_negotiated_commute_leg,
)
from kidcarematch_backend.schemas.commute import Coordinate


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_commute_caches()
    yield
    clear_commute_caches()


def _mock_success_response(url: str = "https://mock.onemap.gov.sg/route") -> httpx.Response:
    # 2-point valid polyline
    # points: (1.35, 103.82) -> (1.31, 103.86)
    return httpx.Response(
        status_code=200,
        json={
            "plan": {
                "itineraries": [
                    {
                        "duration": 1800,
                        "legs": [
                            {
                                "mode": "BUS",
                                "route": "166",
                                "duration": 1800,
                                "distance": 5000,
                                "legGeometry": {"points": "_{feAmwvy@o_@_t@"},
                                "instruction": "Take Bus 166",
                            }
                        ],
                    }
                ]
            }
        },
        request=httpx.Request("GET", url),
    )


def test_negotiated_commute_leg_cached_on_available(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return _mock_success_response(url)

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    start = Coordinate(lat=1.350000, lng=103.820000)
    end = Coordinate(lat=1.310000, lng=103.860000)
    dep = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)

    leg1 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg1.status == "available"
    assert leg1.duration_seconds == 1800
    assert call_count == 1

    leg2 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg2.status == "available"
    assert leg2.duration_seconds == 1800
    assert call_count == 1  # 0 additional HTTP calls


def test_negotiated_commute_leg_preserves_caller_anchors(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return _mock_success_response(url)

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    start = Coordinate(lat=1.350000, lng=103.820000)
    end = Coordinate(lat=1.310000, lng=103.860000)
    dep = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)

    # First call with home -> workplace
    leg1 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg1.from_anchor == "home"
    assert leg1.to_anchor == "workplace"
    assert call_count == 1

    # Second call with same coordinates but different anchors: centre -> workplace
    leg2 = get_negotiated_commute_leg(
        "centre", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg2.from_anchor == "centre"
    assert leg2.to_anchor == "workplace"
    assert call_count == 1  # hit cache, but anchors preserved for caller 2!

    # First leg must still retain its own anchors
    assert leg1.from_anchor == "home"
    assert leg1.to_anchor == "workplace"


def test_negotiated_commute_leg_unavailable_never_cached(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def failing_then_success_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Empty itineraries -> no_itinerary (status == unavailable)
            return httpx.Response(
                status_code=200,
                json={"plan": {"itineraries": []}},
                request=httpx.Request("GET", url),
            )
        return _mock_success_response(url)

    monkeypatch.setattr(
        "kidcarematch_backend.core.commute._fetch_onemap", failing_then_success_fetch
    )

    start = Coordinate(lat=1.350000, lng=103.820000)
    end = Coordinate(lat=1.310000, lng=103.860000)
    dep = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)

    # First call: unavailable
    leg1 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg1.status == "unavailable"
    assert call_count == 1

    # Second call: must NOT be cached! Must retry OneMap and succeed
    leg2 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg2.status == "available"
    assert call_count == 2


def test_negotiated_commute_leg_concurrent_coalescing(monkeypatch: pytest.MonkeyPatch):
    call_count = 0
    barrier = threading.Barrier(6)

    def slow_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return _mock_success_response(url)

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", slow_fetch)

    start = Coordinate(lat=1.350000, lng=103.820000)
    end = Coordinate(lat=1.310000, lng=103.860000)
    dep = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
    results = []

    def worker():
        barrier.wait()
        res = get_negotiated_commute_leg(
            "home", "workplace", start, end, mode="pt", departure=dep
        )
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(r.status == "available" for r in results)
    assert all(r.duration_seconds == 1800 for r in results)
    assert call_count == 1  # 6 concurrent calls coalesced into 1


def test_clear_commute_caches_clears_route_cache(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return _mock_success_response(url)

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    start = Coordinate(lat=1.350000, lng=103.820000)
    end = Coordinate(lat=1.310000, lng=103.860000)
    dep = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)

    leg1 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg1.status == "available"
    assert call_count == 1

    clear_commute_route_cache()

    leg2 = get_negotiated_commute_leg(
        "home", "workplace", start, end, mode="pt", departure=dep
    )
    assert leg2.status == "available"
    assert call_count == 2

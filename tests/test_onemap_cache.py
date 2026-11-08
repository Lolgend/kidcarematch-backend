import threading
import time

import httpx
import pytest

from kidcarematch_backend.core.onemap import (
    clear_onemap_caches,
    clear_onemap_route_cache,
    get_route_duration_and_distance,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_onemap_caches()
    yield
    clear_onemap_caches()


def test_route_duration_and_distance_cached_on_success(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={
                "plan": {
                    "itineraries": [
                        {"duration": 1800}  # 30 mins
                    ]
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", mock_fetch)

    start = (1.352100, 103.819800)
    end = (1.300000, 103.850000)

    # First call: hits external OneMap API
    dur1, dist1 = get_route_duration_and_distance(start, end)
    assert dur1 == 30.0
    assert call_count == 1

    # Second call: returns cached result without invoking _fetch_onemap
    dur2, dist2 = get_route_duration_and_distance(start, end)
    assert dur2 == 30.0
    assert dist1 == dist2
    assert call_count == 1


def test_route_duration_and_distance_coordinate_rounding(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={"route_summary": {"total_time": 1200, "total_distance": 5000}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", mock_fetch)

    # Coordinates differing at 7th decimal place
    start1 = (1.3521001, 103.8198002)
    end1 = (1.3000003, 103.8500004)

    start2 = (1.3521004, 103.8198001)
    end2 = (1.3000001, 103.8500002)

    dur1, _dist1 = get_route_duration_and_distance(start1, end1)
    assert dur1 == 20.0
    assert call_count == 1

    dur2, _dist2 = get_route_duration_and_distance(start2, end2)
    assert dur2 == 20.0
    assert call_count == 1  # hit cache due to 6-decimal normalization


def test_route_duration_and_distance_concurrent_coalescing(monkeypatch: pytest.MonkeyPatch):
    call_count = 0
    barrier = threading.Barrier(5)

    def slow_fetch(url, params, client):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return httpx.Response(
            status_code=200,
            json={"plan": {"itineraries": [{"duration": 900}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", slow_fetch)

    start = (1.350000, 103.820000)
    end = (1.310000, 103.860000)
    results = []

    def worker():
        barrier.wait()
        res = get_route_duration_and_distance(start, end)
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    assert all(r[0] == 15.0 for r in results)
    assert call_count == 1  # 5 concurrent requests coalesced into 1


def test_route_duration_and_distance_fallback_never_cached(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def failing_then_successful_fetch(url, params, client):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectTimeout("OneMap connection timed out")
        return httpx.Response(
            status_code=200,
            json={"plan": {"itineraries": [{"duration": 1500}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(
        "kidcarematch_backend.core.onemap._fetch_onemap", failing_then_successful_fetch
    )

    start = (1.350000, 103.820000)
    end = (1.310000, 103.860000)

    # First call: fails with TimeoutException, falls back to Haversine straight-line estimation
    fallback_dur, fallback_dist = get_route_duration_and_distance(start, end)
    assert fallback_dur > 0.0
    assert fallback_dist > 0.0
    assert call_count == 1

    # Second call: must NOT be cached! Must retry OneMap API and get success
    dur2, _dist2 = get_route_duration_and_distance(start, end)
    assert dur2 == 25.0  # 1500 secs / 60
    assert call_count == 2


def test_clear_onemap_caches_clears_route_cache(monkeypatch: pytest.MonkeyPatch):
    call_count = 0

    def mock_fetch(url, params, client):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={"plan": {"itineraries": [{"duration": 600}]}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", mock_fetch)

    start = (1.350000, 103.820000)
    end = (1.310000, 103.860000)

    dur1, _ = get_route_duration_and_distance(start, end)
    assert dur1 == 10.0
    assert call_count == 1

    clear_onemap_route_cache()

    dur2, _ = get_route_duration_and_distance(start, end)
    assert dur2 == 10.0
    assert call_count == 2

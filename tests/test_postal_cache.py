import threading
import time

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.core.commute import (
    clear_commute_caches,
    clear_commute_postal_cache,
    is_postal_verified,
    resolve_commute_postal,
    set_verified_postal,
)
from kidcarematch_backend.core.onemap import (
    clear_onemap_caches,
    resolve_coordinates_batch,
    resolve_postal_coordinates,
)
from kidcarematch_backend.db.db import PostalCoordinateCache


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _reset():
    clear_onemap_caches()
    clear_commute_caches()
    yield
    clear_onemap_caches()
    clear_commute_caches()


def test_resolve_postal_coordinates_prefers_exact_postal_match_and_warms_commute_cache(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """resolve_postal_coordinates prefers item with POSTAL == clean_postal and warms commute cache."""
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={
                "found": 2,
                "results": [
                    {
                        "POSTAL": "560000",  # Approximate / different postal code
                        "LATITUDE": "1.300000",
                        "LONGITUDE": "103.800000",
                        "ADDRESS": "Approximate Address",
                    },
                    {
                        "POSTAL": "560123",  # Exact match
                        "LATITUDE": "1.365400",
                        "LONGITUDE": "103.845600",
                        "ADDRESS": "Exact Address",
                    },
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", mock_fetch)
    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    coords = resolve_postal_coordinates("560123", session=session)
    assert coords == (1.365400, 103.845600)
    assert call_count == 1
    assert is_postal_verified("560123") is True

    # Public seam verification: resolve_commute_postal hits warmed cache without additional HTTP call
    commute_coord = resolve_commute_postal("560123", session=session)
    assert commute_coord is not None
    assert commute_coord.lat == 1.365400
    assert commute_coord.lng == 103.845600
    assert call_count == 1  # 0 additional HTTP calls

    # Public seam verification: clear_commute_postal_cache clears the cache, so next call issues HTTP
    clear_commute_postal_cache()
    resolve_commute_postal("560123", session=session)
    assert call_count == 2


def test_resolve_postal_coordinates_approximate_fallback_does_not_warm_commute_cache(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """When no exact POSTAL match exists, falls back to result[0] for matching DB, but does NOT warm commute cache."""
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={
                "found": 1,
                "results": [
                    {
                        "POSTAL": "560999",  # Different postal code
                        "LATITUDE": "1.370000",
                        "LONGITUDE": "103.850000",
                        "ADDRESS": "Nearby Address",
                    }
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.onemap._fetch_onemap", mock_fetch)
    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    coords = resolve_postal_coordinates("560888", session=session)
    assert coords == (1.370000, 103.850000)
    assert call_count == 1
    assert is_postal_verified("560888") is False

    # Public seam verification: 560888 was NOT warmed into commute cache,
    # so resolve_commute_postal must issue an external HTTP call (and return None since no exact match)
    commute_coord = resolve_commute_postal("560888", session=session)
    assert commute_coord is None
    assert call_count == 2  # additional HTTP call fired due to cache miss


def test_resolve_coordinates_batch_db_hit_warms_verified_postal(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    """resolve_coordinates_batch warms commute cache only for postals verified in-process."""
    # Pre-populate DB with two rows:
    # 1. "111111": verified in-process
    # 2. "222222": legacy unverified row
    session.add(
        PostalCoordinateCache(
            postal_code="111111",
            latitude=1.311111,
            longitude=103.811111,
            address="Verified Addr",
        )
    )
    session.add(
        PostalCoordinateCache(
            postal_code="222222",
            latitude=1.322222,
            longitude=103.822222,
            address="Legacy Unverified Addr",
        )
    )
    session.commit()

    # Mark 111111 as verified in memory, then evict it from LRU cache by filling capacity
    set_verified_postal("111111", 1.311111, 103.811111)
    for i in range(512):
        set_verified_postal(f"9{i:05d}", 1.0, 103.0)

    assert is_postal_verified("111111") is True

    # resolve_coordinates_batch loads from DB and re-warms verified postals into commute cache
    batch_res = resolve_coordinates_batch(["111111", "222222"], session=session)
    assert "111111" in batch_res
    assert "222222" in batch_res

    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={"found": 0, "results": []},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    # 111111 was re-warmed into commute cache: hits cache with 0 HTTP calls
    coord1 = resolve_commute_postal("111111", session=session)
    assert coord1 is not None
    assert coord1.lat == 1.311111
    assert coord1.lng == 103.811111
    assert call_count == 0

    # 222222 was unverified: not in commute cache -> triggers external HTTP call
    resolve_commute_postal("222222", session=session)
    assert call_count == 1


def test_clear_commute_caches_resets_postal_cache(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """clear_commute_caches clears verified postal resolutions so subsequent lookups re-fetch."""
    call_count = 0

    def mock_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=200,
            json={
                "found": 1,
                "results": [
                    {
                        "POSTAL": "560123",
                        "LATITUDE": "1.365400",
                        "LONGITUDE": "103.845600",
                        "ADDRESS": "Exact Address",
                    }
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", mock_fetch)

    # Initial resolve warms cache
    c1 = resolve_commute_postal("560123", session=session)
    assert c1 is not None and c1.lat == 1.365400
    assert call_count == 1

    # Second call is cache hit (0 additional HTTP calls)
    c2 = resolve_commute_postal("560123", session=session)
    assert c2 is not None and c2.lat == 1.365400
    assert call_count == 1

    # clear_commute_caches resets cache
    clear_commute_caches()

    # Third call must re-fetch via HTTP
    c3 = resolve_commute_postal("560123", session=session)
    assert c3 is not None and c3.lat == 1.365400
    assert call_count == 2


def test_resolve_commute_postal_concurrent_coalescing(monkeypatch: pytest.MonkeyPatch):
    """Concurrent resolve_commute_postal calls for uncached postal coalesce into a single HTTP search call."""
    call_count = 0
    barrier = threading.Barrier(5)

    def slow_fetch(url, params, client=None, timeout=None):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return httpx.Response(
            status_code=200,
            json={
                "found": 1,
                "results": [
                    {
                        "POSTAL": "560123",
                        "LATITUDE": "1.365400",
                        "LONGITUDE": "103.845600",
                        "ADDRESS": "Exact Address",
                    }
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("kidcarematch_backend.core.commute._fetch_onemap", slow_fetch)

    results = []

    def worker():
        barrier.wait()
        coord = resolve_commute_postal("560123")
        results.append(coord)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    assert all(c is not None and c.lat == 1.365400 for c in results)
    # Only 1 external HTTP call fired across 5 concurrent threads!
    assert call_count == 1

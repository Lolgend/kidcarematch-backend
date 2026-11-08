import threading
import time

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.core.commute import (
    _COMMUTE_POSTAL_CACHE,
    clear_commute_caches,
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
    """resolve_postal_coordinates prefers item with POSTAL == clean_postal and warms _COMMUTE_POSTAL_CACHE."""
    call_count = 0

    def mock_fetch(url, params, client):
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

    coords = resolve_postal_coordinates("560123", session=session)
    assert coords == (1.365400, 103.845600)
    assert call_count == 1

    # Warmed into _COMMUTE_POSTAL_CACHE
    assert "560123" in _COMMUTE_POSTAL_CACHE
    assert _COMMUTE_POSTAL_CACHE["560123"].lat == 1.365400
    assert _COMMUTE_POSTAL_CACHE["560123"].lng == 103.845600
    assert is_postal_verified("560123") is True


def test_resolve_postal_coordinates_approximate_fallback_does_not_warm_commute_cache(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """When no exact POSTAL match exists, falls back to result[0] for matching DB, but does NOT warm _COMMUTE_POSTAL_CACHE."""
    call_count = 0

    def mock_fetch(url, params, client):
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

    coords = resolve_postal_coordinates("560888", session=session)
    assert coords == (1.370000, 103.850000)
    assert call_count == 1

    # Must NOT be in _COMMUTE_POSTAL_CACHE because it was not an exact POSTAL match
    assert "560888" not in _COMMUTE_POSTAL_CACHE
    assert is_postal_verified("560888") is False


def test_resolve_coordinates_batch_db_hit_warms_verified_postal(
    session: Session,
):
    """resolve_coordinates_batch warms _COMMUTE_POSTAL_CACHE only for postals verified in-process."""
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

    # Mark 111111 as verified in memory, but clear _COMMUTE_POSTAL_CACHE
    set_verified_postal("111111", 1.311111, 103.811111)
    _COMMUTE_POSTAL_CACHE.clear()
    assert len(_COMMUTE_POSTAL_CACHE) == 0

    batch_res = resolve_coordinates_batch(["111111", "222222"], session=session)
    assert "111111" in batch_res
    assert "222222" in batch_res

    # 111111 was verified in-process -> re-warmed into _COMMUTE_POSTAL_CACHE
    assert "111111" in _COMMUTE_POSTAL_CACHE
    assert _COMMUTE_POSTAL_CACHE["111111"].lat == 1.311111

    # 222222 was unverified -> MUST NOT be in _COMMUTE_POSTAL_CACHE
    assert "222222" not in _COMMUTE_POSTAL_CACHE


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

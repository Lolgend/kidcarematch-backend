import threading
import time

from kidcarematch_backend.core.route_cache import BoundedTTLCacheWithCoalescing


def test_cache_hit_and_miss():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return "val-1", True

    val1 = cache.get_or_compute("k1", compute)
    assert val1 == "val-1"
    assert call_count == 1

    # Second call for k1 hits cache
    val2 = cache.get_or_compute("k1", compute)
    assert val2 == "val-1"
    assert call_count == 1
    assert len(cache) == 1


def test_cache_ttl_expiration():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=0.05
    )
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return f"val-{call_count}", True

    val1 = cache.get_or_compute("k1", compute)
    assert val1 == "val-1"
    assert call_count == 1

    time.sleep(0.06)

    # After TTL, entry should be expired and recomputed
    val2 = cache.get_or_compute("k1", compute)
    assert val2 == "val-2"
    assert call_count == 2


def test_cache_maxsize_lru_eviction():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=3, ttl=60.0
    )

    cache.get_or_compute("k1", lambda: ("v1", True))
    cache.get_or_compute("k2", lambda: ("v2", True))
    cache.get_or_compute("k3", lambda: ("v3", True))

    assert len(cache) == 3
    assert cache.get("k1") == "v1"

    # Access k1 to make it most recently used (LRU: k2 is now oldest)
    cache.get_or_compute("k1", lambda: ("v1-new", True))

    # Add k4 -> should evict k2
    cache.get_or_compute("k4", lambda: ("v4", True))
    assert len(cache) == 3
    assert cache.get("k1") == "v1"
    assert cache.get("k2") is None
    assert cache.get("k3") == "v3"
    assert cache.get("k4") == "v4"


def test_uncacheable_result_not_stored_and_retried():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    attempts = 0

    def compute_failing():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return "fallback-val", False  # not cacheable
        return "success-val", True

    # First attempt returns fallback, not cacheable
    res1 = cache.get_or_compute("k1", compute_failing)
    assert res1 == "fallback-val"
    assert attempts == 1
    assert len(cache) == 0
    assert cache.get("k1") is None

    # Next attempt retries and succeeds, caching the value
    res2 = cache.get_or_compute("k1", compute_failing)
    assert res2 == "success-val"
    assert attempts == 2
    assert len(cache) == 1

    # Third attempt hits cache
    res3 = cache.get_or_compute("k1", compute_failing)
    assert res3 == "success-val"
    assert attempts == 2


def test_concurrent_in_flight_coalescing():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    call_count = 0
    barrier = threading.Barrier(5)
    results = []

    def slow_compute():
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return "coalesced-val", True

    def worker():
        barrier.wait()
        res = cache.get_or_compute("shared-key", slow_compute)
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    assert all(r == "coalesced-val" for r in results)
    # Even though 5 concurrent threads called simultaneously, slow_compute ran only once!
    assert call_count == 1


def test_concurrent_in_flight_uncacheable_coalescing():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    call_count = 0
    barrier = threading.Barrier(4)
    results = []

    def uncacheable_slow_compute():
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return "fallback-result", False

    def worker():
        barrier.wait()
        res = cache.get_or_compute("uncached-key", uncacheable_slow_compute)
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert all(r == "fallback-result" for r in results)
    assert call_count == 1
    # Cache must NOT contain "uncached-key"
    assert len(cache) == 0
    assert cache.get("uncached-key") is None


def test_clear_resets_cache_and_in_flight():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    cache.get_or_compute("k1", lambda: ("v1", True))
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    assert cache.get("k1") is None

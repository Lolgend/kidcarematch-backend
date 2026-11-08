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

    compute_counts: dict[str, int] = {"k1": 0, "k2": 0, "k3": 0, "k4": 0}

    def make_compute(k: str):
        def _fn():
            compute_counts[k] += 1
            return f"val-{k}", True

        return _fn

    cache.get_or_compute("k1", make_compute("k1"))
    cache.get_or_compute("k2", make_compute("k2"))
    cache.get_or_compute("k3", make_compute("k3"))

    assert compute_counts["k1"] == 1
    assert compute_counts["k2"] == 1
    assert compute_counts["k3"] == 1

    # Access k1 to make it most recently used (LRU: k2 is now oldest)
    val_k1 = cache.get_or_compute("k1", make_compute("k1"))
    assert val_k1 == "val-k1"
    assert compute_counts["k1"] == 1  # Cache hit, not recomputed

    # Add k4 -> should evict k2
    val_k4 = cache.get_or_compute("k4", make_compute("k4"))
    assert val_k4 == "val-k4"
    assert compute_counts["k4"] == 1

    # k1 and k3 are still cached
    assert cache.get_or_compute("k1", make_compute("k1")) == "val-k1"
    assert compute_counts["k1"] == 1

    assert cache.get_or_compute("k3", make_compute("k3")) == "val-k3"
    assert compute_counts["k3"] == 1

    # k2 was evicted -> recomputed
    assert cache.get_or_compute("k2", make_compute("k2")) == "val-k2"
    assert compute_counts["k2"] == 2


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

    # Next attempt retries and succeeds, caching the value
    res2 = cache.get_or_compute("k1", compute_failing)
    assert res2 == "success-val"
    assert attempts == 2

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

    # Key was not stored in cache -> subsequent compute is called
    next_res = cache.get_or_compute("uncached-key", lambda: ("new-val", True))
    assert next_res == "new-val"


def test_clear_resets_cache_and_in_flight():
    cache: BoundedTTLCacheWithCoalescing[str, str] = BoundedTTLCacheWithCoalescing(
        maxsize=10, ttl=60.0
    )
    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return "v1", True

    assert cache.get_or_compute("k1", compute) == "v1"
    assert calls == 1

    # Second call hits cache
    assert cache.get_or_compute("k1", compute) == "v1"
    assert calls == 1

    cache.clear()

    # After clear(), next call recomputes
    assert cache.get_or_compute("k1", compute) == "v1"
    assert calls == 2

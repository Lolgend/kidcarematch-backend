"""Bounded in-memory TTL cache with per-key in-flight request coalescing."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class _InFlight(Generic[V]):
    __slots__ = ("cacheable", "event", "exception", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: V | None = None
        self.exception: Exception | None = None
        self.cacheable: bool = False


class BoundedTTLCacheWithCoalescing(Generic[K, V]):
    """Thread-safe bounded in-memory TTL cache with per-key in-flight coalescing.

    Concurrent requests for the same key await the single in-flight computation rather
    than issuing redundant duplicate external requests.
    Only computations marked as cacheable (e.g. status == 'available') are persisted
    in the bounded TTL store; uncacheable fallbacks and transient errors are returned to
    active in-flight waiters but never poison future cache lookups.
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 3600.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: collections.OrderedDict[K, tuple[float, V]] = (
            collections.OrderedDict()
        )
        self._in_flight: dict[K, _InFlight[V]] = {}

    def get_or_compute(
        self,
        key: K,
        compute_fn: Callable[[], tuple[V, bool]],
        timeout: float | None = None,
    ) -> V:
        """Retrieve cached value or compute it, coalescing concurrent calls.

        `compute_fn` must return a tuple of `(result, is_cacheable)`.
        If `is_cacheable` is True, `result` is saved with `ttl` in the LRU cache.
        If `is_cacheable` is False, `result` is returned without caching.
        """
        now = time.monotonic()
        with self._lock:
            # 1. Check existing cache entry
            if key in self._cache:
                expires_at, cached_val = self._cache[key]
                if now < expires_at:
                    self._cache.move_to_end(key)
                    return cached_val
                # Expired
                del self._cache[key]

            # 2. Check if another thread is currently computing this key
            if key in self._in_flight:
                flight = self._in_flight[key]
                is_leader = False
            else:
                flight = _InFlight[V]()
                self._in_flight[key] = flight
                is_leader = True

        if not is_leader:
            # Wait for leader thread to finish computing
            signaled = flight.event.wait(timeout=timeout)
            if not signaled:
                raise TimeoutError(f"Timed out waiting for in-flight key: {key}")
            if flight.exception is not None:
                raise flight.exception
            # Type assertion: flight.result is set if event signaled without exception
            return flight.result  # type: ignore[return-value]

        # Leader executes computation outside lock
        try:
            result, is_cacheable = compute_fn()
        except Exception as exc:
            with self._lock:
                flight.exception = exc
                self._in_flight.pop(key, None)
                flight.event.set()
            raise

        with self._lock:
            if is_cacheable:
                self._cache[key] = (time.monotonic() + self._ttl, result)
                if len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)
            flight.result = result
            flight.cacheable = is_cacheable
            self._in_flight.pop(key, None)
            flight.event.set()

        return result

    def clear(self) -> None:
        """Clear all cached entries and in-flight tracking."""
        with self._lock:
            self._cache.clear()
            self._in_flight.clear()

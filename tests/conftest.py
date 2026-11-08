"""Global test configuration and fixtures for test isolation."""

from collections.abc import Generator

import pytest

from kidcarematch_backend.core.commute import clear_commute_caches
from kidcarematch_backend.core.onemap import clear_onemap_caches


@pytest.fixture(autouse=True)
def _reset_global_caches() -> Generator[None, None, None]:
    """Ensure in-memory OneMap routing and commute caches are reset before and after each test."""
    clear_onemap_caches()
    clear_commute_caches()
    yield
    clear_onemap_caches()
    clear_commute_caches()

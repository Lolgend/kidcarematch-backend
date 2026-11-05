"""Unit tests for OneMap Service Account configuration and in-memory Token Manager (Spec #73, Ticket #74).

Validates:
- Settings configuration defaults and overrides for OneMap service account.
- Dynamic token acquisition and caching.
- Double-checked locking and thread safety under high concurrency.
- Proactive token refresh within the 300s window.
- Resilient Unix epoch timestamp parsing (int, float, numeric string, invalid/expired fallback to 72h).
- Fallback to static ONEMAP_API_TOKEN.
- Safe handling of missing credentials and network failures.
- Invalidation behavior for reactive re-authentication.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from kidcarematch_backend.core.config import Settings, settings
from kidcarematch_backend.core.onemap import OneMapTokenManager, onemap_token_manager


def test_settings_onemap_service_account_configuration(monkeypatch):
    """Verify settings defaults for OneMap service account authentication."""
    monkeypatch.delenv("ONEMAP_EMAIL", raising=False)
    monkeypatch.delenv("ONEMAP_PASSWORD", raising=False)
    monkeypatch.delenv("ONEMAP_API_TOKEN", raising=False)
    test_settings = Settings(_env_file=None)
    assert test_settings.ONEMAP_AUTH_URL == "https://www.onemap.gov.sg/api/auth/post/getToken"
    assert test_settings.ONEMAP_EMAIL is None
    assert test_settings.ONEMAP_PASSWORD is None
    assert test_settings.ONEMAP_API_TOKEN is None


def test_token_acquisition_and_caching(monkeypatch):
    """Verify dynamic token acquisition, in-memory caching, and auth header generation."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")

    call_count = 0
    now = time.time()
    expected_expiry = now + 7200.0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        assert request.url == httpx.URL(settings.ONEMAP_AUTH_URL)
        assert request.headers["content-type"] == "application/json"
        import json

        body = json.loads(request.content.decode("utf-8"))
        assert body == {"email": "svc_test@example.com", "password": "secret_pass"}
        return httpx.Response(
            200,
            json={"access_token": "mock-jwt-token-xyz", "expiry_timestamp": str(expected_expiry)},
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    # First call: triggers network acquisition
    token1 = manager.get_valid_token(client=client)
    assert token1 == "mock-jwt-token-xyz"
    assert call_count == 1
    assert manager.token == "mock-jwt-token-xyz"
    assert manager.expiry_timestamp == pytest.approx(expected_expiry, abs=1.0)

    # Subsequent call: returns cached token without network call
    token2 = manager.get_valid_token(client=client)
    assert token2 == "mock-jwt-token-xyz"
    assert call_count == 1

    # Auth headers verification
    headers = manager.get_auth_headers(client=client)
    assert headers == {"Authorization": "Bearer mock-jwt-token-xyz"}
    assert call_count == 1


def test_token_acquisition_camel_case_response_keys(monkeypatch):
    """Verify handling of camelCase response keys (accessToken, expiryTimestamp)."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")

    now = time.time()
    expected_expiry = now + 3600.0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accessToken": "camel-jwt-abc", "expiryTimestamp": expected_expiry},
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    token = manager.get_valid_token(client=client)
    assert token == "camel-jwt-abc"
    assert manager.token == "camel-jwt-abc"
    assert manager.expiry_timestamp == pytest.approx(expected_expiry, abs=1.0)
    assert manager.get_auth_headers(client=client) == {"Authorization": "Bearer camel-jwt-abc"}


def test_double_checked_locking_multithreaded_concurrency(monkeypatch):
    """Verify thread-safety and double-checked locking under high concurrent load."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")

    call_count = 0
    lock = threading.Lock()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        time.sleep(0.05)  # Simulate network latency to amplify race conditions
        with lock:
            call_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": "concurrent-jwt-token",
                "expiry_timestamp": time.time() + 3600.0,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    concurrency = 15
    results: list[str | None] = []

    def fetch_token() -> str | None:
        return manager.get_valid_token(client=client)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(fetch_token) for _ in range(concurrency)]
        for f in futures:
            results.append(f.result())

    # All threads must receive the same valid token
    assert all(r == "concurrent-jwt-token" for r in results)
    # Double-checked locking ensures exactly 1 network call occurred
    assert call_count == 1


def test_proactive_refresh_window_300s(monkeypatch):
    """Verify proactive refresh triggers at or within 300s of token expiration."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")

    fake_clock = 1000000.0

    def mock_time():
        return fake_clock

    monkeypatch.setattr(time, "time", mock_time)

    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Each token issued expires in 600s
        return httpx.Response(
            200,
            json={
                "access_token": f"token-round-{call_count}",
                "expiry_timestamp": fake_clock + 600.0,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    # 1. Initial acquisition at T = 1,000,000; expires at 1,000,600
    t1 = manager.get_valid_token(client=client)
    assert t1 == "token-round-1"
    assert call_count == 1

    # 2. Advance time by 290s (T = 1,000,290; remaining 310s > 300s threshold)
    fake_clock += 290.0
    t2 = manager.get_valid_token(client=client)
    assert t2 == "token-round-1"
    assert call_count == 1  # Still valid, no refresh

    # 3. Advance time by 15s (T = 1,000,305; remaining 295s <= 300s threshold)
    fake_clock += 15.0
    t3 = manager.get_valid_token(client=client)
    assert t3 == "token-round-2"
    assert call_count == 2  # Proactively refreshed!

    # 4. Advance time past expiration (T = 1,000,905; remaining < 0s)
    fake_clock += 600.0
    t4 = manager.get_valid_token(client=client)
    assert t4 == "token-round-3"
    assert call_count == 3


def test_robust_epoch_parsing_matrix():
    """Verify resilient Unix epoch timestamp parsing across various types and fallbacks."""
    now = 1700000000.0

    # Valid numeric representations > now
    assert OneMapTokenManager._parse_expiry_timestamp(1700003600, now) == 1700003600.0
    assert OneMapTokenManager._parse_expiry_timestamp(1700003600.5, now) == 1700003600.5
    assert OneMapTokenManager._parse_expiry_timestamp("1700003600", now) == 1700003600.0
    assert OneMapTokenManager._parse_expiry_timestamp("1700003600.75", now) == 1700003600.75
    assert OneMapTokenManager._parse_expiry_timestamp("  1700003600  ", now) == 1700003600.0

    # Fallback to 72h (now + 72 * 3600)
    expected_default = now + 72.0 * 3600.0
    assert OneMapTokenManager._parse_expiry_timestamp(None, now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp("", now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp("invalid-date-string", now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp("NaN", now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp("Infinity", now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp([], now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp({}, now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp(now, now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp(now - 100, now) == expected_default
    assert OneMapTokenManager._parse_expiry_timestamp(-500, now) == expected_default


def test_static_token_fallback(monkeypatch):
    """Verify fallback to ONEMAP_API_TOKEN when service account credentials are not configured."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", None)
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", None)
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", "legacy-static-secret")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Network call should not occur when static token fallback is used")

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    token = manager.get_valid_token(client=client)
    assert token == "legacy-static-secret"

    headers = manager.get_auth_headers(client=client)
    assert headers == {"Authorization": "Bearer legacy-static-secret"}


def test_no_credentials_and_no_fallback_safe_return(monkeypatch):
    """Verify safe return of None and empty dict when neither credentials nor fallback exist."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", None)
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", None)
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)

    manager = OneMapTokenManager()
    assert manager.get_valid_token() is None
    assert manager.get_auth_headers() == {}


def test_invalidate_token(monkeypatch):
    """Verify invalidate_token clears cached state and forces re-acquisition."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")

    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"token-version-{call_count}",
                "expiry_timestamp": time.time() + 3600.0,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    manager = OneMapTokenManager()

    # Initial token
    assert manager.get_valid_token(client=client) == "token-version-1"
    assert call_count == 1
    assert manager.token == "token-version-1"

    # Invalidate
    manager.invalidate_token()
    assert manager.token is None
    assert manager.expiry_timestamp is None

    # Re-acquisition
    assert manager.get_valid_token(client=client) == "token-version-2"
    assert call_count == 2
    assert manager.token == "token-version-2"


def test_network_error_resilience(monkeypatch):
    """Verify resilience when network call fails or returns non-200 status."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc_test@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "secret_pass")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", "fallback-static-token")

    def mock_500_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Internal Server Error"})

    client = httpx.Client(transport=httpx.MockTransport(mock_500_handler))
    manager = OneMapTokenManager()

    # Should fall back to static token when network fails
    token = manager.get_valid_token(client=client)
    assert token == "fallback-static-token"
    assert manager.get_auth_headers(client=client) == {"Authorization": "Bearer fallback-static-token"}

    # When static token is also absent, should return None and {}
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    manager.invalidate_token()
    assert manager.get_valid_token(client=client) is None
    assert manager.get_auth_headers(client=client) == {}


def test_global_singleton_instance_available():
    """Verify module-level singleton instance onemap_token_manager is available."""
    assert isinstance(onemap_token_manager, OneMapTokenManager)

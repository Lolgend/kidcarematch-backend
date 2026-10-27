import pytest
from fastapi.testclient import TestClient

from kidcarematch_backend.main import app

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://kidscare-match-sg.web.app",
    "https://kidscare-match-sg.firebaseapp.com",
]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
def test_cors_preflight_allows_configured_origins(client: TestClient, origin: str) -> None:
    """OPTIONS preflight requests from configured origins receive CORS allow headers."""
    response = client.options(
        "/api/v1/centres/summary",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
def test_cors_simple_request_allows_configured_origins(client: TestClient, origin: str) -> None:
    """Cross-origin GET requests from configured origins include CORS allow headers."""
    response = client.get(
        "/",
        headers={"Origin": origin},
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_unconfigured_origin(client: TestClient) -> None:
    """Cross-origin requests from unconfigured origins do not receive access-control-allow-origin."""
    response = client.options(
        "/api/v1/centres/summary",
        headers={
            "Origin": "https://malicious.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers

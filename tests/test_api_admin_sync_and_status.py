"""Tests for administrative ECDA dataset synchronization and external API health monitoring endpoints."""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.api.v1.admin.status import probe_onemap_api
from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.core.config import settings
from kidcarematch_backend.core.onemap import onemap_token_manager
from kidcarematch_backend.db.db import (
    AccountStatus,
    ExternalDataSourceStatus,
    SyncState,
    User,
    UserRole,
    get_session,
    init_db,
)
from kidcarematch_backend.main import app


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine


@pytest.fixture
def client(test_engine):
    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seed_admin_user(test_engine):
    with Session(test_engine) as session:
        admin = User(
            user_id="admin-sync-uuid-1",
            firebase_uid="firebase_admin_sync_123",
            email="admin.sync@kidcarematch.sg",
            full_name="Sync Admin",
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            admin_level=1,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


@pytest.fixture
def seed_parent_user(test_engine):
    with Session(test_engine) as session:
        parent = User(
            user_id="parent-sync-uuid-1",
            firebase_uid="firebase_parent_sync_123",
            email="parent.sync@example.com",
            full_name="Parent User",
            role=UserRole.PARENT,
            account_status=AccountStatus.ACTIVE,
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        return parent


@pytest.fixture
def seed_suspended_admin(test_engine):
    with Session(test_engine) as session:
        admin = User(
            user_id="admin-suspended-sync-1",
            firebase_uid="firebase_suspended_sync_123",
            email="suspended.admin@kidcarematch.sg",
            full_name="Suspended Admin",
            role=UserRole.ADMIN,
            account_status=AccountStatus.SUSPENDED,
            admin_level=2,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


# --- RBAC & Security Boundary Tests ---


def test_sync_ecda_missing_token_returns_401(client: TestClient):
    """POST /api/v1/admin/sync/ecda without token must return HTTP 401."""
    response = client.post("/api/v1/admin/sync/ecda")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_sync_ecda_parent_role_returns_403(client: TestClient, seed_parent_user):
    """POST /api/v1/admin/sync/ecda by parent user must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_parent_user.firebase_uid, email=seed_parent_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/sync/ecda",
        headers={"Authorization": "Bearer mock-parent-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_sync_ecda_suspended_admin_returns_403(client: TestClient, seed_suspended_admin):
    """POST /api/v1/admin/sync/ecda by suspended admin must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/sync/ecda",
        headers={"Authorization": "Bearer mock-suspended-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_get_status_missing_token_returns_401(client: TestClient):
    """GET /api/v1/admin/status without token must return HTTP 401."""
    response = client.get("/api/v1/admin/status")
    assert response.status_code == 401


def test_get_status_parent_role_returns_403(client: TestClient, seed_parent_user):
    """GET /api/v1/admin/status by parent user must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_parent_user.firebase_uid, email=seed_parent_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-parent-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_get_status_suspended_admin_returns_403(client: TestClient, seed_suspended_admin):
    """GET /api/v1/admin/status by suspended admin must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-suspended-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_status_refresh_missing_token_returns_401(client: TestClient):
    """POST /api/v1/admin/status/refresh without token must return HTTP 401."""
    response = client.post("/api/v1/admin/status/refresh")
    assert response.status_code == 401


def test_status_refresh_parent_role_returns_403(client: TestClient, seed_parent_user):
    """POST /api/v1/admin/status/refresh by parent user must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_parent_user.firebase_uid, email=seed_parent_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-parent-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_status_refresh_suspended_admin_returns_403(client: TestClient, seed_suspended_admin):
    """POST /api/v1/admin/status/refresh by suspended admin must return HTTP 403."""
    mock_claims = FirebaseTokenClaims(uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-suspended-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


# --- Asynchronous ECDA Sync Trigger Tests ---


def test_sync_ecda_returns_202_and_structured_payload(client: TestClient, seed_admin_user, monkeypatch):
    """POST /api/v1/admin/sync/ecda enqueues background task and immediately returns HTTP 202 Accepted."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # Intercept sync_centres to avoid external network requests
    mock_sync_called = []
    def mock_sync(**kwargs):
        mock_sync_called.append(True)
        return {"centres_fetched": 0}

    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "sync_centres", mock_sync)
    # Also patch in sync router module if imported directly
    import sys
    if "kidcarematch_backend.api.v1.admin.sync" in sys.modules:
        monkeypatch.setattr(sys.modules["kidcarematch_backend.api.v1.admin.sync"], "sync_centres", mock_sync)

    response = client.post(
        "/api/v1/admin/sync/ecda",
        headers={"Authorization": "Bearer mock-admin-token"},
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "RUNNING"
    assert data["source_id"] == "ECDA_DATASTORE"
    assert "started" in data["message"].lower() or "background" in data["message"].lower()


def test_sync_ecda_background_execution_updates_heartbeat_to_success(
    client: TestClient,
    seed_admin_user,
    test_engine,
    monkeypatch,
):
    """BackgroundTask executes sync_centres and records SUCCESS with record count in heartbeat."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # Mock fetch functions to return controlled test payload
    sample_centres = [
        {
            "centre_code": "SYNC001",
            "centre_name": "Sync Test Centre",
            "last_updated": "2026-10-10",
        }
    ]
    sample_services = [
        {
            "centre_code": "SYNC001",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1200",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-10",
        }
    ]

    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: sample_centres)
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: sample_services)

    response = client.post(
        "/api/v1/admin/sync/ecda",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 202

    # In TestClient, background task completes before post() returns
    with Session(test_engine) as session:
        hb = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert hb is not None
        assert hb.sync_state == SyncState.SUCCESS
        assert hb.records_processed == 2
        assert hb.last_successful_sync is not None
        assert hb.last_attempted_sync is not None
        assert hb.error_message is None


def test_sync_ecda_background_execution_updates_heartbeat_to_failed_on_error(
    client: TestClient,
    seed_admin_user,
    test_engine,
    monkeypatch,
):
    """BackgroundTask records FAILED and error diagnostics upon unhandled exception."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    import kidcarematch_backend.cron.sync_centres as sc_mod
    def mock_fetch_raising(**kwargs):
        raise httpx.ConnectError("Connection refused by data.gov.sg upstream")

    monkeypatch.setattr(sc_mod, "fetch_all_centres", mock_fetch_raising)

    response = client.post(
        "/api/v1/admin/sync/ecda",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 202

    with Session(test_engine) as session:
        hb = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert hb is not None
        assert hb.sync_state == SyncState.FAILED
        assert hb.error_message is not None
        assert "Connection refused" in hb.error_message or "ConnectError" in hb.error_message


# --- External Data Source Status Endpoint Tests ---


def test_get_status_empty_database_returns_monitored_sources_idle(client: TestClient, seed_admin_user):
    """GET /api/v1/admin/status on fresh database returns both ECDA_DATASTORE and ONEMAP_API in IDLE state."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    source_ids = [item["source_id"] for item in data]
    assert "ECDA_DATASTORE" in source_ids
    assert "ONEMAP_API" in source_ids

    for item in data:
        assert item["sync_state"] in ("IDLE", "SUCCESS", "RUNNING", "FAILED")
        assert "source_name" in item
        assert "records_processed" in item


def test_get_status_with_existing_records_returns_persisted_metrics(
    client: TestClient,
    seed_admin_user,
    test_engine,
):
    """GET /api/v1/admin/status retrieves current persisted heartbeat metrics."""
    now = datetime.now(timezone.utc)
    with Session(test_engine) as session:
        ecda_status = ExternalDataSourceStatus(
            source_id="ECDA_DATASTORE",
            source_name="ECDA Preschool Centre Datastore",
            sync_state=SyncState.SUCCESS,
            last_successful_sync=now,
            last_attempted_sync=now,
            records_processed=2500,
            error_message=None,
        )
        onemap_status = ExternalDataSourceStatus(
            source_id="ONEMAP_API",
            source_name="OneMap Geocoding & Mapping API",
            sync_state=SyncState.IDLE,
            records_processed=0,
        )
        session.add(ecda_status)
        session.add(onemap_status)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ECDA_DATASTORE"]["sync_state"] == "SUCCESS"
    assert status_map["ECDA_DATASTORE"]["records_processed"] == 2500
    assert status_map["ONEMAP_API"]["sync_state"] == "IDLE"


# --- Status Refresh On-Demand Probes Tests ---


def test_status_refresh_both_probes_succeed(client: TestClient, seed_admin_user, test_engine, monkeypatch):
    """POST /api/v1/admin/status/refresh pings external endpoints, updates sync_state to SUCCESS, but preserves existing last_successful_sync."""
    earlier_success = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        ecda_status = ExternalDataSourceStatus(
            source_id="ECDA_DATASTORE",
            source_name="ECDA Preschool Datastore",
            sync_state=SyncState.IDLE,
            last_successful_sync=earlier_success,
            last_attempted_sync=earlier_success,
            records_processed=2000,
        )
        session.add(ecda_status)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # Mock probe responses for both data.gov.sg and OneMap
    def mock_get(client_self, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True}
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ECDA_DATASTORE"]["sync_state"] == "SUCCESS"
    assert status_map["ECDA_DATASTORE"]["error_message"] is None
    # Existing last_successful_sync preserved from real dataset synchronization
    assert (
        datetime.fromisoformat(
            status_map["ECDA_DATASTORE"]["last_successful_sync"].replace("Z", "+00:00")
        )
        == earlier_success
    )
    assert status_map["ECDA_DATASTORE"]["last_attempted_sync"] is not None

    assert status_map["ONEMAP_API"]["sync_state"] == "SUCCESS"
    # Never completed real dataset sync, so last_successful_sync remains None
    assert status_map["ONEMAP_API"]["last_successful_sync"] is None
    assert status_map["ONEMAP_API"]["error_message"] is None
    assert status_map["ONEMAP_API"]["last_attempted_sync"] is not None

    with Session(test_engine) as session:
        db_ecda = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert db_ecda is not None
        assert db_ecda.last_successful_sync == earlier_success
        assert db_ecda.last_attempted_sync is not None
        assert db_ecda.last_attempted_sync > earlier_success


def test_status_refresh_probes_failure_records_failed_and_diagnostics(
    client: TestClient,
    seed_admin_user,
    test_engine,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh records FAILED and error diagnostic when upstream probe fails."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    def mock_get(client_self, *args, **kwargs):
        raise httpx.ConnectTimeout("Connection timed out to upstream service")

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ECDA_DATASTORE"]["sync_state"] == "FAILED"
    assert "Connection timed out" in status_map["ECDA_DATASTORE"]["error_message"]

    assert status_map["ONEMAP_API"]["sync_state"] == "FAILED"
    assert "Connection timed out" in status_map["ONEMAP_API"]["error_message"]


def test_status_refresh_preserves_last_successful_sync_on_probe_failure(
    client: TestClient,
    seed_admin_user,
    test_engine,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh must preserve existing last_successful_sync when probe fails."""
    earlier_success = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        onemap_status = ExternalDataSourceStatus(
            source_id="ONEMAP_API",
            source_name="OneMap Geocoding & Mapping API",
            sync_state=SyncState.SUCCESS,
            last_successful_sync=earlier_success,
            last_attempted_sync=earlier_success,
            records_processed=100,
        )
        session.add(onemap_status)
        session.commit()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # ECDA succeeds, OneMap fails with HTTP 503
    def mock_get(client_self, url, *args, **kwargs):
        url_str = str(url)
        if "data.gov.sg" in url_str:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True}
            resp.raise_for_status = MagicMock()
            return resp
        else:
            resp = MagicMock()
            resp.status_code = 503
            resp.text = "Service Unavailable"
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "503 Service Unavailable", request=MagicMock(), response=resp
            )
            return resp

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ECDA_DATASTORE"]["sync_state"] == "SUCCESS"
    assert status_map["ONEMAP_API"]["sync_state"] == "FAILED"
    assert "503" in status_map["ONEMAP_API"]["error_message"]

    # Verify database directly: last_successful_sync was preserved!
    with Session(test_engine) as session:
        hb = session.get(ExternalDataSourceStatus, "ONEMAP_API")
        assert hb is not None
        assert hb.sync_state == SyncState.FAILED
        assert hb.last_successful_sync == earlier_success
        assert hb.last_attempted_sync is not None
        assert hb.last_attempted_sync > earlier_success


# --- OneMap Authentication Health Observability Tests (Ticket #76) ---


def test_get_status_fresh_database_onemap_defaults(client: TestClient, seed_admin_user):
    """GET /api/v1/admin/status on a fresh DB defaults ONEMAP_API to IDLE with 0 records and no errors."""
    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert "ONEMAP_API" in status_map
    onemap = status_map["ONEMAP_API"]
    assert onemap["source_name"] == "OneMap Geocoding & Mapping API"
    assert onemap["sync_state"] == "IDLE"
    assert onemap["records_processed"] == 0
    assert onemap["last_successful_sync"] is None
    assert onemap["last_attempted_sync"] is None
    assert onemap["error_message"] is None


def test_status_refresh_onemap_authenticated_success(
    client: TestClient,
    seed_admin_user,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh acquires token and executes authenticated search probe returning SUCCESS."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "probe_admin@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "probe_secret_password")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    onemap_token_manager.invalidate_token()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    auth_called = False
    search_called = False
    orig_post = httpx.Client.post
    orig_get = httpx.Client.get

    def mock_post(client_self, url, *args, **kwargs):
        nonlocal auth_called
        url_str = str(url)
        if "getToken" in url_str:
            auth_called = True
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "probe-svc-token-success-xyz",
                "expiry_timestamp": time.time() + 3600,
            }
            resp.raise_for_status = MagicMock()
            return resp
        return orig_post(client_self, url, *args, **kwargs)

    def mock_get(client_self, url, *args, **kwargs):
        nonlocal search_called
        url_str = str(url)
        headers = kwargs.get("headers", {})
        if "elastic/search" in url_str:
            search_called = True
            assert headers.get("Authorization") == "Bearer probe-svc-token-success-xyz"
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"found": 1, "results": []}
            resp.raise_for_status = MagicMock()
            return resp
        if "data.gov.sg" in url_str:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True}
            resp.raise_for_status = MagicMock()
            return resp
        return orig_get(client_self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert auth_called is True
    assert search_called is True
    assert status_map["ONEMAP_API"]["sync_state"] == "SUCCESS"
    assert status_map["ONEMAP_API"]["error_message"] is None
    assert status_map["ONEMAP_API"]["last_attempted_sync"] is not None

    # Follow-up GET must also reflect SUCCESS
    get_resp = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert get_resp.status_code == 200
    get_map = {item["source_id"]: item for item in get_resp.json()}
    assert get_map["ONEMAP_API"]["sync_state"] == "SUCCESS"
    assert get_map["ONEMAP_API"]["error_message"] is None


def test_status_refresh_onemap_authenticated_token_failure_401(
    client: TestClient,
    seed_admin_user,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh marks ONEMAP_API as FAILED with diagnostic message when token auth fails."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "probe_admin@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "wrong_credentials")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    onemap_token_manager.invalidate_token()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    orig_post = httpx.Client.post
    orig_get = httpx.Client.get

    def mock_post(client_self, url, *args, **kwargs):
        url_str = str(url)
        if "getToken" in url_str:
            resp = MagicMock()
            resp.status_code = 401
            resp.text = "Unauthorized: Invalid credentials"
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "401 Unauthorized", request=MagicMock(), response=resp
            )
            return resp
        return orig_post(client_self, url, *args, **kwargs)

    def mock_get(client_self, url, *args, **kwargs):
        url_str = str(url)
        if "data.gov.sg" in url_str:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True}
            resp.raise_for_status = MagicMock()
            return resp
        return orig_get(client_self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ONEMAP_API"]["sync_state"] == "FAILED"
    assert status_map["ONEMAP_API"]["error_message"] is not None
    assert "OneMap authentication failed" in status_map["ONEMAP_API"]["error_message"]

    # Verify GET /api/v1/admin/status reports FAILED and diagnostic message
    get_resp = client.get(
        "/api/v1/admin/status",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert get_resp.status_code == 200
    get_map = {item["source_id"]: item for item in get_resp.json()}
    assert get_map["ONEMAP_API"]["sync_state"] == "FAILED"
    assert "OneMap authentication failed" in get_map["ONEMAP_API"]["error_message"]


def test_status_refresh_onemap_authenticated_token_network_error(
    client: TestClient,
    seed_admin_user,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh reports FAILED when token manager encounters network error."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "probe_admin@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "probe_secret_password")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    onemap_token_manager.invalidate_token()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims
    orig_post = httpx.Client.post
    orig_get = httpx.Client.get

    def mock_post(client_self, url, *args, **kwargs):
        url_str = str(url)
        if "getToken" in url_str:
            raise httpx.ConnectError("Connection refused by auth endpoint")
        return orig_post(client_self, url, *args, **kwargs)

    def mock_get(client_self, url, *args, **kwargs):
        url_str = str(url)
        if "data.gov.sg" in url_str:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True}
            resp.raise_for_status = MagicMock()
            return resp
        return orig_get(client_self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert status_map["ONEMAP_API"]["sync_state"] == "FAILED"
    assert "OneMap authentication failed" in status_map["ONEMAP_API"]["error_message"]


def test_status_refresh_onemap_unauthenticated_without_credentials(
    client: TestClient,
    seed_admin_user,
    monkeypatch,
):
    """POST /api/v1/admin/status/refresh executes unauthenticated public probe when credentials not configured."""
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", None)
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", None)
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    onemap_token_manager.invalidate_token()

    mock_claims = FirebaseTokenClaims(uid=seed_admin_user.firebase_uid, email=seed_admin_user.email)
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    search_headers_captured: dict[str, str] = {}
    orig_get = httpx.Client.get

    def mock_get(client_self, url, *args, **kwargs):
        nonlocal search_headers_captured
        url_str = str(url)
        if "elastic/search" in url_str:
            search_headers_captured = kwargs.get("headers", {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"found": 1, "results": []}
            resp.raise_for_status = MagicMock()
            return resp
        if "data.gov.sg" in url_str:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"success": True}
            resp.raise_for_status = MagicMock()
            return resp
        return orig_get(client_self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    response = client.post(
        "/api/v1/admin/status/refresh",
        headers={"Authorization": "Bearer mock-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    status_map = {item["source_id"]: item for item in data}

    assert "Authorization" not in search_headers_captured
    assert status_map["ONEMAP_API"]["sync_state"] == "SUCCESS"
    assert status_map["ONEMAP_API"]["error_message"] is None


def test_probe_onemap_api_unit_behavior(monkeypatch):
    """Unit tests for probe_onemap_api helper covering auth success, failure, 401 eviction, and public probe."""
    # 1. Configured credentials + valid token + search 200 -> (True, None)
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "pass")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)
    onemap_token_manager.invalidate_token()

    def mock_post(client_self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "valid-token-abc", "expiry_timestamp": time.time() + 1000}
        resp.raise_for_status = MagicMock()
        return resp

    def mock_get_200(client_self, url, *args, **kwargs):
        assert kwargs.get("headers", {}).get("Authorization") == "Bearer valid-token-abc"
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    monkeypatch.setattr(httpx.Client, "get", mock_get_200)

    with httpx.Client() as client:
        ok, err = probe_onemap_api(client)
        assert ok is True
        assert err is None

    # 2. Configured credentials + token manager raises exception -> (False, "OneMap authentication failed: ...")
    def mock_get_valid_token_raising(*args, **kwargs):
        raise httpx.RequestError("Upstream timeout")

    monkeypatch.setattr(onemap_token_manager, "get_valid_token", mock_get_valid_token_raising)

    with httpx.Client() as client:
        ok, err = probe_onemap_api(client)
        assert ok is False
        assert err is not None
        assert err.startswith("OneMap authentication failed: Upstream timeout")

    # 3. Configured credentials + token manager returns None -> (False, "OneMap authentication failed: ...")
    monkeypatch.setattr(onemap_token_manager, "get_valid_token", lambda *args, **kwargs: None)
    with httpx.Client() as client:
        ok, err = probe_onemap_api(client)
        assert ok is False
        assert err is not None
        assert "OneMap authentication failed" in err
        assert "Invalid credentials or token acquisition failed" in err

    # 4. Search endpoint returns 401 -> invalidates token and returns (False, err)
    onemap_token_manager.invalidate_token()
    monkeypatch.undo()  # restore real methods
    monkeypatch.setattr(settings, "ONEMAP_EMAIL", "svc@example.com")
    monkeypatch.setattr(settings, "ONEMAP_PASSWORD", "pass")
    monkeypatch.setattr(settings, "ONEMAP_API_TOKEN", None)

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    def mock_get_401(client_self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 401
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=resp
        )
        return resp

    monkeypatch.setattr(httpx.Client, "get", mock_get_401)

    with httpx.Client() as client:
        ok, err = probe_onemap_api(client)
        assert ok is False
        assert "401" in str(err)
        assert onemap_token_manager.token is None


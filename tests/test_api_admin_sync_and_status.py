"""Tests for administrative ECDA dataset synchronization and external API health monitoring endpoints."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
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

"""Tests for administrative subsidy-rule configuration matrix and granular CRUD endpoints."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

from kidcarematch_backend.auth.security import FirebaseTokenClaims, get_token_claims
from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CareType,
    CitizenshipType,
    SubsidyMetric,
    SubsidyRule,
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
            user_id="admin-uuid-1",
            firebase_uid="firebase_admin_123",
            email="admin@kidcarematch.sg",
            full_name="Admin Boss",
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
            user_id="parent-uuid-1",
            firebase_uid="firebase_parent_123",
            email="parent@example.com",
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
            user_id="admin-suspended-1",
            firebase_uid="firebase_suspended_admin_123",
            email="suspended_admin@kidcarematch.sg",
            full_name="Suspended Admin",
            role=UserRole.ADMIN,
            account_status=AccountStatus.SUSPENDED,
            admin_level=2,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        return admin


@pytest.fixture
def seed_subsidy_rules(test_engine):
    with Session(test_engine) as session:
        r1 = SubsidyRule(
            rule_id="SR_BASIC_INFANT_WA",
            subsidy_name="Basic Infant Subsidy",
            tier_name="Base Tier",
            care_type=CareType.INFANT_CARE,
            metric=SubsidyMetric.FIXED,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
            citizenship=CitizenshipType.SC,
            income_min=None,
            income_max=None,
            subsidy_amount=600.0,
            effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        r2 = SubsidyRule(
            rule_id="SR_ADD_INFANT_T1",
            subsidy_name="Additional Infant Subsidy Tier 1",
            tier_name="Tier 1",
            care_type=CareType.INFANT_CARE,
            metric=SubsidyMetric.HOUSEHOLD_INCOME,
            applicant_status=ApplicantStatus.WORKING_APPLICANT,
            citizenship=CitizenshipType.SC,
            income_min=0.0,
            income_max=3000.0,
            subsidy_amount=710.0,
            effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        session.add_all([r1, r2])
        session.commit()
        return [r1, r2]


# --- Security & Authorization Guard Tests ---


def test_get_subsidy_rules_missing_token_returns_401(client: TestClient):
    """Missing Bearer token on subsidy rules list must return HTTP 401 Unauthorized."""
    response = client.get("/api/v1/admin/subsidy-rules")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_get_subsidy_rules_parent_role_returns_403(
    client: TestClient, seed_parent_user
):
    """Authenticated user with role PARENT must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid, email=seed_parent_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/subsidy-rules",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "administrative privileges required" in response.json()["detail"].lower()


def test_get_subsidy_rules_suspended_admin_returns_403(
    client: TestClient, seed_suspended_admin
):
    """Suspended admin calling subsidy rules list must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_suspended_admin.firebase_uid, email=seed_suspended_admin.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/subsidy-rules",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"].lower()


def test_post_subsidy_rule_parent_role_returns_403(
    client: TestClient, seed_parent_user
):
    """Parent user attempting to create a subsidy rule must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid, email=seed_parent_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Unauthorized Rule",
        "care_type": "CHILD_CARE",
        "metric": "FIXED",
        "citizenship": "SC",
        "subsidy_amount": 300.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403


def test_put_subsidy_rule_parent_role_returns_403(client: TestClient, seed_parent_user):
    """Parent user attempting to update a subsidy rule must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid, email=seed_parent_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.put(
        "/api/v1/admin/subsidy-rules/SR_SOME_RULE",
        json={"subsidy_amount": 500.0},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403


def test_delete_subsidy_rule_parent_role_returns_403(
    client: TestClient, seed_parent_user
):
    """Parent user attempting to delete a subsidy rule must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid, email=seed_parent_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.delete(
        "/api/v1/admin/subsidy-rules/SR_SOME_RULE",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403


def test_bulk_put_subsidy_rules_parent_role_returns_403(
    client: TestClient, seed_parent_user
):
    """Parent user attempting bulk subsidy matrix update must receive HTTP 403 Forbidden."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_parent_user.firebase_uid, email=seed_parent_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=[],
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 403


def test_subsidy_rules_route_prefix_isolation(client: TestClient):
    """Unprefixed /subsidy-rules route must return 404 Not Found."""
    response = client.get("/subsidy-rules")
    assert response.status_code == 404


# --- GET /api/v1/admin/subsidy-rules Tests ---


def test_get_subsidy_rules_empty_returns_200_empty_list(
    client: TestClient, seed_admin_user
):
    """GET /api/v1/admin/subsidy-rules on empty DB returns HTTP 200 with empty list."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/subsidy-rules",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    assert response.json() == []


def test_get_subsidy_rules_returns_full_domain_fields(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
):
    """GET /api/v1/admin/subsidy-rules returns all configured rules with complete domain fields."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.get(
        "/api/v1/admin/subsidy-rules",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    rule_map = {r["rule_id"]: r for r in data}
    assert "SR_BASIC_INFANT_WA" in rule_map
    assert "SR_ADD_INFANT_T1" in rule_map

    basic = rule_map["SR_BASIC_INFANT_WA"]
    assert basic["rule_id"] == "SR_BASIC_INFANT_WA"
    assert basic["subsidy_name"] == "Basic Infant Subsidy"
    assert basic["tier_name"] == "Base Tier"
    assert basic["care_type"] == "INFANT_CARE"
    assert basic["metric"] == "FIXED"
    assert basic["applicant_status"] == "WORKING_APPLICANT"
    assert basic["citizenship"] == "SC"
    assert basic["income_min"] is None
    assert basic["income_max"] is None
    assert basic["subsidy_amount"] == 600.0
    assert basic["effective_date"] is not None

    add_t1 = rule_map["SR_ADD_INFANT_T1"]
    assert add_t1["income_min"] == 0.0
    assert add_t1["income_max"] == 3000.0
    assert add_t1["subsidy_amount"] == 710.0


# --- POST /api/v1/admin/subsidy-rules Tests ---


def test_post_subsidy_rule_success_with_generated_id(
    client: TestClient,
    seed_admin_user,
    test_engine,
):
    """POST /api/v1/admin/subsidy-rules creates a rule with auto-generated UUID when rule_id omitted."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Child Care Basic Subsidy",
        "tier_name": "Standard",
        "care_type": "CHILD_CARE",
        "metric": "FIXED",
        "applicant_status": "WORKING_APPLICANT",
        "citizenship": "SC",
        "income_min": None,
        "income_max": None,
        "subsidy_amount": 300.0,
        "effective_date": "2026-01-01T00:00:00Z",
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["rule_id"] is not None
    assert len(data["rule_id"]) > 0
    assert data["subsidy_name"] == "Child Care Basic Subsidy"
    assert data["care_type"] == "CHILD_CARE"
    assert data["metric"] == "FIXED"
    assert data["subsidy_amount"] == 300.0

    # Verify persisted in database
    with Session(test_engine) as session:
        persisted = session.get(SubsidyRule, data["rule_id"])
        assert persisted is not None
        assert persisted.subsidy_name == "Child Care Basic Subsidy"
        assert persisted.subsidy_amount == 300.0


def test_post_subsidy_rule_success_with_explicit_id(
    client: TestClient,
    seed_admin_user,
    test_engine,
):
    """POST /api/v1/admin/subsidy-rules creates a rule preserving an explicit rule_id."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "rule_id": "SR_EXPLICIT_TEST_1",
        "subsidy_name": "Explicit ID Rule",
        "care_type": "INFANT_CARE",
        "metric": "PER_CAPITA_INCOME",
        "applicant_status": "WORKING_APPLICANT",
        "citizenship": "SPR",
        "income_min": 1000.0,
        "income_max": 2000.0,
        "subsidy_amount": 450.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["rule_id"] == "SR_EXPLICIT_TEST_1"
    assert data["citizenship"] == "SPR"
    assert data["metric"] == "PER_CAPITA_INCOME"

    with Session(test_engine) as session:
        rule = session.get(SubsidyRule, "SR_EXPLICIT_TEST_1")
        assert rule is not None
        assert rule.subsidy_amount == 450.0


def test_post_subsidy_rule_duplicate_id_returns_409(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
):
    """POST /api/v1/admin/subsidy-rules with an already existing rule_id returns HTTP 409 Conflict."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "rule_id": "SR_BASIC_INFANT_WA",  # already exists in seed_subsidy_rules
        "subsidy_name": "Duplicate Rule",
        "care_type": "INFANT_CARE",
        "metric": "FIXED",
        "subsidy_amount": 999.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_post_subsidy_rule_invalid_care_type_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: invalid care_type must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Invalid Care Rule",
        "care_type": "ELDER_CARE",
        "metric": "FIXED",
        "subsidy_amount": 100.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


def test_post_subsidy_rule_invalid_metric_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: invalid metric must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Invalid Metric Rule",
        "care_type": "INFANT_CARE",
        "metric": "UNLIMITED",
        "subsidy_amount": 100.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


def test_post_subsidy_rule_invalid_applicant_status_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: invalid applicant_status must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Invalid Status Rule",
        "care_type": "INFANT_CARE",
        "metric": "FIXED",
        "applicant_status": "SEEKING_JOB",
        "subsidy_amount": 100.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


def test_post_subsidy_rule_invalid_citizenship_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: invalid citizenship must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Invalid Citizenship Rule",
        "care_type": "INFANT_CARE",
        "metric": "FIXED",
        "citizenship": "EXPATS",
        "subsidy_amount": 100.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


def test_post_subsidy_rule_negative_amount_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: negative subsidy_amount must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Negative Amount Rule",
        "care_type": "INFANT_CARE",
        "metric": "FIXED",
        "subsidy_amount": -50.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


def test_post_subsidy_rule_invalid_income_range_returns_422(
    client: TestClient, seed_admin_user
):
    """Schema validation: income_min > income_max must return HTTP 422 Unprocessable Entity."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "subsidy_name": "Invalid Range Rule",
        "care_type": "INFANT_CARE",
        "metric": "HOUSEHOLD_INCOME",
        "income_min": 5000.0,
        "income_max": 2000.0,
        "subsidy_amount": 100.0,
    }
    response = client.post(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


# --- PUT /api/v1/admin/subsidy-rules/{rule_id} Tests ---


def test_put_subsidy_rule_not_found_returns_404(client: TestClient, seed_admin_user):
    """PUT /api/v1/admin/subsidy-rules/{rule_id} for non-existent ID returns HTTP 404 Not Found."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.put(
        "/api/v1/admin/subsidy-rules/SR_NON_EXISTENT",
        json={"subsidy_amount": 500.0},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_put_subsidy_rule_in_place_update_success(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """PUT /api/v1/admin/subsidy-rules/{rule_id} updates rule attributes in-place."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    target_rule_id = "SR_BASIC_INFANT_WA"
    payload = {
        "subsidy_amount": 650.0,
        "tier_name": "Updated Tier Name",
        "income_max": 12000.0,
    }
    response = client.put(
        f"/api/v1/admin/subsidy-rules/{target_rule_id}",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["rule_id"] == target_rule_id
    assert data["subsidy_amount"] == 650.0
    assert data["tier_name"] == "Updated Tier Name"
    assert data["income_max"] == 12000.0
    # Other fields remain preserved
    assert data["subsidy_name"] == "Basic Infant Subsidy"
    assert data["care_type"] == "INFANT_CARE"

    # Verify database persistence
    with Session(test_engine) as session:
        updated = session.get(SubsidyRule, target_rule_id)
        assert updated is not None
        assert updated.subsidy_amount == 650.0
        assert updated.tier_name == "Updated Tier Name"
        assert updated.income_max == 12000.0


def test_put_subsidy_rule_invalid_enum_returns_422(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
):
    """PUT /api/v1/admin/subsidy-rules/{rule_id} with invalid enum value returns HTTP 422."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.put(
        "/api/v1/admin/subsidy-rules/SR_BASIC_INFANT_WA",
        json={"care_type": "INVALID_TYPE"},
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 422


# --- DELETE /api/v1/admin/subsidy-rules/{rule_id} Tests ---


def test_delete_subsidy_rule_not_found_returns_404(client: TestClient, seed_admin_user):
    """DELETE /api/v1/admin/subsidy-rules/{rule_id} for non-existent rule returns HTTP 404."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.delete(
        "/api/v1/admin/subsidy-rules/SR_NON_EXISTENT",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_delete_subsidy_rule_success_returns_204(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """DELETE /api/v1/admin/subsidy-rules/{rule_id} removes rule and returns HTTP 204 No Content."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    target_rule_id = "SR_BASIC_INFANT_WA"
    response = client.delete(
        f"/api/v1/admin/subsidy-rules/{target_rule_id}",
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 204

    # Verify completely purged from database
    with Session(test_engine) as session:
        deleted = session.get(SubsidyRule, target_rule_id)
        assert deleted is None


# --- PUT /api/v1/admin/subsidy-rules (Bulk Matrix Update / Replace) Tests ---


def test_bulk_put_subsidy_rules_array_payload_replaces_matrix(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """Bulk PUT with JSON array replaces matrix: updates existing, adds new, removes omitted."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # Incoming matrix contains:
    # 1. SR_BASIC_INFANT_WA updated with subsidy_amount 620.0
    # 2. A brand new rule SR_NEW_CC_RULE
    # Note: SR_ADD_INFANT_T1 is omitted and should be deleted in replacement
    payload = [
        {
            "rule_id": "SR_BASIC_INFANT_WA",
            "subsidy_name": "Basic Infant Subsidy (Revised)",
            "tier_name": "Base Tier Revised",
            "care_type": "INFANT_CARE",
            "metric": "FIXED",
            "applicant_status": "WORKING_APPLICANT",
            "citizenship": "SC",
            "subsidy_amount": 620.0,
        },
        {
            "rule_id": "SR_NEW_CC_RULE",
            "subsidy_name": "New Childcare Rule",
            "tier_name": "New Tier",
            "care_type": "CHILD_CARE",
            "metric": "FIXED",
            "applicant_status": "WORKING_APPLICANT",
            "citizenship": "SC",
            "subsidy_amount": 350.0,
        },
    ]

    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    rule_ids = {r["rule_id"] for r in data}
    assert rule_ids == {"SR_BASIC_INFANT_WA", "SR_NEW_CC_RULE"}

    # Verify DB state
    with Session(test_engine) as session:
        rules = session.exec(select(SubsidyRule)).all()
        assert len(rules) == 2
        rule_dict = {r.rule_id: r for r in rules}
        assert rule_dict["SR_BASIC_INFANT_WA"].subsidy_amount == 620.0
        assert (
            rule_dict["SR_BASIC_INFANT_WA"].subsidy_name
            == "Basic Infant Subsidy (Revised)"
        )
        assert rule_dict["SR_NEW_CC_RULE"].subsidy_amount == 350.0
        # Omitted rule must be gone
        assert "SR_ADD_INFANT_T1" not in rule_dict


def test_bulk_put_subsidy_rules_wrapped_payload_replaces_matrix(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """Bulk PUT with {"rules": [...]} object payload replaces configuration matrix."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = {
        "rules": [
            {
                "rule_id": "SR_REPLACED_SINGLE",
                "subsidy_name": "Single Matrix Rule",
                "care_type": "CHILD_CARE",
                "metric": "FIXED",
                "citizenship": "SC",
                "subsidy_amount": 400.0,
            }
        ]
    }

    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["rule_id"] == "SR_REPLACED_SINGLE"

    with Session(test_engine) as session:
        rules = session.exec(select(SubsidyRule)).all()
        assert len(rules) == 1
        assert rules[0].rule_id == "SR_REPLACED_SINGLE"


def test_bulk_put_subsidy_rules_unconditionally_replaces_matrix(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """Bulk PUT unconditionally replaces matrix: unmentioned rules are purged."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    # Update SR_BASIC_INFANT_WA, do not mention SR_ADD_INFANT_T1
    payload = [
        {
            "rule_id": "SR_BASIC_INFANT_WA",
            "subsidy_name": "Basic Infant Subsidy (Replaced)",
            "care_type": "INFANT_CARE",
            "metric": "FIXED",
            "citizenship": "SC",
            "subsidy_amount": 630.0,
        }
    ]

    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200

    with Session(test_engine) as session:
        rules = session.exec(select(SubsidyRule)).all()
        assert len(rules) == 1  # Only the mentioned rule exists!
        assert rules[0].rule_id == "SR_BASIC_INFANT_WA"
        assert rules[0].subsidy_amount == 630.0


def test_bulk_put_subsidy_rules_empty_payload_clears_matrix(
    client: TestClient,
    seed_admin_user,
    seed_subsidy_rules,
    test_engine,
):
    """Bulk PUT with empty list [] clears active configuration matrix."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=[],
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 200
    assert response.json() == []

    with Session(test_engine) as session:
        rules = session.exec(select(SubsidyRule)).all()
        assert len(rules) == 0


def test_bulk_put_subsidy_rules_duplicate_ids_in_payload_returns_400(
    client: TestClient,
    seed_admin_user,
):
    """Bulk PUT with duplicate rule_id in payload must return HTTP 400 Bad Request."""
    mock_claims = FirebaseTokenClaims(
        uid=seed_admin_user.firebase_uid, email=seed_admin_user.email
    )
    app.dependency_overrides[get_token_claims] = lambda: mock_claims

    payload = [
        {
            "rule_id": "SR_DUPLICATE",
            "subsidy_name": "First Instance",
            "care_type": "CHILD_CARE",
            "metric": "FIXED",
            "subsidy_amount": 300.0,
        },
        {
            "rule_id": "SR_DUPLICATE",
            "subsidy_name": "Second Instance",
            "care_type": "CHILD_CARE",
            "metric": "FIXED",
            "subsidy_amount": 350.0,
        },
    ]
    response = client.put(
        "/api/v1/admin/subsidy-rules",
        json=payload,
        headers={"Authorization": "Bearer mock-token"},
    )
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()

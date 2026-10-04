from datetime import datetime, timezone
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    SubsidyRule,
    User,
    init_db,
)




@pytest.fixture
def test_session():
    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    with Session(test_engine) as session:
        yield session


def test_create_parent_and_admin_users(test_session: Session):
    parent = User(
        user_id="user_parent_01",
        email="parent@example.com",
        password_hash="hashed_pw_123",
        full_name="Jane Doe",
        role="PARENT",
        account_status="ACTIVE",
    )
    admin = User(
        user_id="user_admin_01",
        email="admin@example.com",
        password_hash="hashed_admin_pw",
        full_name="Admin John",
        role="ADMIN",
        account_status="ACTIVE",
        admin_level=1,
    )
    test_session.add(parent)
    test_session.add(admin)
    test_session.commit()

    saved_parent = test_session.exec(select(User).where(User.email == "parent@example.com")).first()
    assert saved_parent is not None
    assert saved_parent.user_id == "user_parent_01"
    assert saved_parent.full_name == "Jane Doe"
    assert saved_parent.role == "PARENT"
    assert saved_parent.account_status == "ACTIVE"
    assert saved_parent.admin_level is None

    saved_admin = test_session.exec(select(User).where(User.email == "admin@example.com")).first()
    assert saved_admin is not None
    assert saved_admin.user_id == "user_admin_01"
    assert saved_admin.full_name == "Admin John"
    assert saved_admin.role == "ADMIN"
    assert saved_admin.account_status == "ACTIVE"
    assert saved_admin.admin_level == 1


def test_user_email_unique_constraint(test_session: Session):
    user1 = User(
        user_id="user_01",
        email="duplicate@example.com",
        password_hash="pw1",
        full_name="User One",
        role="PARENT",
        account_status="ACTIVE",
    )
    test_session.add(user1)
    test_session.commit()

    user2 = User(
        user_id="user_02",
        email="duplicate@example.com",
        password_hash="pw2",
        full_name="User Two",
        role="PARENT",
        account_status="ACTIVE",
    )
    test_session.add(user2)
    with pytest.raises(IntegrityError):
        test_session.commit()


def test_create_and_query_subsidy_rules(test_session: Session):
    # 1. Basic Subsidy: Universal for working mothers in infant care
    rule_basic = SubsidyRule(
        rule_id="SR_BASIC_INFANT_WM",
        subsidy_name="Basic Subsidy",
        tier_name="Standard",
        care_type="INFANT_CARE",
        metric="FIXED",
        applicant_status="WORKING_MOTHER",
        citizenship="SC",
        income_min=None,
        income_max=None,
        subsidy_amount=600.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    # 2. Additional Subsidy Tier 1: Gross monthly household income <= 3000
    rule_add_tier1 = SubsidyRule(
        rule_id="SR_ADD_INFANT_T1",
        subsidy_name="Additional Subsidy",
        tier_name="Tier 1",
        care_type="INFANT_CARE",
        metric="HOUSEHOLD_INCOME",
        applicant_status="WORKING_MOTHER",
        citizenship="SC",
        income_min=0.0,
        income_max=3000.0,
        subsidy_amount=710.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    # 3. Additional Subsidy Tier 2: Gross monthly household income 3001 to 6000
    rule_add_tier2 = SubsidyRule(
        rule_id="SR_ADD_INFANT_T2",
        subsidy_name="Additional Subsidy",
        tier_name="Tier 2",
        care_type="INFANT_CARE",
        metric="HOUSEHOLD_INCOME",
        applicant_status="WORKING_MOTHER",
        citizenship="SC",
        income_min=3000.01,
        income_max=6000.0,
        subsidy_amount=540.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    # 4. Child Care Basic Subsidy
    rule_cc_basic = SubsidyRule(
        rule_id="SR_BASIC_CC_WM",
        subsidy_name="Basic Subsidy",
        tier_name="Standard",
        care_type="CHILD_CARE",
        metric="FIXED",
        applicant_status="WORKING_MOTHER",
        citizenship="SC",
        income_min=None,
        income_max=None,
        subsidy_amount=300.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    test_session.add_all([rule_basic, rule_add_tier1, rule_add_tier2, rule_cc_basic])
    test_session.commit()

    # Query matching rules for a working mother seeking Infant Care with household income $2,500
    household_income = 2500.0
    statement = (
        select(SubsidyRule)
        .where(SubsidyRule.care_type == "INFANT_CARE")
        .where(SubsidyRule.applicant_status == "WORKING_MOTHER")
        .where(SubsidyRule.citizenship == "SC")
    )
    all_eligible = test_session.exec(statement).all()
    assert len(all_eligible) == 3

    # Match rules applicable to $2,500 income
    applicable_rules = [
        r for r in all_eligible
        if (r.metric == "FIXED") or (
            r.metric == "HOUSEHOLD_INCOME"
            and (r.income_min is None or r.income_min <= household_income)
            and (r.income_max is None or household_income <= r.income_max)
        )
    ]
    assert len(applicable_rules) == 2
    rule_names = {r.rule_id for r in applicable_rules}
    assert rule_names == {"SR_BASIC_INFANT_WM", "SR_ADD_INFANT_T1"}
    total_subsidy = sum(r.subsidy_amount for r in applicable_rules)
    assert total_subsidy == 600.0 + 710.0 == 1310.0


def test_external_data_source_status_lifecycle(test_session: Session):
    # 1. Initialize heartbeat status record
    status = ExternalDataSourceStatus(
        source_id="ECDA_DATASTORE",
        source_name="ECDA Preschool Centre Datastore",
        sync_state="IDLE",
        records_processed=0,
    )
    test_session.add(status)
    test_session.commit()

    saved = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert saved is not None
    assert saved.sync_state == "IDLE"
    assert saved.records_processed == 0
    assert saved.last_successful_sync is None
    assert saved.error_message is None

    # 2. Transition to RUNNING
    attempt_time = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
    saved.sync_state = "RUNNING"
    saved.last_attempted_sync = attempt_time
    test_session.add(saved)
    test_session.commit()

    running = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert running is not None
    assert running.sync_state == "RUNNING"
    assert running.last_attempted_sync == attempt_time

    # 3. Transition to SUCCESS
    success_time = datetime(2026, 10, 5, 2, 5, tzinfo=timezone.utc)
    running.sync_state = "SUCCESS"
    running.last_successful_sync = success_time
    running.records_processed = 1500
    running.error_message = None
    test_session.add(running)
    test_session.commit()

    success_rec = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert success_rec is not None
    assert success_rec.sync_state == "SUCCESS"
    assert success_rec.records_processed == 1500
    assert success_rec.last_successful_sync == success_time

    # 4. Transition to FAILED on a subsequent attempt
    failed_attempt_time = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
    success_rec.sync_state = "FAILED"
    success_rec.last_attempted_sync = failed_attempt_time
    success_rec.error_message = "HTTP 502: Bad Gateway from data.gov.sg"
    test_session.add(success_rec)
    test_session.commit()

    failed_rec = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert failed_rec is not None
    assert failed_rec.sync_state == "FAILED"
    assert failed_rec.last_attempted_sync == failed_attempt_time
    # last_successful_sync is preserved from the earlier successful run
    assert failed_rec.last_successful_sync == success_time
    assert failed_rec.error_message == "HTTP 502: Bad Gateway from data.gov.sg"



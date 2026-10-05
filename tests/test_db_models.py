from datetime import datetime, timezone
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from pydantic import ValidationError
from kidcarematch_backend.db.db import (
    AccountStatus,
    ApplicantStatus,
    CareType,
    ExternalDataSourceStatus,
    FamilyPreferenceProfile,
    LocationPreference,
    ModerationStatus,
    PreschoolCentre,
    PreschoolService,
    Review,
    SubsidyMetric,
    SubsidyRule,
    SyncState,
    User,
    UserRole,
    VacancyRecord,
    WatchlistEntry,
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
        role=UserRole.PARENT,
        account_status=AccountStatus.ACTIVE,
    )
    admin = User(
        user_id="user_admin_01",
        email="admin@example.com",
        password_hash="hashed_admin_pw",
        full_name="Admin John",
        role=UserRole.ADMIN,
        account_status=AccountStatus.ACTIVE,
        admin_level=1,
    )
    test_session.add(parent)
    test_session.add(admin)
    test_session.commit()

    saved_parent = test_session.exec(select(User).where(User.email == "parent@example.com")).first()
    assert saved_parent is not None
    assert saved_parent.user_id == "user_parent_01"
    assert saved_parent.full_name == "Jane Doe"
    assert saved_parent.role == UserRole.PARENT
    assert saved_parent.account_status == AccountStatus.ACTIVE
    assert saved_parent.admin_level is None

    saved_admin = test_session.exec(select(User).where(User.email == "admin@example.com")).first()
    assert saved_admin is not None
    assert saved_admin.user_id == "user_admin_01"
    assert saved_admin.full_name == "Admin John"
    assert saved_admin.role == UserRole.ADMIN
    assert saved_admin.account_status == AccountStatus.ACTIVE
    assert saved_admin.admin_level == 1


def test_user_email_unique_constraint(test_session: Session):
    user1 = User(
        user_id="user_01",
        email="duplicate@example.com",
        password_hash="pw1",
        full_name="User One",
        role=UserRole.PARENT,
        account_status=AccountStatus.ACTIVE,
    )
    test_session.add(user1)
    test_session.commit()

    user2 = User(
        user_id="user_02",
        email="duplicate@example.com",
        password_hash="pw2",
        full_name="User Two",
        role=UserRole.PARENT,
        account_status=AccountStatus.ACTIVE,
    )
    test_session.add(user2)
    with pytest.raises(IntegrityError):
        test_session.commit()


def test_create_and_query_subsidy_rules(test_session: Session):
    # 1. Basic Subsidy: Universal for working applicants in infant care
    rule_basic = SubsidyRule(
        rule_id="SR_BASIC_INFANT_WA",
        subsidy_name="Basic Subsidy",
        tier_name="Standard",
        care_type=CareType.INFANT_CARE,
        metric=SubsidyMetric.FIXED,
        applicant_status=ApplicantStatus.WORKING_APPLICANT,
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
        care_type=CareType.INFANT_CARE,
        metric=SubsidyMetric.HOUSEHOLD_INCOME,
        applicant_status=ApplicantStatus.WORKING_APPLICANT,
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
        care_type=CareType.INFANT_CARE,
        metric=SubsidyMetric.HOUSEHOLD_INCOME,
        applicant_status=ApplicantStatus.WORKING_APPLICANT,
        citizenship="SC",
        income_min=3000.01,
        income_max=6000.0,
        subsidy_amount=540.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    # 4. Child Care Basic Subsidy
    rule_cc_basic = SubsidyRule(
        rule_id="SR_BASIC_CC_WA",
        subsidy_name="Basic Subsidy",
        tier_name="Standard",
        care_type=CareType.CHILD_CARE,
        metric=SubsidyMetric.FIXED,
        applicant_status=ApplicantStatus.WORKING_APPLICANT,
        citizenship="SC",
        income_min=None,
        income_max=None,
        subsidy_amount=300.0,
        effective_date=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )

    test_session.add_all([rule_basic, rule_add_tier1, rule_add_tier2, rule_cc_basic])
    test_session.commit()

    # Query matching rules for a working applicant seeking Infant Care with household income $2,500
    household_income = 2500.0
    statement = (
        select(SubsidyRule)
        .where(SubsidyRule.care_type == CareType.INFANT_CARE)
        .where(SubsidyRule.applicant_status == ApplicantStatus.WORKING_APPLICANT)
        .where(SubsidyRule.citizenship == "SC")
    )
    all_eligible = test_session.exec(statement).all()
    assert len(all_eligible) == 3

    # Match rules applicable to $2,500 income
    applicable_rules = [
        r for r in all_eligible
        if (r.metric == SubsidyMetric.FIXED) or (
            r.metric == SubsidyMetric.HOUSEHOLD_INCOME
            and (r.income_min is None or r.income_min <= household_income)
            and (r.income_max is None or household_income <= r.income_max)
        )
    ]
    assert len(applicable_rules) == 2
    rule_names = {r.rule_id for r in applicable_rules}
    assert rule_names == {"SR_BASIC_INFANT_WA", "SR_ADD_INFANT_T1"}
    total_subsidy = sum(r.subsidy_amount for r in applicable_rules)
    assert total_subsidy == 600.0 + 710.0 == 1310.0


def test_external_data_source_status_lifecycle(test_session: Session):
    # 1. Initialize heartbeat status record
    status = ExternalDataSourceStatus(
        source_id="ECDA_DATASTORE",
        source_name="ECDA Preschool Centre Datastore",
        sync_state=SyncState.IDLE,
        records_processed=0,
    )
    test_session.add(status)
    test_session.commit()

    saved = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert saved is not None
    assert saved.sync_state == SyncState.IDLE
    assert saved.records_processed == 0
    assert saved.last_successful_sync is None
    assert saved.error_message is None

    # 2. Transition to RUNNING
    attempt_time = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
    saved.sync_state = SyncState.RUNNING
    saved.last_attempted_sync = attempt_time
    test_session.add(saved)
    test_session.commit()

    running = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert running is not None
    assert running.sync_state == SyncState.RUNNING
    assert running.last_attempted_sync == attempt_time

    # 3. Transition to SUCCESS
    success_time = datetime(2026, 10, 5, 2, 5, tzinfo=timezone.utc)
    running.sync_state = SyncState.SUCCESS
    running.last_successful_sync = success_time
    running.records_processed = 1500
    running.error_message = None
    test_session.add(running)
    test_session.commit()

    success_rec = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert success_rec is not None
    assert success_rec.sync_state == SyncState.SUCCESS
    assert success_rec.records_processed == 1500
    assert success_rec.last_successful_sync == success_time

    # 4. Transition to FAILED on a subsequent attempt
    failed_attempt_time = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
    success_rec.sync_state = SyncState.FAILED
    success_rec.last_attempted_sync = failed_attempt_time
    success_rec.error_message = "HTTP 502: Bad Gateway from data.gov.sg"
    test_session.add(success_rec)
    test_session.commit()

    failed_rec = test_session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
    assert failed_rec is not None
    assert failed_rec.sync_state == SyncState.FAILED
    assert failed_rec.last_attempted_sync == failed_attempt_time
    # last_successful_sync is preserved from the earlier successful run
    assert failed_rec.last_successful_sync == success_time
    assert failed_rec.error_message == "HTTP 502: Bad Gateway from data.gov.sg"


def test_family_preference_profile_multi_creation_for_parent(test_session: Session):
    parent = User(
        user_id="parent_usr_01",
        email="parent1@example.com",
        password_hash="pw_hash_1",
        full_name="Sarah Tan",
        role=UserRole.PARENT,
    )
    test_session.add(parent)
    test_session.commit()
    assert parent.user_id is not None

    # Create two profiles for the same parent (e.g. two children / scenarios)
    profile1 = FamilyPreferenceProfile(
        profile_id="fpp_child1",
        user_id=parent.user_id,
        profile_name="Infant Search - Lucas",
        level="infant",
        citizenship_status="SC",
        service_required="Full Day",
        home_postal_code="560123",
        workplace_postal_code="018989",
        location_preference=LocationPreference.HOME,
        household_income=6500.0,
        budget_max=1500.0,
        enrollment_month_year=datetime(2027, 1, 1, tzinfo=timezone.utc),
        cost_weight=0.7,
        travel_weight=0.3,
    )
    profile2 = FamilyPreferenceProfile(
        profile_id="fpp_child2",
        user_id=parent.user_id,
        profile_name="Playgroup Search - Lucas",
        level="pg",
        citizenship_status="SC",
        service_required="Half Day",
        home_postal_code="560123",
        workplace_postal_code=None,
        location_preference=LocationPreference.EITHER,
        household_income=6500.0,
        budget_max=900.0,
        enrollment_month_year=datetime(2027, 6, 1, tzinfo=timezone.utc),
        cost_weight=0.4,
        travel_weight=0.6,
    )

    test_session.add_all([profile1, profile2])
    test_session.commit()

    saved_parent = test_session.get(User, "parent_usr_01")
    assert saved_parent is not None
    assert len(saved_parent.preference_profiles) == 2
    profile_names = {p.profile_name for p in saved_parent.preference_profiles}
    assert profile_names == {"Infant Search - Lucas", "Playgroup Search - Lucas"}
    assert all(p.user is not None and p.user.user_id == "parent_usr_01" for p in saved_parent.preference_profiles)
    levels = {p.level for p in saved_parent.preference_profiles}
    assert levels == {"infant", "pg"}


def test_review_creation_and_rating_validation(test_session: Session):
    centre = PreschoolCentre(
        centre_code="CTR_REV_01",
        centre_name="Sunshine Preschool",
    )
    parent = User(
        user_id="parent_rev_01",
        email="parent_rev@example.com",
        password_hash="pw_hash_2",
        full_name="David Lim",
        role=UserRole.PARENT,
    )
    test_session.add_all([centre, parent])
    test_session.commit()
    assert centre.centre_id is not None
    assert parent.user_id is not None

    # Valid Review: rating = 5, created_at automatically defaulted
    review = Review(
        review_id="rev_01",
        centre_id=centre.centre_id,
        user_id=parent.user_id,
        rating=5,
        review_description="Excellent curriculum and caring teachers.",
        moderation_status=ModerationStatus.PENDING,
    )
    test_session.add(review)
    test_session.commit()

    saved_review = test_session.get(Review, "rev_01")
    assert saved_review is not None
    assert saved_review.rating == 5
    assert saved_review.moderation_status == ModerationStatus.PENDING
    assert saved_review.created_at is not None
    assert saved_review.created_at.tzinfo == timezone.utc

    # Out-of-bounds rating: rating = 0 or 6 should raise ValidationError / ValueError
    with pytest.raises((ValidationError, ValueError)):
        Review(
            review_id="rev_invalid_low",
            centre_id=centre.centre_id,
            user_id=parent.user_id,
            rating=0,
        )

    with pytest.raises((ValidationError, ValueError)):
        Review(
            review_id="rev_invalid_high",
            centre_id=centre.centre_id,
            user_id=parent.user_id,
            rating=6,
        )


def test_review_single_per_centre_user_uniqueness(test_session: Session):
    centre = PreschoolCentre(
        centre_code="CTR_REV_02",
        centre_name="Blossom Academy",
    )
    parent = User(
        user_id="parent_rev_02",
        email="parent_rev2@example.com",
        password_hash="pw_hash_3",
        full_name="Karen Koh",
        role=UserRole.PARENT,
    )
    test_session.add_all([centre, parent])
    test_session.commit()
    assert centre.centre_id is not None
    assert parent.user_id is not None

    # First review succeeds
    rev1 = Review(
        review_id="rev_karen_01",
        centre_id=centre.centre_id,
        user_id=parent.user_id,
        rating=4,
        review_description="Good experience overall.",
        moderation_status=ModerationStatus.APPROVED,
    )
    test_session.add(rev1)
    test_session.commit()

    # Second review for the same centre and user fails composite unique constraint
    rev2 = Review(
        review_id="rev_karen_02",
        centre_id=centre.centre_id,
        user_id=parent.user_id,
        rating=5,
        review_description="Updating my review.",
        moderation_status=ModerationStatus.PENDING,
    )
    test_session.add(rev2)
    with pytest.raises(IntegrityError):
        test_session.commit()


def test_review_moderation_workflow_audit(test_session: Session):
    centre = PreschoolCentre(
        centre_code="CTR_REV_03",
        centre_name="Little Stars Kindergarten",
    )
    parent = User(
        user_id="parent_rev_03",
        email="parent_rev3@example.com",
        password_hash="pw_hash_4",
        full_name="Alex Tan",
        role=UserRole.PARENT,
    )
    admin = User(
        user_id="admin_auditor_01",
        email="admin_auditor@example.com",
        password_hash="admin_pw_1",
        full_name="Admin Clara",
        role=UserRole.ADMIN,
        admin_level=2,
    )
    test_session.add_all([centre, parent, admin])
    test_session.commit()
    assert centre.centre_id is not None
    assert parent.user_id is not None
    assert admin.user_id is not None

    # 1. Parent submits review -> PENDING
    review = Review(
        review_id="rev_alex_01",
        centre_id=centre.centre_id,
        user_id=parent.user_id,
        rating=3,
        review_description="Clean place but high turnover.",
    )
    test_session.add(review)
    test_session.commit()

    saved = test_session.get(Review, "rev_alex_01")
    assert saved is not None
    assert saved.moderation_status == ModerationStatus.PENDING
    assert saved.moderated_at is None
    assert saved.moderated_by is None

    # 2. Admin moderates review -> APPROVED
    moderation_time = datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc)
    saved.moderation_status = ModerationStatus.APPROVED
    saved.moderated_at = moderation_time
    saved.moderated_by = admin.user_id
    test_session.add(saved)
    test_session.commit()

    approved_rev = test_session.get(Review, "rev_alex_01")
    assert approved_rev is not None
    assert approved_rev.moderation_status == ModerationStatus.APPROVED
    assert approved_rev.moderated_at == moderation_time
    assert approved_rev.moderated_by == "admin_auditor_01"


def test_watchlist_entry_cohort_subscription_and_uniqueness(test_session: Session):
    centre = PreschoolCentre(
        centre_code="CTR_WATCH_01",
        centre_name="Global Tots Preschool",
    )
    parent = User(
        user_id="parent_watch_01",
        email="parent_watch@example.com",
        password_hash="pw_hash_5",
        full_name="Rachel Green",
        role=UserRole.PARENT,
    )
    test_session.add_all([centre, parent])
    test_session.commit()
    assert parent.user_id is not None
    assert centre.centre_id is not None

    # 1. Watchlist subscription for Infant cohort in March 2027
    entry1 = WatchlistEntry(
        watchlist_id="watch_01",
        user_id=parent.user_id,
        centre_id=centre.centre_id,
        level="infant",
        enrollment_month_year=datetime(2027, 3, 1, tzinfo=timezone.utc),
    )
    # 2. Watchlist subscription for Playgroup cohort in March 2027 (different level -> valid)
    entry2 = WatchlistEntry(
        watchlist_id="watch_02",
        user_id=parent.user_id,
        centre_id=centre.centre_id,
        level="pg",
        enrollment_month_year=datetime(2027, 3, 1, tzinfo=timezone.utc),
    )
    # 3. Watchlist subscription for Infant cohort in April 2027 (different month -> valid)
    entry3 = WatchlistEntry(
        watchlist_id="watch_03",
        user_id=parent.user_id,
        centre_id=centre.centre_id,
        level="infant",
        enrollment_month_year=datetime(2027, 4, 1, tzinfo=timezone.utc),
    )
    test_session.add_all([entry1, entry2, entry3])
    test_session.commit()

    saved_entries = test_session.exec(
        select(WatchlistEntry).where(WatchlistEntry.user_id == parent.user_id)
    ).all()
    assert len(saved_entries) == 3

    # 4. Duplicate subscription for exact same (user_id, centre_id, level, enrollment_month_year) -> IntegrityError
    duplicate_entry = WatchlistEntry(
        watchlist_id="watch_dup",
        user_id=parent.user_id,
        centre_id=centre.centre_id,
        level="infant",
        enrollment_month_year=datetime(2027, 3, 1, tzinfo=timezone.utc),
    )
    test_session.add(duplicate_entry)
    with pytest.raises(IntegrityError):
        test_session.commit()


def test_bidirectional_relationships_navigation(test_session: Session):
    centre = PreschoolCentre(
        centre_code="CTR_NAV_01",
        centre_name="Happy Tots Learning",
    )
    parent = User(
        user_id="parent_nav_01",
        email="parent_nav@example.com",
        password_hash="pw_hash_6",
        full_name="Nav Tester",
        role=UserRole.PARENT,
    )
    test_session.add_all([centre, parent])
    test_session.commit()
    assert parent.user_id is not None
    assert centre.centre_id is not None

    # Add profile, review, and watchlist entry
    profile = FamilyPreferenceProfile(
        profile_id="fpp_nav_01",
        user_id=parent.user_id,
        profile_name="Nav Profile",
        service_required="Full Day",
        level="n1",
    )
    review = Review(
        review_id="rev_nav_01",
        centre_id=centre.centre_id,
        user_id=parent.user_id,
        rating=4,
        review_description="Great environment.",
    )
    watchlist = WatchlistEntry(
        watchlist_id="watch_nav_01",
        user_id=parent.user_id,
        centre_id=centre.centre_id,
        level="n1",
        enrollment_month_year=datetime(2027, 5, 1, tzinfo=timezone.utc),
    )
    test_session.add_all([profile, review, watchlist])
    test_session.commit()

    # Check relationships on User
    refreshed_user = test_session.get(User, "parent_nav_01")
    assert refreshed_user is not None
    assert len(refreshed_user.preference_profiles) == 1
    assert refreshed_user.preference_profiles[0].profile_id == "fpp_nav_01"
    assert len(refreshed_user.reviews) == 1
    assert refreshed_user.reviews[0].review_id == "rev_nav_01"
    assert len(refreshed_user.watchlist_entries) == 1
    assert refreshed_user.watchlist_entries[0].watchlist_id == "watch_nav_01"

    # Check relationships on PreschoolCentre
    refreshed_centre = test_session.get(PreschoolCentre, centre.centre_id)
    assert refreshed_centre is not None
    assert len(refreshed_centre.reviews) == 1
    assert refreshed_centre.reviews[0].review_id == "rev_nav_01"
    assert len(refreshed_centre.watchlist_entries) == 1
    assert refreshed_centre.watchlist_entries[0].watchlist_id == "watch_nav_01"


def test_preschool_centre_cascade_deletion(test_session: Session):
    # Setup PreschoolCentre with User and all 4 child entities
    centre = PreschoolCentre(
        centre_code="CTR_CASCADE_01",
        centre_name="Cascade Centre",
    )
    user = User(
        user_id="user_cascade_01",
        email="cascade@example.com",
        password_hash="pw_hash_cascade",
        full_name="Cascade User",
    )
    test_session.add_all([centre, user])
    test_session.commit()
    assert centre.centre_id is not None
    assert user.user_id is not None

    vacancy = VacancyRecord(
        record_id="CTR_CASCADE_01_infant_202610",
        centre_id=centre.centre_id,
        level_type="infant",
        enrollment_month_year=datetime(2026, 10, 1, tzinfo=timezone.utc),
        vacancy_status="Available",
    )
    service = PreschoolService(
        centre_id=centre.centre_id,
        type_of_service="Full Day",
        level="Infant Care",
        fee=1100.0,
        citizenship_type="SC",
    )
    review = Review(
        review_id="rev_cascade_01",
        centre_id=centre.centre_id,
        user_id=user.user_id,
        rating=5,
    )
    watchlist = WatchlistEntry(
        watchlist_id="watch_cascade_01",
        user_id=user.user_id,
        centre_id=centre.centre_id,
        level="infant",
        enrollment_month_year=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    test_session.add_all([vacancy, service, review, watchlist])
    test_session.commit()

    # Ensure all 4 child records exist
    assert test_session.get(VacancyRecord, "CTR_CASCADE_01_infant_202610") is not None
    assert test_session.exec(select(PreschoolService).where(PreschoolService.centre_id == centre.centre_id)).first() is not None
    assert test_session.get(Review, "rev_cascade_01") is not None
    assert test_session.get(WatchlistEntry, "watch_cascade_01") is not None

    # Delete PreschoolCentre
    test_session.delete(centre)
    test_session.commit()

    # Verify that all 4 dependent records were cascade deleted
    assert test_session.get(VacancyRecord, "CTR_CASCADE_01_infant_202610") is None
    assert test_session.exec(select(PreschoolService).where(PreschoolService.centre_id == centre.centre_id)).first() is None
    assert test_session.get(Review, "rev_cascade_01") is None
    assert test_session.get(WatchlistEntry, "watch_cascade_01") is None
    # User should remain intact
    assert test_session.get(User, "user_cascade_01") is not None

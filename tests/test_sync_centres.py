from datetime import datetime, timezone
from typing import Any
import pytest

from kidcarematch_backend.cron.sync_centres import (
    VACANCY_HORIZONS,
    VACANCY_LEVELS,
    parse_vacancies_for_centre,
)


def make_test_centre_payload(
    centre_code: str | None = "PC0001",
    centre_name: str = "Test Kindergarten",
    last_updated: str = "2026-10-01",
    vacancy_status: str = "Available",
    **extra: Any,
) -> dict[str, Any]:
    """Helper to generate a realistic centre payload with all 42 vacancy fields populated."""
    payload: dict[str, Any] = {
        "centre_code": centre_code,
        "centre_name": centre_name,
        "last_updated": last_updated,
    }
    payload.update(extra)
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            payload.setdefault(f"{lvl}_vacancy_{hor}", vacancy_status)
    return payload


def test_parse_vacancies_for_centre_generates_42_records_with_correct_keys():
    centre_record = {
        "centre_code": "PT8553",
        "last_updated": "2026-10-02",
        "infant_vacancy_current_month": "Not Applicable",
        "infant_vacancy_next_month": "Immediate",
        "infant_vacancy_third_month": "Available",
        "infant_vacancy_fourth_month": "Full",
        "infant_vacancy_fifth_month": "Full",
        "infant_vacancy_sixth_month": "Full",
        "infant_vacancy_seventh_month": "Full",
        "pg_vacancy_current_month": "Full",
        "pg_vacancy_next_month": "Full",
        "pg_vacancy_third_month": "Full",
        "pg_vacancy_fourth_month": "Full",
        "pg_vacancy_fifth_month": "Full",
        "pg_vacancy_sixth_month": "Full",
        "pg_vacancy_seventh_month": "Full",
        "n1_vacancy_current_month": "Full",
        "n1_vacancy_next_month": "Full",
        "n1_vacancy_third_month": "Full",
        "n1_vacancy_fourth_month": "Full",
        "n1_vacancy_fifth_month": "Full",
        "n1_vacancy_sixth_month": "Full",
        "n1_vacancy_seventh_month": "Full",
        "n2_vacancy_current_month": "Full",
        "n2_vacancy_next_month": "Full",
        "n2_vacancy_third_month": "Full",
        "n2_vacancy_fourth_month": "Full",
        "n2_vacancy_fifth_month": "Full",
        "n2_vacancy_sixth_month": "Full",
        "n2_vacancy_seventh_month": "Full",
        "k1_vacancy_current_month": "Full",
        "k1_vacancy_next_month": "Full",
        "k1_vacancy_third_month": "Full",
        "k1_vacancy_fourth_month": "Full",
        "k1_vacancy_fifth_month": "Full",
        "k1_vacancy_sixth_month": "Full",
        "k1_vacancy_seventh_month": "Full",
        "k2_vacancy_current_month": "Full",
        "k2_vacancy_next_month": "Full",
        "k2_vacancy_third_month": "Full",
        "k2_vacancy_fourth_month": "Full",
        "k2_vacancy_fifth_month": "Full",
        "k2_vacancy_sixth_month": "Full",
        "k2_vacancy_seventh_month": "Full",
    }

    records = parse_vacancies_for_centre(centre_record, centre_id=42)

    # 6 levels * 7 horizons = 42 records
    assert len(records) == 42

    # Check first record (infant current month)
    first = next(r for r in records if r.level_type == "infant" and r.record_id == "PT8553_infant_202610")
    assert first.centre_id == 42
    assert first.vacancy_status == "Not Applicable"
    assert first.enrollment_month_year == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    # Check second month (infant next month -> November 2026)
    second = next(r for r in records if r.level_type == "infant" and r.record_id == "PT8553_infant_202611")
    assert second.vacancy_status == "Immediate"
    assert second.enrollment_month_year == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

    # Check rollover across year boundary (seventh month is April 2027)
    seventh = next(r for r in records if r.level_type == "infant" and r.record_id == "PT8553_infant_202704")
    assert seventh.vacancy_status == "Full"
    assert seventh.enrollment_month_year == datetime(2027, 4, 1, 0, 0, tzinfo=timezone.utc)


def test_parse_vacancies_anchored_to_sync_date_ignoring_stale_last_updated():
    # Centre profile has a stale last_updated from January 2025
    centre_record = make_test_centre_payload(
        centre_code="STALE01",
        last_updated="2025-01-10",
        vacancy_status="Full",
        infant_vacancy_current_month="Available",
        infant_vacancy_seventh_month="Full",
    )

    # Explicit sync date is October 2026
    explicit_sync_date = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)
    records = parse_vacancies_for_centre(centre_record, centre_id=99, sync_date=explicit_sync_date)

    assert len(records) == 42

    # Verify that horizons anchor to October 2026 instead of January 2025
    current_rec = next(r for r in records if r.level_type == "infant" and r.record_id == "STALE01_infant_202610")
    assert current_rec.enrollment_month_year == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    assert current_rec.vacancy_status == "Available"

    seventh_rec = next(r for r in records if r.level_type == "infant" and r.record_id == "STALE01_infant_202704")
    assert seventh_rec.enrollment_month_year == datetime(2027, 4, 1, 0, 0, tzinfo=timezone.utc)
    assert seventh_rec.vacancy_status == "Full"

    # Verify NO records were generated using 2025 dates
    assert not any("2025" in r.record_id for r in records)


def test_normalize_preschool_level_canonical_and_free_form():
    from kidcarematch_backend.cron.sync_centres import normalize_preschool_level
    from kidcarematch_backend.db.db import PreschoolLevel

    # Infant
    assert normalize_preschool_level("infant") == PreschoolLevel.INFANT
    assert normalize_preschool_level("Infant Care") == PreschoolLevel.INFANT
    assert normalize_preschool_level("Infant Care (2 to 17 months)") == PreschoolLevel.INFANT
    assert normalize_preschool_level("INFANT") == PreschoolLevel.INFANT

    # Playgroup
    assert normalize_preschool_level("pg") == PreschoolLevel.PG
    assert normalize_preschool_level("Playgroup") == PreschoolLevel.PG
    assert normalize_preschool_level("Play Group") == PreschoolLevel.PG
    assert normalize_preschool_level("Playgroup (18 to 29 months)") == PreschoolLevel.PG
    assert normalize_preschool_level("PG (18-30 months)") == PreschoolLevel.PG

    # Nursery 1
    assert normalize_preschool_level("n1") == PreschoolLevel.N1
    assert normalize_preschool_level("Nursery 1") == PreschoolLevel.N1
    assert normalize_preschool_level("Nursery 1 (3 yrs old)") == PreschoolLevel.N1
    assert normalize_preschool_level("Nursery1") == PreschoolLevel.N1
    assert normalize_preschool_level("N1 (3 years old)") == PreschoolLevel.N1

    # Nursery 2
    assert normalize_preschool_level("n2") == PreschoolLevel.N2
    assert normalize_preschool_level("Nursery 2") == PreschoolLevel.N2
    assert normalize_preschool_level("Nursery 2 (4 yrs old)") == PreschoolLevel.N2
    assert normalize_preschool_level("Nursery2") == PreschoolLevel.N2
    assert normalize_preschool_level("N2 (4 years old)") == PreschoolLevel.N2

    # Kindergarten 1
    assert normalize_preschool_level("k1") == PreschoolLevel.K1
    assert normalize_preschool_level("Kindergarten 1") == PreschoolLevel.K1
    assert normalize_preschool_level("Kindergarten 1 (5 yrs old)") == PreschoolLevel.K1
    assert normalize_preschool_level("Kindergarten1") == PreschoolLevel.K1
    assert normalize_preschool_level("K1 (5 years old)") == PreschoolLevel.K1

    # Kindergarten 2
    assert normalize_preschool_level("k2") == PreschoolLevel.K2
    assert normalize_preschool_level("Kindergarten 2") == PreschoolLevel.K2
    assert normalize_preschool_level("Kindergarten 2 (6 yrs old)") == PreschoolLevel.K2
    assert normalize_preschool_level("Kindergarten2") == PreschoolLevel.K2
    assert normalize_preschool_level("K2 (6 years old)") == PreschoolLevel.K2

    # Student Care
    assert normalize_preschool_level("student_care") == PreschoolLevel.STUDENT_CARE
    assert normalize_preschool_level("Student Care") == PreschoolLevel.STUDENT_CARE
    assert normalize_preschool_level("Student Care (7 to 14 yrs old)") == PreschoolLevel.STUDENT_CARE
    assert normalize_preschool_level("StudentCare") == PreschoolLevel.STUDENT_CARE


def test_normalize_preschool_level_invalid_and_empty_returns_none(caplog):
    import logging
    from kidcarematch_backend.cron.sync_centres import normalize_preschool_level

    with caplog.at_level(logging.WARNING):
        assert normalize_preschool_level(None) is None
        assert normalize_preschool_level("") is None
        assert normalize_preschool_level("   ") is None
        assert normalize_preschool_level("na") is None
        assert normalize_preschool_level("Unknown Level") is None
        assert normalize_preschool_level("Senior Daycare") is None

    assert any("Empty or blank preschool service level" in record.message for record in caplog.records)
    assert any("Unmapped preschool service level" in record.message for record in caplog.records)


def test_normalize_citizenship_type_mappings():
    from kidcarematch_backend.cron.sync_centres import normalize_citizenship_type
    from kidcarematch_backend.db.db import CitizenshipType

    # SC
    assert normalize_citizenship_type("SC") == CitizenshipType.SC
    assert normalize_citizenship_type("sc") == CitizenshipType.SC
    assert normalize_citizenship_type("Singapore Citizen") == CitizenshipType.SC
    assert normalize_citizenship_type("Singaporean") == CitizenshipType.SC
    assert normalize_citizenship_type("Citizen") == CitizenshipType.SC
    assert normalize_citizenship_type("SC (Singapore Citizen)") == CitizenshipType.SC

    # SPR
    assert normalize_citizenship_type("SPR") == CitizenshipType.SPR
    assert normalize_citizenship_type("spr") == CitizenshipType.SPR
    assert normalize_citizenship_type("PR") == CitizenshipType.SPR
    assert normalize_citizenship_type("pr") == CitizenshipType.SPR
    assert normalize_citizenship_type("Permanent Resident") == CitizenshipType.SPR
    assert normalize_citizenship_type("Singapore Permanent Resident") == CitizenshipType.SPR

    # OTHERS
    assert normalize_citizenship_type("Others") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("OTHERS") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("others") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("Foreigner") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("Non-resident") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("Non-PR") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("Non-Citizen") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("Not Singaporean") == CitizenshipType.OTHERS
    assert normalize_citizenship_type(None) == CitizenshipType.OTHERS
    assert normalize_citizenship_type("") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("na") == CitizenshipType.OTHERS
    assert normalize_citizenship_type("unlisted_category") == CitizenshipType.OTHERS


def test_parse_centre_service_valid_record():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service
    from kidcarematch_backend.db.db import CitizenshipType, PreschoolLevel

    raw_rec = {
        "centre_code": "EB0001",
        "type_of_service": "Full Day",
        "levels_offered": "Kindergarten 1 (5 yrs old)",
        "fees": "1125",
        "type_of_citizenship": "Others",
        "last_updated": "2026-07-17",
        "remarks": "na",
    }
    centre_map = {"EB0001": 101}

    service = parse_centre_service(raw_rec, centre_map)

    assert service is not None
    assert service.centre_id == 101
    assert service.type_of_service == "Full Day"
    assert service.level == PreschoolLevel.K1
    assert service.fee == 1125.0
    assert service.citizenship_type == CitizenshipType.OTHERS
    assert service.last_updated == datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    assert service.remarks is None  # 'na' is normalized to None


def test_parse_centre_service_null_citizenship_defaults_to_others():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service
    from kidcarematch_backend.db.db import CitizenshipType, PreschoolLevel

    raw_rec = {
        "centre_code": "EB0001",
        "type_of_service": "Full Day",
        "levels_offered": "Infant Care",
        "fees": "1400",
        "type_of_citizenship": None,
    }
    centre_map = {"EB0001": 101}

    service = parse_centre_service(raw_rec, centre_map)

    assert service is not None
    assert service.centre_id == 101
    assert service.type_of_service == "Full Day"
    assert service.level == PreschoolLevel.INFANT
    assert service.fee == 1400.0
    assert service.citizenship_type == CitizenshipType.OTHERS


def test_parse_centre_service_unmapped_level_returns_none(caplog):
    import logging
    from kidcarematch_backend.cron.sync_centres import parse_centre_service

    raw_rec = {
        "centre_code": "EB0001",
        "type_of_service": "Full Day",
        "levels_offered": "Senior Activity Program",
        "fees": "500",
        "type_of_citizenship": "SC",
    }
    centre_map = {"EB0001": 101}

    with caplog.at_level(logging.WARNING):
        service = parse_centre_service(raw_rec, centre_map)

    assert service is None
    assert any("Unmapped preschool service level" in record.message for record in caplog.records)


def test_parse_centre_service_empty_or_blank_level_logs_warning_and_returns_none(caplog):
    import logging
    from kidcarematch_backend.cron.sync_centres import parse_centre_service

    for empty_level in ("", "   ", None, "NA"):
        raw_rec = {
            "centre_code": "EB0001",
            "type_of_service": "Full Day",
            "levels_offered": empty_level,
            "fees": "500",
            "type_of_citizenship": "SC",
        }
        centre_map = {"EB0001": 101}

        with caplog.at_level(logging.WARNING):
            service = parse_centre_service(raw_rec, centre_map)

        assert service is None
        assert any("Empty or blank preschool service level" in record.message for record in caplog.records)
        caplog.clear()


def test_parse_centre_service_unknown_centre_returns_none():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service

    raw_rec = {
        "centre_code": "UNKNOWN",
        "type_of_service": "Full Day",
        "levels_offered": "Infant Care",
        "fees": "1500",
        "type_of_citizenship": "SC",
    }
    centre_map = {"EB0001": 101}

    assert parse_centre_service(raw_rec, centre_map) is None


def test_sync_centres_orchestration_with_mocks(monkeypatch):
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import CitizenshipType, PreschoolCentre, PreschoolLevel, PreschoolService, VacancyRecord
    from sqlmodel import create_engine, Session, SQLModel, select

    # Use in-memory SQLite for test orchestration
    test_engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(test_engine)

    # Patch engine in sync_centres
    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    sample_centres = [
        make_test_centre_payload(
            centre_code="PC001",
            centre_name="Test Kindergarten",
            last_updated="2026-10-01",
            vacancy_status="Full",
            infant_vacancy_current_month="Immediate",
        )
    ]

    sample_services = [
        {
            "centre_code": "PC001",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "800",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
            "remarks": None,
        },
        {
            "centre_code": "NON_EXISTENT",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "800",
            "type_of_citizenship": "SC",
        },
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: sample_centres)
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: sample_services)

    stats = sync_centres(max_records=1, dry_run=False, db_engine=test_engine)

    assert stats["centres_inserted"] == 1
    assert stats["vacancies_inserted"] == 42
    assert stats["vacancies_updated"] == 0
    assert stats["vacancies_synced"] == 42
    assert stats["services_inserted"] == 1
    assert stats["services_skipped"] == 1

    with Session(test_engine) as session:
        centres = session.exec(select(PreschoolCentre)).all()
        assert len(centres) == 1
        assert centres[0].centre_code == "PC001"

        vacancies = session.exec(select(VacancyRecord)).all()
        assert len(vacancies) == 42

        services = session.exec(select(PreschoolService)).all()
        assert len(services) == 1
        assert services[0].fee == 800.0
        assert services[0].level == PreschoolLevel.INFANT
        assert services[0].citizenship_type == CitizenshipType.SC


def test_sync_centres_multi_centre_horizon_alignment(monkeypatch):
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import VacancyRecord
    from sqlmodel import create_engine, Session, SQLModel, select

    test_engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(test_engine)

    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Two centres with very different last_updated timestamps
    centre_1 = make_test_centre_payload(
        centre_code="CTR001",
        centre_name="Old Profile Centre",
        last_updated="2024-01-15",
    )
    centre_2 = make_test_centre_payload(
        centre_code="CTR002",
        centre_name="Recent Profile Centre",
        last_updated="2026-09-30",
    )

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre_1, centre_2])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    reference_sync_date = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    stats = sync_centres(db_engine=test_engine, sync_date=reference_sync_date)

    assert stats["centres_inserted"] == 2
    assert stats["vacancies_inserted"] == 84
    assert stats["vacancies_updated"] == 0
    assert stats["vacancies_synced"] == 84

    with Session(test_engine) as session:
        records = session.exec(select(VacancyRecord)).all()
        assert len(records) == 84

        ctr1_records = [r for r in records if r.record_id and r.record_id.startswith("CTR001")]
        ctr2_records = [r for r in records if r.record_id and r.record_id.startswith("CTR002")]

        assert len(ctr1_records) == 42
        assert len(ctr2_records) == 42

        # Extract distinct enrollment months for both centres
        ctr1_months = sorted({r.enrollment_month_year for r in ctr1_records})
        ctr2_months = sorted({r.enrollment_month_year for r in ctr2_records})

        expected_months = [
            datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 2, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 3, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 4, 1, 0, 0, tzinfo=timezone.utc),
        ]

        assert ctr1_months == expected_months
        assert ctr2_months == expected_months


def test_sync_centres_rolling_cycle_preserves_historical_records(monkeypatch):
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import VacancyRecord
    from sqlmodel import create_engine, Session, SQLModel, select

    test_engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(test_engine)

    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Run 1: October 2026 sync
    oct_centre = make_test_centre_payload(
        centre_code="ROLL01",
        centre_name="Rolling Horizon Centre",
        last_updated="2026-10-01",
        vacancy_status="Available",
        infant_vacancy_current_month="Immediate",
    )

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [oct_centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    oct_sync_date = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    stats1 = sync_centres(db_engine=test_engine, sync_date=oct_sync_date)
    assert stats1["vacancies_inserted"] == 42
    assert stats1["vacancies_updated"] == 0
    assert stats1["vacancies_synced"] == 42

    with Session(test_engine) as session:
        records_oct = session.exec(select(VacancyRecord)).all()
        assert len(records_oct) == 42
        oct_infant = session.get(VacancyRecord, "ROLL01_infant_202610")
        assert oct_infant is not None
        assert oct_infant.vacancy_status == "Immediate"

    # Run 2: November 2026 sync (calendar rolls forward by 1 month)
    nov_centre = make_test_centre_payload(
        centre_code="ROLL01",
        centre_name="Rolling Horizon Centre",
        last_updated="2026-11-01",
        vacancy_status="Full",
        infant_vacancy_current_month="Full",
    )

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [nov_centre])

    nov_sync_date = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    stats2 = sync_centres(db_engine=test_engine, sync_date=nov_sync_date)
    assert stats2["vacancies_inserted"] == 6
    assert stats2["vacancies_updated"] == 36
    assert stats2["vacancies_synced"] == 42

    with Session(test_engine) as session:
        all_records = session.exec(select(VacancyRecord)).all()
        # 6 levels * 7 months in Oct = 42
        # Nov introduces 6 new seventh_month records (202705) and updates 36 overlapping records (202611..202704)
        # Total records = 42 + 6 = 48
        assert len(all_records) == 48

        # 1. Historical October record remains intact and untouched
        hist_oct_infant = session.get(VacancyRecord, "ROLL01_infant_202610")
        assert hist_oct_infant is not None
        assert hist_oct_infant.vacancy_status == "Immediate"
        assert hist_oct_infant.enrollment_month_year == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

        # 2. Overlapping November record was updated
        updated_nov_infant = session.get(VacancyRecord, "ROLL01_infant_202611")
        assert updated_nov_infant is not None
        assert updated_nov_infant.vacancy_status == "Full"
        assert updated_nov_infant.enrollment_month_year == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

        # 3. New seventh month (May 2027) was inserted
        new_may_infant = session.get(VacancyRecord, "ROLL01_infant_202705")
        assert new_may_infant is not None
        assert new_may_infant.vacancy_status == "Full"
        assert new_may_infant.enrollment_month_year == datetime(2027, 5, 1, 0, 0, tzinfo=timezone.utc)


def test_preschool_service_composite_uniqueness_constraint():
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.db.db import CitizenshipType, PreschoolCentre, PreschoolLevel, PreschoolService, init_db

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)

    with Session(test_engine) as session:
        centre = PreschoolCentre(centre_code="UNIQ01", centre_name="Unique Centre")
        session.add(centre)
        session.commit()
        session.refresh(centre)

        service1 = PreschoolService(
            centre_id=centre.centre_id,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1200.0,
            citizenship_type=CitizenshipType.SC,
        )
        session.add(service1)
        session.commit()

        # Attempt to insert exact duplicate offering (same centre_id, type_of_service, level, citizenship_type)
        service2 = PreschoolService(
            centre_id=centre.centre_id,
            type_of_service="Full Day",
            level=PreschoolLevel.INFANT,
            fee=1300.0,
            citizenship_type=CitizenshipType.SC,
        )
        session.add(service2)
        with pytest.raises(IntegrityError):
            session.commit()


def test_parse_centre_service_resolves_tp_code():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service
    from kidcarematch_backend.db.db import CitizenshipType, PreschoolLevel

    raw_rec = {
        "centre_code": "TP8888",
        "type_of_service": "Full Day",
        "levels_offered": "Playgroup",
        "fees": "950",
        "type_of_citizenship": "PR",
    }
    # Mapping indexed by tp_code
    centre_map = {"TP8888": 202}

    service = parse_centre_service(raw_rec, centre_map)

    assert service is not None
    assert service.centre_id == 202
    assert service.type_of_service == "Full Day"
    assert service.level == PreschoolLevel.PG
    assert service.fee == 950.0
    assert service.citizenship_type == CitizenshipType.SPR


def test_sync_centres_resolves_services_for_tp_code_centres(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Centre operating with primary centre_code and a temporary permit tp_code
    centre_with_tp = make_test_centre_payload(
        centre_code="PC9999",
        tp_code="TP9999",
        centre_name="Temporary Permit Centre",
        last_updated="2026-10-01",
    )

    # Services API returns records using tp_code instead of centre_code
    services_with_tp = [
        {
            "centre_code": "TP9999",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1000",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        },
        {
            "centre_code": "PC9999",
            "type_of_service": "Half Day",
            "levels_offered": "Playgroup",
            "fees": "600",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        },
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre_with_tp])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: services_with_tp)

    stats = sync_centres(db_engine=test_engine)

    assert stats["centres_inserted"] == 1
    assert stats["services_inserted"] == 2
    assert stats["services_skipped"] == 0

    with Session(test_engine) as session:
        centre = session.exec(select(PreschoolCentre).where(PreschoolCentre.centre_code == "PC9999")).first()
        assert centre is not None
        assert centre.centre_code == "PC9999"

        services = session.exec(select(PreschoolService).where(PreschoolService.centre_id == centre.centre_id)).all()
        assert len(services) == 2
        service_types = {s.type_of_service for s in services}
        assert service_types == {"Full Day", "Half Day"}


def test_sync_centres_updates_existing_services_idempotently(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import PreschoolService, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre_rec = make_test_centre_payload(
        centre_code="PC5555",
        centre_name="Idempotent Centre",
        last_updated="2026-10-01",
    )

    service_initial = [
        {
            "centre_code": "PC5555",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1200",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
            "remarks": "Initial fee",
        }
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre_rec])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: service_initial)

    stats1 = sync_centres(db_engine=test_engine)
    assert stats1["services_inserted"] == 1
    assert stats1["services_updated"] == 0

    # Second sync: fee changes and remarks update
    service_updated = [
        {
            "centre_code": "PC5555",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1250",
            "type_of_citizenship": "SC",
            "last_updated": "2026-11-01",
            "remarks": "Revised fee",
        }
    ]
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: service_updated)

    stats2 = sync_centres(db_engine=test_engine)
    assert stats2["services_inserted"] == 0
    assert stats2["services_updated"] == 1

    with Session(test_engine) as session:
        services = session.exec(select(PreschoolService)).all()
        assert len(services) == 1
        assert services[0].fee == 1250.0
        assert services[0].remarks == "Revised fee"
        assert services[0].last_updated == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def test_sync_centres_tp_code_only_centre_and_subsequent_update(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, VacancyRecord, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Initial sync: new centre only has tp_code, centre_code is None
    tp_only_centre = make_test_centre_payload(
        centre_code=None,
        tp_code="TP7777",
        centre_name="Brand New TP Centre",
        last_updated="2026-10-01",
    )

    services_initial = [
        {
            "centre_code": "TP7777",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1100",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        }
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [tp_only_centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: services_initial)

    stats1 = sync_centres(db_engine=test_engine)
    assert stats1["centres_inserted"] == 1
    assert stats1["vacancies_inserted"] == 42
    assert stats1["vacancies_updated"] == 0
    assert stats1["services_inserted"] == 1
    assert stats1["services_skipped"] == 0

    with Session(test_engine) as session:
        centres = session.exec(select(PreschoolCentre)).all()
        assert len(centres) == 1
        assert centres[0].centre_code == "TP7777"

        vacancies = session.exec(select(VacancyRecord).where(VacancyRecord.centre_id == centres[0].centre_id)).all()
        assert len(vacancies) == 42
        assert all(v.record_id.startswith("TP7777_") for v in vacancies)

    # Second sync: centre now has permanent centre_code assigned while keeping tp_code
    centre_updated = make_test_centre_payload(
        centre_code="EC7777",
        tp_code="TP7777",
        centre_name="Brand New TP Centre (Registered)",
        last_updated="2026-11-01",
    )

    services_updated = [
        {
            "centre_code": "TP7777",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1150",
            "type_of_citizenship": "SC",
            "last_updated": "2026-11-01",
        }
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre_updated])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: services_updated)

    stats2 = sync_centres(db_engine=test_engine)
    assert stats2["centres_updated"] == 1
    assert stats2["vacancies_updated"] == 42
    assert stats2["vacancies_inserted"] == 0
    assert stats2["services_updated"] == 1
    assert stats2["services_skipped"] == 0

    with Session(test_engine) as session:
        centres = session.exec(select(PreschoolCentre)).all()
        assert len(centres) == 1
        assert centres[0].centre_code == "EC7777"

        # Verify NO duplicate vacancies were created during transition from TP7777 to EC7777
        vacancies = session.exec(select(VacancyRecord).where(VacancyRecord.centre_id == centres[0].centre_id)).all()
        assert len(vacancies) == 42
        assert all(v.record_id.startswith("EC7777_") for v in vacancies)

        services = session.exec(select(PreschoolService)).all()
        assert len(services) == 1
        assert services[0].fee == 1150.0
        assert services[0].centre_id == centres[0].centre_id


def test_code_transition_migrates_historical_vacancies_without_duplicates(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, VacancyRecord, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Sync 1: September 2026 - Centre only has tp_code
    sep_centre = make_test_centre_payload(
        centre_code=None,
        tp_code="TP1234",
        centre_name="Initial TP Centre",
        last_updated="2026-09-01",
    )
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [sep_centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    sep_date = datetime(2026, 9, 15, tzinfo=timezone.utc)
    stats1 = sync_centres(db_engine=test_engine, sync_date=sep_date)
    assert stats1["centres_inserted"] == 1
    assert stats1["vacancies_inserted"] == 42

    # Sync 2: October 2026 - Centre now has official centre_code "EC1234" alongside "TP1234"
    oct_centre = make_test_centre_payload(
        centre_code="EC1234",
        tp_code="TP1234",
        centre_name="Registered Centre",
        last_updated="2026-10-01",
    )
    oct_services = [
        {
            "centre_code": "TP1234",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1300",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        }
    ]
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [oct_centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: oct_services)

    oct_date = datetime(2026, 10, 15, tzinfo=timezone.utc)
    stats2 = sync_centres(db_engine=test_engine, sync_date=oct_date)
    assert stats2["centres_updated"] == 1
    assert stats2["vacancies_updated"] == 36  # 36 overlapping months updated
    assert stats2["vacancies_inserted"] == 6   # 6 new seventh_month (2027-04) records inserted
    assert stats2["services_inserted"] == 1

    with Session(test_engine) as session:
        centres = session.exec(select(PreschoolCentre)).all()
        assert len(centres) == 1
        assert centres[0].centre_code == "EC1234"

        all_vacancies = session.exec(select(VacancyRecord).where(VacancyRecord.centre_id == centres[0].centre_id)).all()
        # 42 original from Sep + 6 new for Apr 2027 = 48 total records
        assert len(all_vacancies) == 48

        # Verify NO records exist with the old TP1234 prefix
        assert not any(v.record_id.startswith("TP1234_") for v in all_vacancies)
        assert all(v.record_id.startswith("EC1234_") for v in all_vacancies)

        # Historical September record was migrated to EC1234 and preserved
        hist_sep = session.get(VacancyRecord, "EC1234_infant_202609")
        assert hist_sep is not None
        assert hist_sep.enrollment_month_year == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)

        services = session.exec(select(PreschoolService)).all()
        assert len(services) == 1
        assert services[0].centre_id == centres[0].centre_id
        assert services[0].fee == 1300.0


def test_sync_centres_mixed_services_normalization_and_skipping(monkeypatch, caplog):
    import logging
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import (
        CitizenshipType,
        PreschoolCentre,
        PreschoolLevel,
        PreschoolService,
        init_db,
    )
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre = make_test_centre_payload(
        centre_code="PC_MIXED",
        centre_name="Mixed Services Centre",
        last_updated="2026-10-01",
    )

    mixed_services = [
        # 1. Infant Care + SC
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1500",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        },
        # 2. Kindergarten 1 (5 yrs old) + Others
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Kindergarten 1 (5 yrs old)",
            "fees": "1100",
            "type_of_citizenship": "Others",
            "last_updated": "2026-10-01",
        },
        # 3. Playgroup (18 to 29 months) + PR
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Playgroup (18 to 29 months)",
            "fees": "1200",
            "type_of_citizenship": "PR",
            "last_updated": "2026-10-01",
        },
        # 4. Nursery 2 + None citizenship -> defaults to OTHERS
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Nursery 2",
            "fees": "1000",
            "type_of_citizenship": None,
            "last_updated": "2026-10-01",
        },
        # 5. Nursery 1 + Non-resident -> OTHERS
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Half Day",
            "levels_offered": "Nursery 1",
            "fees": "700",
            "type_of_citizenship": "Non-resident",
            "last_updated": "2026-10-01",
        },
        # 6. Student Care + Singapore Citizen -> SC
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Student Care (7 to 14 yrs old)",
            "fees": "600",
            "type_of_citizenship": "Singapore Citizen",
            "last_updated": "2026-10-01",
        },
        # 7. Unmapped level -> should skip and log warning
        {
            "centre_code": "PC_MIXED",
            "type_of_service": "Full Day",
            "levels_offered": "Adult Learning Care",
            "fees": "500",
            "type_of_citizenship": "SC",
            "last_updated": "2026-10-01",
        },
        # 8. Non-existent centre -> should skip
        {
            "centre_code": "NON_EXISTENT_CTR",
            "type_of_service": "Full Day",
            "levels_offered": "Infant Care",
            "fees": "1500",
            "type_of_citizenship": "SC",
        },
    ]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: mixed_services)

    with caplog.at_level(logging.WARNING):
        stats = sync_centres(db_engine=test_engine)

    assert stats["centres_inserted"] == 1
    assert stats["services_inserted"] == 6
    assert stats["services_skipped"] == 2
    assert any("Unmapped preschool service level" in record.message for record in caplog.records)

    with Session(test_engine) as session:
        db_services = session.exec(select(PreschoolService)).all()
        assert len(db_services) == 6

        services_by_level = {s.level: s for s in db_services}

        assert PreschoolLevel.INFANT in services_by_level
        assert services_by_level[PreschoolLevel.INFANT].citizenship_type == CitizenshipType.SC
        assert services_by_level[PreschoolLevel.INFANT].fee == 1500.0

        assert PreschoolLevel.K1 in services_by_level
        assert services_by_level[PreschoolLevel.K1].citizenship_type == CitizenshipType.OTHERS
        assert services_by_level[PreschoolLevel.K1].fee == 1100.0

        assert PreschoolLevel.PG in services_by_level
        assert services_by_level[PreschoolLevel.PG].citizenship_type == CitizenshipType.SPR
        assert services_by_level[PreschoolLevel.PG].fee == 1200.0

        assert PreschoolLevel.N2 in services_by_level
        assert services_by_level[PreschoolLevel.N2].citizenship_type == CitizenshipType.OTHERS
        assert services_by_level[PreschoolLevel.N2].fee == 1000.0

        assert PreschoolLevel.N1 in services_by_level
        assert services_by_level[PreschoolLevel.N1].citizenship_type == CitizenshipType.OTHERS
        assert services_by_level[PreschoolLevel.N1].type_of_service == "Half Day"
        assert services_by_level[PreschoolLevel.N1].fee == 700.0

        assert PreschoolLevel.STUDENT_CARE in services_by_level
        assert services_by_level[PreschoolLevel.STUDENT_CARE].citizenship_type == CitizenshipType.SC
        assert services_by_level[PreschoolLevel.STUDENT_CARE].fee == 600.0


def test_sync_centres_heartbeat_lifecycle_success(monkeypatch):
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import ExternalDataSourceStatus, SyncState, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre = make_test_centre_payload(
        centre_code="HB_CTR_01",
        centre_name="Heartbeat Centre 1",
        last_updated="2026-10-01",
    )
    service = {
        "centre_code": "HB_CTR_01",
        "type_of_service": "Full Day",
        "levels_offered": "Infant Care",
        "fees": "1200",
        "type_of_citizenship": "SC",
        "last_updated": "2026-10-01",
    }

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [service])

    before_sync = datetime.now(timezone.utc)
    stats = sync_centres(db_engine=test_engine)
    after_sync = datetime.now(timezone.utc)

    assert stats["centres_inserted"] == 1
    assert stats["services_inserted"] == 1

    with Session(test_engine) as session:
        status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert status is not None
        assert status.source_id == "ECDA_DATASTORE"
        assert status.sync_state == SyncState.SUCCESS
        assert status.last_attempted_sync is not None
        assert before_sync <= status.last_attempted_sync <= after_sync
        assert status.last_successful_sync is not None
        assert before_sync <= status.last_successful_sync <= after_sync
        assert status.records_processed == 2  # 1 centre + 1 service
        assert status.error_message is None


def test_sync_centres_heartbeat_lifecycle_running_state(monkeypatch):
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import ExternalDataSourceStatus, SyncState, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    observed_state_during_fetch: dict[str, Any] = {}

    def mock_fetch_all_centres(**kwargs: Any) -> list[dict[str, Any]]:
        # Inspect database during the fetch execution to verify RUNNING state
        with Session(test_engine) as session:
            status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
            if status:
                observed_state_during_fetch["sync_state"] = status.sync_state
                observed_state_during_fetch["last_attempted_sync"] = status.last_attempted_sync
        return [make_test_centre_payload(centre_code="RUN_01", centre_name="Running Test")]

    monkeypatch.setattr(sc_mod, "fetch_all_centres", mock_fetch_all_centres)
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    before_sync = datetime.now(timezone.utc)
    sync_centres(db_engine=test_engine)
    after_sync = datetime.now(timezone.utc)

    assert observed_state_during_fetch["sync_state"] == SyncState.RUNNING
    assert observed_state_during_fetch["last_attempted_sync"] is not None
    assert before_sync <= observed_state_during_fetch["last_attempted_sync"] <= after_sync


def test_sync_centres_heartbeat_lifecycle_failure_on_fetch(monkeypatch):
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import ExternalDataSourceStatus, SyncState, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    def failing_fetch_centres(**kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ECDA Datastore Gateway Timeout (504)")

    monkeypatch.setattr(sc_mod, "fetch_all_centres", failing_fetch_centres)

    before_sync = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="ECDA Datastore Gateway Timeout"):
        sync_centres(db_engine=test_engine)
    after_sync = datetime.now(timezone.utc)

    with Session(test_engine) as session:
        status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert status is not None
        assert status.sync_state == SyncState.FAILED
        assert status.last_attempted_sync is not None
        assert before_sync <= status.last_attempted_sync <= after_sync
        assert status.last_successful_sync is None
        assert status.error_message is not None
        assert "ECDA Datastore Gateway Timeout (504)" in status.error_message


def test_sync_centres_heartbeat_lifecycle_failure_preserves_previous_success_timestamp(monkeypatch):
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import ExternalDataSourceStatus, SyncState, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre = make_test_centre_payload(centre_code="PREV_01", centre_name="Prev Success Centre")

    # Run 1: Successful sync
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    sync_centres(db_engine=test_engine)

    with Session(test_engine) as session:
        status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert status is not None
        assert status.sync_state == SyncState.SUCCESS
        assert status.last_successful_sync is not None
        initial_successful_sync = status.last_successful_sync
        assert status.records_processed == 1

    # Run 2: Failed sync
    def failing_fetch_services(**kwargs: Any) -> list[dict[str, Any]]:
        raise ConnectionError("Services API Connection Refused")

    monkeypatch.setattr(sc_mod, "fetch_all_services", failing_fetch_services)

    with pytest.raises(ConnectionError, match="Services API Connection Refused"):
        sync_centres(db_engine=test_engine)

    with Session(test_engine) as session:
        status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert status is not None
        assert status.sync_state == SyncState.FAILED
        assert status.last_attempted_sync is not None
        assert status.last_attempted_sync >= initial_successful_sync
        # last_successful_sync must still be preserved from Run 1
        assert status.last_successful_sync == initial_successful_sync
        assert status.error_message is not None
        assert "Services API Connection Refused" in status.error_message


def test_sync_centres_heartbeat_lifecycle_dry_run_does_not_persist_status(monkeypatch):
    from sqlmodel import Session, create_engine
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import ExternalDataSourceStatus, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre = make_test_centre_payload(centre_code="DRY_01", centre_name="Dry Run Centre")
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    sync_time = datetime(2026, 10, 6, 15, 0, 0, tzinfo=timezone.utc)
    stats = sync_centres(db_engine=test_engine, dry_run=True, sync_date=sync_time)

    assert stats["centres_fetched"] == 1

    with Session(test_engine) as session:
        # Status record should not be created or left dirty in dry-run mode
        status = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        assert status is None


def test_sync_vacancies_does_not_issue_per_record_queries(monkeypatch):
    """Ensure vacancy syncing does not suffer from N+1 query loop / per-record autoflushes."""
    from sqlalchemy import event
    from sqlmodel import create_engine

    import kidcarematch_backend.cron.sync_centres as sc_mod
    from kidcarematch_backend.cron.sync_centres import sync_centres
    from kidcarematch_backend.db.db import init_db

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # 5 centres = 210 vacancies
    centres = [
        make_test_centre_payload(
            centre_code=f"CTR{i:03d}",
            centre_name=f"Centre {i}",
            last_updated="2026-10-01",
        )
        for i in range(5)
    ]
    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: centres)
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    queries: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(test_engine, "before_cursor_execute", before_cursor_execute)

    sync_centres(db_engine=test_engine)

    vacancy_select_queries = [
        q for q in queries
        if "FROM vacancyrecord" in q or "from vacancyrecord" in q
    ]

    # Vacancy lookups must be bulk/in-memory (O(1)), NOT 1 query per vacancy (which would be 210 queries)
    assert len(vacancy_select_queries) <= 2, (
        f"Too many vacancy SELECT queries ({len(vacancy_select_queries)} queries for 210 vacancies). "
        "Vacancy sync is executing per-record queries instead of bulk/in-memory resolution."
    )









from datetime import datetime, timezone
import pytest

from kidcarematch_backend.cron.sync_centres import parse_vacancies_for_centre


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


def test_parse_centre_service_valid_record():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service

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
    assert service.level == "Kindergarten 1 (5 yrs old)"
    assert service.fee == 1125.0
    assert service.citizenship_type == "Others"
    assert service.last_updated == datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    assert service.remarks is None  # 'na' is normalized to None


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
    from kidcarematch_backend.db.db import PreschoolCentre, VacancyRecord, PreschoolService
    from sqlmodel import create_engine, Session, SQLModel, select

    # Use in-memory SQLite for test orchestration
    test_engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(test_engine)

    # Patch engine in sync_centres
    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    sample_centres = [
        {
            "centre_code": "PC001",
            "centre_name": "Test Kindergarten",
            "last_updated": "2026-10-01",
            "infant_vacancy_current_month": "Immediate",
        }
    ]
    # Add other 41 fields with default
    for lvl in sc_mod.VACANCY_LEVELS:
        for hor, _ in sc_mod.VACANCY_HORIZONS:
            f = f"{lvl}_vacancy_{hor}"
            if f not in sample_centres[0]:
                sample_centres[0][f] = "Full"

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

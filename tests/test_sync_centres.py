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


def test_parse_vacancies_anchored_to_sync_date_ignoring_stale_last_updated():
    # Centre profile has a stale last_updated from January 2025
    centre_record = {
        "centre_code": "STALE01",
        "last_updated": "2025-01-10",
        "infant_vacancy_current_month": "Available",
        "infant_vacancy_seventh_month": "Full",
    }
    # Populate dummy entries for remaining fields
    from kidcarematch_backend.cron.sync_centres import VACANCY_LEVELS, VACANCY_HORIZONS
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            field = f"{lvl}_vacancy_{hor}"
            centre_record.setdefault(field, "Full")

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


def test_sync_centres_multi_centre_horizon_alignment(monkeypatch):
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import VacancyRecord
    from sqlmodel import create_engine, Session, SQLModel, select

    test_engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(test_engine)

    import kidcarematch_backend.cron.sync_centres as sc_mod
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Two centres with very different last_updated timestamps
    centre_1 = {
        "centre_code": "CTR001",
        "centre_name": "Old Profile Centre",
        "last_updated": "2024-01-15",
    }
    centre_2 = {
        "centre_code": "CTR002",
        "centre_name": "Recent Profile Centre",
        "last_updated": "2026-09-30",
    }
    for c in (centre_1, centre_2):
        for lvl in VACANCY_LEVELS:
            for hor, _ in VACANCY_HORIZONS:
                c[f"{lvl}_vacancy_{hor}"] = "Available"

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [centre_1, centre_2])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    reference_sync_date = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    stats = sync_centres(db_engine=test_engine, sync_date=reference_sync_date)

    assert stats["centres_inserted"] == 2
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
    oct_centre = {
        "centre_code": "ROLL01",
        "centre_name": "Rolling Horizon Centre",
        "last_updated": "2026-10-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            oct_centre[f"{lvl}_vacancy_{hor}"] = "Available"
    # Mark October infant as "Immediate"
    oct_centre["infant_vacancy_current_month"] = "Immediate"

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [oct_centre])
    monkeypatch.setattr(sc_mod, "fetch_all_services", lambda **kwargs: [])

    oct_sync_date = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    sync_centres(db_engine=test_engine, sync_date=oct_sync_date)

    with Session(test_engine) as session:
        records_oct = session.exec(select(VacancyRecord)).all()
        assert len(records_oct) == 42
        oct_infant = session.get(VacancyRecord, "ROLL01_infant_202610")
        assert oct_infant is not None
        assert oct_infant.vacancy_status == "Immediate"

    # Run 2: November 2026 sync (calendar rolls forward by 1 month)
    nov_centre = {
        "centre_code": "ROLL01",
        "centre_name": "Rolling Horizon Centre",
        "last_updated": "2026-11-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            nov_centre[f"{lvl}_vacancy_{hor}"] = "Full"
    # In Nov, current_month is 2026-11, so infant in Nov is now "Full"
    nov_centre["infant_vacancy_current_month"] = "Full"

    monkeypatch.setattr(sc_mod, "fetch_all_centres", lambda **kwargs: [nov_centre])

    nov_sync_date = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    sync_centres(db_engine=test_engine, sync_date=nov_sync_date)

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
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, init_db

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
            level="Infant Care",
            fee=1200.0,
            citizenship_type="SC",
        )
        session.add(service1)
        session.commit()

        # Attempt to insert exact duplicate offering (same centre_id, type_of_service, level, citizenship_type)
        service2 = PreschoolService(
            centre_id=centre.centre_id,
            type_of_service="Full Day",
            level="Infant Care",
            fee=1300.0,
            citizenship_type="SC",
        )
        session.add(service2)
        with pytest.raises(IntegrityError):
            session.commit()


def test_parse_centre_service_resolves_tp_code():
    from kidcarematch_backend.cron.sync_centres import parse_centre_service

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
    assert service.level == "Playgroup"
    assert service.fee == 950.0
    assert service.citizenship_type == "PR"


def test_sync_centres_resolves_services_for_tp_code_centres(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Centre operating with primary centre_code and a temporary permit tp_code
    centre_with_tp = {
        "centre_code": "PC9999",
        "tp_code": "TP9999",
        "centre_name": "Temporary Permit Centre",
        "last_updated": "2026-10-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            centre_with_tp[f"{lvl}_vacancy_{hor}"] = "Available"

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
        assert centre.tp_code == "TP9999"

        services = session.exec(select(PreschoolService).where(PreschoolService.centre_id == centre.centre_id)).all()
        assert len(services) == 2
        service_types = {s.type_of_service for s in services}
        assert service_types == {"Full Day", "Half Day"}


def test_sync_centres_updates_existing_services_idempotently(monkeypatch):
    from sqlmodel import Session, create_engine, select
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import PreschoolService, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    centre_rec = {
        "centre_code": "PC5555",
        "centre_name": "Idempotent Centre",
        "last_updated": "2026-10-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            centre_rec[f"{lvl}_vacancy_{hor}"] = "Available"

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
    from kidcarematch_backend.cron.sync_centres import sync_centres, VACANCY_LEVELS, VACANCY_HORIZONS
    from kidcarematch_backend.db.db import PreschoolCentre, PreschoolService, init_db
    import kidcarematch_backend.cron.sync_centres as sc_mod

    test_engine = create_engine("sqlite:///:memory:")
    init_db(test_engine)
    monkeypatch.setattr(sc_mod, "engine", test_engine)

    # Initial sync: new centre only has tp_code, centre_code is None
    tp_only_centre = {
        "centre_code": None,
        "tp_code": "TP7777",
        "centre_name": "Brand New TP Centre",
        "last_updated": "2026-10-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            tp_only_centre[f"{lvl}_vacancy_{hor}"] = "Available"

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
    assert stats1["services_inserted"] == 1
    assert stats1["services_skipped"] == 0

    # Second sync: centre now has permanent centre_code assigned while keeping tp_code
    centre_updated = {
        "centre_code": "EC7777",
        "tp_code": "TP7777",
        "centre_name": "Brand New TP Centre (Registered)",
        "last_updated": "2026-11-01",
    }
    for lvl in VACANCY_LEVELS:
        for hor, _ in VACANCY_HORIZONS:
            centre_updated[f"{lvl}_vacancy_{hor}"] = "Available"

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
    assert stats2["services_updated"] == 1
    assert stats2["services_skipped"] == 0

    with Session(test_engine) as session:
        centres = session.exec(select(PreschoolCentre)).all()
        assert len(centres) == 1
        assert centres[0].centre_code == "EC7777"
        assert centres[0].tp_code == "TP7777"

        services = session.exec(select(PreschoolService)).all()
        assert len(services) == 1
        assert services[0].fee == 1150.0
        assert services[0].centre_id == centres[0].centre_id





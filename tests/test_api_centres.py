from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    PreschoolCentre,
    PreschoolLevel,
    SyncState,
    VacancyRecord,
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


def test_get_centre_summary_empty_database_returns_200_and_defaults(
    client: TestClient,
):
    """GET /api/v1/centres/summary on an empty database returns 200 with zero counts and null sync time."""
    response = client.get("/api/v1/centres/summary")

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "total_centres": 0,
        "spark_certified_count": 0,
        "gov_subsidy_count": 0,
        "centres_with_vacancies_count": 0,
        "last_sync_time": None,
    }


def test_get_centre_summary_with_data_returns_accurate_metrics(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/summary returns accurate aggregates without requiring authentication."""
    sync_time_1 = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)
    sync_time_2 = datetime(2026, 10, 11, 14, 30, 0, tzinfo=timezone.utc)

    with Session(test_engine) as session:
        centre_1 = PreschoolCentre(
            centre_code="PC001",
            centre_name="First Step Preschool",
            spark_certified=True,
            gov_subsidy=True,
        )
        centre_2 = PreschoolCentre(
            centre_code="PC002",
            centre_name="Sunshine Kids Academy",
            spark_certified=False,
            gov_subsidy=True,
        )
        centre_3 = PreschoolCentre(
            centre_code="PC003",
            centre_name="Bright Star Childcare",
            spark_certified=True,
            gov_subsidy=False,
        )
        centre_4 = PreschoolCentre(
            centre_code="PC004",
            centre_name="Little Learners Haven",
            spark_certified=False,
            gov_subsidy=False,
        )
        session.add_all([centre_1, centre_2, centre_3, centre_4])
        session.commit()
        session.refresh(centre_1)
        session.refresh(centre_2)
        session.refresh(centre_3)
        session.refresh(centre_4)

        # Vacancies:
        # centre_1 has 2 open vacancies (must be counted only once in distinct centres)
        # centre_2 has 1 open vacancy ('available')
        # centre_3 has 1 full vacancy
        # centre_4 has 1 waitlist vacancy
        session.add_all(
            [
                VacancyRecord(
                    record_id="VR001",
                    centre_id=centre_1.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Immediate",
                ),
                VacancyRecord(
                    record_id="VR002",
                    centre_id=centre_1.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.PG,
                    enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
                VacancyRecord(
                    record_id="VR003",
                    centre_id=centre_2.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.N1,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="available",  # case-insensitive check
                ),
                VacancyRecord(
                    record_id="VR004",
                    centre_id=centre_3.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.N2,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Full",
                ),
                VacancyRecord(
                    record_id="VR005",
                    centre_id=centre_4.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.K1,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Waitlist",
                ),
            ]
        )

        # External data source status
        session.add_all(
            [
                ExternalDataSourceStatus(
                    source_id="centres_api",
                    source_name="Data.gov.sg Centres",
                    sync_state=SyncState.SUCCESS,
                    last_successful_sync=sync_time_1,
                ),
                ExternalDataSourceStatus(
                    source_id="services_api",
                    source_name="Data.gov.sg Services",
                    sync_state=SyncState.SUCCESS,
                    last_successful_sync=sync_time_2,
                ),
            ]
        )
        session.commit()

    response = client.get("/api/v1/centres/summary")

    assert response.status_code == 200
    data = response.json()
    assert data["total_centres"] == 4
    assert data["spark_certified_count"] == 2
    assert data["gov_subsidy_count"] == 2
    assert data["centres_with_vacancies_count"] == 2
    assert (
        datetime.fromisoformat(data["last_sync_time"].replace("Z", "+00:00"))
        == sync_time_2
    )


def test_get_centre_summary_no_open_vacancies_and_null_sync(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/summary handles centres without open vacancies and null sync timestamps."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC005",
            centre_name="Oak Tree Kindergarten",
            spark_certified=True,
            gov_subsidy=False,
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        session.add(
            VacancyRecord(
                record_id="VR010",
                centre_id=centre.centre_id,  # type: ignore[arg-type]
                level_type=PreschoolLevel.N1,
                enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                vacancy_status="Full",
            )
        )
        session.add(
            ExternalDataSourceStatus(
                source_id="failed_source",
                source_name="Failed Provider",
                sync_state=SyncState.FAILED,
                last_successful_sync=None,
            )
        )
        session.commit()

    response = client.get("/api/v1/centres/summary")

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "total_centres": 1,
        "spark_certified_count": 1,
        "gov_subsidy_count": 0,
        "centres_with_vacancies_count": 0,
        "last_sync_time": None,
    }

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from kidcarematch_backend.db.db import (
    CitizenshipType,
    ExternalDataSourceStatus,
    ModerationStatus,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    Review,
    SyncState,
    User,
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


def test_get_centres_empty_database_returns_empty_envelope(client: TestClient):
    """GET /api/v1/centres on an empty database returns 200 with empty items list and default pagination."""
    response = client.get("/api/v1/centres")

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_get_centres_default_pagination(client: TestClient, test_engine):
    """GET /api/v1/centres returns default limit=20 and total count across pages."""
    with Session(test_engine) as session:
        centres = [
            PreschoolCentre(
                centre_code=f"PC{i:03d}",
                centre_name=f"Preschool Centre {i:03d}",
                postal=f"560{i:03d}",
                address=f"Block {i} Ang Mo Kio Ave {i % 10}",
                spark_certified=(i % 2 == 0),
                gov_subsidy=(i % 3 == 0),
            )
            for i in range(1, 26)
        ]
        session.add_all(centres)
        session.commit()

    response = client.get("/api/v1/centres")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 25
    assert data["limit"] == 20
    assert data["offset"] == 0
    assert len(data["items"]) == 20

    first_item = data["items"][0]
    assert first_item["centre_code"] == "PC001"
    assert first_item["centre_name"] == "Preschool Centre 001"
    assert "spark_certified" in first_item
    assert "gov_subsidy" in first_item
    assert "postal" in first_item
    assert "address" in first_item


def test_get_centres_custom_limit_and_offset(client: TestClient, test_engine):
    """GET /api/v1/centres respects custom limit and offset query parameters."""
    with Session(test_engine) as session:
        centres = [
            PreschoolCentre(
                centre_code=f"PAGE{i:02d}",
                centre_name=f"Centre Number {i:02d}",
            )
            for i in range(1, 16)
        ]
        session.add_all(centres)
        session.commit()

    response = client.get("/api/v1/centres?limit=5&offset=10")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 15
    assert data["limit"] == 5
    assert data["offset"] == 10
    assert len(data["items"]) == 5
    assert [item["centre_code"] for item in data["items"]] == [
        "PAGE11",
        "PAGE12",
        "PAGE13",
        "PAGE14",
        "PAGE15",
    ]


@pytest.mark.parametrize(
    ("param", "val"),
    [
        ("limit", 0),
        ("limit", -1),
        ("limit", 101),
        ("limit", "invalid"),
        ("offset", -1),
        ("offset", "invalid"),
        ("spark_certified", "not_a_bool"),
        ("gov_subsidy", "not_a_bool"),
        ("level", "invalid_level"),
        ("district", 0),
        ("district", 29),
        ("district", -1),
        ("district", "invalid"),
    ],
)
def test_get_centres_invalid_parameters_return_422(
    client: TestClient, param: str, val: object
):
    """Invalid query parameter types or boundaries return HTTP 400 or HTTP 422."""
    response = client.get(f"/api/v1/centres?{param}={val}")
    assert response.status_code in (400, 422)


def test_get_centres_filter_by_query_name_and_address(
    client: TestClient, test_engine
):
    """Query parameter 'query' performs case-insensitive substring search across centre name and address."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="C01",
            centre_name="Little Skool-House On-the-Green",
            address="1 Jurong West Central 3",
        )
        c2 = PreschoolCentre(
            centre_code="C02",
            centre_name="PCF Sparkletots Preschool",
            address="Block 220 Jurong East St 21",
        )
        c3 = PreschoolCentre(
            centre_code="C03",
            centre_name="My First Skool",
            address="Block 505 Tampines Ave 5",
        )
        session.add_all([c1, c2, c3])
        session.commit()

    # Search matching centre_name
    r1 = client.get("/api/v1/centres?query=skool")
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["total"] == 2
    codes1 = {item["centre_code"] for item in data1["items"]}
    assert codes1 == {"C01", "C03"}

    # Search matching address (case-insensitive)
    r2 = client.get("/api/v1/centres?query=JURONG")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["total"] == 2
    codes2 = {item["centre_code"] for item in data2["items"]}
    assert codes2 == {"C01", "C02"}

    # Search with no match
    r3 = client.get("/api/v1/centres?query=woodlands")
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["total"] == 0
    assert data3["items"] == []


def test_get_centres_filter_by_postal_prefix(client: TestClient, test_engine):
    """Query parameter 'postal_prefix' filters centres whose postal code starts with the prefix."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="P01",
            centre_name="Centre Ang Mo Kio",
            postal="560123",
        )
        c2 = PreschoolCentre(
            centre_code="P02",
            centre_name="Centre Bishan",
            postal="570456",
        )
        c3 = PreschoolCentre(
            centre_code="P03",
            centre_name="Centre Tampines",
            postal="520789",
        )
        c4 = PreschoolCentre(
            centre_code="P04",
            centre_name="Centre Without Postal",
            postal=None,
        )
        session.add_all([c1, c2, c3, c4])
        session.commit()

    r1 = client.get("/api/v1/centres?postal_prefix=56")
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["total"] == 1
    assert data1["items"][0]["centre_code"] == "P01"

    r2 = client.get("/api/v1/centres?postal_prefix=5")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["total"] == 3
    codes = {item["centre_code"] for item in data2["items"]}
    assert codes == {"P01", "P02", "P03"}

    r3 = client.get("/api/v1/centres?postal_prefix=99")
    assert r3.status_code == 200
    assert r3.json()["total"] == 0


def test_get_centres_filter_by_boolean_flags(client: TestClient, test_engine):
    """Query parameters 'spark_certified' and 'gov_subsidy' filter by boolean flags."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="B01",
            centre_name="Certified and Subsidized",
            spark_certified=True,
            gov_subsidy=True,
        )
        c2 = PreschoolCentre(
            centre_code="B02",
            centre_name="Certified Only",
            spark_certified=True,
            gov_subsidy=False,
        )
        c3 = PreschoolCentre(
            centre_code="B03",
            centre_name="Subsidized Only",
            spark_certified=False,
            gov_subsidy=True,
        )
        c4 = PreschoolCentre(
            centre_code="B04",
            centre_name="Neither",
            spark_certified=False,
            gov_subsidy=False,
        )
        session.add_all([c1, c2, c3, c4])
        session.commit()

    r_spark_true = client.get("/api/v1/centres?spark_certified=true")
    assert r_spark_true.status_code == 200
    assert {i["centre_code"] for i in r_spark_true.json()["items"]} == {"B01", "B02"}

    r_spark_false = client.get("/api/v1/centres?spark_certified=false")
    assert r_spark_false.status_code == 200
    assert {i["centre_code"] for i in r_spark_false.json()["items"]} == {"B03", "B04"}

    r_sub_true = client.get("/api/v1/centres?gov_subsidy=true")
    assert r_sub_true.status_code == 200
    assert {i["centre_code"] for i in r_sub_true.json()["items"]} == {"B01", "B03"}

    r_sub_false = client.get("/api/v1/centres?gov_subsidy=false")
    assert r_sub_false.status_code == 200
    assert {i["centre_code"] for i in r_sub_false.json()["items"]} == {"B02", "B04"}


def test_get_centres_filter_by_level_vacancies(client: TestClient, test_engine):
    """Query parameter 'level' filters centres that have open ('Immediate' or 'Available') vacancies for that cohort."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(centre_code="L01", centre_name="Centre Infant Immediate")
        c2 = PreschoolCentre(centre_code="L02", centre_name="Centre Infant Available")
        c3 = PreschoolCentre(centre_code="L03", centre_name="Centre Infant Full")
        c4 = PreschoolCentre(centre_code="L04", centre_name="Centre PG Available")
        session.add_all([c1, c2, c3, c4])
        session.commit()
        for c in (c1, c2, c3, c4):
            session.refresh(c)

        session.add_all(
            [
                VacancyRecord(
                    record_id="V_L01",
                    centre_id=c1.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Immediate",
                ),
                VacancyRecord(
                    record_id="V_L02",
                    centre_id=c2.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
                    vacancy_status="available",
                ),
                VacancyRecord(
                    record_id="V_L03",
                    centre_id=c3.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Not Available",
                ),
                VacancyRecord(
                    record_id="V_L04",
                    centre_id=c4.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.PG,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
            ]
        )
        session.commit()

    # Infant filter should return L01 (Immediate) and L02 (available), but not L03 (Not Available) or L04 (PG only)
    r_infant = client.get("/api/v1/centres?level=infant")
    assert r_infant.status_code == 200
    data_infant = r_infant.json()
    assert data_infant["total"] == 2
    assert {i["centre_code"] for i in data_infant["items"]} == {"L01", "L02"}

    # PG filter should return L04
    r_pg = client.get("/api/v1/centres?level=pg")
    assert r_pg.status_code == 200
    data_pg = r_pg.json()
    assert data_pg["total"] == 1
    assert data_pg["items"][0]["centre_code"] == "L04"

    # N1 filter with no vacancies should return empty list
    r_n1 = client.get("/api/v1/centres?level=n1")
    assert r_n1.status_code == 200
    assert r_n1.json()["total"] == 0
    assert r_n1.json()["items"] == []


def test_get_centres_multi_criteria_filtering(client: TestClient, test_engine):
    """Combine multiple filter criteria (keyword, postal prefix, certification, subsidy, and level)."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="MC01",
            centre_name="Super Skool Jurong West",
            address="123 Jurong West Ave 1",
            postal="640123",
            spark_certified=True,
            gov_subsidy=True,
        )
        c2 = PreschoolCentre(
            centre_code="MC02",
            centre_name="Super Skool Tampines",
            address="456 Tampines St 42",
            postal="520456",
            spark_certified=True,
            gov_subsidy=True,
        )
        c3 = PreschoolCentre(
            centre_code="MC03",
            centre_name="Super Skool Jurong East",
            address="789 Jurong East St 13",
            postal="600789",
            spark_certified=False,  # Not certified
            gov_subsidy=True,
        )
        session.add_all([c1, c2, c3])
        session.commit()
        for c in (c1, c2, c3):
            session.refresh(c)

        session.add_all(
            [
                VacancyRecord(
                    record_id="V_MC01",
                    centre_id=c1.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.N2,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
                VacancyRecord(
                    record_id="V_MC02",
                    centre_id=c2.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.N2,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
            ]
        )
        session.commit()

    # Search: query="jurong", postal_prefix="64", spark_certified=true, gov_subsidy=true, level="n2"
    params = {
        "query": "jurong",
        "postal_prefix": "64",
        "spark_certified": "true",
        "gov_subsidy": "true",
        "level": "n2",
    }
    response = client.get("/api/v1/centres", params=params)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["centre_code"] == "MC01"


def test_get_centres_public_access_without_authentication(client: TestClient):
    """GET /api/v1/centres does not require Authorization header and returns 200."""
    response = client.get("/api/v1/centres")
    assert response.status_code == 200


def test_get_centre_by_id_not_found_returns_404(client: TestClient):
    """GET /api/v1/centres/{centre_id} returns HTTP 404 if centre does not exist."""
    response = client.get("/api/v1/centres/99999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_get_centre_by_id_success(client: TestClient, test_engine):
    """GET /api/v1/centres/{centre_id} returns 200 and detailed metadata with default empty relations."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC100",
            centre_name="Maple Bear Early Learning",
            contact="61234567",
            email="maple@example.com",
            postal="123456",
            website_url="https://maplebear.sg",
            food_offered="Halal Certified",
            address="10 Orchard Road, #01-01",
            second_languages="Chinese, Malay",
            spark_certified=True,
            weekday_full_day_hours="7:00 am - 7:00 pm",
            saturday_hours="7:00 am - 2:00 pm",
            extended_operating_hours=True,
            transport_provided=True,
            gov_subsidy=True,
            last_updated=datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}")
    assert response.status_code == 200
    data = response.json()

    assert data["centre_id"] == centre_id
    assert data["centre_code"] == "PC100"
    assert data["centre_name"] == "Maple Bear Early Learning"
    assert data["contact"] == "61234567"
    assert data["email"] == "maple@example.com"
    assert data["postal"] == "123456"
    assert data["website_url"] == "https://maplebear.sg"
    assert data["food_offered"] == "Halal Certified"
    assert data["address"] == "10 Orchard Road, #01-01"
    assert data["second_languages"] == "Chinese, Malay"
    assert data["spark_certified"] is True
    assert data["weekday_full_day_hours"] == "7:00 am - 7:00 pm"
    assert data["saturday_hours"] == "7:00 am - 2:00 pm"
    assert data["extended_operating_hours"] is True
    assert data["transport_provided"] is True
    assert data["gov_subsidy"] is True
    assert data["last_updated"] == "2026-10-01T10:00:00Z"
    assert data["vacancies"] == []
    assert data["services"] == []
    assert data["review_summary"] == {
        "average_rating": None,
        "total_reviews": 0,
    }


def test_get_centre_by_id_vacancies_and_services_ordered(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id} returns correctly ordered vacancies and services."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC200",
            centre_name="EtonHouse International",
        )
        other_centre = PreschoolCentre(
            centre_code="PC999",
            centre_name="Other Centre",
        )
        session.add_all([centre, other_centre])
        session.commit()
        session.refresh(centre)
        session.refresh(other_centre)

        # Vacancies inserted out-of-order:
        # Expected order:
        # 1. 2026-11-01, infant
        # 2. 2026-11-01, n1
        # 3. 2026-12-01, infant
        # 4. 2026-12-01, pg
        session.add_all(
            [
                VacancyRecord(
                    record_id="V2",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.PG,
                    enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
                VacancyRecord(
                    record_id="V1",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
                    vacancy_status="Immediate",
                ),
                VacancyRecord(
                    record_id="V3",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.N1,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Full",
                ),
                VacancyRecord(
                    record_id="V4",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Available",
                ),
                VacancyRecord(
                    record_id="V_OTHER",
                    centre_id=other_centre.centre_id,  # type: ignore[arg-type]
                    level_type=PreschoolLevel.INFANT,
                    enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                    vacancy_status="Immediate",
                ),
            ]
        )

        # Services inserted out-of-order:
        # Expected order: level ASC, citizenship_type ASC
        # level: infant ("infant") < n1 ("n1") < n2 ("n2")
        # For infant: OTHERS ("OTHERS") < SC ("SC") < SPR ("SPR")
        session.add_all(
            [
                PreschoolService(
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    type_of_service="Full Day",
                    level=PreschoolLevel.N2,
                    fee=1200.0,
                    citizenship_type=CitizenshipType.OTHERS,
                    remarks="Foreigner tier",
                ),
                PreschoolService(
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    type_of_service="Full Day",
                    level=PreschoolLevel.INFANT,
                    fee=950.0,
                    citizenship_type=CitizenshipType.SPR,
                    remarks="SPR tier",
                ),
                PreschoolService(
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    type_of_service="Full Day",
                    level=PreschoolLevel.INFANT,
                    fee=750.0,
                    citizenship_type=CitizenshipType.SC,
                    remarks="SC tier",
                ),
                PreschoolService(
                    centre_id=other_centre.centre_id,  # type: ignore[arg-type]
                    type_of_service="Full Day",
                    level=PreschoolLevel.INFANT,
                    fee=500.0,
                    citizenship_type=CitizenshipType.SC,
                ),
            ]
        )
        session.commit()
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}")
    assert response.status_code == 200
    data = response.json()

    # Verify vacancies count and ordered sequence
    vacancies = data["vacancies"]
    assert len(vacancies) == 4
    assert [
        (v["enrollment_month_year"][:10], v["level_type"], v["record_id"])
        for v in vacancies
    ] == [
        ("2026-11-01", "infant", "V4"),
        ("2026-11-01", "n1", "V3"),
        ("2026-12-01", "infant", "V1"),
        ("2026-12-01", "pg", "V2"),
    ]

    # Verify services count and ordered sequence (level ASC, citizenship_type ASC)
    services = data["services"]
    assert len(services) == 3
    assert [
        (s["level"], s["citizenship_type"], s["fee"])
        for s in services
    ] == [
        ("infant", "SC", 750.0),
        ("infant", "SPR", 950.0),
        ("n2", "OTHERS", 1200.0),
    ]


def test_get_centre_by_id_review_summary_strictly_approved(
    client: TestClient, test_engine
):
    """Review summary counts and averages only reviews with ModerationStatus.APPROVED."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC300",
            centre_name="Review Target Centre",
        )
        other_centre = PreschoolCentre(
            centre_code="PC301",
            centre_name="Other Review Centre",
        )
        session.add_all([centre, other_centre])
        session.commit()
        session.refresh(centre)
        session.refresh(other_centre)

        users = [
            User(
                user_id=f"u_{i}",
                firebase_uid=f"fb_{i}",
                email=f"user_{i}@example.com",
                full_name=f"Parent {i}",
            )
            for i in range(1, 6)
        ]
        session.add_all(users)
        session.commit()

        # Centre 1 reviews:
        # User 1: 5 stars, APPROVED
        # User 2: 5 stars, APPROVED
        # User 3: 4 stars, APPROVED  -> (5 + 5 + 4) / 3 = 14 / 3 = 4.67
        # User 4: 1 star, PENDING (excluded)
        # User 5: 1 star, REJECTED (excluded)
        session.add_all(
            [
                Review(
                    review_id="REV_1",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="u_1",
                    rating=5,
                    moderation_status=ModerationStatus.APPROVED,
                ),
                Review(
                    review_id="REV_2",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="u_2",
                    rating=5,
                    moderation_status=ModerationStatus.APPROVED,
                ),
                Review(
                    review_id="REV_3",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="u_3",
                    rating=4,
                    moderation_status=ModerationStatus.APPROVED,
                ),
                Review(
                    review_id="REV_4",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="u_4",
                    rating=1,
                    moderation_status=ModerationStatus.PENDING,
                ),
                Review(
                    review_id="REV_5",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="u_5",
                    rating=1,
                    moderation_status=ModerationStatus.REJECTED,
                ),
            ]
        )
        session.commit()
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}")
    assert response.status_code == 200
    data = response.json()

    review_summary = data["review_summary"]
    assert review_summary["total_reviews"] == 3
    assert review_summary["average_rating"] == 4.67


def test_get_centre_by_code_not_found_returns_404(client: TestClient):
    """GET /api/v1/centres/code/{centre_code} returns HTTP 404 when centre_code does not exist."""
    response = client.get("/api/v1/centres/code/NONEXISTENT_CODE")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_get_centre_by_code_success(client: TestClient, test_engine):
    """GET /api/v1/centres/code/{centre_code} returns identical payload structure to by_id."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="ECDA-0042",
            centre_name="Superstar Montessori",
            contact="65551234",
            email="superstar@example.com",
            postal="543210",
            spark_certified=True,
            gov_subsidy=True,
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        session.add(
            VacancyRecord(
                record_id="VR_CODE_1",
                centre_id=centre.centre_id,  # type: ignore[arg-type]
                level_type=PreschoolLevel.K1,
                enrollment_month_year=datetime(2026, 11, 1, tzinfo=timezone.utc),
                vacancy_status="Immediate",
            )
        )
        session.add(
            PreschoolService(
                centre_id=centre.centre_id,  # type: ignore[arg-type]
                type_of_service="Full Day",
                level=PreschoolLevel.K1,
                fee=800.0,
                citizenship_type=CitizenshipType.SC,
            )
        )

        user = User(
            user_id="u_code",
            firebase_uid="fb_code",
            email="parent_code@example.com",
            full_name="Parent Code",
        )
        session.add(user)
        session.commit()

        session.add(
            Review(
                review_id="REV_CODE_1",
                centre_id=centre.centre_id,  # type: ignore[arg-type]
                user_id="u_code",
                rating=5,
                moderation_status=ModerationStatus.APPROVED,
            )
        )
        session.commit()
        centre_id = centre.centre_id

    response_by_code = client.get("/api/v1/centres/code/ECDA-0042")
    assert response_by_code.status_code == 200
    data_by_code = response_by_code.json()

    response_by_id = client.get(f"/api/v1/centres/{centre_id}")
    assert response_by_id.status_code == 200
    data_by_id = response_by_id.json()

    # Verify payloads are identical
    assert data_by_code == data_by_id
    assert data_by_code["centre_code"] == "ECDA-0042"
    assert len(data_by_code["vacancies"]) == 1
    assert data_by_code["vacancies"][0]["level_type"] == "k1"
    assert len(data_by_code["services"]) == 1
    assert data_by_code["services"][0]["fee"] == 800.0
    assert data_by_code["review_summary"] == {
        "average_rating": 5.0,
        "total_reviews": 1,
    }


def test_get_centre_endpoints_public_access_without_authentication(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}, /code/{centre_code}, and /{centre_id}/reviews do not require authentication."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_PUB",
            centre_name="Public Access Test Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        centre_id = centre.centre_id

    r_id = client.get(f"/api/v1/centres/{centre_id}")
    assert r_id.status_code == 200

    r_code = client.get("/api/v1/centres/code/PC_PUB")
    assert r_code.status_code == 200

    r_rev = client.get(f"/api/v1/centres/{centre_id}/reviews")
    assert r_rev.status_code == 200



def test_get_centre_reviews_not_found_returns_404(client: TestClient):
    """GET /api/v1/centres/{centre_id}/reviews returns HTTP 404 if centre does not exist."""
    response = client.get("/api/v1/centres/99999/reviews")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_get_centre_reviews_empty_returns_200_envelope(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}/reviews returns 200 with empty items list and default pagination metadata."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_REV_0",
            centre_name="Empty Reviews Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}/reviews")
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_get_centre_reviews_strictly_filters_approved_and_sanitizes_anonymity(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}/reviews strictly returns approved reviews for the target centre only,

    exposing sanitized fields (review_id, rating, comment, created_at) without parent identifiers.
    """
    dt1 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 11, 14, 0, 0, tzinfo=timezone.utc)

    with Session(test_engine) as session:
        target_centre = PreschoolCentre(
            centre_code="PC_REV_TARGET",
            centre_name="Target Review Centre",
        )
        other_centre = PreschoolCentre(
            centre_code="PC_REV_OTHER",
            centre_name="Other Review Centre",
        )
        session.add_all([target_centre, other_centre])
        session.commit()
        session.refresh(target_centre)
        session.refresh(other_centre)

        users = [
            User(
                user_id=f"parent_{i}",
                firebase_uid=f"fb_parent_{i}",
                email=f"parent_{i}@example.com",
                full_name=f"Parent Name {i}",
            )
            for i in range(1, 6)
        ]
        session.add_all(users)
        session.commit()

        # Target centre reviews:
        # 1. Approved with comment (dt2)
        # 2. Approved without comment (dt1)
        # 3. Pending review (should be excluded)
        # 4. Rejected review (should be excluded)
        # Other centre review:
        # 5. Approved review for other centre (should be excluded)
        session.add_all(
            [
                Review(
                    review_id="REV_APP_1",
                    centre_id=target_centre.centre_id,  # type: ignore[arg-type]
                    user_id="parent_1",
                    rating=5,
                    review_description="Outstanding teachers and nurturing environment!",
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=dt2,
                ),
                Review(
                    review_id="REV_APP_2",
                    centre_id=target_centre.centre_id,  # type: ignore[arg-type]
                    user_id="parent_2",
                    rating=4,
                    review_description=None,
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=dt1,
                ),
                Review(
                    review_id="REV_PEND_3",
                    centre_id=target_centre.centre_id,  # type: ignore[arg-type]
                    user_id="parent_3",
                    rating=2,
                    review_description="Pending moderation feedback",
                    moderation_status=ModerationStatus.PENDING,
                    created_at=dt2,
                ),
                Review(
                    review_id="REV_REJ_4",
                    centre_id=target_centre.centre_id,  # type: ignore[arg-type]
                    user_id="parent_4",
                    rating=1,
                    review_description="Inappropriate rejected text",
                    moderation_status=ModerationStatus.REJECTED,
                    created_at=dt2,
                ),
                Review(
                    review_id="REV_OTHER_5",
                    centre_id=other_centre.centre_id,  # type: ignore[arg-type]
                    user_id="parent_5",
                    rating=5,
                    review_description="Great other centre",
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=dt2,
                ),
            ]
        )
        session.commit()
        centre_id = target_centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}/reviews")
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 2
    assert data["limit"] == 20
    assert data["offset"] == 0
    assert len(data["items"]) == 2

    # Verify first review (dt2 is newer)
    item_1 = data["items"][0]
    assert item_1["review_id"] == "REV_APP_1"
    assert item_1["rating"] == 5
    assert item_1["comment"] == "Outstanding teachers and nurturing environment!"
    assert item_1["created_at"] == "2026-10-11T14:00:00Z"

    # Verify second review (dt1 is older, null comment)
    item_2 = data["items"][1]
    assert item_2["review_id"] == "REV_APP_2"
    assert item_2["rating"] == 4
    assert item_2["comment"] is None
    assert item_2["created_at"] == "2026-10-10T12:00:00Z"

    # Strict anonymity assertion: verify none of the items expose parent/internal keys
    forbidden_keys = {
        "user_id",
        "user",
        "parent",
        "parent_id",
        "email",
        "full_name",
        "firebase_uid",
        "moderation_status",
        "moderated_by",
        "moderated_at",
        "review_description",
    }
    for item in data["items"]:
        item_keys = set(item.keys())
        assert item_keys == {"review_id", "rating", "comment", "created_at"}
        assert forbidden_keys.isdisjoint(item_keys)


def test_get_centre_reviews_ordered_chronologically_desc(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}/reviews orders reviews chronologically with most recent first."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_REV_ORDER",
            centre_name="Order Test Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        users = [
            User(
                user_id=f"order_parent_{i}",
                firebase_uid=f"order_fb_{i}",
                email=f"order_{i}@example.com",
                full_name=f"Order Parent {i}",
            )
            for i in range(1, 5)
        ]
        session.add_all(users)
        session.commit()

        # Insert out of chronological order:
        # A: Oct 1, B: Oct 5, C: Oct 3, D: Oct 9
        session.add_all(
            [
                Review(
                    review_id="REV_A",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="order_parent_1",
                    rating=3,
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
                ),
                Review(
                    review_id="REV_B",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="order_parent_2",
                    rating=4,
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc),
                ),
                Review(
                    review_id="REV_C",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="order_parent_3",
                    rating=5,
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc),
                ),
                Review(
                    review_id="REV_D",
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    user_id="order_parent_4",
                    rating=5,
                    moderation_status=ModerationStatus.APPROVED,
                    created_at=datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}/reviews")
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 4
    review_ids = [item["review_id"] for item in data["items"]]
    assert review_ids == ["REV_D", "REV_B", "REV_C", "REV_A"]


def test_get_centre_reviews_pagination_defaults_and_custom_offsets(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}/reviews returns correct envelope with default and custom pagination parameters."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_REV_PAGE",
            centre_name="Pagination Test Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        users = [
            User(
                user_id=f"page_user_{i}",
                firebase_uid=f"page_fb_{i}",
                email=f"page_user_{i}@example.com",
                full_name=f"Page User {i}",
            )
            for i in range(1, 26)
        ]
        session.add_all(users)
        session.commit()

        # Create 25 approved reviews with sequential timestamps
        reviews = [
            Review(
                review_id=f"REV_PG_{i:02d}",
                centre_id=centre.centre_id,  # type: ignore[arg-type]
                user_id=f"page_user_{i}",
                rating=4,
                review_description=f"Review number {i}",
                moderation_status=ModerationStatus.APPROVED,
                created_at=datetime(2026, 10, 1, 0, i, 0, tzinfo=timezone.utc),
            )
            for i in range(1, 26)
        ]
        session.add_all(reviews)
        session.commit()
        centre_id = centre.centre_id

    # 1. Default pagination: limit=20, offset=0
    response_default = client.get(f"/api/v1/centres/{centre_id}/reviews")
    assert response_default.status_code == 200
    data_default = response_default.json()

    assert data_default["total"] == 25
    assert data_default["limit"] == 20
    assert data_default["offset"] == 0
    assert len(data_default["items"]) == 20
    # First item is newest (minute 25 -> REV_PG_25)
    assert data_default["items"][0]["review_id"] == "REV_PG_25"
    assert data_default["items"][19]["review_id"] == "REV_PG_06"

    # 2. Custom pagination: limit=5, offset=10
    response_custom = client.get(
        f"/api/v1/centres/{centre_id}/reviews?limit=5&offset=10"
    )
    assert response_custom.status_code == 200
    data_custom = response_custom.json()

    assert data_custom["total"] == 25
    assert data_custom["limit"] == 5
    assert data_custom["offset"] == 10
    assert len(data_custom["items"]) == 5
    # Sequence offset 10 to 14: REV_PG_15, REV_PG_14, REV_PG_13, REV_PG_12, REV_PG_11
    assert [item["review_id"] for item in data_custom["items"]] == [
        "REV_PG_15",
        "REV_PG_14",
        "REV_PG_13",
        "REV_PG_12",
        "REV_PG_11",
    ]


@pytest.mark.parametrize(
    ("param", "val"),
    [
        ("limit", 0),
        ("limit", -1),
        ("limit", 101),
        ("limit", "not_an_int"),
        ("offset", -1),
        ("offset", "not_an_int"),
    ],
)
def test_get_centre_reviews_invalid_pagination_returns_422(
    client: TestClient, param: str, val: object
):
    """GET /api/v1/centres/{centre_id}/reviews returns HTTP 400 or HTTP 422 for invalid limit or offset values."""
    response = client.get(f"/api/v1/centres/1/reviews?{param}={val}")
    assert response.status_code in (400, 422)


def test_get_centre_by_id_vacancies_comprehensive_developmental_cohort_ordering(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id} orders vacancies by enrollment month ASC then developmental cohort progression."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_DEV_VAC",
            centre_name="Developmental Order Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        # Insert cohort levels out-of-order for the same enrollment month
        # Alphabetical order: infant, k1, k2, n1, n2, pg, student_care
        # Developmental progression: infant -> pg -> n1 -> n2 -> k1 -> k2 -> student_care
        cohorts_shuffled = [
            (PreschoolLevel.STUDENT_CARE, "V_SC"),
            (PreschoolLevel.K2, "V_K2"),
            (PreschoolLevel.PG, "V_PG"),
            (PreschoolLevel.K1, "V_K1"),
            (PreschoolLevel.N2, "V_N2"),
            (PreschoolLevel.INFANT, "V_INF"),
            (PreschoolLevel.N1, "V_N1"),
        ]

        target_month = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for level, rec_id in cohorts_shuffled:
            session.add(
                VacancyRecord(
                    record_id=rec_id,
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    level_type=level,
                    enrollment_month_year=target_month,
                    vacancy_status="Available",
                )
            )
        session.commit()
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}")
    assert response.status_code == 200
    data = response.json()

    ordered_levels = [v["level_type"] for v in data["vacancies"]]
    assert ordered_levels == [
        "infant",
        "pg",
        "n1",
        "n2",
        "k1",
        "k2",
        "student_care",
    ]


def test_get_centre_by_id_services_citizenship_priority_and_level_ordering(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id} orders services by developmental progression then citizenship priority."""
    with Session(test_engine) as session:
        centre = PreschoolCentre(
            centre_code="PC_DEV_SVC",
            centre_name="Citizenship Priority Centre",
        )
        session.add(centre)
        session.commit()
        session.refresh(centre)

        # For INFANT: insert OTHERS, SPR, SC (alphabetical would put OTHERS first)
        # Priority order must be SC -> SPR -> OTHERS
        # For PG: insert OTHERS, SC
        # For K1: insert SC
        # Developmental level progression must put PG before K1
        test_services = [
            (PreschoolLevel.K1, CitizenshipType.SC, 1100.0),
            (PreschoolLevel.PG, CitizenshipType.OTHERS, 950.0),
            (PreschoolLevel.INFANT, CitizenshipType.OTHERS, 1400.0),
            (PreschoolLevel.PG, CitizenshipType.SC, 600.0),
            (PreschoolLevel.INFANT, CitizenshipType.SPR, 1100.0),
            (PreschoolLevel.INFANT, CitizenshipType.SC, 800.0),
        ]

        for level, cit, fee in test_services:
            session.add(
                PreschoolService(
                    centre_id=centre.centre_id,  # type: ignore[arg-type]
                    type_of_service="Full Day",
                    level=level,
                    fee=fee,
                    citizenship_type=cit,
                )
            )
        session.commit()
        centre_id = centre.centre_id

    response = client.get(f"/api/v1/centres/{centre_id}")
    assert response.status_code == 200
    data = response.json()

    services_seq = [
        (s["level"], s["citizenship_type"], s["fee"]) for s in data["services"]
    ]
    assert services_seq == [
        ("infant", "SC", 800.0),
        ("infant", "SPR", 1100.0),
        ("infant", "OTHERS", 1400.0),
        ("pg", "SC", 600.0),
        ("pg", "OTHERS", 950.0),
        ("k1", "SC", 1100.0),
    ]


def test_get_centre_summary_ecda_heartbeat_filtering_and_fallback(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/summary filters sync timestamp to ECDA_DATASTORE, with fallback to max if not present."""
    t_ecda = datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    t_other_later = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)

    with Session(test_engine) as session:
        # Both ECDA and another data source with a newer timestamp exist
        session.add(
            ExternalDataSourceStatus(
                source_id="ECDA_DATASTORE",
                source_name="ECDA Datastore",
                sync_state=SyncState.SUCCESS,
                last_successful_sync=t_ecda,
            )
        )
        session.add(
            ExternalDataSourceStatus(
                source_id="OTHER_FEED",
                source_name="Other Feed",
                sync_state=SyncState.SUCCESS,
                last_successful_sync=t_other_later,
            )
        )
        session.commit()

    # Must specifically return ECDA_DATASTORE timestamp despite OTHER_FEED being newer
    response = client.get("/api/v1/centres/summary")
    assert response.status_code == 200
    data = response.json()
    assert (
        datetime.fromisoformat(data["last_sync_time"].replace("Z", "+00:00"))
        == t_ecda
    )

    # When ECDA is deleted (not present), falls back to overall max
    with Session(test_engine) as session:
        ecda_rec = session.get(ExternalDataSourceStatus, "ECDA_DATASTORE")
        if ecda_rec:
            session.delete(ecda_rec)
            session.commit()

    response2 = client.get("/api/v1/centres/summary")
    assert response2.status_code == 200
    data2 = response2.json()
    assert (
        datetime.fromisoformat(data2["last_sync_time"].replace("Z", "+00:00"))
        == t_other_later
    )














def test_get_postal_districts_reference_endpoint_returns_all_28_districts(
    client: TestClient,
):
    """GET /api/v1/centres/districts returns all 28 canonical Postal Districts in ascending order."""
    response = client.get("/api/v1/centres/districts")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 28

    # Ensure strictly ascending 1 to 28
    assert [item["district"] for item in data] == list(range(1, 29))

    # Verify key canonical districts
    d1 = next(item for item in data if item["district"] == 1)
    assert d1["name"] == "Raffles Place, Cecil, Marina, People's Park"
    assert d1["postal_sectors"] == ["01", "02", "03", "04", "05", "06"]

    d14 = next(item for item in data if item["district"] == 14)
    assert d14["name"] == "Geylang, Eunos, Paya Lebar"
    assert d14["postal_sectors"] == ["38", "39", "40", "41"]

    d17 = next(item for item in data if item["district"] == 17)
    assert d17["name"] == "Loyang, Changi"
    assert d17["postal_sectors"] == ["49", "50", "81"]

    d19 = next(item for item in data if item["district"] == 19)
    assert d19["name"] == "Serangoon Garden, Hougang, Punggol"
    assert d19["postal_sectors"] == ["53", "54", "55", "82"]

    d23 = next(item for item in data if item["district"] == 23)
    assert d23["name"] == "Hillview, Dairy Farm, Bukit Panjang, Choa Chu Kang"
    assert d23["postal_sectors"] == ["65", "66", "67", "68"]

    # Ensure unassigned sector 74 does not belong to any district
    all_sectors = [sector for item in data for sector in item["postal_sectors"]]
    assert "74" not in all_sectors
    assert len(all_sectors) == 81


def test_centres_response_includes_resolved_district_and_name(
    client: TestClient, test_engine
):
    """PreschoolCentreItem and detail responses include resolved district and district_name."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="D01_C",
            centre_name="Downtown Marina Centre",
            postal="018956",
        )
        c2 = PreschoolCentre(
            centre_code="D14_C",
            centre_name="Geylang Learning Hub",
            postal="380001",
        )
        c3 = PreschoolCentre(
            centre_code="D23_C",
            centre_name="Hillview Child Development",
            postal="650123",
        )
        c4 = PreschoolCentre(
            centre_code="D74_C",
            centre_name="Unmapped Sector Centre",
            postal="740001",
        )
        c5 = PreschoolCentre(
            centre_code="D_NONE_C",
            centre_name="No Postal Centre",
            postal=None,
        )
        session.add_all([c1, c2, c3, c4, c5])
        session.commit()
        session.refresh(c2)

    # Test listing endpoint
    res = client.get("/api/v1/centres?limit=10")
    assert res.status_code == 200
    items = {item["centre_code"]: item for item in res.json()["items"]}

    assert items["D01_C"]["district"] == 1
    assert items["D01_C"]["district_name"] == "Raffles Place, Cecil, Marina, People's Park"

    assert items["D14_C"]["district"] == 14
    assert items["D14_C"]["district_name"] == "Geylang, Eunos, Paya Lebar"

    assert items["D23_C"]["district"] == 23
    assert items["D23_C"]["district_name"] == "Hillview, Dairy Farm, Bukit Panjang, Choa Chu Kang"

    assert items["D74_C"]["district"] is None
    assert items["D74_C"]["district_name"] is None

    assert items["D_NONE_C"]["district"] is None
    assert items["D_NONE_C"]["district_name"] is None

    # Test detail by ID
    res_detail_id = client.get(f"/api/v1/centres/{c2.centre_id}")
    assert res_detail_id.status_code == 200
    detail_data = res_detail_id.json()
    assert detail_data["district"] == 14
    assert detail_data["district_name"] == "Geylang, Eunos, Paya Lebar"

    # Test detail by code
    res_detail_code = client.get("/api/v1/centres/code/D14_C")
    assert res_detail_code.status_code == 200
    code_data = res_detail_code.json()
    assert code_data["district"] == 14
    assert code_data["district_name"] == "Geylang, Eunos, Paya Lebar"


def test_get_centres_filter_by_single_district(
    client: TestClient, test_engine
):
    """Filtering by a single district returns centres matching all postal sectors in that district."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="D01_C1",
            centre_name="Raffles Quay Childcare",
            postal="018956",  # Sector 01 -> District 1
        )
        c2 = PreschoolCentre(
            centre_code="D01_C2",
            centre_name="Shenton Way Infant Care",
            postal="069123",  # Sector 06 -> District 1
        )
        c3 = PreschoolCentre(
            centre_code="D14_C1",
            centre_name="Geylang East Preschool",
            postal="380001",  # Sector 38 -> District 14
        )
        c4 = PreschoolCentre(
            centre_code="D14_C2",
            centre_name="Eunos Learning Center",
            postal="409002",  # Sector 40 -> District 14
        )
        c5 = PreschoolCentre(
            centre_code="D23_C1",
            centre_name="Bukit Panjang Kids Center",
            postal="670123",  # Sector 67 -> District 23
        )
        session.add_all([c1, c2, c3, c4, c5])
        session.commit()

    # Query district 1 (leading zeros)
    res_d1 = client.get("/api/v1/centres?district=1")
    assert res_d1.status_code == 200
    data_d1 = res_d1.json()
    assert data_d1["total"] == 2
    codes_d1 = {item["centre_code"] for item in data_d1["items"]}
    assert codes_d1 == {"D01_C1", "D01_C2"}

    # Query district 14
    res_d14 = client.get("/api/v1/centres?district=14")
    assert res_d14.status_code == 200
    data_d14 = res_d14.json()
    assert data_d14["total"] == 2
    codes_d14 = {item["centre_code"] for item in data_d14["items"]}
    assert codes_d14 == {"D14_C1", "D14_C2"}

    # Query district 28 with no centres
    res_d28 = client.get("/api/v1/centres?district=28")
    assert res_d28.status_code == 200
    assert res_d28.json()["total"] == 0


def test_get_centres_filter_by_multiple_districts_and_deduplication(
    client: TestClient, test_engine
):
    """Filtering by multiple districts returns the union, and duplicate params are deduplicated."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="M_D01",
            centre_name="Marina Preschool",
            postal="029876",  # Sector 02 -> District 1
        )
        c2 = PreschoolCentre(
            centre_code="M_D14",
            centre_name="Paya Lebar Preschool",
            postal="419876",  # Sector 41 -> District 14
        )
        c3 = PreschoolCentre(
            centre_code="M_D23",
            centre_name="Choa Chu Kang Preschool",
            postal="680123",  # Sector 68 -> District 23
        )
        c4 = PreschoolCentre(
            centre_code="M_D74",
            centre_name="Unassigned Sector Preschool",
            postal="740123",  # Unassigned sector 74
        )
        c5 = PreschoolCentre(
            centre_code="M_NONE",
            centre_name="Missing Postal Preschool",
            postal=None,
        )
        session.add_all([c1, c2, c3, c4, c5])
        session.commit()

    # Query multiple districts: 14 and 23
    res_multi = client.get("/api/v1/centres?district=14&district=23")
    assert res_multi.status_code == 200
    data_multi = res_multi.json()
    assert data_multi["total"] == 2
    codes_multi = {item["centre_code"] for item in data_multi["items"]}
    assert codes_multi == {"M_D14", "M_D23"}

    # Duplicate district query: district=14&district=14
    res_dup = client.get("/api/v1/centres?district=14&district=14")
    assert res_dup.status_code == 200
    data_dup = res_dup.json()
    assert data_dup["total"] == 1
    assert data_dup["items"][0]["centre_code"] == "M_D14"

    # Ensure unmapped (74) and None are excluded when filtering by all districts containing 1, 14, 23
    res_all_three = client.get("/api/v1/centres?district=1&district=14&district=23")
    assert res_all_three.status_code == 200
    data_all_three = res_all_three.json()
    assert data_all_three["total"] == 3
    codes_all_three = {item["centre_code"] for item in data_all_three["items"]}
    assert codes_all_three == {"M_D01", "M_D14", "M_D23"}


def test_get_centres_combined_district_with_all_other_filters(
    client: TestClient, test_engine
):
    """District filter combines conjunctively (AND) with query, postal_prefix, spark, subsidy, and level."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="COMB_1",
            centre_name="Sunflower Geylang",
            address="100 Geylang Rd",
            postal="380001",  # District 14, prefix 38
            spark_certified=True,
            gov_subsidy=True,
        )
        c2 = PreschoolCentre(
            centre_code="COMB_2",
            centre_name="Sunflower Eunos",
            address="200 Eunos Rd",
            postal="380002",  # District 14, prefix 38
            spark_certified=True,
            gov_subsidy=False,
        )
        c3 = PreschoolCentre(
            centre_code="COMB_3",
            centre_name="Moonlight Geylang",
            address="300 Geylang Rd",
            postal="380003",  # District 14, prefix 38
            spark_certified=False,
            gov_subsidy=True,
        )
        c4 = PreschoolCentre(
            centre_code="COMB_4",
            centre_name="Sunflower Paya Lebar",
            address="400 Paya Lebar Rd",
            postal="400001",  # District 14, prefix 40
            spark_certified=True,
            gov_subsidy=True,
        )
        c5 = PreschoolCentre(
            centre_code="COMB_5",
            centre_name="Sunflower Raffles",
            address="10 Raffles Quay",
            postal="018956",  # District 1, prefix 01
            spark_certified=True,
            gov_subsidy=True,
        )
        session.add_all([c1, c2, c3, c4, c5])
        session.commit()
        session.refresh(c1)
        session.refresh(c2)
        session.refresh(c3)
        session.refresh(c4)
        session.refresh(c5)
        assert c1.centre_id is not None
        assert c2.centre_id is not None
        assert c3.centre_id is not None
        assert c4.centre_id is not None
        assert c5.centre_id is not None

        # Vacancies
        v1 = VacancyRecord(
            record_id="VR_C1_infant",
            centre_id=c1.centre_id,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            vacancy_status="Immediate",
        )
        v2 = VacancyRecord(
            record_id="VR_C2_infant",
            centre_id=c2.centre_id,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            vacancy_status="Immediate",
        )
        v3 = VacancyRecord(
            record_id="VR_C3_infant",
            centre_id=c3.centre_id,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            vacancy_status="Immediate",
        )
        v4 = VacancyRecord(
            record_id="VR_C4_k1",
            centre_id=c4.centre_id,
            level_type=PreschoolLevel.K1,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            vacancy_status="Immediate",
        )
        v5 = VacancyRecord(
            record_id="VR_C5_infant",
            centre_id=c5.centre_id,
            level_type=PreschoolLevel.INFANT,
            enrollment_month_year=datetime(2026, 12, 1, tzinfo=timezone.utc),
            vacancy_status="Immediate",
        )
        session.add_all([v1, v2, v3, v4, v5])
        session.commit()

    # 1. District 14 + spark_certified=true -> C1, C2, C4
    r1 = client.get("/api/v1/centres?district=14&spark_certified=true")
    assert r1.status_code == 200
    assert {i["centre_code"] for i in r1.json()["items"]} == {"COMB_1", "COMB_2", "COMB_4"}

    # 2. District 14 + gov_subsidy=true -> C1, C3, C4
    r2 = client.get("/api/v1/centres?district=14&gov_subsidy=true")
    assert r2.status_code == 200
    assert {i["centre_code"] for i in r2.json()["items"]} == {"COMB_1", "COMB_3", "COMB_4"}

    # 3. District 14 + level=infant -> C1, C2, C3
    r3 = client.get("/api/v1/centres?district=14&level=infant")
    assert r3.status_code == 200
    assert {i["centre_code"] for i in r3.json()["items"]} == {"COMB_1", "COMB_2", "COMB_3"}

    # 4. District 14 + postal_prefix=38 -> C1, C2, C3
    r4 = client.get("/api/v1/centres?district=14&postal_prefix=38")
    assert r4.status_code == 200
    assert {i["centre_code"] for i in r4.json()["items"]} == {"COMB_1", "COMB_2", "COMB_3"}

    # 5. District 14 + postal_prefix=40 -> C4
    r5 = client.get("/api/v1/centres?district=14&postal_prefix=40")
    assert r5.status_code == 200
    assert {i["centre_code"] for i in r5.json()["items"]} == {"COMB_4"}

    # 6. District 14 + query=sunflower -> C1, C2, C4
    r6 = client.get("/api/v1/centres?district=14&query=sunflower")
    assert r6.status_code == 200
    assert {i["centre_code"] for i in r6.json()["items"]} == {"COMB_1", "COMB_2", "COMB_4"}

    # 7. Fully combined: District 14 + query=sunflower + postal_prefix=38 + spark=true + subsidy=true + level=infant -> ONLY C1
    r7 = client.get(
        "/api/v1/centres?district=14&query=sunflower&postal_prefix=38&spark_certified=true&gov_subsidy=true&level=infant"
    )
    assert r7.status_code == 200
    assert {i["centre_code"] for i in r7.json()["items"]} == {"COMB_1"}

    # 8. Multi-district union combined: District 1 + District 14 + query=sunflower + spark=true + subsidy=true + level=infant -> C1 and C5
    r8 = client.get(
        "/api/v1/centres?district=1&district=14&query=sunflower&spark_certified=true&gov_subsidy=true&level=infant"
    )
    assert r8.status_code == 200
    assert {i["centre_code"] for i in r8.json()["items"]} == {"COMB_1", "COMB_5"}


def test_centres_invalid_and_malformed_postal_resolves_to_null(
    client: TestClient, test_engine
):
    """Centres with invalid, partial, or malformed postal codes resolve district and district_name to null."""
    with Session(test_engine) as session:
        c1 = PreschoolCentre(
            centre_code="INV_SHORT",
            centre_name="Short Postal Centre",
            postal="12",
        )
        c2 = PreschoolCentre(
            centre_code="INV_ALPHA",
            centre_name="Alpha Postal Centre",
            postal="12ABCD",
        )
        c3 = PreschoolCentre(
            centre_code="INV_FIVE",
            centre_name="Five Digit Postal Centre",
            postal="12345",
        )
        c4 = PreschoolCentre(
            centre_code="INV_LONG",
            centre_name="Seven Digit Postal Centre",
            postal="1234567",
        )
        session.add_all([c1, c2, c3, c4])
        session.commit()

    res = client.get("/api/v1/centres?limit=10")
    assert res.status_code == 200
    items = {item["centre_code"]: item for item in res.json()["items"]}

    for code in ("INV_SHORT", "INV_ALPHA", "INV_FIVE", "INV_LONG"):
        assert items[code]["district"] is None
        assert items[code]["district_name"] is None


def test_get_centre_location_not_found(client: TestClient):
    """GET /api/v1/centres/{centre_id}/location returns 404 if centre does not exist."""
    response = client.get("/api/v1/centres/999999/location")
    assert response.status_code == 404
    assert response.json()["detail"] == "Preschool centre not found"


def test_get_centre_location_missing_or_invalid_postal(
    client: TestClient, test_engine
):
    """GET /api/v1/centres/{centre_id}/location returns status unavailable and coordinate null if postal is missing or invalid."""
    with Session(test_engine) as session:
        c_none = PreschoolCentre(
            centre_code="LOC_NONE",
            centre_name="No Postal Centre",
            postal=None,
        )
        c_short = PreschoolCentre(
            centre_code="LOC_SHORT",
            centre_name="Short Postal Centre",
            postal="123",
        )
        c_alpha = PreschoolCentre(
            centre_code="LOC_ALPHA",
            centre_name="Alpha Postal Centre",
            postal="56012A",
        )
        session.add_all([c_none, c_short, c_alpha])
        session.commit()
        session.refresh(c_none)
        session.refresh(c_short)
        session.refresh(c_alpha)
        id_none, id_short, id_alpha = c_none.centre_id, c_short.centre_id, c_alpha.centre_id

    for centre_id in (id_none, id_short, id_alpha):
        res = client.get(f"/api/v1/centres/{centre_id}/location")
        assert res.status_code == 200
        data = res.json()
        assert data["centre_id"] == centre_id
        assert data["coordinate"] is None
        assert data["status"] == "unavailable"


def test_get_centre_location_onemap_success(
    client: TestClient, test_engine, monkeypatch
):
    """GET /api/v1/centres/{centre_id}/location returns status available and coordinate if exact postal matches."""
    with Session(test_engine) as session:
        c = PreschoolCentre(
            centre_code="LOC_VALID",
            centre_name="Valid Postal Centre",
            postal="560123",
        )
        session.add(c)
        session.commit()
        session.refresh(c)
        centre_id = c.centre_id

    original_get = httpx.Client.get

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if "elastic/search" in url_str:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": "VALID CENTRE",
                        "POSTAL": "560123",
                        "LATITUDE": "1.369145",
                        "LONGITUDE": "103.851234",
                    }
                ],
            }
            return mock_resp
        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.get(f"/api/v1/centres/{centre_id}/location")
    assert res.status_code == 200
    data = res.json()
    assert data["centre_id"] == centre_id
    assert data["status"] == "available"
    assert data["coordinate"] == {"lat": 1.369145, "lng": 103.851234}


def test_get_centre_location_onemap_mismatched_postal(
    client: TestClient, test_engine, monkeypatch
):
    """GET /api/v1/centres/{centre_id}/location returns status unavailable if OneMap result has different postal."""
    with Session(test_engine) as session:
        c = PreschoolCentre(
            centre_code="LOC_MISMATCH",
            centre_name="Mismatched Postal Centre",
            postal="560123",
        )
        session.add(c)
        session.commit()
        session.refresh(c)
        centre_id = c.centre_id

    original_get = httpx.Client.get

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if "elastic/search" in url_str:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "found": 1,
                "results": [
                    {
                        "SEARCHVAL": "SOME OTHER PLACE",
                        "POSTAL": "560999",  # Fuzzy match with wrong postal
                        "LATITUDE": "1.369145",
                        "LONGITUDE": "103.851234",
                    }
                ],
            }
            return mock_resp
        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.get(f"/api/v1/centres/{centre_id}/location")
    assert res.status_code == 200
    data = res.json()
    assert data["centre_id"] == centre_id
    assert data["coordinate"] is None
    assert data["status"] == "unavailable"


def test_get_centre_location_onemap_failure(
    client: TestClient, test_engine, monkeypatch
):
    """GET /api/v1/centres/{centre_id}/location returns status unavailable if OneMap request fails or times out."""
    with Session(test_engine) as session:
        c = PreschoolCentre(
            centre_code="LOC_FAIL",
            centre_name="Failing Postal Centre",
            postal="560123",
        )
        session.add(c)
        session.commit()
        session.refresh(c)
        centre_id = c.centre_id

    original_get = httpx.Client.get

    def mock_get(self, url, *args, **kwargs):
        url_str = str(url)
        if "elastic/search" in url_str:
            raise httpx.ConnectTimeout("Connection timed out")
        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    res = client.get(f"/api/v1/centres/{centre_id}/location")
    assert res.status_code == 200
    data = res.json()
    assert data["centre_id"] == centre_id
    assert data["coordinate"] is None
    assert data["status"] == "unavailable"


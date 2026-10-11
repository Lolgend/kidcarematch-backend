from datetime import datetime, timezone

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
    ],
)
def test_get_centres_invalid_parameters_return_422(
    client: TestClient, param: str, val: object
):
    """Invalid query parameter types or boundaries return HTTP 422 Unprocessable Entity."""
    response = client.get(f"/api/v1/centres?{param}={val}")
    assert response.status_code == 422


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
    """GET /api/v1/centres/{centre_id} and /code/{centre_code} do not require authentication."""
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






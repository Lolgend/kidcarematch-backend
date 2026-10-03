"""Cron job to fetch preschool centres from data.gov.sg and populate PreschoolCentre table."""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlmodel import Session, SQLModel, select

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import (
    PreschoolCentre,
    PreschoolService,
    VacancyRecord,
    engine,
    init_db,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sync_centres")

DATA_GOV_SG_API_URL = "https://data.gov.sg/api/action/datastore_search"

REQUESTED_FIELDS = [
    "centre_code",
    "tp_code",
    "centre_name",
    "centre_contact_no",
    "contactno_lifesg",
    "centre_email_address",
    "emailaddress_lifesg",
    "centre_address",
    "postal_code",
    "centre_website",
    "website_lifesg",
    "food_offered",
    "second_languages_offered",
    "spark_certified",
    "weekday_full_day",
    "saturday",
    "extended_operating_hours",
    "provision_of_transport",
    "government_subsidy",
    "last_updated",
] + [
    f"{level}_vacancy_{horizon}"
    for level in ("infant", "pg", "n1", "n2", "k1", "k2")
    for horizon in (
        "current_month",
        "next_month",
        "third_month",
        "fourth_month",
        "fifth_month",
        "sixth_month",
        "seventh_month",
    )
]


def clean_str(val: Any) -> str | None:
    """Strip string and normalize empty/na values to None."""
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in ("na", "n/a", "null", "none", ""):
        return None
    return s


def parse_bool(val: Any) -> bool:
    """Parse government dataset string values into booleans."""
    if not val:
        return False
    return str(val).strip().lower() in ("yes", "true", "1", "y")


def parse_datetime(val: Any) -> datetime | None:
    """Parse date or timestamp string into UTC datetime."""
    clean_val = clean_str(val)
    if not clean_val:
        return None

    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(clean_val, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(clean_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.warning("Could not parse datetime value: %r", val)
        return None


VACANCY_LEVELS = ["infant", "pg", "n1", "n2", "k1", "k2"]
VACANCY_HORIZONS = [
    ("current_month", 0),
    ("next_month", 1),
    ("third_month", 2),
    ("fourth_month", 3),
    ("fifth_month", 4),
    ("sixth_month", 5),
    ("seventh_month", 6),
]


def add_months_to_date(base_dt: datetime, months_to_add: int) -> datetime:
    """Return the 1st of the month offset by months_to_add from base_dt in UTC."""
    year = base_dt.year
    month = base_dt.month + months_to_add
    year += (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)


def parse_vacancies_for_centre(rec: dict[str, Any], centre_id: int) -> list[VacancyRecord]:
    """Extract 42 VacancyRecord entries from a raw centre API record."""
    code = clean_str(rec.get("centre_code")) or clean_str(rec.get("tp_code"))
    if not code:
        return []

    base_dt = parse_datetime(rec.get("last_updated")) or datetime.now(timezone.utc)
    vacancies: list[VacancyRecord] = []

    for level in VACANCY_LEVELS:
        for horizon_key, month_offset in VACANCY_HORIZONS:
            field_name = f"{level}_vacancy_{horizon_key}"
            val = rec.get(field_name)
            raw_val = str(val).strip() if val is not None else ""
            if raw_val.lower() in ("na", "n/a", "not applicable"):
                status = "Not Applicable"
            else:
                status = clean_str(val) or "Not Available"

            target_month = add_months_to_date(base_dt, month_offset)
            record_id = f"{code}_{level}_{target_month.strftime('%Y%m')}"

            vacancies.append(
                VacancyRecord(
                    record_id=record_id,
                    centre_id=centre_id,
                    level_type=level,
                    enrollment_month_year=target_month,
                    vacancy_status=status,
                )
            )

    return vacancies


def parse_float(val: Any) -> float:
    """Parse numeric fee value safely."""
    clean_val = clean_str(val)
    if not clean_val:
        return 0.0
    try:
        return float(clean_val)
    except ValueError:
        logger.warning("Could not parse fee float: %r", val)
        return 0.0


def parse_centre_service(
    rec: dict[str, Any],
    centre_map: dict[str, int],
) -> PreschoolService | None:
    """Parse raw listing of centre services record into a PreschoolService instance."""
    code = clean_str(rec.get("centre_code"))
    if not code:
        return None

    centre_id = centre_map.get(code)
    if not centre_id:
        return None

    service_type = clean_str(rec.get("type_of_service"))
    level = clean_str(rec.get("levels_offered"))
    citizenship = clean_str(rec.get("type_of_citizenship"))

    if not service_type or not level or not citizenship:
        return None

    fee = parse_float(rec.get("fees"))
    updated_at = parse_datetime(rec.get("last_updated"))
    remarks = clean_str(rec.get("remarks"))

    return PreschoolService(
        centre_id=centre_id,
        type_of_service=service_type,
        level=level,
        fee=fee,
        citizenship_type=citizenship,
        last_updated=updated_at,
        remarks=remarks,
    )


def fetch_datastore_records(
    resource_id: str,
    fields: list[str],
    entity_name: str = "records",
    batch_size: int = 1000,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch paginated records from data.gov.sg datastore search endpoint."""
    all_records: list[dict[str, Any]] = []
    offset = 0
    fields_query = ",".join(fields)

    with httpx.Client(timeout=30.0) as client:
        while True:
            limit = batch_size
            if max_records and (len(all_records) + limit) > max_records:
                limit = max_records - len(all_records)

            if limit <= 0:
                break

            params: dict[str, str | int] = {
                "resource_id": resource_id,
                "limit": limit,
                "offset": offset,
                "fields": fields_query,
            }

            logger.info("Fetching %s from data.gov.sg (offset=%d, limit=%d)...", entity_name, offset, limit)

            retries = 3
            data: dict[str, Any] = {}
            for attempt in range(1, retries + 1):
                try:
                    response = client.get(DATA_GOV_SG_API_URL, params=params)
                    response.raise_for_status()
                    data = response.json() or {}
                    break
                except (httpx.HTTPError, httpx.TimeoutException) as exc:
                    logger.warning("%s API request failed (attempt %d/%d): %s", entity_name, attempt, retries, exc)
                    if attempt == retries:
                        raise
                    time.sleep(attempt * 2)

            result = data.get("result", {})
            records = result.get("records", [])

            if not records:
                logger.info("No more %s returned from API.", entity_name)
                break

            all_records.extend(records)
            logger.info("Fetched %d %s so far.", len(all_records), entity_name)

            total_available = result.get("total")
            offset += len(records)

            if total_available and offset >= total_available:
                logger.info("Reached total available %s (%d).", entity_name, total_available)
                break

            if max_records and len(all_records) >= max_records:
                break

    return all_records


def fetch_all_centres(
    resource_id: str,
    batch_size: int = 1000,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch all centre records from data.gov.sg using pagination."""
    return fetch_datastore_records(
        resource_id=resource_id,
        fields=REQUESTED_FIELDS,
        entity_name="centres",
        batch_size=batch_size,
        max_records=max_records,
    )


SERVICES_REQUESTED_FIELDS = [
    "centre_code",
    "centre_name",
    "class_of_licence",
    "type_of_service",
    "levels_offered",
    "fees",
    "type_of_citizenship",
    "last_updated",
    "remarks",
]


def fetch_all_services(
    resource_id: str,
    batch_size: int = 1000,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch all centre service records from data.gov.sg using pagination."""
    return fetch_datastore_records(
        resource_id=resource_id,
        fields=SERVICES_REQUESTED_FIELDS,
        entity_name="services",
        batch_size=batch_size,
        max_records=max_records,
    )


def sync_centres(
    max_records: int | None = None,
    dry_run: bool = False,
    db_engine: Any = None,
) -> dict[str, int]:
    """Sync data.gov.sg preschool centres, vacancies, and services into database."""
    centres_resource_id = settings.DATA_GOV_SG_CENTRES_RESOURCE_ID
    services_resource_id = settings.DATA_GOV_SG_SERVICES_RESOURCE_ID
    logger.info("Starting sync with centres resource ID: %s", centres_resource_id)

    target_engine = db_engine or engine
    # Ensure tables exist in database
    init_db(target_engine)

    centre_records = fetch_all_centres(
        resource_id=centres_resource_id,
        batch_size=1000,
        max_records=max_records,
    )
    logger.info("Total centre records fetched from API: %d", len(centre_records))

    stats = {
        "centres_fetched": len(centre_records),
        "centres_inserted": 0,
        "centres_updated": 0,
        "centres_skipped": 0,
        "vacancies_synced": 0,
        "services_fetched": 0,
        "services_inserted": 0,
        "services_updated": 0,
        "services_skipped": 0,
    }

    # Fetch services before entering the database transaction to avoid holding open connections
    service_records = fetch_all_services(
        resource_id=services_resource_id,
        batch_size=1000,
        max_records=max_records,
    )
    stats["services_fetched"] = len(service_records)
    logger.info("Total service records fetched from API: %d", len(service_records))

    with Session(target_engine) as session:
        # Load existing centres by centre_code and id
        existing_centres = {
            c.centre_code: c for c in session.exec(select(PreschoolCentre)).all()
        }
        logger.info("Found %d existing centres in the database.", len(existing_centres))

        # 1. Upsert Centres and Vacancies
        for rec in centre_records:
            code = clean_str(rec.get("centre_code")) or clean_str(rec.get("tp_code"))
            name = clean_str(rec.get("centre_name"))

            if not code or not name:
                stats["centres_skipped"] += 1
                continue

            contact = clean_str(rec.get("centre_contact_no")) or clean_str(rec.get("contactno_lifesg"))
            email = clean_str(rec.get("centre_email_address")) or clean_str(rec.get("emailaddress_lifesg"))
            postal = clean_str(rec.get("postal_code"))
            website = clean_str(rec.get("centre_website")) or clean_str(rec.get("website_lifesg"))
            food = clean_str(rec.get("food_offered"))
            address = clean_str(rec.get("centre_address"))
            languages = clean_str(rec.get("second_languages_offered"))
            spark = parse_bool(rec.get("spark_certified"))
            weekday_hours = clean_str(rec.get("weekday_full_day"))
            sat_hours = clean_str(rec.get("saturday"))
            ext_hours = parse_bool(rec.get("extended_operating_hours"))
            transport = parse_bool(rec.get("provision_of_transport"))
            subsidy = parse_bool(rec.get("government_subsidy"))
            updated_at = parse_datetime(rec.get("last_updated"))

            centre: PreschoolCentre
            if code in existing_centres:
                centre = existing_centres[code]
                centre.centre_name = name
                centre.contact = contact
                centre.email = email
                centre.postal = postal
                centre.website_url = website
                centre.food_offered = food
                centre.address = address
                centre.second_languages = languages
                centre.spark_certified = spark
                centre.weekday_full_day_hours = weekday_hours
                centre.saturday_hours = sat_hours
                centre.extended_operating_hours = ext_hours
                centre.transport_provided = transport
                centre.gov_subsidy = subsidy
                centre.last_updated = updated_at
                session.add(centre)
                stats["centres_updated"] += 1
            else:
                centre = PreschoolCentre(
                    centre_code=code,
                    centre_name=name,
                    contact=contact,
                    email=email,
                    postal=postal,
                    website_url=website,
                    food_offered=food,
                    address=address,
                    second_languages=languages,
                    spark_certified=spark,
                    weekday_full_day_hours=weekday_hours,
                    saturday_hours=sat_hours,
                    extended_operating_hours=ext_hours,
                    transport_provided=transport,
                    gov_subsidy=subsidy,
                    last_updated=updated_at,
                )
                session.add(centre)
                session.flush()  # populate centre_id for foreign keys
                existing_centres[code] = centre
                stats["centres_inserted"] += 1

            # Sync Vacancies for this centre
            vacancies = parse_vacancies_for_centre(rec, centre.centre_id)  # type: ignore[arg-type]
            for v in vacancies:
                existing_v = session.get(VacancyRecord, v.record_id)
                if existing_v:
                    existing_v.vacancy_status = v.vacancy_status
                    existing_v.enrollment_month_year = v.enrollment_month_year
                    session.add(existing_v)
                else:
                    session.add(v)
                stats["vacancies_synced"] += 1

        session.flush()

        # Build centre code / tp_code map for services lookup
        centre_map: dict[str, int] = {}
        for c in existing_centres.values():
            if c.centre_code and c.centre_id:
                centre_map[c.centre_code] = c.centre_id

        # 2. Sync Services
        # Index existing services by (centre_id, type_of_service, level, citizenship_type)
        existing_services_list = session.exec(select(PreschoolService)).all()
        existing_services: dict[tuple[int, str, str, str], PreschoolService] = {
            (s.centre_id, s.type_of_service, s.level, s.citizenship_type): s
            for s in existing_services_list
        }

        for s_rec in service_records:
            parsed_service = parse_centre_service(s_rec, centre_map)
            if not parsed_service:
                stats["services_skipped"] += 1
                continue

            s_key = (
                parsed_service.centre_id,
                parsed_service.type_of_service,
                parsed_service.level,
                parsed_service.citizenship_type,
            )
            if s_key in existing_services:
                svc = existing_services[s_key]
                svc.fee = parsed_service.fee
                svc.last_updated = parsed_service.last_updated
                svc.remarks = parsed_service.remarks
                session.add(svc)
                stats["services_updated"] += 1
            else:
                session.add(parsed_service)
                existing_services[s_key] = parsed_service
                stats["services_inserted"] += 1

        if dry_run:
            logger.info("Dry-run enabled: rolling back transaction without saving changes.")
            session.rollback()
        else:
            session.commit()
            logger.info("Transaction committed successfully.")

    logger.info("Sync completed: %s", stats)
    return stats


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Sync preschool centres from data.gov.sg into database."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of records to fetch (useful for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse records without committing to database",
    )
    args = parser.parse_args()

    start_time = time.time()
    try:
        sync_centres(max_records=args.limit, dry_run=args.dry_run)
        elapsed = time.time() - start_time
        logger.info("Job finished in %.2f seconds.", elapsed)
    except Exception as exc:
        logger.exception("Sync job failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

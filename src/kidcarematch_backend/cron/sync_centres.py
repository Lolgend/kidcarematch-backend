"""Cron job to fetch preschool centres from data.gov.sg and populate PreschoolCentre table."""

import argparse
import logging
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from kidcarematch_backend.core.config import settings
from kidcarematch_backend.db.db import (
    CitizenshipType,
    ExternalDataSourceStatus,
    PreschoolCentre,
    PreschoolLevel,
    PreschoolService,
    SyncState,
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
ECDA_DATASTORE_SOURCE_ID = "ECDA_DATASTORE"
ECDA_DATASTORE_SOURCE_NAME = "ECDA Preschool Centre Datastore"

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
            return datetime.strptime(clean_val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(clean_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
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


def resolve_centre_identifier(rec: dict[str, Any]) -> str | None:
    """Extract primary centre_code or fallback temporary permit code (tp_code)."""
    return clean_str(rec.get("centre_code")) or clean_str(rec.get("tp_code"))


def add_months_to_date(base_dt: datetime, months_to_add: int) -> datetime:
    """Offset base_dt by months_to_add and truncate to the 1st of the month at 00:00:00 UTC."""
    year = base_dt.year
    month = base_dt.month + months_to_add
    year += (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)


def parse_vacancies_for_centre(
    rec: dict[str, Any],
    centre_id: int,
    sync_date: datetime | None = None,
    centre_code: str | None = None,
) -> list[VacancyRecord]:
    """Extract 42 VacancyRecord entries from a raw centre API record."""
    code = centre_code or resolve_centre_identifier(rec)
    if not code:
        return []

    base_dt = sync_date or datetime.now(timezone.utc)
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
                    level_type=PreschoolLevel(level),
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


def normalize_preschool_level(val: Any) -> PreschoolLevel | None:
    """Normalize raw ECDA service offering level string to canonical PreschoolLevel enum member."""
    clean_val = clean_str(val)
    if not clean_val:
        logger.warning("Empty or blank preschool service level: %r", val)
        return None

    s = clean_val.strip().lower()

    # Direct match against enum value
    try:
        return PreschoolLevel(s)
    except ValueError:
        pass

    # Normalize separators
    normalized = re.sub(r"[_\-]+", " ", s)
    normalized = " ".join(normalized.split())

    # Direct match against enum member name (e.g. STUDENT_CARE)
    upper_name = normalized.upper().replace(" ", "_")
    if upper_name in PreschoolLevel.__members__:
        return PreschoolLevel[upper_name]

    # Keyword / prefix heuristics
    if "infant" in normalized:
        return PreschoolLevel.INFANT

    if "playgroup" in normalized or "play group" in normalized or re.search(r"\bpg\b", normalized):
        return PreschoolLevel.PG

    if "pre nursery" in normalized or "nursery 1" in normalized or "nursery1" in normalized or re.search(r"\bn1\b", normalized):
        return PreschoolLevel.N1

    if "nursery 2" in normalized or "nursery2" in normalized or re.search(r"\bn2\b", normalized):
        return PreschoolLevel.N2

    if "nursery" in normalized:
        return PreschoolLevel.N2

    if "kindergarten 1" in normalized or "kindergarten1" in normalized or re.search(r"\bk1\b", normalized):
        return PreschoolLevel.K1

    if "kindergarten 2" in normalized or "kindergarten2" in normalized or re.search(r"\bk2\b", normalized):
        return PreschoolLevel.K2

    if "student care" in normalized or "studentcare" in normalized or "student_care" in normalized:
        return PreschoolLevel.STUDENT_CARE

    logger.warning("Unmapped preschool service level: %r", val)
    return None


def normalize_citizenship_type(val: Any) -> CitizenshipType:
    """Normalize raw citizenship string to canonical CitizenshipType enum, defaulting to OTHERS."""
    clean_val = clean_str(val)
    if not clean_val:
        return CitizenshipType.OTHERS

    s = clean_val.strip().upper()

    try:
        return CitizenshipType(s)
    except ValueError:
        pass

    normalized = re.sub(r"[_\-]+", " ", s)
    normalized = " ".join(normalized.split())

    # Check for negative or explicit other/foreign markers first to avoid false-positive submatches
    if any(k in normalized for k in ("NON", "NOT", "OTHER", "FOREIGN")):
        return CitizenshipType.OTHERS

    if normalized in ("SC", "SINGAPORE CITIZEN", "SINGAPORE CITIZENS", "SINGAPOREAN", "CITIZEN", "CITIZENS") or re.search(r"\bSC\b", normalized):
        return CitizenshipType.SC

    if normalized in ("SPR", "PR", "SINGAPORE PERMANENT RESIDENT", "SINGAPORE PERMANENT RESIDENTS", "PERMANENT RESIDENT", "PERMANENT RESIDENTS", "PERMANENTRESIDENT") or re.search(r"\b(SPR|PR)\b", normalized):
        return CitizenshipType.SPR

    return CitizenshipType.OTHERS


def parse_centre_service(
    rec: dict[str, Any],
    centre_map: dict[str, int],
) -> PreschoolService | None:
    """Parse raw listing of centre services record into a PreschoolService instance."""
    code = resolve_centre_identifier(rec)
    if not code:
        return None

    centre_id = centre_map.get(code)
    if not centre_id:
        return None

    service_type = clean_str(rec.get("type_of_service"))
    if not service_type:
        return None

    level = normalize_preschool_level(rec.get("levels_offered"))
    if not level:
        return None

    citizenship = normalize_citizenship_type(rec.get("type_of_citizenship"))

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
    batch_size: int = 5000,
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


def update_data_source_status(
    db_engine: Any,
    source_id: str,
    source_name: str,
    sync_state: SyncState,
    last_attempted_sync: datetime | None = None,
    last_successful_sync: datetime | None = None,
    records_processed: int | None = None,
    error_message: str | None = None,
    clear_error_message: bool = False,
) -> ExternalDataSourceStatus:
    """Helper to update or create an ExternalDataSourceStatus heartbeat record."""
    with Session(db_engine) as session:
        status = session.get(ExternalDataSourceStatus, source_id)
        if not status:
            status = ExternalDataSourceStatus(
                source_id=source_id,
                source_name=source_name,
                sync_state=sync_state,
                last_attempted_sync=last_attempted_sync,
                last_successful_sync=last_successful_sync,
                records_processed=records_processed if records_processed is not None else 0,
                error_message=error_message,
            )
        else:
            status.sync_state = sync_state
            if last_attempted_sync is not None:
                status.last_attempted_sync = last_attempted_sync
            if last_successful_sync is not None:
                status.last_successful_sync = last_successful_sync
            if records_processed is not None:
                status.records_processed = records_processed
            if clear_error_message:
                status.error_message = None
            elif error_message is not None:
                status.error_message = error_message

        session.add(status)
        session.commit()
        session.refresh(status)
        return status


def sync_centres(
    max_records: int | None = None,
    dry_run: bool = False,
    db_engine: Any = None,
    sync_date: datetime | None = None,
) -> dict[str, int]:
    """Sync data.gov.sg preschool centres, vacancies, and services into database."""
    centres_resource_id = settings.DATA_GOV_SG_CENTRES_RESOURCE_ID
    services_resource_id = settings.DATA_GOV_SG_SERVICES_RESOURCE_ID
    logger.info("Starting sync with centres resource ID: %s", centres_resource_id)

    target_engine = db_engine or engine
    # Ensure tables exist in database
    init_db(target_engine)

    execution_time = datetime.now(timezone.utc)
    effective_sync_date = sync_date or execution_time

    # 1. Operational Heartbeat: Transition state to RUNNING with attempt timestamp (if not dry run)
    if not dry_run:
        update_data_source_status(
            db_engine=target_engine,
            source_id=ECDA_DATASTORE_SOURCE_ID,
            source_name=ECDA_DATASTORE_SOURCE_NAME,
            sync_state=SyncState.RUNNING,
            last_attempted_sync=execution_time,
        )

    try:
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
            "vacancies_inserted": 0,
            "vacancies_updated": 0,
            "vacancies_skipped": 0,
            "vacancies_synced": 0,
            "services_fetched": 0,
            "services_inserted": 0,
            "services_updated": 0,
            "services_skipped": 0,
        }

        # Fetch services before entering the database transaction to avoid holding open connections
        service_records = fetch_all_services(
            resource_id=services_resource_id,
            batch_size=5000,
            max_records=max_records,
        )
        stats["services_fetched"] = len(service_records)
        logger.info("Total service records fetched from API: %d", len(service_records))

        with Session(target_engine) as session:
            # Load existing centres indexed by centre_code
            existing_centres: dict[str, PreschoolCentre] = {}
            for db_centre in session.exec(select(PreschoolCentre)).all():
                if db_centre.centre_code:
                    existing_centres[db_centre.centre_code] = db_centre
            logger.info("Loaded %d existing centres from database.", len(existing_centres))

            # Load existing vacancies indexed by record_id to avoid N+1 query loops
            existing_vacancies: dict[str, VacancyRecord] = {}
            for db_vacancy in session.exec(select(VacancyRecord)).all():
                if db_vacancy.record_id:
                    existing_vacancies[db_vacancy.record_id] = db_vacancy
            logger.info("Loaded %d existing vacancies from database.", len(existing_vacancies))

            # In-memory map from temporary permit code to centre_id for services resolution
            tp_alias_map: dict[str, int] = {}

            # 1. Upsert Centres and Vacancies
            for rec in centre_records:
                centre_code = clean_str(rec.get("centre_code"))
                tp_code = clean_str(rec.get("tp_code"))
                identifier = centre_code or tp_code
                name = clean_str(rec.get("centre_name"))

                if not identifier or not name:
                    stats["centres_skipped"] += 1
                    stats["vacancies_skipped"] += 42
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

                centre: PreschoolCentre | None = None
                old_code: str | None = None
                if centre_code and centre_code in existing_centres:
                    centre = existing_centres[centre_code]
                elif tp_code and tp_code in existing_centres:
                    centre = existing_centres[tp_code]
                    old_code = centre.centre_code

                if centre is not None:
                    # Handle code transition: previously keyed under tp_code, now received permanent centre_code
                    if centre_code and old_code and centre_code != old_code:
                        logger.info("Migrating centre code from %s to %s for centre_id=%s", old_code, centre_code, centre.centre_id)
                        centre.centre_code = centre_code
                        # Migrate existing vacancies to avoid duplicates while preserving history
                        old_vacancies = session.exec(
                            select(VacancyRecord).where(VacancyRecord.centre_id == centre.centre_id)
                        ).all()
                        for ev in old_vacancies:
                            if ev.record_id and ev.record_id.startswith(f"{old_code}_"):
                                suffix = ev.record_id[len(old_code) + 1 :]
                                new_record_id = f"{centre_code}_{suffix}"
                                existing_vacancies.pop(ev.record_id, None)
                                session.delete(ev)
                                session.flush()
                                new_v = VacancyRecord(
                                    record_id=new_record_id,
                                    centre_id=ev.centre_id,
                                    level_type=ev.level_type,
                                    enrollment_month_year=ev.enrollment_month_year,
                                    vacancy_status=ev.vacancy_status,
                                )
                                session.add(new_v)
                                existing_vacancies[new_record_id] = new_v
                        session.flush()
                        existing_centres.pop(old_code, None)
                        existing_centres[centre_code] = centre

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
                    existing_centres[centre.centre_code] = centre
                    stats["centres_updated"] += 1
                else:
                    centre = PreschoolCentre(
                        centre_code=identifier,
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
                    existing_centres[centre.centre_code] = centre
                    stats["centres_inserted"] += 1

                if tp_code and centre.centre_id:
                    tp_alias_map[tp_code] = centre.centre_id

                # Sync Vacancies for this centre
                vacancies = parse_vacancies_for_centre(
                    rec,
                    centre.centre_id,  # type: ignore[arg-type]
                    sync_date=effective_sync_date,
                    centre_code=centre.centre_code,
                )
                for v in vacancies:
                    if not v.record_id:
                        continue
                    existing_v = existing_vacancies.get(v.record_id)
                    if existing_v:
                        existing_v.vacancy_status = v.vacancy_status
                        existing_v.enrollment_month_year = v.enrollment_month_year
                        session.add(existing_v)
                        stats["vacancies_updated"] += 1
                    else:
                        session.add(v)
                        existing_vacancies[v.record_id] = v
                        stats["vacancies_inserted"] += 1
                    stats["vacancies_synced"] += 1

            session.flush()

            # Build centre code / tp_code map for services lookup
            centre_map: dict[str, int] = {}
            for centre in existing_centres.values():
                if centre.centre_id and centre.centre_code:
                    centre_map[centre.centre_code] = centre.centre_id
            for tp, cid in tp_alias_map.items():
                centre_map[tp] = cid

            # 2. Sync Services
            # Index existing services by (centre_id, type_of_service, level, citizenship_type)
            existing_services_list = session.exec(select(PreschoolService)).all()
            existing_services: dict[tuple[int, str, PreschoolLevel, CitizenshipType], PreschoolService] = {
                (s.centre_id, s.type_of_service, s.level, s.citizenship_type): s
                for s in existing_services_list
            }

            for i, s_rec in enumerate(service_records):
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

                # 2. Operational Heartbeat: Transition state to SUCCESS with processed counts
                update_data_source_status(
                    db_engine=target_engine,
                    source_id=ECDA_DATASTORE_SOURCE_ID,
                    source_name=ECDA_DATASTORE_SOURCE_NAME,
                    sync_state=SyncState.SUCCESS,
                    last_successful_sync=datetime.now(timezone.utc),
                    records_processed=len(centre_records) + len(service_records),
                    clear_error_message=True,
                )

        logger.info("Sync completed: %s", stats)
        return stats

    except Exception:
        logger.exception("Sync execution failed for %s", ECDA_DATASTORE_SOURCE_ID)
        if not dry_run:
            failure_trace = traceback.format_exc()
            update_data_source_status(
                db_engine=target_engine,
                source_id=ECDA_DATASTORE_SOURCE_ID,
                source_name=ECDA_DATASTORE_SOURCE_NAME,
                sync_state=SyncState.FAILED,
                error_message=failure_trace,
            )
        raise


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
    except Exception:
        logger.exception("Sync job failed")
        sys.exit(1)


if __name__ == "__main__":
    main()

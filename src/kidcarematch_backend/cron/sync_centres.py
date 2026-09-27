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
from kidcarematch_backend.db.db import PreschoolCentre, engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sync_centres")

DATA_GOV_SG_API_URL = "https://data.gov.sg/api/action/datastore_search"

REQUESTED_FIELDS = [
    "centre_code",
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


def fetch_all_centres(
    resource_id: str,
    batch_size: int = 1000,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch all centre records from data.gov.sg using pagination."""
    all_records: list[dict[str, Any]] = []
    offset = 0
    fields_query = ",".join(REQUESTED_FIELDS)

    with httpx.Client(timeout=30.0) as client:
        while True:
            limit = batch_size
            if max_records and (len(all_records) + limit) > max_records:
                limit = max_records - len(all_records)

            if limit <= 0:
                break

            params = {
                "resource_id": resource_id,
                "limit": limit,
                "offset": offset,
                "fields": fields_query,
            }

            logger.info("Fetching records from data.gov.sg (offset=%d, limit=%d)...", offset, limit)

            retries = 3
            data = None
            for attempt in range(1, retries + 1):
                try:
                    response = client.get(DATA_GOV_SG_API_URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                    break
                except (httpx.HTTPError, httpx.TimeoutException) as exc:
                    logger.warning("API request failed (attempt %d/%d): %s", attempt, retries, exc)
                    if attempt == retries:
                        raise
                    time.sleep(attempt * 2)

            result = data.get("result", {})
            records = result.get("records", [])

            if not records:
                logger.info("No more records returned from API.")
                break

            all_records.extend(records)
            logger.info("Fetched %d records so far.", len(all_records))

            total_available = result.get("total")
            offset += len(records)

            if total_available and offset >= total_available:
                logger.info("Reached total available records (%d).", total_available)
                break

            if max_records and len(all_records) >= max_records:
                break

    return all_records


def sync_centres(
    max_records: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Sync data.gov.sg preschool centres into the PreschoolCentre database table."""
    resource_id = settings.DATA_GOV_SG_CENTRES_RESOURCE_ID
    logger.info("Starting preschool centres sync with resource ID: %s", resource_id)

    # Ensure tables exist in database
    SQLModel.metadata.create_all(engine)

    records = fetch_all_centres(
        resource_id=resource_id,
        batch_size=1000,
        max_records=max_records,
    )
    logger.info("Total records fetched from API: %d", len(records))

    stats = {"total_fetched": len(records), "inserted": 0, "updated": 0, "skipped": 0}

    with Session(engine) as session:
        # Load existing centres by centre_code to support idempotent upserts
        existing_centres = {
            c.centre_code: c for c in session.exec(select(PreschoolCentre)).all()
        }
        logger.info("Found %d existing centres in the database.", len(existing_centres))

        for rec in records:
            code = clean_str(rec.get("centre_code"))
            name = clean_str(rec.get("centre_name"))

            if not code or not name:
                stats["skipped"] += 1
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

            if code in existing_centres:
                # Update existing row (keeps existing centre_id and relationship integrity)
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
                stats["updated"] += 1
            else:
                # Insert new row
                new_centre = PreschoolCentre(
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
                session.add(new_centre)
                existing_centres[code] = new_centre
                stats["inserted"] += 1

        if dry_run:
            logger.info("Dry-run enabled: rolling back transaction without saving changes.")
            session.rollback()
        else:
            session.commit()
            logger.info("Transaction committed successfully.")

    logger.info(
        "Sync completed: %d fetched, %d inserted, %d updated, %d skipped.",
        stats["total_fetched"],
        stats["inserted"],
        stats["updated"],
        stats["skipped"],
    )
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

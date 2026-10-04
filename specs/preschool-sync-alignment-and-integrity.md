# Spec: Preschool Centre Vacancy Horizon Alignment and Service Relational Integrity

## Problem Statement

When parents and caregivers search for childcare vacancies and service offerings on KidCareMatch, vacancy comparisons and monthly enrollment planning require accurate, timeline-aligned data across all centres. Currently, vacancy records are derived using each individual preschool centre's profile update date (`last_updated`). When centres have stale update timestamps, their 7 relative enrollment months anchor to different points in the past, causing cross-centre timeline misalignment in queries and failing to provide consistent future availability.

Additionally, preschool services lack a database-level uniqueness constraint, leaving service offerings vulnerable to duplicate records upon repeated synchronizations. Furthermore, newly registered centres that operate under temporary identifiers (`tp_code`) cannot resolve their service offerings, resulting in missing childcare service options for parents.

## Solution

1. Align all 7 Relative Horizon Months (`current_month` through `seventh_month`) to a unified, deterministic calendar sync month across all Preschool Centres, ensuring that monthly enrollment availability records are synchronized on the identical calendar timeline while preserving historical vacancy records across sync cycles.
2. Enforce relational integrity and prevent duplicate service offerings by introducing a database-level composite uniqueness constraint on Preschool Services (`centre_id`, `type_of_service`, `level`, `citizenship_type`) while retaining an auto-increment surrogate primary key for clean API routing and child table relationships.
3. Enable fallback identifier mapping (`tp_code`) during preschool service ingestion so that centres with temporary identifiers retain their associated service offerings and fee structures.

## User Stories

1. As a parent planning infant enrollment, I want to view infant care vacancies across multiple Preschool Centres for a specific future month, so that I can compare availability on a synchronized, uniform timeline.
2. As a parent exploring childcare options, I want vacancy records to remain comparable across centres regardless of when individual centres last updated their profiles, so that I am not misled by misaligned calendar windows.
3. As a parent looking at past availability trends, I want previous months' Vacancy Records to persist in the database as new sync cycles roll forward, so that I can see historical vacancy patterns.
4. As a parent comparing program costs, I want to see exactly one accurate service fee entry per service type, age level, and citizenship tier at a Preschool Centre, so that I am never confused by duplicated pricing records.
5. As an applicant viewing a newly established Preschool Centre operating under a temporary permit code (`tp_code`), I want to see its full list of Preschool Services and fees, so that I can evaluate its offerings before official registration codes are finalized.
6. As a frontend engineer building the centre search page, I want to query Vacancy Records within a calendar date range (e.g. the next 7 months) using standard date filters, so that I do not need complex per-centre date offset calculations on the client.
7. As a backend engineer running the data synchronization cron job, I want sync executions to update existing Vacancy Records idempotently via deterministic natural keys, so that repeated runs do not create duplicate or conflicting records.
8. As a backend engineer running concurrent or repeated service synchronization, I want the database schema to strictly reject duplicate Preschool Service offerings, so that relational data integrity is guaranteed at the storage engine level.
9. As an API client fetching specific service details, I want each Preschool Service to have a concise numeric identifier, so that individual services can be addressed and linked cleanly via standard REST endpoints.
10. As a system administrator monitoring data quality, I want sync metrics to accurately report counts of inserted, updated, and skipped centres, vacancies, and services, so that data ingestion health can be audited.

## Implementation Decisions

- **Sync Calendar Month Anchoring**: The 7 Relative Horizon Months (`current_month` through `seventh_month`) for every Preschool Centre are anchored to the calendar month of the synchronization execution date (1st of the sync month in UTC). The natural key format `{centre_code}_{level}_{YYYYMM}` is retained, guaranteeing deterministic primary keys across all centres in the same sync period.
- **Surrogate Key with Composite Uniqueness for Preschool Services**: Preschool Services retain a single-column auto-increment surrogate primary key (`service_id`) for REST ergonomics and foreign key efficiency. A composite database-level uniqueness constraint is enforced across `(centre_id, type_of_service, level, citizenship_type)`.
- **Dual Identifier Resolution for Services**: The synchronization mapping layer indexes Preschool Centres by both primary `centre_code` and temporary registration code (`tp_code`), and service parsing resolves centres using either code.
- **Deduplication and Idempotent Ingestion**: Vacancy Records are upserted against their natural key (`record_id`). Preschool Services are matched against existing records by `(centre_id, type_of_service, level, citizenship_type)` to update fees and remarks in place rather than inserting duplicates.
- **Windows Test Environment Reliability**: Test runner configuration disables cache provider directory creation issues on Windows environments running Python 3.10.

## Testing Decisions

- **What makes a good test**: Tests must verify externally observable behaviors—verifying that all 42 Vacancy Records for a centre span the exact 7 consecutive calendar months from the reference sync period, that duplicate service insertions violate database constraints, and that services associated with temporary-coded centres resolve successfully. Tests should not assert internal loop variables or private helper state.
- **Modules tested**:
  - The synchronization pipeline orchestration module (`sync_centres`) and its vacancy and service extraction parsers.
  - The persistence schema layer verifying model table creation and composite uniqueness constraints.
- **Seams**:
  - Primary Seam (End-to-End Orchestration): `sync_centres(max_records, dry_run, db_engine)` executing against an in-memory SQLite database engine with mocked HTTP API inputs.
  - Unit Seams: `parse_vacancies_for_centre(rec, centre_id, sync_date)` and `parse_centre_service(rec, centre_map)`.
- **Prior Art**: Existing test cases in `tests/test_sync_centres.py` using SQLite in-memory test engines and mock payload injection.

## Out of Scope

- Client-side vacancy filtering or search UI implementation.
- Automated notification pipelines for when vacancies open up.
- Modifying the external ECDA / data.gov.sg API contracts.
- Historical vacancy backfilling prior to system deployment.

## Further Notes

- `GLOSSARY.md` terms (`Preschool Centre`, `Vacancy Record`, `Preschool Service`, `Relative Horizon Month`) must be consistently adhered to in all documentation, tests, and future schema expansions.
- When `gh` CLI credentials are authenticated, this specification can be published directly to GitHub issues as a `ready-for-agent` issue.

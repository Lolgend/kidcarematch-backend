# User Identity, Subsidy Rules, and Matching Entities Architecture

## Context

KidCareMatch requires user authentication, personalized childcare matching for parents, subsidy calculation against Singapore ECDA policy structures, community review moderation, and external dataset sync observability.

## Decisions

1. **Single-Table User Identity**: Rather than separate polymorphic tables or joined inheritance, `User` and `Administrator` are unified in a single table with an indexed `role` discriminator (`PARENT`, `ADMIN`), account status (`ACTIVE`, `SUSPENDED`), and an optional administrative tier (`admin_level`). Email addresses are enforced unique at the database level.
2. **Comprehensive Subsidy Rule Structure**: `SubsidyRule` models ECDA early childhood subsidy structures across care types (`INFANT_CARE`, `CHILD_CARE`), means-test metrics (`FIXED`, `HOUSEHOLD_INCOME`, `PER_CAPITA_INCOME`), applicant criteria (`WORKING_APPLICANT`, `NON_WORKING_APPLICANT`), and citizenship tiers with configurable income bounds and effective date timestamps.
3. **Singleton Heartbeat Record for Sync Status**: `ExternalDataSourceStatus` maintains a dedicated record per external data provider (`ECDA_DATASTORE`, `ECDA_SERVICES`), capturing real-time sync state (`IDLE`, `RUNNING`, `SUCCESS`, `FAILED`), execution timestamps, records processed, and failure error traces.
4. **Surrogate Key Relational and Cascade Integrity**: Matching entities (`Review`, `WatchlistEntry`) and sync entities reference `PreschoolCentre.centre_id` (`int`) as foreign keys with database-level `ondelete="CASCADE"` and ORM `cascade="all, delete-orphan"` to guarantee relational consistency and automated cascade deletion upon centre removal.
5. **Strict Domain Typing and Timestamps**: Discrete domain attributes (`UserRole`, `AccountStatus`, `ModerationStatus`, `SyncState`, `CareType`, `SubsidyMetric`, `ApplicantStatus`, `LocationPreference`) are modeled as `str, Enum`. `Review` enforces mandatory submission timestamps defaulting to UTC, and `FamilyPreferenceProfile` harmonizes cohort level and enrollment period naming (`level`, `enrollment_month_year`).

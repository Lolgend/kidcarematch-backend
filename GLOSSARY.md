# KidCareMatch Backend

Domain vocabulary for preschool childcare centres, services, and vacancy matching in Singapore.

## Language

**Preschool Centre**:
A licensed childcare centre or kindergarten in Singapore registered with ECDA, identified primarily by its `centre_code` (or temporary `tp_code`).
_Avoid_: Daycare, school, facility

**Vacancy Record**:
A record of admission vacancy availability for a specific age level at a preschool centre for a particular enrollment month.
_Avoid_: Opening, slot, availability record

**Preschool Service**:
A specific program offering at a preschool centre, characterized by service type, level, citizenship tier, and monthly fee.
_Avoid_: Course, class, pricing plan

**Relative Horizon Month**:
The 7-month forward-looking enrollment period (`current_month` through `seventh_month`) published by ECDA.
_Avoid_: Forecast window, future month

**User**:
An authenticated account entity representing either a parent seeking childcare services or a system administrator, distinguished by role and account status.
_Avoid_: Account holder, client, member

**Firebase UID**:
The immutable unique subject identifier (`sub`) assigned to a user by Firebase Authentication upon account registration.
_Avoid_: Auth ID, Firebase ID, external user ID

**Federated Authentication**:
An identity architecture where user credential verification and token issuance are delegated to an external identity provider (Firebase Auth), eliminating local password storage.
_Avoid_: Third-party login, social auth, SSO

**User Profile Lifecycle**:
The state and progression of a user's account in KidCareMatch, spanning initial Firebase credential registration, application onboarding profile creation, and active or suspended account status.
_Avoid_: User onboarding flow, account lifecycle

**Account Deactivation**:
The canonical state and operational semantics (`AccountStatus.SUSPENDED`) in which a user account is administratively suspended. Suspended accounts retain historical relational integrity (associated reviews, preference profiles, and watchlist entries remain intact) while strictly barring session authentication and protected administrative or user API interactions.
_Avoid_: User soft-deletion, account termination, ban, blacklisting

**Administrator**:
A privileged user role authorized to manage early childhood subsidy rules, review moderation workflows, and monitor external data sync pipelines.
_Avoid_: Superuser, mod, staff

**Subsidy Rule**:
A government funding policy configuration defining means-tested or universal subsidy amounts for Singapore early childhood care, based on care type, applicant status, citizenship, and income thresholds.
_Avoid_: Discount rule, grant scheme, rebate config

**External Data Source Status**:
A persistent operational heartbeat record tracking the real-time synchronization state, execution timestamps, record counts, and failure diagnostics for an external data provider.
_Avoid_: Sync log, healthcheck record, source tracker

**OneMap API**:
The authoritative geospatial mapping, geocoding, and reverse-geocoding web services provided by the Singapore Land Authority (SLA), utilized for preschool centre coordinate resolution, postal code lookup, and parental travel proximity matching.
_Avoid_: Google Maps Platform, SLA GIS, SLA Map service

**Family Preference Profile**:
A parent-configured matching criteria profile representing a specific child or search scenario, capturing target service type, location postal anchors, household income, budget ceiling, and cost/travel trade-off weights. An authenticated parent may maintain a portfolio of multiple distinct preference profiles, each bearing a unique profile name within the parent's account.
_Avoid_: Search filter, match config, family setting, preference document

**Review**:
A parent-submitted evaluation of a preschool centre comprising a 1-to-5 star rating and qualitative commentary, governed by a tri-state moderation workflow (`PENDING`, `APPROVED`, `REJECTED`) and strictly unique per parent per centre.
_Avoid_: Feedback, rating entry, centre comment

**Reactive Moderation**:
A content moderation model where parent-submitted preschool centre reviews default immediately to `APPROVED` status and become publicly visible upon submission, subject to post-publication auditing, community flagging, or administrative override rather than blocking on pre-publication approval queues.
_Avoid_: Pre-moderation, automatic filtering, unmoderated content

**Watchlist Entry**:
A cohort-specific alert subscription tracking vacancy availability for a specific age level and enrollment month at a preschool centre.
_Avoid_: Favourite, bookmark, notification subscription

**Preschool Level**:
The developmental age cohort for childcare enrollment, canonically represented by short codes (`infant`, `pg`, `n1`, `n2`, `k1`, `k2`, `student_care`).
_Avoid_: Age group, grade, tier, class level

**Citizenship Type**:
The Singapore residency classification tier determining subsidy eligibility and fee scales, canonically categorized as Singapore Citizen (`SC`), Singapore Permanent Resident (`SPR`), or Foreigner/Non-Resident (`OTHERS`).
_Avoid_: Nationality status, resident type, citizen tier

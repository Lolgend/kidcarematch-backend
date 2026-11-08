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

**OneMap Service Account**:
A centralized system-level developer credential registered with SLA OneMap, utilized by the backend to programmatically authenticate and obtain short-lived access tokens for geospatial queries.
_Avoid_: User account, developer profile, OneMap login, OneMap user

**OneMap Access Token**:
A time-limited JSON Web Token (JWT) issued by SLA OneMap to authorize backend API requests for geocoding and public transit routing.
_Avoid_: OneMap API key, OneMap bearer, OneMap session

**Family Preference Profile**:
A parent-configured matching criteria profile representing a specific child or search scenario, capturing target service type, location postal anchors, household income, budget ceiling, and Importance Ratings across matching factors. An authenticated parent may maintain a portfolio of multiple distinct preference profiles, each bearing a unique profile name within the parent's account.
_Avoid_: Search filter, match config, family setting

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


**Postal District**:
One of Singapore's 28 official geographical postal districts (D01 through D28), each grouping one or more two-digit postal code sectors (the first two digits of a six-digit Singapore postal code) and associated with a canonical set of locality names.
_Avoid_: Region, zone, planning area, neighbourhood

**Vacancy Fit**:
The normalized percentage score (0% to 100%) indicating how well a preschool centre's reported ECDA vacancy status matches a child's required developmental level and target enrollment month.
_Avoid_: Availability score, opening rating

**Cost Fit**:
The normalized percentage score (0% to 100%) comparing an estimated monthly payable fee against a parent's preferred budget ceiling.
_Avoid_: Price score, affordability index, budget rating

**Travel Fit**:
The normalized percentage score (0% to 100%) evaluating a parent's total morning commute time (Home to Centre, and optionally Centre to Workplace) against preferred and maximum duration thresholds.
_Avoid_: Commute score, distance score, transit rating

**Importance Rating**:
A parent-assigned integer score from 1 (least important) to 5 (most important) expressing the priority of Vacancy Fit, Cost Fit, or Travel Fit within a Family Preference Profile.
_Avoid_: Priority rank, slider value, percentage weight

**Effective Matching Weight**:
The normalized proportion derived from a factor's Importance Rating relative to the sum of all active Importance Ratings, used to weight Vacancy Fit, Cost Fit, and Travel Fit when calculating Overall Fit.
_Avoid_: Raw rating, unnormalized weight

**Overall Fit**:
The composite weighted matching score (0% to 100%) calculated as the linear combination of Vacancy Fit, Cost Fit, and Travel Fit weighted by their Effective Matching Weights.
_Avoid_: Total match, recommendation score, rank score

**Commute Deviation**:
The signed difference in morning travel time incurred by travelling via a Preschool Centre relative to the direct baseline route from Home to Workplace, supported by travel distance context.
_Avoid_: Detour delay, detour distance, route overhead

**Estimated Monthly Payable Fee**:
The net estimated monthly childcare expense after deducting all applicable Singapore government preschool subsidies (Basic and Additional Subsidies) from the centre's published programme fee.
_Avoid_: Out-of-pocket price, net school fee, subsidised rate

**Fit Explanation**:
The AI-generated explanatory narrative, top-recommendation rationale, and comparative ranking rationale explaining the quantitative scoring breakdown and why candidate preschool centres are ordered in their specific sequence within the recommended shortlist (standardized to a default of Top 6).
_Avoid_: AI justification, match summary, reasoning notes

**Commute Route**:
A personalised journey used in preschool centre details to compare travel from a family's home to a Preschool Centre, continuing to the workplace when configured on the Family Preference Profile, with route geometry, estimated travel time, and distance.
_Avoid_: Navigation, trip plan

**Journey Leg**:
A distinct portion of a travel route between two consecutive anchors (`home` to `centre`, `centre` to `workplace`, or direct `home` to `workplace`).
_Avoid_: Segment, hop, stage

**Travel Time Estimate**:
The estimated duration of travelling along a Commute Route or Journey Leg, excluding any time spent dropping a child off at the Preschool Centre.
_Avoid_: Trip duration, total elapsed time

**Representative Weekday/Weekend Estimate**:
A Travel Time Estimate evaluated for legacy sample conditions at 08:00 Singapore time on the next upcoming Wednesday (weekday) or Saturday (weekend) to compare Preschool Centres consistently across a browsing session, rather than an average across all days or departure times.
_Avoid_: Average commute time, traffic forecast

**Selected Weekday Sampling Policy**:
A public transport evaluation policy (`weekday_0800_wed_thu_fri_v1`) evaluating a single representative weekday selected from a bounded cycle of upcoming Wednesday, Thursday, or Friday at 08:00 SGT, expiring strictly at the primary Wednesday 08:00 SGT.
_Avoid_: Weekend sampling, rolling weekday average, dynamic departure query

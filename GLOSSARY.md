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

**Administrator**:
A privileged user role authorized to manage early childhood subsidy rules, review moderation workflows, and monitor external data sync pipelines.
_Avoid_: Superuser, mod, staff

**Subsidy Rule**:
A government funding policy configuration defining means-tested or universal subsidy amounts for Singapore early childhood care, based on care type, applicant status, citizenship, and income thresholds.
_Avoid_: Discount rule, grant scheme, rebate config

**External Data Source Status**:
A persistent operational heartbeat record tracking the real-time synchronization state, execution timestamps, record counts, and failure diagnostics for an external data provider.
_Avoid_: Sync log, healthcheck record, source tracker


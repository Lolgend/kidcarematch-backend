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

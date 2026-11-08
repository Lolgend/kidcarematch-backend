# Multi-Criteria Childcare Centre Matching and Gemini AI Explanation Architecture

## Context

KidCareMatch provides parents in Singapore with personalized preschool recommendations by balancing admission availability, financial affordability, and daily commute logistics. Previously, parents could only view unsorted lists or filter centres by postal district. To deliver intelligent recommendations, the platform requires an algorithmic multi-criteria scoring engine combining official ECDA vacancy statuses, government early childhood subsidies, and OneMap routing detour deviations, complemented by Gemini generative AI to provide transparent explanations for the rankings.

## Decisions

1. **Three-Factor Weighted Linear Matching Model**:
   Parents rate Vacancy Fit, Cost Fit, and Travel Fit independently using integer Importance Ratings $r_v, r_c, r_t \in \{1, 2, 3, 4, 5\}$ (defaulting to $r_v = 3, r_c = 3, r_t = 3$). Effective Matching Weights are derived once by normalizing each rating against the sum of active ratings ($w_i = r_i / (r_v + r_c + r_t)$, where $w_v, w_c, w_t \in (0.0, 1.0)$ and sum to $1.0$). Matching scores are computed as a normalized linear combination:
   $$\text{Overall Fit} = (\text{Vacancy Fit} \times w_v) + (\text{Cost Fit} \times w_c) + (\text{Travel Fit} \times w_t)$$

2. **ECDA Cohort Level Filtering and Vacancy Fit Scoring**:
   Centres that do not offer the child's requested `PreschoolLevel` cohort are strictly excluded from the candidate pool. For candidate centres offering the level, reported ECDA vacancy status for the target enrollment month maps quantitatively:
   - `"Available"` (and legacy `"Immediate"`): $100\%$
   - `"Limited"`: $50\%$
   - `"Full"`, `"Not Applicable"`, or unrecorded: $0\%$
   Centres with $0\%$ Vacancy Fit remain visible in the ranking as lower-priority alternatives, allowing parents to monitor changing vacancy situations.

3. **Government Subsidy Engine and Cost Fit Calculation**:
   - Net monthly fee is estimated as:
     $$\text{Estimated Monthly Payable Fee} = \text{Published Programme Fee} - \text{Estimated Government Subsidy}$$
   - Published programme fee matches candidate centre services by requested level, citizenship tier (falling back to `OTHERS`), and service type (falling back to `"Full Day"`).
   - Subsidies are evaluated against `SubsidyRule` records by care type (`INFANT_CARE` vs `CHILD_CARE`), citizenship (`SC`, `SPR`, `OTHERS`), and applicant status (`WORKING_APPLICANT` vs `NON_WORKING_APPLICANT`, defaulting to `WORKING_APPLICANT`).
   - The engine evaluates both Gross Monthly Household Income (HHI) and Per-Capita Income (PCI) and applies the maximum eligible subsidy benefit.
   - If Estimated Monthly Payable Fee $\le$ Preferred Budget (or $\le 0$), $\text{Cost Fit} = 100\%$; otherwise $\text{Cost Fit} = \min\left(100, \frac{\text{Preferred Budget}}{\text{Estimated Monthly Payable Fee}} \times 100\right)$.

4. **Two-Stage Spatial Corridor Routing & Deviation Scoring**:
   - To prevent rate limiting and high latency across ~1,500 centres (which would require 3,000+ OneMap HTTP calls per search), candidate evaluation uses a two-stage approach:
     - **Stage 1 (Corridor Pre-Filter)**: Coordinates for Home, Workplace, and candidate centres are resolved via OneMap with persistent coordinate caching. Candidates are pruned by straight-line distance corridor relative to the direct baseline route.
     - **Stage 2 (Detour Routing)**: OneMap Routing API (`pt` public transit, falling back to straight-line travel time estimates during external API degradation) evaluates the top ~30 spatial candidates.
   - For **Dual-Anchor** (Home + Workplace), Travel Fit evaluates commute delay deviation:
     $$\Delta Time = Time(Home \to Centre) + Time(Centre \to Workplace) - Time(Home \to Workplace)$$
   - For **Single-Anchor** (Home only), Travel Fit evaluates direct physical distance:
     $$Distance = Distance(Home \to Centre)$$
   - Travel Fit is normalized relative to the evaluated candidate pool: the best candidate receives $100\%$, the worst receives $0\%$, and intermediate candidates scale linearly.

5. **Family Preference Profile Schema Evolution**:
   `FamilyPreferenceProfile` and associated API request/response schemas persist raw integer Importance Ratings in `vacancy_weight`, `cost_weight`, and `travel_weight` ($1$ through $5$ inclusive, defaulting to $3, 3, 3$) alongside `applicant_status` (defaulting to `WORKING_APPLICANT`). Both profile-linked and inline `POST /api/v1/centres/match` requests accept `1`–`5` integer ratings and derive normalized Effective Matching Weights once for scoring and AI explanation generation.

6. **Gemini AI Explanation with Resilient Fallback**:
   Top recommendation explanations are powered by `gemini-2.5-flash` using `google-genai` and `GEMINI_API_KEY`. Gemini generates a synthesized rationale explaining why Centre #1 achieved the top rank, plus a concise one-line highlight for each top candidate. If the Gemini API call times out or encounters network/quota errors, the service falls back to a deterministic structured template, guaranteeing zero matching API failures.

7. **Matching REST API Endpoint**:
   `POST /api/v1/centres/match` supports both profile-linked matching (`profile_id`) and ad-hoc simulation payloads, returning the top $N$ candidates (default 5, maximum 20) with scoring metrics, breakdown components, and the AI explanation.

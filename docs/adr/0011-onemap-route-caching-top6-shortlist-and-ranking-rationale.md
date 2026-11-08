# OneMap Route TTL Caching, Top-6 Shortlist, and Comparative Ranking Rationale

## Context

When a parent runs preschool matching for a Family Preference Profile, the platform previously generated an excessive burst of external OneMap API calls across both the multi-criteria matching engine and the subsequent per-card commute evaluations:

1. **Uncached Stage 2 Detour Routing in Multi-Criteria Matching**: Matching evaluated public transit routing across the top 30 straight-line spatial candidates (`STAGE1_MAX_CANDIDATES = 30`), triggering up to 61 sequential OneMap API routing requests (1 direct baseline + $30 \times 2$ candidate legs in dual-anchor mode) without HTTP connection reuse or route caching. When parents adjusted Importance Ratings, the backend re-executed all 61 external OneMap API routing requests from scratch despite identical postal anchors.
2. **Redundant Commute & Postal Lookups Across Shortlisted Results**: Immediately after receiving the matched shortlist, the client fired parallel Commute Route requests for every displayed card (previously 10 centres). Each request independently queried OneMap for `home → centre`, `centre → workplace`, and redundantly re-queried the identical `home → workplace` direct baseline leg across all cards. When the in-memory verified postal cache was cold, concurrent requests also issued duplicate OneMap API Search calls for identical postal anchors.
3. **Missing Comparative Ranking Explanation**: While Fit Explanation provided a high-level summary narrative and top-centre rationale, parents lacked a comparative explanation of *why* the shortlisted centres are ordered in their specific relative sequence (#1 through #N), and the UI banner did not explicitly label the explanation as AI-generated.

## Decisions

1. **Spatial Corridor Stage 1 Pool Reduction (`STAGE1_MAX_CANDIDATES = 15`) and Dynamic Scaling**:
   - Reduced `STAGE1_MAX_CANDIDATES` from `30` to `15` in `core/onemap.py`. This cuts cold-cache matching routing calls roughly in half (from at most 61 down to at most 31 OneMap API calls).
   - Dynamically scale Stage 1 candidate pruning using `effective_stage1_cap = max(STAGE1_MAX_CANDIDATES, criteria.top_n)` (`max(15, top_n)`). When a caller explicitly requests `top_n > 15` (up to the maximum `20`), every candidate within the requested shortlist receives Stage 2 public transit routing rather than falling back to straight-line distance approximations.

2. **Standardized Default Top-6 Shortlist (`top_n = 6`)**:
   - Standardized the default match shortlist size `top_n = 6` across `CentreMatchRequest` and `MatchCriteria` (retaining the `1` to `20` allowed range for API flexibility).
   - Six recommendations provide parents with a balanced, highly relevant comparison set that fits naturally into responsive grid viewports (2x3 or 3x2) without cognitive overload.
   - Crucially, reducing the frontend shortlist from 10 to 6 directly reduces the downstream per-card `/commute` request volume by 40% (from 10 concurrent requests to 6).

3. **Bounded TTL Route Caching and In-Flight Request Coalescing**:
   - Introduced `BoundedTTLCacheWithCoalescing` (`core/route_cache.py`), a thread-safe, bounded in-memory cache (`maxsize = 1024`, `ttl = 3600.0` seconds / 1 hour) with per-key in-flight request coalescing.
   - Applied to both multi-criteria matching routing (`core/onemap.py:get_route_duration_and_distance`) and strict commute route legs (`core/commute.py:get_negotiated_commute_leg`).
   - **Per-Key In-Flight Coalescing**: When multiple threads or concurrent requests demand the same route coordinates or journey leg simultaneously, the first thread executes the OneMap API request while subsequent threads wait on a threading event and reuse the result, preventing burst stampedes to OneMap API.
   - **Cacheability Invariant**: Only cache successful, verified OneMap API routing responses (`plan.itineraries` or `route_summary` in OneMap; `status == "available"` in commute legs). **Never cache Haversine straight-line fallbacks or unavailable legs** (timeouts, network errors, provider 5xx, or missing itineraries). This guarantees that transient external API degradation recovers immediately on retry without cache poisoning.
   - **Shared HTTP Client Reuse**: Wrapped postal batch coordinate resolution and Stage 2 transit routing in a single shared `httpx.Client` session during `execute_multi_criteria_match`, eliminating per-call TCP connection teardown and TLS handshake overhead.

4. **Verified Postal Cache Warming & Concurrent Postal Coalescing**:
   - In `core/onemap.py:resolve_postal_coordinates`, when OneMap Search returns an exact `POSTAL` match with valid WGS84 coordinates, warm the in-memory verified commute postal cache (`_COMMUTE_POSTAL_CACHE`) alongside the persistent database cache.
   - In `core/commute.py:resolve_commute_postal`, added per-postal in-flight request coalescing so concurrent `/commute` requests for the Top 6 shortlist never execute duplicate OneMap Search calls for shared `home` or `workplace` anchors.
   - **Legacy Security Invariant**: Strict rejection of legacy unverified database cache rows on `_COMMUTE_POSTAL_CACHE` miss is preserved, ensuring untrusted coordinate records are never blindly accepted.

5. **Comparative Ranking Rationale in Fit Explanation**:
   - Extended `FitExplanationResponse` schema with `ranking_rationale: str` and `fallback_used: bool = False`.
   - Updated the Gemini prompt (`build_explanation_prompt`) to require a 2–4 sentence comparative narrative explaining why candidate centres are ordered in their specific sequence (#1 through #N) based on Overall Fit, comparing Vacancy Fit, Cost Fit (Estimated Monthly Payable Fee), and Travel Fit (Commute Deviation or distance).
   - Implemented deterministic fallback logic (`generate_deterministic_fallback`) providing synthesized ranking explanations for 0 candidates, 1 candidate, and 2+ candidates when Gemini API keys are unconfigured, timed out, or rate-limited, guaranteeing zero matching API failures.

## Consequences

- Cold matching runs require at most 31 OneMap API calls (down from 61).
- Repeated matching runs with identical coordinates (such as when parents tune Importance Ratings) require 0 external OneMap routing calls.
- Parallel commute requests for the Top 6 cards coalesce the identical `home → workplace` direct baseline route into a single OneMap API query.
- Transient OneMap API degradation does not persist in memory because Haversine fallbacks and unavailable legs are excluded from the cache.
- Global test fixture `tests/conftest.py` automatically resets route and postal caches (`clear_onemap_caches()` and `clear_commute_caches()`) around every test to maintain deterministic isolation.

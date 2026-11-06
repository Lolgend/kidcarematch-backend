# Direct Commute Baseline, Elapsed Timing Certification, and Selected Weekday Sampling

## Context

Preschool centre details and commute comparison (Spec #87 and Backend Handover) require an opt-in direct home → workplace baseline route, elapsed travel time certification, and negotiated weekday sampling (`weekday_0800_wed_thu_fri_v1`). Previously, the centre-detail commute endpoint (ADR-0009) returned only via-centre route journeys across fixed Wednesday/Saturday sample pairs, lacking direct route comparison, elapsed timing certification, or Thursday/Friday fallbacks when public transport schedules change.

To support interactive commute comparisons without compromising operational latency, bounding external OneMap provider load, or violating multi-criteria matching invariants (ADR-0007), we establish an extensible capability contract, bounded concurrency, aggregate deadlines, and failure classification.

## Decisions

1. **Capability Discovery Contract (`GET /api/v1/commute/capabilities`)**:
   - Exposes independent capability markers: `direct_baseline_contract: "direct_baseline_v1"` and `sampling_contract: "weekday_0800_wed_thu_fri_v1"`.
   - Requires active parent authorization (`401` for unauthenticated, `403` for non-parent or suspended users).
   - Driven by feature flags in `core/config.py`: `COMMUTE_ENABLE_DIRECT_BASELINE` and `COMMUTE_ENABLE_WEEKDAY_SAMPLING` (enabled by default upon release gate validation).

2. **Stateless HMAC-SHA256 Cycle Token Lifecycle**:
   - For `sampling_contract: "weekday_0800_wed_thu_fri_v1"`, client initiates sampling with `cycle_id: null, sample_index: 0`.
   - Server issues a signed HMAC-SHA256 token encoding primary Wednesday 08:00 SGT, issuance timestamp, version, and expiration.
   - Cycle tokens expire **strictly at primary Wednesday 08:00 SGT**, ensuring next-cycle refresh alignment and preventing fallbacks from keeping stale cycles alive.
   - Bounded fallback samples (`sample_index: 1` for following Thursday 08:00 SGT, `sample_index: 2` for following Friday 08:00 SGT) reuse the signed cycle token; expired tokens return `409 Conflict` (`sample_expired`), and invalid or tampered tokens return `422 Unprocessable Entity`.

3. **Elapsed Timing Certification & Component Failure Survival**:
   - Compares total elapsed time from requested departure (08:00 SGT) including initial waiting, transfers, and transit legs.
   - For public transport, requires verified OneMap `endTime` satisfying `duration <= elapsed <= 86400` seconds (`duration_basis: "elapsed_from_requested_departure"`, `arrival_source: "provider_end_time"`, `initial_wait_included: true`). If `endTime` is missing or synthetic, comparison fails closed with `timing_unverified` while preserving usable route geometries and totals.
   - For provider-default modes (`drive`, `walk`, `cycle`), certifies compatible static estimates (`duration_basis: "provider_default"`).
   - Independent component survival: failure of direct baseline does not invalidate via-centre routes; conversely, failure of a via leg preserves complete direct baseline routes.

4. **Operational Guardrails & Concurrency Limits**:
   - **Pool Bound**: Evaluations run in a bounded worker pool (`ThreadPoolExecutor(max_workers=COMMUTE_MAX_WORKERS)` where `COMMUTE_MAX_WORKERS = 4`).
   - **Per-Call Timeout**: Individual OneMap routing API requests enforce `COMMUTE_CALL_TIMEOUT_SECONDS = 6.0` (with 1 reactive 401 token invalidation retry).
   - **Aggregate Evaluation Deadline**: Commute evaluations enforce an aggregate deadline of `COMMUTE_EVALUATION_DEADLINE_SECONDS = 12.0`.
   - **Cancellation & Budget Exhaustion**: When the aggregate deadline expires, pending futures are cancelled, incomplete legs are assigned `status: "unavailable"` with reason `evaluation_budget_exhausted` (`failure_class: "service"`, `retryable: true`), and the executor is shut down non-blockingly (`executor.shutdown(wait=False, cancel_futures=True)`) so hanging worker threads do not delay client responses.

5. **Latency & Concurrency Benchmark Telemetry**:
   Measured synthetic OneMap routing load across 100 benchmark iterations under the 4-worker pool:
   - **Sequential (3 legs)**: p50 = 124.3ms, p95 = 155.7ms, p99 = 170.0ms.
   - **PT Mode (Concurrent Leg 1 || Direct Leg, followed by Leg 2)**: p50 = 91.7ms, p95 = 109.4ms, p99 = 124.3ms (~26% speedup at p50).
   - **Non-PT Mode (Concurrent Leg 1 || Leg 2 || Direct Leg)**: p50 = 47.2ms, p95 = 63.0ms, p99 = 63.8ms (~62% speedup at p50).

6. **ADR-0009 Presentation Exception**:
   - ADR-0009 mandated returning Wednesday/Saturday dual-sample pairs for all commute requests.
   - For clients explicitly negotiating `weekday_0800_wed_thu_fri_v1`, ADR-0010 supersedes ADR-0009's presentation rule by returning exactly one representative weekday sample (`kind: "weekday"`, `sample_pair: null`).
   - Legacy unnegotiated requests strictly retain ADR-0009's dual-sample pair contract and JSON shape without breaking existing consumers.

7. **Preservation of ADR-0007 Matching Architecture**:
   - The multi-criteria matching engine (`core/onemap.py:get_route_duration_and_distance` and `POST /api/v1/centres/match`) remains completely untouched and unaffected.
   - All spatial corridor pruning, Haversine approximations, and candidate ranking weights defined in ADR-0007 are preserved without modification.

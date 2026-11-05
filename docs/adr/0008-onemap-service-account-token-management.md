# OneMap Service Account Token Lifecycle and In-Memory Caching Architecture

## Context

KidCareMatch relies on the Singapore Land Authority (SLA) OneMap API to perform Singapore postal code coordinate geocoding and public transit detour routing calculations (ADR-0007). SLA OneMap restricts API access using short-lived JSON Web Tokens (JWT) that expire after 72 hours and must be dynamically provisioned via the authentication endpoint `/api/auth/post/getToken` using service account developer credentials.

Previously, the application relied on a static `ONEMAP_API_TOKEN` environment variable. Manual static token provisioning suffered from severe operational deficiencies: tokens inevitably expired every 3 days, causing unannounced outages and HTTP 401 Unauthorized errors in postal coordinate lookup and detour commute calculations, while demanding continuous manual intervention from operations staff. To ensure high availability and resilient headless background execution, KidCareMatch requires an automated, thread-safe, and self-healing token lifecycle architecture.

## Decisions

1. **In-Memory Token Manager (`OneMapTokenManager`) with Double-Checked Locking**:
   Dynamic token retrieval and caching are managed in-memory via `OneMapTokenManager`. The manager maintains cached token state (`_token`), parsed expiration epoch (`_expiry_timestamp`), and a re-entrant `threading.RLock`. Fast-path evaluation checks validity without lock contention, while cache misses acquire the lock and re-evaluate validity under double-checked locking before executing an HTTP POST request to `settings.ONEMAP_AUTH_URL` (`{"email": ..., "password": ...}`). This guarantees thread safety across concurrent requests while preventing thundering herd spikes against upstream SLA authentication servers.

2. **5-Minute Proactive Token Renewal Window**:
   To prevent mid-flight token expiration during high-latency multi-stage detour routing queries and guard against clock skew between backend hosts and SLA infrastructure, `OneMapTokenManager` implements a 300-second (5-minute) proactive renewal buffer (`REFRESH_BUFFER_SECS = 300.0`). If the current time is within 300 seconds of the token's expiration timestamp, the manager considers the token expired and requests a fresh token. Upstream Unix epoch timestamps are parsed flexibly (accepting integers, floats, and numeric strings), defaulting safely to a 72-hour lifespan (`DEFAULT_EXPIRY_HOURS = 72.0`) if the timestamp is missing, malformed, or invalid.

3. **Reactive 401 Cache Invalidation and Single-Attempt Retry**:
   In the event of unforeseen premature token revocation or server-side invalidation by SLA OneMap, both geocoding (`resolve_postal_coordinates`) and transit routing (`get_route_duration_and_distance`) adapters implement a transparent reactive retry loop. Upon encountering an HTTP 401 Unauthorized status, the adapter immediately invokes `onemap_token_manager.invalidate_token()`, retrieves fresh authentication headers, and re-executes the request exactly once before proceeding.

4. **Uniform Authorization Bearer Header Injection**:
   OneMap authentication is centralized through `onemap_token_manager.get_auth_headers()`, which supplies standard `{"Authorization": f"Bearer {token}"}` headers. This ensures consistent header formatting across both geocoding (`/api/common/elastic/search`) and detour routing (`/api/public/routingsvc/route`) services.

5. **Resilient Fallback and Graceful Degradation**:
   - **Static Fallback**: If service account credentials (`ONEMAP_EMAIL`, `ONEMAP_PASSWORD`) are not configured, the manager seamlessly falls back to the legacy `ONEMAP_API_TOKEN` setting or unauthenticated requests.
   - **Graceful Spatial Degradation**: In the event of catastrophic upstream network outages, authentication endpoint downtime, or persistent 5xx/401 errors, detour routing gracefully falls back to great-circle straight-line distance (`haversine_distance_km`) and estimated transit durations (`estimate_transit_duration_mins`). Childcare matching queries never fail or throw unhandled 500 Internal Server Errors due to external map provider dependencies.

6. **Administrative Health Observability and Heartbeat Probes**:
   In alignment with ADR-0005, OneMap integration health is integrated into the administrative operational monitoring framework (`ExternalDataSourceStatus`). The `probe_onemap_api` probe explicitly validates OneMap Service Account authentication, reports detailed diagnostic failure messages to administrative users when credentials fail, evicts invalid cached tokens upon HTTP 401 responses, and updates operational sync states (`SUCCESS` vs `FAILED`) on demand (`POST /api/v1/admin/status/refresh`).

## Considered Options

- **In-Memory Thread-Safe Caching (Chosen)**:
  Sub-millisecond retrieval latency with zero database I/O overhead. A singleton instance manages the token within the application process lifecycle, leveraging standard Python threading primitives for thread-safe concurrency.
- **Relational Database Persistence**:
  Storing ephemeral 72-hour tokens in the primary relational database would introduce superfluous database write transactions, connection pool pressure, and distributed locking complexity without tangible availability gains.
- **Per-Parent OneMap Credentials**:
  Requiring individual parents to register developer accounts with SLA OneMap would create unacceptable onboarding friction. Geospatial mapping and transit calculations are core system infrastructure services that belong strictly at the platform layer.

## Consequences & Trade-offs

- **Memory Footprint**:
  Negligible memory consumption per application instance (storing a single string token and a floating-point epoch timestamp).
- **Multi-Instance Distributed Sync**:
  Each application process or container replica independently and safely acquires and manages its own 72-hour token. SLA OneMap supports multiple active service account tokens concurrently without invalidating prior tokens, eliminating the need for distributed cross-node coordination.
- **Resilient Degradation**:
  Parent matching searches and coordinate lookups maintain uninterrupted availability even during upstream SLA OneMap auth outages or routing failures through haversine approximations and persistent coordinate caching.

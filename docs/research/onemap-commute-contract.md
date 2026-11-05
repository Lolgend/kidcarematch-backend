# OneMap Commute Display Contract & Provider Research

## Overview

This document establishes the verified provider contract for OneMap Search and Routing APIs used by the KidCareMatch commute comparison feature.
It documents request shapes, authentication, response schemas, geometry encoding, and day-dependent variation constraints for routing.

## 1. Authentication

- **Provider**: Singapore Land Authority (SLA) OneMap REST API
- **Mechanism**: Bearer token authentication in HTTP request header:
  ```http
  Authorization: Bearer <ONEMAP_API_TOKEN>
  ```
- **Token Validity**: OneMap access tokens are issued via `https://www.onemap.gov.sg/api/auth/post/getToken` using registered developer credentials and are valid for 72 hours.
- **Display Adapter Boundary**: Tokens are stored and managed securely server-side in backend configuration (`settings.ONEMAP_API_TOKEN` / `settings.ONEMAP_API_KEY`). The client never interacts directly with OneMap credentials.

## 2. Geocoding / Search API Contract

- **Endpoint**: `https://www.onemap.gov.sg/api/common/elastic/search`
- **Method**: `GET`
- **Parameters**:
  - `searchVal`: 6-digit postal code (e.g. `560123`)
  - `returnGeom`: `"Y"`
  - `getAddrDetails`: `"Y"`
  - `pageNum`: `1`
- **Exact-Postal Match Requirement**:
  OneMap elastic search uses fuzzy/relevance matching. A search for postal `560123` may return results for similar addresses or nearby postal codes first.
  The strict commute display contract **requires exact match validation**:
  ```python
  clean_postal = postal.strip()
  match = next((r for r in results if str(r.get("POSTAL", "")).strip() == clean_postal), None)
  ```
  If no result matches the requested 6-digit postal code exactly, the location is marked `unresolvable` (`None`).
  No legacy relevance matches or approximate coordinates may be used for personal commute anchors.

## 3. Driving Routing Contract (`routeType=drive`)

- **Endpoint**: `https://www.onemap.gov.sg/api/public/routingsvc/route`
- **Method**: `GET`
- **Parameters**:
  - `start`: `"{start_lat},{start_lng}"` (WGS84 decimal degrees, e.g. `"1.3691,103.8512"`)
  - `end`: `"{end_lat},{end_lng}"` (WGS84 decimal degrees, e.g. `"1.3521,103.8198"`)
  - `routeType`: `"drive"`
- **Response Structure**:
  ```json
  {
    "status_message": "Found route between points",
    "route_geometry": "sagGwhtxRsDkC...",
    "route_summary": {
      "total_time": 840,
      "total_distance": 6800,
      "start_point": "ANG MO KIO AVENUE 3",
      "end_point": "TOA PAYOH CENTRAL"
    }
  }
  ```
- **Units**:
  - `total_time`: Duration in **seconds** (integer/float). Converted to `duration_seconds` in backend schemas.
  - `total_distance`: Distance in **meters** (integer/float). Converted to `distance_meters` in backend schemas.
- **Geometry**:
  - `route_geometry`: Google Encoded Polyline format (5-digit precision, factor 1e5).
  - Decoded into a list of coordinate points: `[{"lat": float, "lng": float}, ...]`.
  - Must never be empty. If `route_geometry` is null, empty string, or cannot be decoded, the leg is considered `unavailable`.
- **Day-Dependent Variation**:
  - OneMap driving routing does not offer verified historical/predictive time-of-day traffic matrices.
  - For driving, the display contract issues a **single representative estimate** with sample kind `"single"`. No invented or synthetic weekday vs weekend traffic difference is returned.

## 4. Public Transport Routing Contract (`routeType=pt`)

- **Endpoint**: `https://www.onemap.gov.sg/api/public/routingsvc/route`
- **Method**: `GET`
- **Parameters**:
  - `start`: `"{start_lat},{start_lng}"` (WGS84 decimal degrees, e.g. `"1.3691,103.8512"`)
  - `end`: `"{end_lat},{end_lng}"` (WGS84 decimal degrees, e.g. `"1.3521,103.8198"`)
  - `routeType`: `"pt"`
  - `date`: `"{MM-DD-YYYY}"` (e.g. `"11-11-2026"`, next representative Wednesday)
  - `time`: `"{HH:MM:SS}"` (e.g. `"08:00:00"`)
  - `mode`: `"TRANSIT"`
  - `maxWalkDistance`: `1000`
  - `numItineraries`: `1`
- **Response Structure**:
  ```json
  {
    "plan": {
      "date": 1762300800000,
      "itineraries": [
        {
          "duration": 1500,
          "walkTime": 600,
          "transitTime": 900,
          "waitingTime": 0,
          "walkDistance": 500.0,
          "legs": [
            {
              "mode": "WALK",
              "duration": 300,
              "distance": 250,
              "legGeometry": { "points": "sagGwhtxRsDkC..." }
            },
            {
              "mode": "BUS",
              "route": "159",
              "duration": 900,
              "distance": 4500,
              "legGeometry": { "points": "sagGwhtxRsDkC..." }
            },
            {
              "mode": "WALK",
              "duration": 300,
              "distance": 250,
              "legGeometry": { "points": "sagGwhtxRsDkC..." }
            }
          ]
        }
      ]
    }
  }
  ```
- **Units & Normalization**:
  - `itinerary.duration`: Total journey duration in seconds (`duration_seconds`).
  - `sub_leg.distance`: Sub-leg distance in meters. Summed across legs for journey `distance_meters`.
  - `itinerary.legs`: Parsed into `TransitLeg` objects capturing mode (`WALK`, `BUS`, `SUBWAY`), route designation, duration, distance, and per-leg geometry.
- **Geometry**:
  - Per-leg encoded polylines extracted from `legGeometry.points` and combined into complete leg geometry.
  - If geometry is missing or decoded geometry is empty, status is `unavailable`.
- **Representative Weekday Sampling & Sequential Onward Timing**:
  - Evaluated at 08:00 Asia/Singapore on the next upcoming Wednesday (`kind="weekday"`).
  - When workplace is present, Leg 2 (centre -> workplace) departs at Leg 1 estimated arrival time (`first_leg_arrival = departure + duration_seconds`) with 0 added drop-off duration.
  - If Leg 1 arrival cannot be determined, Leg 2 is marked `unavailable` without making an invented onward request.
  - Totals are withheld when any requested leg fails.
  - NEVER fall back to straight-line approximations.

## 5. Walking and Cycling Routing Contracts (`routeType=walk`, `routeType=cycle`)

- **Endpoints**: Same OneMap routing endpoint (`https://www.onemap.gov.sg/api/public/routingsvc/route`)
- **Method**: `GET`
- **Parameters**:
  - `start`: `"{start_lat},{start_lng}"`
  - `end`: `"{end_lat},{end_lng}"`
  - `routeType`: `"walk"` for walking, `"cycle"` for cycling
- **Response Structure & Units**:
  - Exactly matches the driving response structure with `route_geometry` (Google encoded polyline), `route_summary.total_time` in seconds, and `route_summary.total_distance` in meters.
- **Day-Dependent Variation**:
  - Walking and cycling do not have date- or time-dependent timetable schedules or variable congestion models in OneMap.
  - Returns a single representative estimate with sample kind `"single"`.
  - The UI and backend display contract present a single estimate without synthetic weekday or weekend differences.

## 5. Leg Failure & Fallback Policy

- Unlike matching algorithms (which use straight-line approximations for candidate ranking under ADR-0007), the **commute display contract strictly forbids straight-line fallback**.
- If OneMap routing fails, times out, returns HTTP error, or yields unusable geometry:
  - `status`: `"unavailable"`
  - `source`: `None`
  - `duration_seconds`: `None`
  - `distance_meters`: `None`
  - `geometry`: `None`
  - `message`: Specific reason (e.g. `"Route geometry unavailable"`, `"Route request timed out"`)
- If any requested leg in a journey is `"unavailable"`, complete totals (`RouteTotals`) are set to `None` to prevent misleading parents with partial numbers as a complete journey.

## 5. Multi-Leg Routing (Home -> Centre -> Workplace)

- When workplace postal is provided:
  - Leg 1: `from_anchor="home"`, `to_anchor="centre"`
  - Leg 2: `from_anchor="centre"`, `to_anchor="workplace"`
- Time spent at the centre is excluded. Total commute time is the sum of travel durations only:
  $$\text{duration\_seconds} = \text{duration\_seconds}_{\text{Leg 1}} + \text{duration\_seconds}_{\text{Leg 2}}$$
- When no workplace postal is provided, only a single leg (`home` -> `centre`) is computed and returned.

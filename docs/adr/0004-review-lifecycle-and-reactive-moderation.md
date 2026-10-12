# Review Lifecycle and Reactive Moderation Architecture

## Context

KidCareMatch empowers parents to share verified feedback and star ratings on preschool centres in Singapore, helping families make informed early childhood care decisions. The platform requires a community feedback system that balances immediate feedback publication with abuse prevention, relational integrity, and strict author access controls.

## Decisions

1. **RESTful Hybrid Routing**: Review submission is nested under the parent centre collection (`POST /api/v1/centres/{centre_id}/reviews`) to emphasize the compositional relationship with preschool centres and integrate seamlessly with public centre review listings. Conversely, review-level mutations, deletions, and direct lookups operate on canonical resource endpoints (`GET/PATCH/DELETE /api/v1/reviews/{review_id}`).
2. **Reactive Moderation Default**: Newly submitted reviews default to `ModerationStatus.APPROVED`. Rather than placing every submission into a pending administrative queue, reviews are published immediately to provide real-time community transparency. Reviews remain visible unless later flagged, audited, or transitioned to `PENDING` or `REJECTED` by administrative moderators.
3. **Composite Uniqueness and Integrity**: Each parent can submit at most one review per preschool centre, enforced at the database level by the composite unique constraint `uq_review_centre_user` (`centre_id`, `user_id`) and mapped to HTTP 409 Conflict. Rating values are bounded to 1–5 integers via Pydantic schema validation and database check constraints (`ck_review_rating_1_to_5`), returning HTTP 422 Unprocessable Entity for invalid values.
4. **In-Place Edit Retention**: The edit endpoint (`PATCH /api/v1/reviews/{review_id}`) permits authoring parents to adjust ratings and commentaries in place while preserving `APPROVED` status, avoiding interruption to published feedback.
5. **Author-Only Hard Deletion**: The `DELETE /api/v1/reviews/{review_id}` endpoint enables authoring parents to permanently delete their reviews (returning HTTP 204 No Content). Administrative interventions operate separately via moderation status flags, keeping user self-service distinct from administrative moderation.
6. **Author Discovery Endpoints**: To support rich client experiences, two author-focused discovery queries are provided: `GET /api/v1/centres/{centre_id}/reviews/mine` to determine centre review existence, and `GET /api/v1/reviews/me` to retrieve a paginated personal review portfolio.

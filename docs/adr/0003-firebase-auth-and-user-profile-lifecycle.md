# Firebase Authentication and User Profile Lifecycle Architecture

## Context

KidCareMatch requires secure user authentication, identity federation (supporting passwordless, email/password, and OAuth providers), and personalized childcare matching for parents without taking on the operational and security risks of storing password credentials in the local application database.

## Decisions

1. **Federated Authentication via Firebase**: Delegate credential storage, password hashing, multi-factor authentication, and token issuance to Firebase Authentication. The `password_hash` column is eliminated from the `User` table, leaving no secrets or passwords stored locally.
2. **Decoupled Identity with Surrogate Keys**: The `User` entity indexes a mandatory unique `firebase_uid` matching the subject claim (`sub`) of verified Firebase ID tokens. However, internal relational foreign keys (`FamilyPreferenceProfile`, `Review`, `WatchlistEntry`) continue referencing an internal surrogate UUID `user_id`. This decouples the relational domain graph from external identity provider specifics and migration paths.
3. **Explicit Profile Lifecycle and Onboarding**: Authentication against Firebase alone does not implicitly provision an application account. A user completes a distinct onboarding step (`POST /signup`) after authenticating with Firebase, creating their `User` record with role `PARENT` and status `ACTIVE`. Subsequent requests authenticate via bearer ID tokens mapped to the persisted `User` record by `firebase_uid`.

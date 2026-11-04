"""Authentication package for kidcarematch_backend."""

from kidcarematch_backend.auth.security import (
    FirebaseTokenClaims,
    get_current_admin,
    get_current_user,
    get_firebase_app,
    get_optional_current_user,
    get_optional_token_claims,
    get_token_claims,
)

__all__ = [
    "FirebaseTokenClaims",
    "get_current_admin",
    "get_current_user",
    "get_firebase_app",
    "get_optional_current_user",
    "get_optional_token_claims",
    "get_token_claims",
]

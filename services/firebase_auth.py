"""Firebase Authentication ID-token verification."""

import time
from collections.abc import Mapping

import requests
from cachecontrol import CacheControl
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_firebase_token

FIREBASE_ISSUER_PREFIX = "https://securetoken.google.com/"
MAX_FIREBASE_UID_CHARACTERS = 128
TOKEN_CLOCK_SKEW_SECONDS = 60
FIREBASE_ADMIN_ROLE = "admin"
FIREBASE_USER_ROLE = "user"


class FirebaseTokenInvalidError(ValueError):
    """The supplied value is not a valid Firebase ID token."""


class FirebaseTokenVerificationUnavailableError(RuntimeError):
    """Firebase signing material could not be obtained."""


class FirebaseIDTokenVerifier:
    """Verify Firebase ID tokens with cached public signing certificates."""

    def __init__(self, project_id: str) -> None:
        normalized_project_id = project_id.strip()
        if not normalized_project_id:
            raise ValueError("FIREBASE_PROJECT_ID is required")
        self._project_id = normalized_project_id
        session = CacheControl(requests.Session())
        self._request = Request(session=session)

    def verify(self, token: str) -> dict[str, object]:
        try:
            header = jwt.decode_header(token)  # type: ignore[no-untyped-call]
        except (google_auth_exceptions.GoogleAuthError, TypeError, ValueError) as exc:
            raise FirebaseTokenInvalidError("Firebase ID token is malformed") from exc
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str) or not key_id:
            raise FirebaseTokenInvalidError("Firebase ID token header is invalid")

        try:
            claims = verify_firebase_token(  # type: ignore[no-untyped-call]
                token,
                self._request,
                audience=self._project_id,
                clock_skew_in_seconds=TOKEN_CLOCK_SKEW_SECONDS,
            )
        except google_auth_exceptions.TransportError as exc:
            raise FirebaseTokenVerificationUnavailableError(
                "Firebase signing certificates are unavailable"
            ) from exc
        except (google_auth_exceptions.GoogleAuthError, TypeError, ValueError) as exc:
            raise FirebaseTokenInvalidError("Firebase ID token is invalid") from exc

        self._validate_firebase_claims(claims)
        return dict(claims)

    def _validate_firebase_claims(self, claims: Mapping[str, object]) -> None:
        expected_issuer = f"{FIREBASE_ISSUER_PREFIX}{self._project_id}"
        if claims.get("iss") != expected_issuer:
            raise FirebaseTokenInvalidError("Firebase ID token issuer is invalid")

        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > MAX_FIREBASE_UID_CHARACTERS
        ):
            raise FirebaseTokenInvalidError("Firebase ID token subject is invalid")

        auth_time = claims.get("auth_time")
        if (
            isinstance(auth_time, bool)
            or not isinstance(auth_time, (int, float))
            or auth_time > time.time() + TOKEN_CLOCK_SKEW_SECONDS
        ):
            raise FirebaseTokenInvalidError("Firebase ID token auth_time is invalid")


def firebase_role(
    claims: Mapping[str, object],
    admin_emails: frozenset[str],
) -> str:
    """Derive an application role from a verified Firebase email claim."""
    email = claims.get("email")
    if isinstance(email, str) and email.strip().casefold() in admin_emails:
        return FIREBASE_ADMIN_ROLE
    return FIREBASE_USER_ROLE

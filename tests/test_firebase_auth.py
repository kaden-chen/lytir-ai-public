import base64
import json
import time
from collections.abc import Mapping

import pytest
from google.auth import exceptions as google_auth_exceptions

import services.firebase_auth as firebase_auth
from services.firebase_auth import (
    FirebaseIDTokenVerifier,
    FirebaseTokenInvalidError,
    FirebaseTokenVerificationUnavailableError,
    firebase_role,
)


def token_with_header(header: dict[str, object]) -> str:
    def encode(value: object) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
        return encoded.decode()

    signature = base64.urlsafe_b64encode(b"signature").rstrip(b"=").decode()
    return f"{encode(header)}.{encode({})}.{signature}"


def valid_claims() -> dict[str, object]:
    now = int(time.time())
    return {
        "aud": "test-project",
        "iss": "https://securetoken.google.com/test-project",
        "sub": "firebase-user-123",
        "auth_time": now - 60,
        "iat": now - 60,
        "exp": now + 3600,
    }


def test_verifier_returns_verified_firebase_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def verify(
        token: str,
        request: object,
        audience: str | None = None,
        clock_skew_in_seconds: int = 0,
    ) -> Mapping[str, object]:
        captured.update(
            token=token,
            request=request,
            audience=audience,
            clock_skew_in_seconds=clock_skew_in_seconds,
        )
        return valid_claims()

    monkeypatch.setattr(firebase_auth, "verify_firebase_token", verify)
    token = token_with_header({"alg": "RS256", "kid": "test-key"})

    claims = FirebaseIDTokenVerifier(" test-project ").verify(token)

    assert claims == valid_claims()
    assert captured["token"] == token
    assert captured["audience"] == "test-project"
    assert captured["clock_skew_in_seconds"] == 60


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://securetoken.google.com/another-project"),
        ("sub", ""),
        ("sub", "x" * 129),
        ("auth_time", "not-a-timestamp"),
        ("auth_time", time.time() + 120),
    ],
)
def test_verifier_rejects_invalid_firebase_claims(
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
    value: object,
) -> None:
    claims = valid_claims()
    claims[claim] = value
    monkeypatch.setattr(
        firebase_auth,
        "verify_firebase_token",
        lambda *_args, **_kwargs: claims,
    )

    with pytest.raises(FirebaseTokenInvalidError):
        FirebaseIDTokenVerifier("test-project").verify(
            token_with_header({"alg": "RS256", "kid": "test-key"})
        )


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "HS256", "kid": "test-key"},
        {"alg": "RS256"},
        {"alg": "RS256", "kid": ""},
        {"alg": "RS256", "kid": 123},
    ],
)
def test_verifier_rejects_invalid_token_headers(header: dict[str, object]) -> None:
    with pytest.raises(FirebaseTokenInvalidError):
        FirebaseIDTokenVerifier("test-project").verify(token_with_header(header))


def test_verifier_classifies_certificate_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Mapping[str, object]:
        raise google_auth_exceptions.TransportError(  # type: ignore[no-untyped-call]
            "certificate endpoint failed"
        )

    monkeypatch.setattr(firebase_auth, "verify_firebase_token", fail)

    with pytest.raises(FirebaseTokenVerificationUnavailableError):
        FirebaseIDTokenVerifier("test-project").verify(
            token_with_header({"alg": "RS256", "kid": "test-key"})
        )


def test_verifier_classifies_signature_failure_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Mapping[str, object]:
        raise google_auth_exceptions.InvalidValue(  # type: ignore[no-untyped-call]
            "signature failed"
        )

    monkeypatch.setattr(firebase_auth, "verify_firebase_token", fail)

    with pytest.raises(FirebaseTokenInvalidError):
        FirebaseIDTokenVerifier("test-project").verify(
            token_with_header({"alg": "RS256", "kid": "test-key"})
        )


def test_firebase_role_matches_verified_email_case_insensitively() -> None:
    claims: dict[str, object] = {"email": " Admin@Example.com "}

    assert firebase_role(claims, frozenset({"admin@example.com"})) == "admin"


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"email": 123},
        {"email": "user@example.com"},
    ],
)
def test_firebase_role_defaults_to_user(claims: dict[str, object]) -> None:
    assert firebase_role(claims, frozenset({"admin@example.com"})) == "user"

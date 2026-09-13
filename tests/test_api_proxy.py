import asyncio
import json

import azure.functions as func
import pytest

import blueprints.api_proxy as api_proxy
from services.firebase_auth import (
    FirebaseTokenInvalidError,
    FirebaseTokenVerificationUnavailableError,
)

VERIFIED_CLAIMS: dict[str, object] = {
    "sub": "firebase-user-123",
    "email": "user@example.com",
}


class FakeVerifier:
    def __init__(
        self,
        result: dict[str, object] | Exception,
    ) -> None:
        self.result = result
        self.tokens: list[str] = []

    def verify(self, token: str) -> dict[str, object]:
        self.tokens.append(token)
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


def request(
    method: str,
    target: str,
    *,
    token: str | None = None,
    record_id: str | None = None,
    params: dict[str, str] | None = None,
    body: bytes = b"",
) -> func.HttpRequest:
    route_params = {"target": target}
    path = f"/api/proxy/{target}"
    if record_id is not None:
        route_params["id"] = record_id
        path = f"{path}/{record_id}"
    headers = {"Content-Type": "application/json", "X-Request-ID": "request-1"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return func.HttpRequest(
        method=method,
        url=f"http://localhost:7071{path}",
        headers=headers,
        params={} if params is None else params,
        route_params=route_params,
        body=body,
    )


def install_verifier(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object] | Exception,
) -> FakeVerifier:
    verifier = FakeVerifier(result)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "test-project")
    monkeypatch.setattr(api_proxy, "_build_token_verifier", lambda _project: verifier)
    return verifier


def payload(response: func.HttpResponse) -> dict[str, object]:
    parsed: object = json.loads(response.get_body())
    assert isinstance(parsed, dict)
    return parsed


def test_proxy_mapping_covers_every_existing_http_api() -> None:
    mappings = {
        (target.method, target.path, target.has_id)
        for target in api_proxy.PROXY_TARGETS
    }

    assert mappings == {
        ("GET", "diag", False),
        ("POST", "ai-qa-diagnostic", False),
        ("POST", "ai-qa", False),
        ("GET", "earthquakes", False),
        ("GET", "earthquakes", True),
        ("POST", "earthquakes-acquire", False),
    }


@pytest.mark.parametrize("target", ["ai-qa", "earthquakes", "earthquakes-acquire"])
def test_protected_proxy_target_requires_token(target: str) -> None:
    response = asyncio.run(api_proxy.proxy_api(request("GET", target)))

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_protected_proxy_target_rejects_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_verifier(monkeypatch, FirebaseTokenInvalidError("invalid token"))

    response = asyncio.run(
        api_proxy.proxy_api(request("POST", "ai-qa", token="invalid-token"))
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_valid_token_dispatches_post_without_forwarding_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = install_verifier(monkeypatch, VERIFIED_CLAIMS)
    received: list[func.HttpRequest] = []
    expected = func.HttpResponse(
        body=b'{"answer":"unchanged"}',
        status_code=202,
        headers={"X-Downstream": "preserved"},
        mimetype="application/json",
    )

    def handler(req: func.HttpRequest) -> func.HttpResponse:
        received.append(req)
        return expected

    monkeypatch.setattr(
        api_proxy,
        "PROXY_TARGETS",
        (api_proxy.ProxyTarget("ai-qa", "POST", handler),),
    )
    body = b'{"question":"What happened?"}'
    req = request(
        "POST",
        "ai-qa",
        token="valid-token",
        params={"trace": "true"},
        body=body,
    )

    response = asyncio.run(api_proxy.proxy_api(req))

    assert response is expected
    assert verifier.tokens == ["valid-token"]
    assert len(received) == 1
    assert received[0].get_body() == body
    assert dict(received[0].params) == {"trace": "true"}
    assert received[0].headers.get("Authorization") is None
    assert received[0].headers["Content-Type"] == "application/json"
    assert received[0].headers["X-Request-ID"] == "request-1"


def test_proxy_adapts_exact_record_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_verifier(monkeypatch, VERIFIED_CLAIMS)
    received: list[func.HttpRequest] = []

    def handler(req: func.HttpRequest) -> func.HttpResponse:
        received.append(req)
        return func.HttpResponse(status_code=204)

    monkeypatch.setattr(
        api_proxy,
        "PROXY_TARGETS",
        (api_proxy.ProxyTarget("earthquakes", "GET", handler, has_id=True),),
    )
    req = request(
        "GET",
        "earthquakes",
        token="valid-token",
        record_id="record-123",
        params={"format": "csv"},
    )

    response = asyncio.run(api_proxy.proxy_api_with_id(req))

    assert response.status_code == 204
    assert received[0].route_params == {"id": "record-123"}
    assert dict(received[0].params) == {"format": "csv"}


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic credential", "Bearer invalid-token"],
)
def test_diag_returns_normal_response_without_valid_token(
    monkeypatch: pytest.MonkeyPatch,
    authorization: str | None,
) -> None:
    if authorization == "Bearer invalid-token":
        install_verifier(
            monkeypatch,
            FirebaseTokenInvalidError("invalid token"),
        )
    req = request("GET", "diag")
    if authorization is not None:
        req = func.HttpRequest(
            method=req.method,
            url=req.url,
            headers={"Authorization": authorization},
            params=dict(req.params),
            route_params=dict(req.route_params),
            body=req.get_body(),
        )

    response = asyncio.run(api_proxy.proxy_api(req))
    response_payload = payload(response)

    assert response.status_code == 200
    assert response_payload["status"] == "200 - OK"
    assert "security" not in response_payload


def test_diag_returns_normal_response_when_firebase_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)

    response = asyncio.run(
        api_proxy.proxy_api(request("GET", "diag", token="unverifiable-token"))
    )

    assert response.status_code == 200
    response_payload = payload(response)
    assert "security" not in response_payload


def test_protected_target_requires_firebase_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)

    response = asyncio.run(
        api_proxy.proxy_api(request("POST", "ai-qa", token="unverifiable-token"))
    )

    assert response.status_code == 503


def test_diag_attaches_decoded_claims_for_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_verifier(monkeypatch, VERIFIED_CLAIMS)
    monkeypatch.setenv("ADMIN_USER_EMAILS", "USER@EXAMPLE.COM")

    response = asyncio.run(
        api_proxy.proxy_api(request("GET", "diag", token="valid-token"))
    )

    assert response.status_code == 200
    response_payload = payload(response)
    assert response_payload["security"] == {
        "claims": VERIFIED_CLAIMS,
        "role": "admin",
    }
    assert b"valid-token" not in response.get_body()


def test_diag_provides_authoritative_role_separately_from_decoded_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = {**VERIFIED_CLAIMS, "role": "admin"}
    install_verifier(monkeypatch, claims)
    monkeypatch.setenv("ADMIN_USER_EMAILS", "another@example.com")

    response = asyncio.run(
        api_proxy.proxy_api(request("GET", "diag", token="valid-token"))
    )

    security = payload(response)["security"]
    assert isinstance(security, dict)
    response_claims = security["claims"]
    assert isinstance(response_claims, dict)
    assert response_claims["role"] == "admin"
    assert security["role"] == "user"


def test_diag_ignores_unavailable_token_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_verifier(
        monkeypatch,
        FirebaseTokenVerificationUnavailableError("certificates unavailable"),
    )

    response = asyncio.run(
        api_proxy.proxy_api(request("GET", "diag", token="unverifiable-token"))
    )

    assert response.status_code == 200
    response_payload = payload(response)
    assert "security" not in response_payload


def test_protected_target_returns_service_unavailable_when_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_verifier(
        monkeypatch,
        FirebaseTokenVerificationUnavailableError("certificates unavailable"),
    )

    response = asyncio.run(
        api_proxy.proxy_api(request("POST", "ai-qa", token="unverifiable-token"))
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("target", "method", "expected_status"),
    [
        ("not-mapped", "GET", 404),
        ("ai-qa", "GET", 405),
    ],
)
def test_proxy_rejects_unknown_targets_and_wrong_methods(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    method: str,
    expected_status: int,
) -> None:
    install_verifier(monkeypatch, VERIFIED_CLAIMS)

    response = asyncio.run(
        api_proxy.proxy_api(request(method, target, token="valid-token"))
    )

    assert response.status_code == expected_status

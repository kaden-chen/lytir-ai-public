"""Firebase-authenticated proxy for allowlisted application APIs."""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import azure.functions as func

from blueprints.ai_qa import answer_ai_qa
from blueprints.ai_qa_diagnostic import diagnose_ai_qa
from blueprints.diagnostic import diag
from blueprints.earthquake_acquisition import acquire_earthquakes
from blueprints.earthquake_retrieval import (
    retrieve_earthquake,
    retrieve_earthquakes,
)
from config import ServiceSettings
from services.firebase_auth import (
    FirebaseIDTokenVerifier,
    FirebaseTokenInvalidError,
    FirebaseTokenVerificationUnavailableError,
    firebase_role,
)

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]

ProxyHandler = Callable[
    [func.HttpRequest], func.HttpResponse | Awaitable[func.HttpResponse]
]


@dataclass(frozen=True)
class ProxyTarget:
    path: str
    method: str
    handler: ProxyHandler
    has_id: bool = False


PROXY_TARGETS = (
    ProxyTarget("diag", "GET", diag),
    ProxyTarget("ai-qa-diagnostic", "POST", diagnose_ai_qa),
    ProxyTarget("ai-qa", "POST", answer_ai_qa),
    ProxyTarget("earthquakes", "GET", retrieve_earthquakes),
    ProxyTarget("earthquakes", "GET", retrieve_earthquake, has_id=True),
    ProxyTarget("earthquakes-acquire", "POST", acquire_earthquakes),
)


@lru_cache(maxsize=2)
def _build_token_verifier(project_id: str) -> FirebaseIDTokenVerifier:
    return FirebaseIDTokenVerifier(project_id)


@blueprint.route(
    route="proxy/{target}",
    methods=["GET", "POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
async def proxy_api(req: func.HttpRequest) -> func.HttpResponse:
    """Authenticate and dispatch one allowlisted API without a path ID."""
    return await _dispatch(req, record_id=None)


@blueprint.route(
    route="proxy/{target}/{id}",
    methods=["GET", "POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
async def proxy_api_with_id(req: func.HttpRequest) -> func.HttpResponse:
    """Authenticate and dispatch one allowlisted API with a path ID."""
    return await _dispatch(req, record_id=req.route_params.get("id"))


async def _dispatch(
    req: func.HttpRequest,
    *,
    record_id: str | None,
) -> func.HttpResponse:
    target_path = req.route_params.get("target", "")
    optional_auth = target_path == "diag" and record_id is None
    claims, auth_error = _authenticate(req, required=not optional_auth)
    if auth_error is not None:
        return auth_error

    target, resolution_error = _resolve_target(
        target_path,
        req.method,
        has_id=record_id is not None,
    )
    if resolution_error is not None:
        return resolution_error
    if target is None:
        raise RuntimeError("Resolved proxy target is missing")

    adapted_request = _adapt_request(req, record_id=record_id)
    response = await _invoke(target.handler, adapted_request)
    if optional_auth and claims is not None:
        return _attach_firebase_identity(
            response,
            claims,
            firebase_role(claims, ServiceSettings.firebase_admin_emails()),
        )
    return response


def _authenticate(
    req: func.HttpRequest,
    *,
    required: bool,
) -> tuple[dict[str, object] | None, func.HttpResponse | None]:
    try:
        token = _bearer_token(req)
    except FirebaseTokenInvalidError:
        if required:
            return None, _error_response(
                "A valid Firebase ID token is required",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None, None

    if token is None:
        if required:
            return None, _error_response(
                "A valid Firebase ID token is required",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None, None

    try:
        project_id = ServiceSettings.firebase_project_id()
        claims = _build_token_verifier(project_id).verify(token)
    except FirebaseTokenInvalidError:
        if required:
            return None, _error_response(
                "A valid Firebase ID token is required",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None, None
    except ValueError:
        logging.error("Firebase proxy authentication is not configured")
        if required:
            return None, _error_response(
                "Firebase authentication is temporarily unavailable", 503
            )
        return None, None
    except FirebaseTokenVerificationUnavailableError:
        logging.warning("Firebase ID token verification is temporarily unavailable")
        if required:
            return None, _error_response(
                "Firebase authentication is temporarily unavailable", 503
            )
        return None, None
    return claims, None


def _bearer_token(req: func.HttpRequest) -> str | None:
    authorization = req.headers.get("Authorization")
    if authorization is None:
        return None
    if not isinstance(authorization, str):
        raise FirebaseTokenInvalidError("Authorization header is invalid")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
        raise FirebaseTokenInvalidError("Authorization header is invalid")
    return parts[1]


def _resolve_target(
    path: str,
    method: str,
    *,
    has_id: bool,
) -> tuple[ProxyTarget | None, func.HttpResponse | None]:
    path_targets = tuple(
        target
        for target in PROXY_TARGETS
        if target.path == path and target.has_id is has_id
    )
    if not path_targets:
        return None, _error_response("Proxy target was not found", 404)

    normalized_method = method.upper()
    for target in path_targets:
        if target.method == normalized_method:
            return target, None

    allowed_methods = ", ".join(sorted({target.method for target in path_targets}))
    return None, _error_response(
        "Proxy target does not support this method",
        405,
        headers={"Allow": allowed_methods},
    )


def _adapt_request(
    req: func.HttpRequest,
    *,
    record_id: str | None,
) -> func.HttpRequest:
    headers = {
        name: value
        for name, value in req.headers.items()
        if name.casefold() != "authorization"
    }
    route_params = {} if record_id is None else {"id": record_id}
    return func.HttpRequest(
        method=req.method,
        url=req.url,
        headers=headers,
        params=dict(req.params),
        route_params=route_params,
        body=req.get_body(),
    )


async def _invoke(
    handler: ProxyHandler,
    req: func.HttpRequest,
) -> func.HttpResponse:
    response = handler(req)
    if inspect.isawaitable(response):
        return await response
    return response


def _attach_firebase_identity(
    response: func.HttpResponse,
    claims: dict[str, object],
    role: str,
) -> func.HttpResponse:
    try:
        payload = json.loads(response.get_body())
    except (TypeError, ValueError):
        logging.error("Diagnostic proxy response was not valid JSON")
        return response
    if not isinstance(payload, dict):
        logging.error("Diagnostic proxy response was not a JSON object")
        return response
    payload["security"] = {
        "claims": claims,
        "role": role,
    }
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=response.status_code,
        headers=dict(response.headers),
        mimetype=response.mimetype,
        charset=response.charset,
    )


def _error_response(
    message: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(
            {
                "error": message,
                "utc_now": datetime.now(UTC).isoformat(),
            }
        ),
        status_code=status_code,
        headers=headers,
        mimetype="application/json",
    )

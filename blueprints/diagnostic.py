"""Function-protected application diagnostic endpoint."""

import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import azure.functions as func

from config import ServiceSettings

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]

REDACTED_QUERY_PARAMETERS = frozenset({"code"})
REDACTED_VALUE = "***"


@blueprint.route(
    route="diag",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def diag(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Diagnostic HTTP trigger processed a request.")

    response = {
        "url": _redact_url(req.url),
        "params": _redact_params(dict(req.params)),
        "utc_now": datetime.now(UTC).isoformat(),
        "status": "200 - OK",
        "environment_variables": ServiceSettings.environment_variable_availability(),
    }

    return func.HttpResponse(
        body=json.dumps(response),
        status_code=200,
        mimetype="application/json",
    )


def _redact_params(params: dict[str, str]) -> dict[str, str]:
    return {
        name: REDACTED_VALUE if name.casefold() in REDACTED_QUERY_PARAMETERS else value
        for name, value in params.items()
    }


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    redacted_query = urlencode(
        [
            (
                name,
                REDACTED_VALUE
                if name.casefold() in REDACTED_QUERY_PARAMETERS
                else value,
            )
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        safe="*",
    )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, redacted_query, parsed.fragment)
    )

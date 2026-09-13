"""HTTP trigger for running the earthquake ingestion pipeline."""

import json
from datetime import UTC, datetime

import azure.functions as func

from models import EarthquakeEnvelope, EarthquakeMetadataRecord
from pipelines import run_earthquake_ingestion
from utils import is_truthy

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]


def _response(
    metadata: list[EarthquakeMetadataRecord],
    reason: str,
    status_code: int,
    *,
    normalized: int = 0,
    skipped: int = 0,
    published: int = 0,
    failed: int = 0,
    events: list[EarthquakeEnvelope] | None = None,
) -> func.HttpResponse:
    body: dict[str, object] = {
        "counts": {
            "retrieved": normalized + skipped,
            "normalized": normalized,
            "skipped": skipped,
            "published": published,
            "failed": failed,
        },
        "metadata": metadata,
        "utc_now": datetime.now(UTC).isoformat(),
        "reason": reason,
    }
    if events is not None:
        body["events"] = events

    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


def _read_dry_run(req: func.HttpRequest) -> bool:
    if not req.get_body().strip():
        return False

    try:
        body: object = req.get_json()
    except ValueError as exc:
        raise ValueError("request body must be valid JSON") from exc

    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    return is_truthy(body.get("dry_run", False))


@blueprint.route(
    route="earthquakes/acquire",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def acquire_earthquakes(req: func.HttpRequest) -> func.HttpResponse:
    """Download, normalize, and publish the configured USGS feed."""
    try:
        dry_run = _read_dry_run(req)
    except ValueError as exc:
        return _response(
            metadata=[],
            reason=f"Invalid request: {exc}",
            status_code=400,
        )

    result = run_earthquake_ingestion(dry_run=dry_run)
    return _response(
        metadata=result.metadata,
        reason=result.reason,
        status_code=200 if result.succeeded else 500,
        normalized=result.normalized,
        skipped=result.skipped,
        published=result.published,
        failed=result.failed,
        events=result.events if dry_run else None,
    )

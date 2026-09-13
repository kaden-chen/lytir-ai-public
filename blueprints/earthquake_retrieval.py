"""HTTP triggers for retrieving earthquake metadata from Cosmos DB."""

import csv
import io
import json
import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import cast
from uuid import UUID

import azure.functions as func
from azure.cosmos import CosmosClient

from config import ServiceSettings
from models import EarthquakeQuery, EarthquakeRetrievalRecord
from services import EarthquakeQueryService, EarthquakeQueryUnavailableError
from services.earthquake_query_service import CosmosContainer

DEFAULT_QUERY_WINDOW = timedelta(hours=2)
DEFAULT_RADIUS_KM = 5.0
MILES_TO_KILOMETERS = 1.609344
CSV_FIELDS = (
    "id",
    "event_type",
    "magnitude",
    "place",
    "occurred_at_utc",
    "updated_at_utc",
    "longitude",
    "latitude",
    "depth_km",
)
COLLECTION_FILTERS = {
    "start_time",
    "end_time",
    "latitude",
    "longitude",
    "radius_km",
    "radius_mi",
    "min_magnitude",
    "max_magnitude",
}

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]


@lru_cache(maxsize=1)
def _build_service(
    connection_string: str, database_name: str, container_name: str
) -> EarthquakeQueryService:
    """Build one Cosmos client per warm worker and reuse its connection pool."""
    client = CosmosClient.from_connection_string(connection_string)
    container = client.get_database_client(database_name).get_container_client(
        container_name
    )
    return EarthquakeQueryService(
        cast(CosmosContainer, container),
        client=client,
    )


@blueprint.route(
    route="earthquakes",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def retrieve_earthquakes(req: func.HttpRequest) -> func.HttpResponse:
    """Return filtered, deduplicated earthquake metadata."""
    now = datetime.now(UTC)
    try:
        settings = ServiceSettings()
    except ValueError:
        logging.exception("Invalid earthquake retrieval configuration")
        return _error_response("Earthquake retrieval is not configured", 500, now)

    try:
        filters = _parse_query(req.params, settings, now=now)
        response_format = _parse_format(req.params)
    except ValueError as exc:
        return _error_response(f"Invalid request: {exc}", 400, now)

    try:
        result = _service_for(settings).query(filters)
    except EarthquakeQueryUnavailableError:
        logging.exception("Cosmos DB was unavailable during earthquake retrieval")
        return _error_response("Earthquake data is temporarily unavailable", 503, now)
    except Exception:
        logging.exception("Unexpected earthquake retrieval failure")
        return _error_response("Earthquake retrieval failed", 500, now)

    logging.info(
        "Earthquake retrieval response: format=%s count=%d",
        response_format,
        len(result.items),
    )
    if response_format == "csv":
        return _csv_response(result.items, filename="earthquakes.csv")
    return func.HttpResponse(
        body=json.dumps(
            {
                "items": result.items,
                "count": len(result.items),
                "time_range": {
                    "start_utc": _format_utc(filters.start_time),
                    "end_utc": _format_utc(filters.end_time),
                },
                "utc_now": now.isoformat(),
            }
        ),
        status_code=200,
        mimetype="application/json",
    )


@blueprint.route(
    route="earthquakes/{id}",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def retrieve_earthquake(req: func.HttpRequest) -> func.HttpResponse:
    """Return one exact stored earthquake record by its Cosmos UUID."""
    now = datetime.now(UTC)
    try:
        _reject_collection_filters(req.params)
        response_format = _parse_format(req.params)
        record_id = str(UUID(req.route_params.get("id", "")))
    except ValueError as exc:
        return _error_response(f"Invalid request: {exc}", 400, now)

    try:
        settings = ServiceSettings()
    except ValueError:
        logging.exception("Invalid earthquake retrieval configuration")
        return _error_response("Earthquake retrieval is not configured", 500, now)

    try:
        record = _service_for(settings).get_by_id(record_id)
    except EarthquakeQueryUnavailableError:
        logging.exception("Cosmos DB was unavailable during exact earthquake lookup")
        return _error_response("Earthquake data is temporarily unavailable", 503, now)
    except Exception:
        logging.exception("Unexpected exact earthquake lookup failure")
        return _error_response("Earthquake retrieval failed", 500, now)

    if record is None:
        return _error_response("Earthquake record was not found", 404, now)
    logging.info("Exact earthquake retrieval response: format=%s", response_format)
    if response_format == "csv":
        return _csv_response([record], filename=f"earthquake-{record_id}.csv")
    return func.HttpResponse(
        body=json.dumps(record),
        status_code=200,
        mimetype="application/json",
    )


def _parse_query(
    params: Mapping[str, str],
    settings: ServiceSettings,
    *,
    now: datetime,
) -> EarthquakeQuery:
    end_text = _nonempty(params.get("end_time"))
    start_text = _nonempty(params.get("start_time"))
    end_time = now if end_text is None else _parse_datetime(end_text, "end_time")
    if start_text is None:
        try:
            start_time = end_time - DEFAULT_QUERY_WINDOW
        except OverflowError as exc:
            raise ValueError("end_time is too early for the default window") from exc
    else:
        start_time = _parse_datetime(start_text, "start_time")
    if start_time >= end_time:
        raise ValueError("start_time must be earlier than end_time")
    max_window_hours = settings.earthquake_query_max_window_hours
    if (end_time - start_time).total_seconds() > max_window_hours * 3600:
        raise ValueError(f"time window cannot exceed {max_window_hours} hours")

    latitude_text = _nonempty(params.get("latitude"))
    longitude_text = _nonempty(params.get("longitude"))
    radius_km_text = _nonempty(params.get("radius_km"))
    radius_mi_text = _nonempty(params.get("radius_mi"))
    if (latitude_text is None) != (longitude_text is None):
        raise ValueError("latitude and longitude must be supplied together")
    if latitude_text is None:
        if radius_km_text is not None or radius_mi_text is not None:
            raise ValueError("radius requires latitude and longitude")
        latitude = longitude = radius_km = None
    else:
        latitude = _parse_number(latitude_text, "latitude")
        longitude = _parse_number(cast(str, longitude_text), "longitude")
        if not -90 <= latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not -180 <= longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if radius_km_text is not None:
            radius_km = _parse_number(radius_km_text, "radius_km")
        elif radius_mi_text is not None:
            radius_km = _parse_number(radius_mi_text, "radius_mi") * MILES_TO_KILOMETERS
        else:
            radius_km = DEFAULT_RADIUS_KM
        if radius_km <= 0:
            raise ValueError("radius must be greater than zero")
        max_radius_km = settings.earthquake_query_max_radius_km
        if radius_km > max_radius_km:
            raise ValueError(f"radius cannot exceed {max_radius_km:g} km")

    min_magnitude = _parse_optional_number(params, "min_magnitude")
    max_magnitude = _parse_optional_number(params, "max_magnitude")
    if (
        min_magnitude is not None
        and max_magnitude is not None
        and min_magnitude > max_magnitude
    ):
        raise ValueError("min_magnitude cannot exceed max_magnitude")

    return EarthquakeQuery(
        start_time=start_time,
        end_time=end_time,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        min_magnitude=min_magnitude,
        max_magnitude=max_magnitude,
    )


def _service_for(settings: ServiceSettings) -> EarthquakeQueryService:
    return _build_service(
        settings.cosmos_earthquake_db_connection_string,
        settings.cosmos_earthquake_db_name,
        settings.cosmos_earthquake_data_container_name,
    )


def _parse_format(params: Mapping[str, str]) -> str:
    canonical = _nonempty(params.get("format"))
    alias = _nonempty(params.get("fmt"))
    value = canonical if canonical is not None else alias
    if value is None:
        return "json"
    normalized = value.casefold()
    if normalized not in {"json", "csv"}:
        raise ValueError("format must be json or csv")
    return normalized


def _reject_collection_filters(params: Mapping[str, str]) -> None:
    if any(_nonempty(params.get(name)) is not None for name in COLLECTION_FILTERS):
        raise ValueError("collection filters are not valid for exact record lookup")


def _parse_datetime(value: str, field_name: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_optional_number(params: Mapping[str, str], name: str) -> float | None:
    value = _nonempty(params.get(name))
    return None if value is None else _parse_number(value, name)


def _parse_number(value: str, field_name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _csv_response(
    items: list[EarthquakeRetrievalRecord], *, filename: str
) -> func.HttpResponse:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for item in items:
        item_values = cast(Mapping[str, object], item)
        writer.writerow({name: _csv_safe(item_values[name]) for name in CSV_FIELDS})
    return func.HttpResponse(
        body=output.getvalue(),
        status_code=200,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Result-Count": str(len(items)),
        },
        mimetype="text/csv",
        charset="utf-8",
    )


def _csv_safe(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def _error_response(reason: str, status_code: int, now: datetime) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"reason": reason, "utc_now": now.isoformat()}),
        status_code=status_code,
        mimetype="application/json",
    )

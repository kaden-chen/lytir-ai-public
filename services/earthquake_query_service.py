"""Query and deduplicate earthquake metadata stored in Cosmos DB."""

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from azure.core.exceptions import AzureError

from models import EarthquakeQuery, EarthquakeRetrievalRecord

EARTH_RADIUS_KM = 6_371.0088

COLLECTION_QUERY = """
SELECT
    c.id AS cosmos_id,
    c._ts AS cosmos_ts,
    c.Body.id AS source_id,
    c.Body.schema_version AS schema_version,
    c.Body.metadata.event_type AS event_type,
    c.Body.metadata.magnitude AS magnitude,
    c.Body.metadata.place AS place,
    c.Body.metadata.occurred_at_utc AS occurred_at_utc,
    c.Body.metadata.updated_at_utc AS updated_at_utc,
    c.Body.metadata.longitude AS longitude,
    c.Body.metadata.latitude AS latitude,
    c.Body.metadata.depth_km AS depth_km
FROM c
WHERE c.Body.metadata.occurred_at_utc >= @start_time
  AND c.Body.metadata.occurred_at_utc < @end_time
"""

EXACT_QUERY = """
SELECT TOP 1
    c.id AS cosmos_id,
    c._ts AS cosmos_ts,
    c.Body.id AS source_id,
    c.Body.schema_version AS schema_version,
    c.Body.metadata.event_type AS event_type,
    c.Body.metadata.magnitude AS magnitude,
    c.Body.metadata.place AS place,
    c.Body.metadata.occurred_at_utc AS occurred_at_utc,
    c.Body.metadata.updated_at_utc AS updated_at_utc,
    c.Body.metadata.longitude AS longitude,
    c.Body.metadata.latitude AS latitude,
    c.Body.metadata.depth_km AS depth_km
FROM c
WHERE c.id = @id
"""

LATEST_OBSERVED_QUERY = """
SELECT TOP 20
    c.Body.metadata.occurred_at_utc AS occurred_at_utc
FROM c
WHERE IS_DEFINED(c.Body.metadata.occurred_at_utc)
ORDER BY c.Body.metadata.occurred_at_utc DESC
"""


class CosmosContainer(Protocol):
    """Subset of the Cosmos container interface used by this service."""

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, object]],
        enable_cross_partition_query: bool,
    ) -> Iterable[dict[str, Any]]: ...


class EarthquakeQueryUnavailableError(RuntimeError):
    """Raised when Cosmos cannot complete a retrieval query."""


@dataclass(frozen=True)
class EarthquakeQueryResult:
    """Records and operational counts for one collection query."""

    items: list[EarthquakeRetrievalRecord]
    candidates: int
    malformed: int
    duplicates: int
    selected: int


@dataclass(frozen=True)
class _Candidate:
    source_id: str
    cosmos_timestamp: float
    updated_at: datetime | None
    occurred_at: datetime
    record: EarthquakeRetrievalRecord


class EarthquakeQueryService:
    """Read, validate, deduplicate, and filter raw Cosmos earthquake records."""

    def __init__(
        self, container: CosmosContainer, *, client: object | None = None
    ) -> None:
        self._container = container
        # Keep the shared client alive for the lifetime of its container proxy.
        self._client = client

    def query(self, filters: EarthquakeQuery) -> EarthquakeQueryResult:
        """Return every matching logical earthquake in the bounded time range."""
        parameters: list[dict[str, object]] = [
            {"name": "@start_time", "value": _format_query_utc(filters.start_time)},
            {"name": "@end_time", "value": _format_query_utc(filters.end_time)},
        ]
        raw_items = self._execute(COLLECTION_QUERY, parameters)

        winners: dict[str, _Candidate] = {}
        malformed = 0
        for raw_item in raw_items:
            try:
                candidate = _parse_candidate(raw_item)
            except (KeyError, TypeError, ValueError, OverflowError):
                malformed += 1
                continue

            if not filters.start_time <= candidate.occurred_at < filters.end_time:
                malformed += 1
                continue

            previous = winners.get(candidate.source_id)
            if previous is None or _winner_key(candidate) > _winner_key(previous):
                winners[candidate.source_id] = candidate

        selected = list(winners.values())
        items = [
            candidate.record
            for candidate in selected
            if _matches_filters(candidate, filters)
        ]
        items.sort(
            key=lambda item: (_parse_utc(item["occurred_at_utc"]), item["id"]),
            reverse=True,
        )

        result = EarthquakeQueryResult(
            items=items,
            candidates=len(raw_items),
            malformed=malformed,
            duplicates=len(raw_items) - malformed - len(selected),
            selected=len(selected),
        )
        logging.info(
            "Earthquake query completed: candidates=%d malformed=%d duplicates=%d "
            "selected=%d returned=%d window_hours=%.3f location_filter=%s "
            "magnitude_filter=%s",
            result.candidates,
            result.malformed,
            result.duplicates,
            result.selected,
            len(result.items),
            (filters.end_time - filters.start_time).total_seconds() / 3600,
            filters.latitude is not None,
            filters.min_magnitude is not None or filters.max_magnitude is not None,
        )
        return result

    def get_by_id(self, record_id: str) -> EarthquakeRetrievalRecord | None:
        """Return one exact Cosmos document by its validated root UUID."""
        raw_items = self._execute(
            EXACT_QUERY,
            [{"name": "@id", "value": record_id}],
        )
        if not raw_items:
            return None
        try:
            return _parse_candidate(raw_items[0]).record
        except (KeyError, TypeError, ValueError, OverflowError):
            logging.warning("Exact earthquake record is malformed: id=%s", record_id)
            return None

    def latest_observed_at(self) -> datetime | None:
        """Return the latest valid earthquake occurrence time in the container."""
        raw_items = self._execute(LATEST_OBSERVED_QUERY, [])
        latest: datetime | None = None
        for item in raw_items:
            try:
                occurred_at = _parse_utc(
                    _required_string(item["occurred_at_utc"], "occurred_at")
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if latest is None or occurred_at > latest:
                latest = occurred_at
        return latest

    def _execute(
        self, query: str, parameters: list[dict[str, object]]
    ) -> list[dict[str, Any]]:
        try:
            return list(
                self._container.query_items(
                    query=query,
                    parameters=parameters,
                    enable_cross_partition_query=True,
                )
            )
        except AzureError as exc:
            raise EarthquakeQueryUnavailableError(
                "Cosmos DB could not complete the earthquake query"
            ) from exc


def _parse_candidate(item: dict[str, Any]) -> _Candidate:
    cosmos_id_value = item["cosmos_id"]
    source_id = item["source_id"]
    schema_version = item["schema_version"]
    event_type = item["event_type"]
    if not isinstance(cosmos_id_value, str):
        raise ValueError("Cosmos ID must be a string")
    cosmos_id = str(UUID(cosmos_id_value))
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("Source ID must be a non-empty string")
    if type(schema_version) is not int or schema_version <= 0:
        raise ValueError("Schema version must be a positive integer")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("Event type must be a non-empty string")

    cosmos_timestamp = _required_number(item["cosmos_ts"], "Cosmos timestamp")
    magnitude = _optional_number(item.get("magnitude"), "magnitude")
    place = _optional_string(item.get("place"), "place")
    occurred_at = _parse_utc(_required_string(item["occurred_at_utc"], "occurred_at"))
    updated_text = item.get("updated_at_utc")
    updated_at = (
        None
        if updated_text is None
        else _parse_utc(_required_string(updated_text, "updated_at"))
    )
    longitude = _required_number(item["longitude"], "longitude")
    latitude = _required_number(item["latitude"], "latitude")
    depth_km = _required_number(item["depth_km"], "depth")
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude is out of range")
    if not -90 <= latitude <= 90:
        raise ValueError("Latitude is out of range")

    record = EarthquakeRetrievalRecord(
        id=cosmos_id,
        event_type=event_type,
        magnitude=magnitude,
        place=place,
        occurred_at_utc=_format_utc(occurred_at),
        updated_at_utc=None if updated_at is None else _format_utc(updated_at),
        longitude=longitude,
        latitude=latitude,
        depth_km=depth_km,
    )
    return _Candidate(
        source_id=source_id,
        cosmos_timestamp=cosmos_timestamp,
        updated_at=updated_at,
        occurred_at=occurred_at,
        record=record,
    )


def _winner_key(candidate: _Candidate) -> tuple[datetime, float, str]:
    updated_at = candidate.updated_at or datetime.min.replace(tzinfo=UTC)
    return updated_at, candidate.cosmos_timestamp, candidate.record["id"]


def _matches_filters(candidate: _Candidate, filters: EarthquakeQuery) -> bool:
    magnitude = candidate.record["magnitude"]
    if filters.min_magnitude is not None and (
        magnitude is None or magnitude < filters.min_magnitude
    ):
        return False
    if filters.max_magnitude is not None and (
        magnitude is None or magnitude > filters.max_magnitude
    ):
        return False
    if filters.latitude is None:
        return True
    assert filters.longitude is not None
    assert filters.radius_km is not None
    return _within_radius(
        filters.latitude,
        filters.longitude,
        candidate.record["latitude"],
        candidate.record["longitude"],
        filters.radius_km,
    )


def _within_radius(
    center_latitude: float,
    center_longitude: float,
    latitude: float,
    longitude: float,
    radius_km: float,
) -> bool:
    angular_radius = radius_km / EARTH_RADIUS_KM
    latitude_delta = math.degrees(angular_radius)
    if (
        not center_latitude - latitude_delta
        <= latitude
        <= center_latitude + latitude_delta
    ):
        return False

    reaches_pole = (
        center_latitude - latitude_delta <= -90
        or center_latitude + latitude_delta >= 90
    )
    if not reaches_pole and angular_radius < math.pi:
        denominator = math.cos(math.radians(center_latitude))
        if denominator:
            ratio = min(1.0, math.sin(angular_radius) / abs(denominator))
            longitude_delta = math.degrees(math.asin(ratio))
            normalized_delta = abs(
                (longitude - center_longitude + 180.0) % 360.0 - 180.0
            )
            if normalized_delta > longitude_delta:
                return False

    center_latitude_rad = math.radians(center_latitude)
    latitude_rad = math.radians(latitude)
    latitude_difference = latitude_rad - center_latitude_rad
    longitude_difference = math.radians(longitude - center_longitude)
    haversine = (
        math.sin(latitude_difference / 2) ** 2
        + math.cos(center_latitude_rad)
        * math.cos(latitude_rad)
        * math.sin(longitude_difference / 2) ** 2
    )
    distance = 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(haversine)))
    return distance <= radius_km


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_query_utc(value: datetime) -> str:
    """Use fixed precision so Cosmos string ranges preserve time ordering."""
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _required_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _optional_number(value: object, field_name: str) -> float | None:
    return None if value is None else _required_number(value, field_name)

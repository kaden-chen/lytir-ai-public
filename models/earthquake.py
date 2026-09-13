"""Earthquake event shapes used between pipeline services."""

from typing import Any, TypedDict


class EarthquakeMetadata(TypedDict):
    """Compact normalized fields used for queries and AI context."""

    event_type: str
    magnitude: float | None
    place: str | None
    occurred_at_utc: str
    updated_at_utc: str | None
    longitude: float
    latitude: float
    depth_km: float
    source_url: str | None


class EarthquakeMetadataRecord(EarthquakeMetadata):
    """Normalized event metadata with its source event ID."""

    id: str


class EarthquakeEnvelope(TypedDict):
    """Message sent to IoT Hub for one USGS feature."""

    id: str
    schema_version: int
    source: str
    metadata: EarthquakeMetadata
    event: dict[str, Any]

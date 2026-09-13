"""Normalize USGS GeoJSON features without discarding their source data."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from models import EarthquakeEnvelope, EarthquakeMetadata


@dataclass(frozen=True)
class NormalizationResult:
    """Normalized events and the count of unusable source features."""

    events: list[EarthquakeEnvelope]
    skipped: int


class EarthquakeNormalizer:
    """Create query-friendly metadata around original USGS features."""

    def normalize_feed(self, feed: dict[str, Any]) -> NormalizationResult:
        """Normalize valid features and count malformed entries."""
        if feed.get("type") != "FeatureCollection":
            raise ValueError("USGS response must be a GeoJSON FeatureCollection")

        features = feed.get("features")
        if not isinstance(features, list):
            raise ValueError("USGS FeatureCollection must contain a features list")

        events: list[EarthquakeEnvelope] = []
        skipped = 0
        for feature in features:
            if not isinstance(feature, dict):
                skipped += 1
                continue
            try:
                events.append(self.normalize_feature(feature))
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                skipped += 1

        return NormalizationResult(events=events, skipped=skipped)

    def normalize_feature(self, feature: dict[str, Any]) -> EarthquakeEnvelope:
        """Wrap one valid USGS feature with compact normalized metadata."""
        if feature.get("type") != "Feature":
            raise ValueError("USGS event must be a GeoJSON Feature")

        event_id = feature.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("USGS event must have an id")

        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            raise ValueError("USGS event must have properties and geometry objects")
        if geometry.get("type") != "Point":
            raise ValueError("USGS event geometry must be a Point")

        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 3:
            raise ValueError("USGS event must have longitude, latitude, and depth")

        event_type = properties.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("USGS event must have an event type")

        longitude = self._required_number(coordinates[0], "longitude")
        latitude = self._required_number(coordinates[1], "latitude")
        depth_km = self._required_number(coordinates[2], "depth")
        if not -180 <= longitude <= 180:
            raise ValueError("USGS event longitude is out of range")
        if not -90 <= latitude <= 90:
            raise ValueError("USGS event latitude is out of range")

        metadata: EarthquakeMetadata = {
            "event_type": event_type,
            "magnitude": self._optional_number(properties.get("mag"), "magnitude"),
            "place": self._optional_string(properties.get("place")),
            "occurred_at_utc": self._milliseconds_to_utc(properties.get("time")),
            "updated_at_utc": self._optional_milliseconds_to_utc(
                properties.get("updated")
            ),
            "longitude": longitude,
            "latitude": latitude,
            "depth_km": depth_km,
            "source_url": self._optional_string(properties.get("url")),
        }

        return {
            "id": event_id,
            "schema_version": 1,
            "source": "usgs",
            "metadata": metadata,
            "event": feature,
        }

    @staticmethod
    def _required_number(value: object, field_name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"USGS event {field_name} must be numeric")
        return float(value)

    @classmethod
    def _optional_number(cls, value: object, field_name: str) -> float | None:
        if value is None:
            return None
        return cls._required_number(value, field_name)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @classmethod
    def _milliseconds_to_utc(cls, value: object) -> str:
        milliseconds = cls._required_number(value, "time")
        return (
            datetime.fromtimestamp(milliseconds / 1000, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @classmethod
    def _optional_milliseconds_to_utc(cls, value: object) -> str | None:
        return None if value is None else cls._milliseconds_to_utc(value)
